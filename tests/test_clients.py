"""
Unit tests for Kalshi and Gemini prediction market clients and main trading pipeline.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from main import extract_asset_symbol, extract_price_cents, is_spot_momentum_aligned, process_single_market
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
            dry_run=True,
        )

        self.assertEqual(snapshot["tick"], "BTC15M-TEST")
        self.assertEqual(snapshot["ex"], "gemini")
        self.assertEqual(snapshot["y"], 42)
        self.assertEqual(snapshot["n"], 58)


if __name__ == "__main__":
    unittest.main()
