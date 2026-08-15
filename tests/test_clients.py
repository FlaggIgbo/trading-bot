"""
Unit tests for Kalshi and Gemini prediction market clients and main trading pipeline.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from database import save_portfolio_pnl, save_settlements
from main import (
    batch_fetch_crypto_spot_prices,
    calculate_seconds_until_next_5min_mark,
    extract_asset_symbol,
    extract_price_cents,
    is_spot_momentum_aligned,
    process_single_market,
)
from signals.gemini_client import GeminiClient, GeminiPredictionClient, GeminiTradingClient
from signals.kalshi_client import KalshiClient, round_price_to_tick


class TestClientInterfaces(unittest.TestCase):
    def test_gemini_client_instantiation(self):
        client = GeminiClient()
        self.assertIsNotNone(client)
        self.assertIsInstance(client, GeminiPredictionClient)
        self.assertIsInstance(client, GeminiTradingClient)

    def test_gemini_auth_headers(self):
        client = GeminiTradingClient()
        client.api_key = "test_key"
        client.api_secret = "test_secret"
        client.account = "trading-bot"

        headers = client._build_auth_headers("/v1/balances")
        self.assertIn("X-GEMINI-APIKEY", headers)
        self.assertEqual(headers["X-GEMINI-APIKEY"], "test_key")
        self.assertIn("X-GEMINI-PAYLOAD", headers)
        self.assertIn("X-GEMINI-SIGNATURE", headers)

    def test_gemini_balance_multi_currency(self):
        client = GeminiTradingClient()
        # Mock _post response with USD, GUSD, and other tokens
        mock_balances = [
            {"currency": "USD", "amount": "10.50", "available": "10.50"},
            {"currency": "GUSD", "amount": "25.00", "available": "25.00"},
            {"currency": "BTC", "amount": "0.5", "available": "0.5"},
        ]
        with patch.object(client, "_post", return_value=mock_balances):
            bal = client.get_balance()
            self.assertEqual(bal["balance"], 35.50)
            self.assertEqual(bal["available"], 35.50)

    def test_gemini_instant_deposit_balance(self):
        client = GeminiTradingClient()
        # Mock pending ACH / instant deposit where available > amount
        mock_balances = [
            {"currency": "USD", "amount": "0.001", "available": "10.001", "availableForWithdrawal": "0.001"},
        ]
        with patch.object(client, "_post", return_value=mock_balances):
            bal = client.get_balance()
            self.assertAlmostEqual(bal["available"], 10.001)
            pnl = client.get_portfolio_pnl_summary()
            self.assertAlmostEqual(pnl["balance_dollars"], 10.001)

    def test_price_helper_functions(self):
        self.assertEqual(round_price_to_tick(0.423), "0.42")
        self.assertEqual(round_price_to_tick(0.001), "0.01")
        self.assertEqual(round_price_to_tick(1.05), "0.99")

    def test_extract_asset_symbol(self):
        self.assertEqual(extract_asset_symbol("KXBTC15M", "KXBTC15M-26AUG140015-15"), "BTC")
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
        client.exchange_name = "Gemini"
        client.get_market_position.return_value = None
        client.has_existing_position_or_order.return_value = False

        market = {
            "ticker": "BTC15M-TEST",
            "series_ticker": "BTC15M",
            "yes_ask_dollars": "0.4200",
            "no_ask_dollars": "0.5800",
            "yes_bid": 41,
            "yes_ask": 42,
            "no_bid": 57,
            "no_ask": 58,
            "floor_strike": 60000.0,
            "exchange": "gemini",
        }
        now = datetime.now(timezone.utc)

        snapshot = process_single_market(
            market=market,
            is_trading_interval=False,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60100.0},
            dry_run=True,
        )

        self.assertEqual(snapshot["tick"], "BTC15M-TEST")
        self.assertEqual(snapshot["ex"], "gemini")
        self.assertEqual(snapshot["y"], 42)
        self.assertEqual(snapshot["n"], 58)
        self.assertEqual(snapshot["p"], 60100.0)

    def test_stop_loss_exclusive_gating(self):
        client = MagicMock()
        client.exchange_name = "Gemini"
        client.evaluate_and_execute_stop_loss.return_value = {
            "triggered": True,
            "side": "yes",
            "loss_cents": 12,
            "exit_price": 28,
        }

        market = {
            "ticker": "BTC15M-TEST",
            "series_ticker": "BTC15M",
            "yes_ask": 28,
            "no_ask": 72,
            "floor_strike": 60000.0,
            "exchange": "gemini",
        }
        now = datetime.now(timezone.utc)

        # At minute 10 (% 15 == 10) -> is_stop_loss_interval is True
        snapshot = process_single_market(
            market=market,
            is_trading_interval=True,  # Even if force_trade is set
            client=client,
            now_utc=now,
            minute_past_hour=10,
            spot_cache={"BTC": 59900.0},
            dry_run=True,
        )

        self.assertIn("trd", snapshot)
        self.assertEqual(snapshot["trd"]["action"], "stop_loss_sold")
        # Ensure buy was not evaluated after stop loss trigger
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
            "exchange": "gemini",
            "balance_dollars": 100.0,
            "portfolio_value_dollars": 100.0,
        }
        res = save_portfolio_pnl(pnl_data)
        self.assertTrue(res)
        mock_coll.insert_one.assert_called_once()
        inserted_doc = mock_coll.insert_one.call_args[0][0]
        self.assertEqual(inserted_doc["ex"], "gemini")


class TestProfitFloorAndStopLoss(unittest.TestCase):
    def setUp(self):
        self.client = GeminiTradingClient()
        self.ticker = "KXBTC15M-TEST"

    def test_profit_floor_arming_at_70_cents(self):
        # Mock active YES position bought at 40¢
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 40,
        }

        # Evaluate at current price 72¢ (>= 70¢)
        with patch.object(self.client, "cancel_and_verify_resting_orders") as mock_cancel:
            res = self.client.evaluate_and_execute_stop_loss(
                ticker=self.ticker,
                current_yes_price=72,
                current_no_price=28,
                dry_run=False,
            )
            self.assertFalse(res["triggered"])
            self.assertEqual(res["action"], "canceled_tp_let_run")
            self.assertEqual(res["profit_floor_cents"], 62)  # 72 - 10 = 62¢
            self.assertTrue(self.client.has_profit_floor(self.ticker))
            self.assertEqual(self.client.get_profit_floor(self.ticker), 62)
            mock_cancel.assert_called_once_with(self.ticker)

    def test_profit_floor_ratchet_upwards(self):
        # Position arms at 70¢ -> floor 60¢
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 40,
        }
        self.client.set_profit_floor(self.ticker, 60)

        # Price rises to 85¢ -> floor should ratchet up to 75¢
        with patch.object(self.client, "cancel_and_verify_resting_orders"):
            res = self.client.evaluate_and_execute_stop_loss(
                ticker=self.ticker,
                current_yes_price=85,
                current_no_price=15,
                dry_run=False,
            )
            self.assertFalse(res["triggered"])
            self.assertEqual(res["profit_floor_cents"], 75)  # 85 - 10 = 75¢
            self.assertEqual(self.client.get_profit_floor(self.ticker), 75)

    def test_profit_floor_liquidation_trigger(self):
        # Position armed with floor at 65¢ (entry at 40¢)
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 40,
        }
        self.client.set_profit_floor(self.ticker, 65)

        # Market drops to 64¢ (<= 65¢)
        with patch.object(self.client, "cancel_and_verify_resting_orders") as mock_cancel, \
             patch.object(self.client, "execute_liquidation") as mock_liquidate:
            res = self.client.evaluate_and_execute_stop_loss(
                ticker=self.ticker,
                current_yes_price=64,
                current_no_price=36,
                dry_run=False,
            )
            self.assertTrue(res["triggered"])
            self.assertEqual(res["action"], "profit_floor_sold")
            self.assertEqual(res["profit_locked_cents"], 24)  # 64 - 40 = 24¢ profit
            self.assertEqual(res["exit_price"], 64)
            # Ensure profit floor is cleared after exit
            self.assertFalse(self.client.has_profit_floor(self.ticker))
            mock_cancel.assert_called_once_with(self.ticker)
            mock_liquidate.assert_called_once_with(self.ticker, "yes", 1, 64)

    def test_standard_stop_loss_liquidation(self):
        # Position bought at 42¢ with NO armed profit floor
        self.client._positions_cache[self.ticker] = {
            "side": "yes",
            "count": 1,
            "entry_price_cents": 42,
        }

        # Market drops to 30¢ (loss = 12¢ >= 10¢ limit and price <= 30¢)
        with patch.object(self.client, "cancel_and_verify_resting_orders") as mock_cancel, \
             patch.object(self.client, "execute_liquidation") as mock_liquidate:
            res = self.client.evaluate_and_execute_stop_loss(
                ticker=self.ticker,
                current_yes_price=30,
                current_no_price=70,
                dry_run=False,
            )
            self.assertTrue(res["triggered"])
            self.assertEqual(res["action"], "stop_loss_sold")
            self.assertEqual(res["loss_cents"], 12)
            self.assertEqual(res["exit_price"], 30)
            mock_cancel.assert_called_once_with(self.ticker)
            mock_liquidate.assert_called_once_with(self.ticker, "yes", 1, 30)


if __name__ == "__main__":
    unittest.main()
