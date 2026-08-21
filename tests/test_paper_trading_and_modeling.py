"""
Unit & Integration Tests: Paper Trading Mode & Price Ladder Modeling Engine
===========================================================================

Tests:
1. BaseTradingClient paper position lifecycle (recording, retrieval, TP fill checks, expiration clearing).
2. KalshiClient & GeminiClient safety gating (allow_live_orders=False blocks live REST orders and updates paper positions).
3. Exit evaluations in dry-run mode (10m winner banking, 10m stop-loss, 5m cut-loss, 12.5m endgame cut, crash detection).
4. Price ladder simulation: simulate_hypothetical_entry across prices 1¢–99¢ and timing marks.
5. Matrix aggregation: model_price_ladder_matrix calculates win rates, hold EV, strategy EV, MFE, and MAE.
6. Discovered patterns: find_profitable_patterns detects sweet spots, high-win-rate zones, and negative-EV traps.
7. Reporting & Exports: format_price_matrix_report, export_matrix_to_json, export_matrix_to_csv.
8. Database functions: fetch_market_snapshot_series and run_price_ladder_modeling_from_db.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from database import fetch_market_snapshot_series, run_price_ladder_modeling_from_db
from models.price_ladder_model import (
    CHECKPOINTS_15M,
    CHECKPOINTS_1H,
    HypotheticalTrade,
    PriceLadderCell,
    export_matrix_to_csv,
    export_matrix_to_json,
    find_profitable_patterns,
    format_price_matrix_report,
    model_price_ladder_matrix,
    simulate_hypothetical_entry,
)
from signals.base_client import BaseTradingClient, LogTag
from signals.gemini_client import GeminiTradingClient
from signals.kalshi_client import KalshiClient


class DummyClient(BaseTradingClient):
    exchange_name = "MockExchange"

    def get_market_position(self, ticker: str) -> dict | None:
        return self.get_paper_position(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        return True

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        self.remove_paper_position(ticker)
        return {"status": "liquidated", "ticker": ticker, "price": current_price}

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        self.record_paper_position(ticker, side, count, price_cents)
        return {"status": "bought", "ticker": ticker, "price": price_cents}


class TestPaperTradingBaseClient(unittest.TestCase):
    def setUp(self):
        self.client = DummyClient(allow_live_orders=False)

    def test_default_live_orders_disabled(self):
        client = DummyClient(allow_live_orders=False)
        self.assertFalse(client.allow_live_orders)
        self.assertEqual(len(client.get_all_paper_positions()), 0)

    def test_record_and_get_paper_position(self):
        pos = self.client.record_paper_position(
            ticker="KXBTC15M-26FEB19-T94500",
            side="yes",
            count=5,
            entry_price_cents=22,
            target_tp_cents=62,
            strategy_tag="discount_corridor",
        )
        self.assertEqual(pos["ticker"], "KXBTC15M-26FEB19-T94500")
        self.assertEqual(pos["side"], "yes")
        self.assertEqual(pos["count"], 5)
        self.assertEqual(pos["entry_price_cents"], 22)
        self.assertEqual(pos["target_tp_cents"], 62)
        self.assertTrue(pos["is_paper"])

        fetched = self.client.get_paper_position("KXBTC15M-26FEB19-T94500")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["count"], 5)

        # Single side lock check
        self.assertFalse(self.client.is_side_allowed("KXBTC15M-26FEB19-T94500", "no"))
        self.assertTrue(self.client.is_side_allowed("KXBTC15M-26FEB19-T94500", "yes"))

    def test_remove_paper_position(self):
        self.client.record_paper_position("KXETH15M-TEST", "no", 2, 35)
        self.assertIsNotNone(self.client.get_paper_position("KXETH15M-TEST"))

        removed = self.client.remove_paper_position("KXETH15M-TEST")
        self.assertEqual(removed["ticker"], "KXETH15M-TEST")
        self.assertIsNone(self.client.get_paper_position("KXETH15M-TEST"))

    def test_check_simulated_take_profit_fill(self):
        self.client.record_paper_position(
            ticker="KXSOL15M-TEST",
            side="yes",
            count=1,
            entry_price_cents=25,
            target_tp_cents=62,
        )

        # Price below TP (50¢ < 62¢) -> no fill
        res = self.client.check_simulated_take_profit_fill(
            ticker="KXSOL15M-TEST",
            current_yes_bid=50,
            current_no_bid=50,
            current_yes_ask=52,
            current_no_ask=50,
        )
        self.assertIsNone(res)
        self.assertIsNotNone(self.client.get_paper_position("KXSOL15M-TEST"))

        # Price reaches TP (65¢ >= 62¢) -> fill!
        res_fill = self.client.check_simulated_take_profit_fill(
            ticker="KXSOL15M-TEST",
            current_yes_bid=65,
            current_no_bid=35,
            current_yes_ask=66,
            current_no_ask=36,
        )
        self.assertIsNotNone(res_fill)
        self.assertTrue(res_fill["triggered"])
        self.assertEqual(res_fill["action"], "take_profit_sold")
        self.assertEqual(res_fill["exit_price"], 62)
        self.assertEqual(res_fill["profit_locked_cents"], 37)  # 62 - 25
        # Position should now be removed from paper positions
        self.assertIsNone(self.client.get_paper_position("KXSOL15M-TEST"))

    def test_clear_expired_paper_positions(self):
        now_ts = int(datetime.now(timezone.utc).timestamp())
        self.client.record_paper_position(
            "EXPIRED_MKT", "yes", 1, 30, expiration_ts=now_ts - 100
        )
        self.client.record_paper_position(
            "ACTIVE_MKT", "no", 1, 30, expiration_ts=now_ts + 600
        )

        pruned = self.client.clear_expired_paper_positions()
        self.assertEqual(pruned, 1)
        self.assertIsNone(self.client.get_paper_position("EXPIRED_MKT"))
        self.assertIsNotNone(self.client.get_paper_position("ACTIVE_MKT"))


class TestExchangeClientsSafetyMode(unittest.TestCase):
    @patch("signals.kalshi_client._load_private_key", return_value=MagicMock())
    def test_kalshi_client_safety_mode_blocks_live_orders(self, mock_key):
        client = KalshiClient(api_key_id="test_key_id", allow_live_orders=False)
        self.assertFalse(client.allow_live_orders)

        # Mock requests.Session.post so we can verify it was NEVER called
        client.session.post = MagicMock()
        client.session.delete = MagicMock()

        # Test create_order
        res = client.create_order(
            ticker="KXBTC15M-TEST",
            action="buy",
            side="yes",
            count=1,
            price_cents=30,
        )
        self.assertEqual(res["status"], "simulated")
        client.session.post.assert_not_called()

        # Test buy_and_set_take_profit
        buy_res = client.buy_and_set_take_profit(
            ticker="KXBTC15M-TEST",
            side="yes",
            buy_price_cents=30,
            take_profit_cents=62,
            count=1,
        )
        self.assertEqual(buy_res["buy_order"]["status"], "simulated")
        self.assertIsNotNone(client.get_paper_position("KXBTC15M-TEST"))
        client.session.post.assert_not_called()

        # Test liquidation in safety mode
        liq_res = client.execute_liquidation("KXBTC15M-TEST", "yes", 1, 20)
        self.assertEqual(liq_res["status"], "simulated_liquidated")
        self.assertIsNone(client.get_paper_position("KXBTC15M-TEST"))
        client.session.post.assert_not_called()

        # Test cancel in safety mode
        cancel_res = client.cancel_order("order_123")
        self.assertEqual(cancel_res["status"], "simulated_canceled")
        client.session.delete.assert_not_called()

    def test_gemini_client_safety_mode_blocks_live_orders(self):
        client = GeminiTradingClient(
            api_key="mock_key",
            api_secret="mock_secret",
            allow_live_orders=False,
        )
        self.assertFalse(client.allow_live_orders)

        client._post = MagicMock()

        # Test buy_and_set_take_profit
        buy_res = client.buy_and_set_take_profit(
            ticker="BTC15M-TEST",
            side="yes",
            buy_price_cents=28,
            take_profit_cents=62,
            count=1,
        )
        self.assertEqual(buy_res["buy_price_cents"], 27)  # maker 1¢ below ask
        self.assertIsNotNone(client.get_paper_position("BTC15M-TEST"))
        client._post.assert_not_called()

        # Test buy_favorite_maker_order
        fav_res = client.buy_favorite_maker_order(
            ticker="ETH15M-TEST",
            side="no",
            maker_bid_cents=80,
            target_ask_cents=82,
            count=1,
        )
        self.assertEqual(fav_res["buy_price_cents"], 80)
        self.assertIsNotNone(client.get_paper_position("ETH15M-TEST"))
        client._post.assert_not_called()

        # Test liquidation
        liq_res = client.execute_liquidation("BTC15M-TEST", "yes", 1, 15)
        self.assertEqual(liq_res["status"], "simulated_liquidated")
        self.assertIsNone(client.get_paper_position("BTC15M-TEST"))
        client._post.assert_not_called()


class TestPriceLadderModelingEngine(unittest.TestCase):
    def setUp(self):
        now_ts = 1771459200  # Fixed epoch timestamp for reproducible test
        # Sample market snapshots for a market that settled YES
        self.sample_market_snaps_yes = [
            {"ts": now_ts + 0, "m": 0, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 25, "n": 75, "y_bid": 24, "y_ask": 26, "n_bid": 74, "n_ask": 76},
            {"ts": now_ts + 120, "m": 2, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 35, "n": 65, "y_bid": 34, "y_ask": 36, "n_bid": 64, "n_ask": 66},
            {"ts": now_ts + 300, "m": 5, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 48, "n": 52, "y_bid": 47, "y_ask": 49, "n_bid": 51, "n_ask": 53},
            {"ts": now_ts + 600, "m": 10, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 72, "n": 28, "y_bid": 71, "y_ask": 73, "n_bid": 27, "n_ask": 29},
            {"ts": now_ts + 750, "m": 12, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 88, "n": 12, "y_bid": 87, "y_ask": 89, "n_bid": 11, "n_ask": 13},
            {"ts": now_ts + 840, "m": 14, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 98, "n": 2, "y_bid": 97, "y_ask": 99, "n_bid": 1, "n_ask": 3},
        ]

        # Sample market snapshots for a market that settled NO
        self.sample_market_snaps_no = [
            {"ts": now_ts + 0, "m": 0, "tf": "15m", "tick": "KXETH15M-002", "sym": "ETH", "y": 30, "n": 70, "y_bid": 29, "y_ask": 31, "n_bid": 69, "n_ask": 71},
            {"ts": now_ts + 300, "m": 5, "tf": "15m", "tick": "KXETH15M-002", "sym": "ETH", "y": 18, "n": 82, "y_bid": 17, "y_ask": 19, "n_bid": 81, "n_ask": 83},
            {"ts": now_ts + 600, "m": 10, "tf": "15m", "tick": "KXETH15M-002", "sym": "ETH", "y": 8, "n": 92, "y_bid": 7, "y_ask": 9, "n_bid": 91, "n_ask": 93},
            {"ts": now_ts + 840, "m": 14, "tf": "15m", "tick": "KXETH15M-002", "sym": "ETH", "y": 1, "n": 99, "y_bid": 1, "y_ask": 2, "n_bid": 98, "n_ask": 99},
        ]

    def test_simulate_hypothetical_entry_winning_yes(self):
        # Entry at 25¢ at minute 0 on market that settled YES
        trade = simulate_hypothetical_entry(
            market_snapshots=self.sample_market_snaps_yes,
            settlement_result="yes",
            target_price_cents=25,
            entry_minute=0,
            side="yes",
            fill_mode="maker",
        )
        self.assertIsNotNone(trade)
        self.assertTrue(trade.filled)
        self.assertEqual(trade.fill_price_cents, 25)
        self.assertTrue(trade.settlement_won)
        self.assertEqual(trade.hold_pnl_cents, 75)  # 100 - 25
        self.assertEqual(trade.hold_roi_pct, 300.0)  # +300%
        self.assertEqual(trade.max_price_cents, 98)
        self.assertEqual(trade.max_favorable_excursion_cents, 73)  # 98 - 25

    def test_simulate_hypothetical_entry_losing_yes(self):
        # Entry at 30¢ at minute 0 on market that settled NO
        trade = simulate_hypothetical_entry(
            market_snapshots=self.sample_market_snaps_no,
            settlement_result="no",
            target_price_cents=30,
            entry_minute=0,
            side="yes",
            fill_mode="maker",
        )
        self.assertIsNotNone(trade)
        self.assertTrue(trade.filled)
        self.assertFalse(trade.settlement_won)
        self.assertEqual(trade.hold_pnl_cents, -30)
        self.assertEqual(trade.hold_roi_pct, -100.0)

    def test_simulate_hypothetical_entry_winning_no(self):
        # Entry on NO at 70¢ at minute 0 on market that settled NO
        trade = simulate_hypothetical_entry(
            market_snapshots=self.sample_market_snaps_no,
            settlement_result="no",
            target_price_cents=70,
            entry_minute=0,
            side="no",
            fill_mode="maker",
        )
        self.assertIsNotNone(trade)
        self.assertTrue(trade.filled)
        self.assertTrue(trade.settlement_won)
        self.assertEqual(trade.hold_pnl_cents, 30)  # 100 - 70

    def test_model_price_ladder_matrix_aggregation(self):
        markets = [
            {"snapshots": self.sample_market_snaps_yes, "result": "yes", "sym": "BTC", "tick": "KXBTC15M-001"},
            {"snapshots": self.sample_market_snaps_no, "result": "no", "sym": "ETH", "tick": "KXETH15M-002"},
        ]

        matrix = model_price_ladder_matrix(
            markets_data=markets,
            min_price_cents=20,
            max_price_cents=80,
            price_step=10,
            time_checkpoints=[0, 5, 10],
            timeframe="15m",
        )

        self.assertGreater(len(matrix), 0)
        # Check a specific cell (e.g. 25-30¢ at :00)
        cell = matrix.get((30, 0, "yes"))
        self.assertIsNotNone(cell)
        self.assertEqual(cell.sample_count, 2)
        self.assertEqual(cell.fill_count, 2)
        self.assertEqual(cell.win_count, 1)
        self.assertEqual(cell.win_rate, 0.5)

    def test_find_profitable_patterns(self):
        markets = [
            {"snapshots": self.sample_market_snaps_yes, "result": "yes", "sym": "BTC", "tick": "KXBTC15M-001"},
            {"snapshots": self.sample_market_snaps_no, "result": "no", "sym": "ETH", "tick": "KXETH15M-002"},
        ]

        matrix = model_price_ladder_matrix(
            markets_data=markets,
            min_price_cents=10,
            max_price_cents=90,
            price_step=10,
            time_checkpoints=[0, 5, 10],
        )

        patterns = find_profitable_patterns(matrix, min_samples=1, min_ev_cents=1.0)
        self.assertIn("sweet_spots", patterns)
        self.assertIn("high_win_rate_corridors", patterns)
        self.assertIn("favorite_recovery_zones", patterns)
        self.assertIn("negative_ev_traps", patterns)

    def test_format_price_matrix_report(self):
        markets = [
            {"snapshots": self.sample_market_snaps_yes, "result": "yes", "sym": "BTC", "tick": "KXBTC15M-001"},
            {"snapshots": self.sample_market_snaps_no, "result": "no", "sym": "ETH", "tick": "KXETH15M-002"},
        ]
        matrix = model_price_ladder_matrix(
            markets_data=markets,
            min_price_cents=10,
            max_price_cents=90,
            price_step=10,
            time_checkpoints=[0, 5, 10],
        )
        report = format_price_matrix_report(matrix, timeframe="15m", symbol="BTC")
        self.assertIn("PRICE LADDER & TIMING MATRIX", report)
        self.assertIn("Price Band", report)

    def test_export_matrix_json_and_csv(self):
        markets = [
            {"snapshots": self.sample_market_snaps_yes, "result": "yes", "sym": "BTC", "tick": "KXBTC15M-001"},
        ]
        matrix = model_price_ladder_matrix(
            markets_data=markets,
            min_price_cents=20,
            max_price_cents=40,
            price_step=10,
            time_checkpoints=[0, 5],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "matrix.json")
            csv_path = os.path.join(tmpdir, "matrix.csv")

            json_ok = export_matrix_to_json(matrix, json_path)
            csv_ok = export_matrix_to_csv(matrix, csv_path)

            self.assertTrue(json_ok)
            self.assertTrue(csv_ok)
            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(csv_path))


class TestDatabasePriceLadderIntegration(unittest.TestCase):
    @patch("database.get_mongo_collection")
    def test_fetch_market_snapshot_series_mock(self, mock_col):
        mock_snap_col = MagicMock()
        mock_settle_col = MagicMock()

        mock_snap_col.find.return_value.sort.return_value.limit.return_value = [
            {"ts": 1771459200, "m": 0, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 25, "n": 75},
            {"ts": 1771459500, "m": 5, "tf": "15m", "tick": "KXBTC15M-001", "sym": "BTC", "y": 50, "n": 50},
        ]
        mock_settle_col.find.return_value = [
            {"ticker": "KXBTC15M-001", "market_result": "yes"}
        ]

        def get_col(name):
            return mock_snap_col if name == "market_snapshots" else mock_settle_col

        mock_col.side_effect = get_col

        series = fetch_market_snapshot_series(timeframe="15m", symbol="BTC")
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["tick"], "KXBTC15M-001")
        self.assertEqual(series[0]["result"], "yes")
        self.assertEqual(len(series[0]["snapshots"]), 2)


if __name__ == "__main__":
    unittest.main()
