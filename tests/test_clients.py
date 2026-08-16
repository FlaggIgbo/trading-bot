"""
Unit tests for Kalshi prediction market client and main trading pipeline.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from database import save_portfolio_pnl
from main import (
    batch_fetch_crypto_spot_prices,
    calculate_seconds_until_next_5min_mark,
    calculate_seconds_until_next_checkpoint,
    extract_asset_symbol,
    extract_price_cents,
    is_spot_momentum_aligned,
    parse_cents,
    process_single_market,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import KalshiClient, round_price_to_tick


class DummyTradingClient(BaseTradingClient):
    exchange_name = "TestExchange"

    def __init__(self):
        super().__init__()
        self._positions_cache: dict[str, dict] = {}
        self._orders_cache: dict[str, list[dict]] = {}

    def get_market_position(self, ticker: str) -> dict | None:
        return self._positions_cache.get(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        self._orders_cache.pop(ticker, None)
        return True

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        self._positions_cache.pop(ticker, None)
        self.clear_profit_floor(ticker)
        self.clear_entry_price(ticker)
        return {"status": "liquidated", "price": current_price}

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        self._positions_cache[ticker] = {
            "side": side.lower(),
            "count": count,
            "entry_price_cents": price_cents,
        }
        self.set_entry_price(ticker, price_cents)
        return {"status": "bought", "price": price_cents, "fill_count": count}


class TestClientInterfaces(unittest.TestCase):
    def test_price_helper_functions(self):
        self.assertEqual(round_price_to_tick(0.423), "0.42")
        self.assertEqual(round_price_to_tick(0.001), "0.01")
        self.assertEqual(round_price_to_tick(1.05), "0.99")
        self.assertEqual(parse_cents("0.4500"), 45)
        self.assertEqual(parse_cents(None, 65), 65)

    def test_extract_asset_symbol(self):
        self.assertEqual(extract_asset_symbol("KXBTC15M", "KXBTC15M-26AUG140015-15"), "BTC")
        self.assertEqual(extract_asset_symbol("KXETH15M", "KXETH15M-26AUG140015-15"), "ETH")
        self.assertEqual(extract_asset_symbol("ETH15M", "GEMINI-ETH15M-100"), "ETH")
        self.assertEqual(extract_asset_symbol("UNKNOWN", "XYZ123"), "UNKNOWN")

    def test_spot_momentum_alignment(self):
        # Bullish: Spot (60000) >= Target (59000) -> YES aligned, NO not aligned
        self.assertTrue(is_spot_momentum_aligned("yes", 60000.0, 59000.0))
        self.assertFalse(is_spot_momentum_aligned("no", 60000.0, 59000.0))

        # Bearish: Spot (58000) < Target (59000) -> YES not aligned, NO aligned
        self.assertFalse(is_spot_momentum_aligned("yes", 58000.0, 59000.0))
        self.assertTrue(is_spot_momentum_aligned("no", 58000.0, 59000.0))

        # Dead-heat ATM zone (0.01% dist) -> both aligned
        self.assertTrue(is_spot_momentum_aligned("yes", 59005.0, 59000.0))
        self.assertTrue(is_spot_momentum_aligned("no", 59005.0, 59000.0))

    def test_extract_price_cents(self):
        mkt = {"yes_ask_dollars": "0.4500", "no_ask": 55}
        self.assertEqual(extract_price_cents(mkt, "yes"), 45)
        self.assertEqual(extract_price_cents(mkt, "no"), 55)

    def test_process_single_market_snapshot(self):
        client = MagicMock()
        client.exchange_name = "Kalshi"
        client.get_market_position.return_value = None
        client.has_existing_position_or_order.return_value = False

        market = {
            "ticker": "KXBTC15M-TEST",
            "series_ticker": "KXBTC15M",
            "yes_ask_dollars": "0.4200",
            "yes_bid_dollars": "0.4000",
            "no_ask_dollars": "0.6000",
            "no_bid_dollars": "0.5800",
            "floor_strike": 60000.0,
            "volume_24h": 1500,
        }
        now = datetime(2026, 8, 14, 0, 0, 30, tzinfo=timezone.utc)

        snapshot = process_single_market(
            market=market,
            is_trading_interval=False,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60100.0},
            dry_run=True,
        )

        self.assertEqual(snapshot["tick"], "KXBTC15M-TEST")
        self.assertEqual(snapshot["sym"], "BTC")
        self.assertEqual(snapshot["y"], 42)
        self.assertEqual(snapshot["n"], 60)
        self.assertEqual(snapshot["p"], 60100.0)

    def test_stop_loss_exclusive_gating(self):
        client = MagicMock()
        client.exchange_name = "Kalshi"
        client.evaluate_and_execute_10min_strategy.return_value = {
            "triggered": True,
            "side": "yes",
            "loss_cents": 12,
            "exit_price": 28,
            "action": "stop_loss_sold",
        }

        market = {
            "ticker": "KXBTC15M-TEST",
            "series_ticker": "KXBTC15M",
            "yes_ask": 28,
            "no_ask": 72,
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        now = datetime.now(timezone.utc)

        # At minute 10 (% 15 == 10) -> is_stop_loss_interval is True
        snapshot = process_single_market(
            market=market,
            is_trading_interval=True,
            client=client,
            now_utc=now,
            minute_past_hour=10,
            spot_cache={"BTC": 59900.0},
            dry_run=True,
        )

        self.assertIn("trd", snapshot)
        self.assertEqual(snapshot["trd"]["action"], "stop_loss_sold")
        client.buy_and_set_take_profit.assert_not_called()

    def test_batch_fetch_crypto_spot_prices(self):
        with patch("main.get_crypto_spot_price", side_effect=lambda sym: 60000.0 if sym == "BTC" else 2000.0):
            res = batch_fetch_crypto_spot_prices(["BTC", "ETH", "BTC"])
            self.assertEqual(len(res), 2)
            self.assertEqual(res["BTC"], 60000.0)
            self.assertEqual(res["ETH"], 2000.0)

    def test_calculate_seconds_until_next_5min_mark(self):
        sec = calculate_seconds_until_next_5min_mark(offset_seconds=30.0)
        self.assertGreaterEqual(sec, 15.0)
        self.assertLessEqual(sec, 600.0)

    @patch("database.get_mongo_collection")
    def test_database_exchange_tagging(self, mock_get_coll):
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll

        pnl_data = {
            "ts": 1234567890,
            "exchange": "kalshi",
            "balance_dollars": 100.0,
            "portfolio_value_dollars": 100.0,
        }
        res = save_portfolio_pnl(pnl_data)
        self.assertTrue(res)
        mock_coll.insert_one.assert_called_once()
        inserted_doc = mock_coll.insert_one.call_args[0][0]
        self.assertEqual(inserted_doc["ex"], "kalshi")


class TestKalshiTradingAndRiskStrategy(unittest.TestCase):
    def setUp(self):
        self.client = DummyTradingClient()
        self.ticker = "KXBTC15M-TEST"

    def test_10min_strategy_rolls_winner_and_rebuys_at_ask(self):
        # Mock active YES position bought at 40¢
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 40,
        }

        # Evaluate at current bid 72¢, ask 74¢ (>= 70¢)
        with patch.object(self.client, "cancel_and_verify_resting_orders") as mock_cancel, \
             patch.object(self.client, "execute_liquidation") as mock_liquidate, \
             patch.object(self.client, "execute_buy") as mock_buy:
            res = self.client.evaluate_and_execute_10min_strategy(
                ticker=self.ticker,
                current_yes_bid=72,
                current_no_bid=26,
                current_yes_ask=74,
                current_no_ask=28,
                dry_run=False,
            )
            self.assertTrue(res["triggered"])
            self.assertEqual(res["action"], "winner_rolled_rebuy")
            self.assertEqual(res["exit_price"], 72)
            self.assertEqual(res["profit_locked_cents"], 32)
            self.assertEqual(res["rebuy_price_cents"], 74)
            mock_cancel.assert_called_once_with(self.ticker)
            mock_liquidate.assert_called_once_with(self.ticker, "yes", 1, 72)
            mock_buy.assert_called_once_with(self.ticker, "yes", 1, 74)
            self.assertEqual(self.client.get_entry_price(self.ticker), 74)

    def test_standard_stop_loss_liquidation(self):
        # Position bought at 42¢
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 42,
        }

        # Market drops to 30¢ (loss = 12¢ >= 10¢ limit and price <= 30¢)
        with patch.object(self.client, "cancel_and_verify_resting_orders") as mock_cancel, \
             patch.object(self.client, "execute_liquidation") as mock_liquidate:
            res = self.client.evaluate_and_execute_10min_strategy(
                ticker=self.ticker,
                current_yes_bid=30,
                current_no_bid=68,
                current_yes_ask=32,
                current_no_ask=70,
                dry_run=False,
            )
            self.assertTrue(res["triggered"])
            self.assertEqual(res["action"], "stop_loss_sold")
            self.assertEqual(res["loss_cents"], 12)
            self.assertEqual(res["exit_price"], 30)
            mock_cancel.assert_called_once_with(self.ticker)
            mock_liquidate.assert_called_once_with(self.ticker, "yes", 1, 30)

    def test_entry_price_tracking_and_cleanup(self):
        self.client.set_entry_price("TEST-TICKER", 54)
        self.assertEqual(self.client.get_entry_price("TEST-TICKER"), 54)
        self.client.clear_entry_price("TEST-TICKER")
        self.assertIsNone(self.client.get_entry_price("TEST-TICKER"))

    def test_endgame_12_5min_losing_position_liquidates_below_floor(self):
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 74,
        }

        with patch.object(self.client, "cancel_and_verify_resting_orders") as mock_cancel, \
             patch.object(self.client, "execute_liquidation") as mock_liquidate:
            res = self.client.evaluate_and_execute_12_5min_endgame_check(
                ticker=self.ticker,
                current_yes_price=22,
                current_no_price=78,
                dry_run=False,
                endgame_floor=25,
            )
            self.assertTrue(res["triggered"])
            self.assertEqual(res["action"], "endgame_loss_cut")
            self.assertEqual(res["loss_cents"], 52)
            self.assertEqual(res["exit_price"], 22)
            mock_cancel.assert_called_once_with(self.ticker)
            mock_liquidate.assert_called_once_with(self.ticker, "yes", 1, 22)

    def test_endgame_12_5min_winning_position_holds_to_settlement(self):
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 74,
        }

        with patch.object(self.client, "cancel_and_verify_resting_orders"), \
             patch.object(self.client, "execute_liquidation") as mock_liquidate:
            res = self.client.evaluate_and_execute_12_5min_endgame_check(
                ticker=self.ticker,
                current_yes_price=88,
                current_no_price=12,
                dry_run=False,
                endgame_floor=25,
            )
            self.assertFalse(res["triggered"])
            self.assertEqual(res["action"], "holding_winner")
            mock_liquidate.assert_not_called()

    def test_kalshi_taker_liquidation_payloads(self):
        with patch.object(KalshiClient, "__init__", return_value=None):
            kalshi = KalshiClient()
            kalshi.session = MagicMock()
            kalshi._profit_floors = {}
            kalshi._entry_prices = {}

            with patch.object(kalshi, "post") as mock_post:
                # Test YES liquidation produces immediate taker ask at 0.0100
                kalshi.execute_liquidation("KXBTC15M-TEST", "yes", 2, 45)
                mock_post.assert_called_once()
                payload = mock_post.call_args[1]["payload"]
                self.assertEqual(payload["ticker"], "KXBTC15M-TEST")
                self.assertEqual(payload["side"], "ask")
                self.assertEqual(payload["count"], "2")
                self.assertEqual(payload["price"], "0.0100")
                self.assertEqual(payload["time_in_force"], "immediate_or_cancel")

            with patch.object(kalshi, "post") as mock_post:
                # Test NO liquidation produces immediate taker bid on YES at 0.9900
                kalshi.execute_liquidation("KXBTC15M-TEST", "no", 1, 30)
                mock_post.assert_called_once()
                payload = mock_post.call_args[1]["payload"]
                self.assertEqual(payload["ticker"], "KXBTC15M-TEST")
                self.assertEqual(payload["side"], "bid")
                self.assertEqual(payload["count"], "1")
                self.assertEqual(payload["price"], "0.9900")
                self.assertEqual(payload["time_in_force"], "immediate_or_cancel")

    def test_kalshi_client_init_and_entry_prices(self):
        # Verify that KalshiClient initializes base state properly without manual mocking
        with patch("signals.kalshi_client._load_private_key", return_value=MagicMock()):
            client = KalshiClient(api_key_id="test-key-id")
            self.assertTrue(hasattr(client, "_entry_prices"))
            self.assertTrue(hasattr(client, "_profit_floors"))
            self.assertIsNone(client.get_entry_price("TEST"))
            client.set_entry_price("TEST", 45)
            self.assertEqual(client.get_entry_price("TEST"), 45)
            client.clear_entry_price("TEST")
            self.assertIsNone(client.get_entry_price("TEST"))

    def test_base_trading_client_uninitialized_entry_prices_safety(self):
        # Even if a subclass somehow omitted super().__init__(), properties prevent AttributeError
        class UninitializedClient(BaseTradingClient):
            def __init__(self):
                pass
            def get_market_position(self, ticker): return None
            def cancel_and_verify_resting_orders(self, ticker): return True
            def execute_liquidation(self, ticker, side, count, price): return {}
            def execute_buy(self, ticker, side, count, price): return {}
            def cancel_unfilled_buy_orders(self, ticker): pass
            def get_positions(self, **kwargs): return {}
            def get_orders(self, **kwargs): return {}
            def get_15m_crypto_markets(self, **kwargs): return []

        client = UninitializedClient()
        self.assertIsNone(client.get_entry_price("TEST"))
        client.set_entry_price("TEST", 50)
        self.assertEqual(client.get_entry_price("TEST"), 50)
        client.clear_entry_price("TEST")
        self.assertIsNone(client.get_entry_price("TEST"))
        self.assertIsNone(client.get_profit_floor("TEST"))
        client.set_profit_floor("TEST", 60)
        self.assertEqual(client.get_profit_floor("TEST"), 60)
        client.clear_profit_floor("TEST")
        self.assertIsNone(client.get_profit_floor("TEST"))

    def test_calculate_seconds_until_next_checkpoint_schedule(self):
        # At 14:02:00 -> should sleep until 14:05:30 (210s)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 15, 14, 2, 0, tzinfo=timezone.utc)
            sec = calculate_seconds_until_next_checkpoint(offset_seconds=30.0)
            self.assertEqual(sec, 210.0)

        # At 14:05:35 -> should sleep until 14:10:30 (295s)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 15, 14, 5, 35, tzinfo=timezone.utc)
            sec = calculate_seconds_until_next_checkpoint(offset_seconds=30.0)
            self.assertEqual(sec, 295.0)

        # At 14:10:35 -> should sleep until 14:12:30 (115s)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 15, 14, 10, 35, tzinfo=timezone.utc)
            sec = calculate_seconds_until_next_checkpoint(offset_seconds=30.0)
            self.assertEqual(sec, 115.0)

        # At 14:12:35 -> should sleep until 14:15:30 (175s)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 15, 14, 12, 35, tzinfo=timezone.utc)
            sec = calculate_seconds_until_next_checkpoint(offset_seconds=30.0)
            self.assertEqual(sec, 175.0)

    def test_minute_12_process_single_market_evaluates_endgame(self):
        client = MagicMock()
        client.exchange_name = "Kalshi"
        client.evaluate_and_execute_12_5min_endgame_check.return_value = {
            "triggered": True,
            "side": "yes",
            "loss_cents": 12,
            "exit_price": 20,
            "action": "endgame_loss_cut",
        }

        market = {
            "ticker": "KXBTC15M-TEST",
            "series_ticker": "KXBTC15M",
            "yes_ask": 22,
            "yes_bid": 20,
            "no_ask": 80,
            "no_bid": 78,
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        now = datetime.now(timezone.utc)

        mock_filter = {
            "sl_threshold": 10,
            "sl_floor": 30,
            "cut_5min": 40,
            "endgame_12_5_floor": 25,
            "min_buy": 32,
            "max_buy": 50,
            "tp": 82,
            "avoid_mid_zone": True,
            "mid_zone_min": 37,
            "mid_zone_max": 43,
            "is_exploration": False,
        }

        with patch("main.get_dynamic_filter_for_market", return_value=mock_filter):
            snapshot = process_single_market(
                market=market,
                is_trading_interval=False,
                client=client,
                now_utc=now,
                minute_past_hour=12,
                spot_cache={"BTC": 59900.0},
                dry_run=False,
            )

        client.evaluate_and_execute_12_5min_endgame_check.assert_called_once_with(
            ticker="KXBTC15M-TEST",
            current_yes_price=20,
            current_no_price=78,
            dry_run=False,
            endgame_floor=25,
        )
        self.assertIn("trd", snapshot)
        self.assertEqual(snapshot["trd"]["action"], "endgame_loss_cut")

    def test_minute_10_process_single_market_evaluates_10min_strategy(self):
        client = MagicMock()
        client.exchange_name = "Kalshi"
        client.evaluate_and_execute_10min_strategy.return_value = {
            "triggered": True,
            "side": "yes",
            "exit_price": 72,
            "profit_locked_cents": 32,
            "rebuy_price_cents": 74,
            "action": "winner_rolled_rebuy",
        }

        market = {
            "ticker": "KXBTC15M-TEST",
            "series_ticker": "KXBTC15M",
            "yes_ask": 74,
            "yes_bid": 72,
            "no_ask": 28,
            "no_bid": 26,
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        now = datetime.now(timezone.utc)

        mock_filter = {
            "sl_threshold": 10,
            "sl_floor": 30,
            "cut_5min": 40,
            "endgame_12_5_floor": 25,
            "min_buy": 32,
            "max_buy": 50,
            "tp": 82,
            "avoid_mid_zone": True,
            "mid_zone_min": 37,
            "mid_zone_max": 43,
            "is_exploration": False,
        }

        with patch("main.get_dynamic_filter_for_market", return_value=mock_filter):
            snapshot = process_single_market(
                market=market,
                is_trading_interval=False,
                client=client,
                now_utc=now,
                minute_past_hour=10,
                spot_cache={"BTC": 60100.0},
                dry_run=False,
            )

        client.evaluate_and_execute_10min_strategy.assert_called_once_with(
            ticker="KXBTC15M-TEST",
            current_yes_bid=72,
            current_no_bid=26,
            current_yes_ask=74,
            current_no_ask=28,
            dry_run=False,
            stop_loss_threshold_cents=10,
            price_floor=30,
        )
        self.assertIn("trd", snapshot)
        self.assertEqual(snapshot["trd"]["action"], "winner_rolled_rebuy")
        self.assertEqual(snapshot["trd"]["profit_locked_cents"], 32)
        self.assertEqual(snapshot["trd"]["rebuy_price_cents"], 74)

    def test_cancellation_failure_aborts_liquidation(self):
        """Verifies that failed order cancellations abort liquidations to avoid locked position errors."""
        client = DummyTradingClient()
        client._positions_cache["KXBTC15M-TEST"] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 40,
        }

        with patch.object(client, "cancel_and_verify_resting_orders", return_value=False):
            with patch.object(client, "execute_liquidation") as mock_liq:
                # 1. 10m stop loss
                res_10m = client.evaluate_and_execute_10min_strategy(
                    "KXBTC15M-TEST", current_yes_bid=25, current_no_bid=75, stop_loss_threshold_cents=10
                )
                self.assertFalse(res_10m["triggered"])
                self.assertEqual(res_10m.get("error"), "resting_orders_not_cleared")
                mock_liq.assert_not_called()

                # 2. 5m cut loss
                res_5m = client.evaluate_and_execute_5min_cut_loss(
                    "KXBTC15M-TEST", current_yes_price=30, current_no_price=70, min_price_threshold=40
                )
                self.assertFalse(res_5m["triggered"])
                self.assertEqual(res_5m.get("error"), "resting_orders_not_cleared")
                mock_liq.assert_not_called()

                # 3. 12.5m endgame check
                res_12_5m = client.evaluate_and_execute_12_5min_endgame_check(
                    "KXBTC15M-TEST", current_yes_price=20, current_no_price=80, endgame_floor=25
                )
                self.assertFalse(res_12_5m["triggered"])
                self.assertEqual(res_12_5m.get("error"), "resting_orders_not_cleared")
                mock_liq.assert_not_called()

    def test_kalshi_client_ioc_only_liquidation_no_gtc(self):
        """Verifies that KalshiClient execute_liquidation strictly uses IOC and never submits 99c GTC limit orders."""
        client = KalshiClient(api_key_id="test_key", private_key=MagicMock())

        # Test NO liquidation
        posted_payloads = []
        with patch.object(client, "post") as mock_post:
            mock_post.side_effect = [Exception("Primary IOC fail"), {"status": "executed"}]
            with patch.object(client, "get_market", return_value={"market": {"no_bid": 30}}):
                client.execute_liquidation("KXBTC15M-TEST", "no", 1, 30)

            self.assertEqual(mock_post.call_count, 2)
            first_call_payload = mock_post.call_args_list[0][1]["payload"]
            second_call_payload = mock_post.call_args_list[1][1]["payload"]

            # Both attempts MUST be immediate_or_cancel
            self.assertEqual(first_call_payload["time_in_force"], "immediate_or_cancel")
            self.assertEqual(second_call_payload["time_in_force"], "immediate_or_cancel")
            self.assertNotEqual(second_call_payload["time_in_force"], "good_till_canceled")

    def test_kalshi_client_batch_position_and_order_caching(self):
        """Verifies that KalshiClient batch-caches portfolio positions and resting orders to eliminate 429 rate limit errors."""
        client = KalshiClient(api_key_id="test_key", private_key=MagicMock())

        fake_positions_resp = {
            "market_positions": [
                {
                    "ticker": "KXBTC15M-TEST",
                    "position_fp": "1.0",
                    "market_exposure_dollars": "0.42",
                },
                {
                    "ticker": "KXETHD-TEST",
                    "position_fp": "-2.0",
                    "market_exposure_dollars": "0.76",
                }
            ]
        }

        fake_orders_resp = {
            "orders": [
                {
                    "ticker": "KXBTC15M-TEST",
                    "order_id": "ord_1",
                    "client_order_id": "sell_tp_yes_KXBTC15M-TEST_123",
                    "side": "ask",
                }
            ]
        }

        with patch.object(client, "get_positions", return_value=fake_positions_resp) as mock_get_pos:
            with patch.object(client, "get_orders", return_value=fake_orders_resp) as mock_get_orders:
                # Query 50 different strike tickers in a loop
                for i in range(50):
                    pos = client.get_market_position(f"KXBTC15M-STRIKE-{i}")
                    self.assertIsNone(pos)
                    has_tp = client.has_resting_take_profit_order(f"KXBTC15M-STRIKE-{i}")
                    self.assertFalse(has_tp)

                # Query the existing tickers
                pos_btc = client.get_market_position("KXBTC15M-TEST")
                self.assertIsNotNone(pos_btc)
                self.assertEqual(pos_btc["side"], "yes")
                self.assertEqual(pos_btc["count"], 1)

                pos_eth = client.get_market_position("KXETHD-TEST")
                self.assertIsNotNone(pos_eth)
                self.assertEqual(pos_eth["side"], "no")
                self.assertEqual(pos_eth["count"], 2)

                has_tp_btc = client.has_resting_take_profit_order("KXBTC15M-TEST")
                self.assertTrue(has_tp_btc)

                # Despite 100+ lookups, exactly 1 batch GET /portfolio/positions and 1 batch GET /portfolio/orders were made!
                self.assertEqual(mock_get_pos.call_count, 1)
                self.assertEqual(mock_get_orders.call_count, 1)


if __name__ == "__main__":
    unittest.main()
