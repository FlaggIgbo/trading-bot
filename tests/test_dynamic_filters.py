"""
Unit tests for the Dynamic Price Filters & 6-Hour In-Memory Calibration Engine.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from database import (
    DEFAULT_DYNAMIC_PRICE_MAP,
    calibrate_dynamic_price_map_from_db,
    get_dynamic_filter_for_market,
    should_refresh_dynamic_map,
)


class TestDynamicPriceFilters(unittest.TestCase):
    def test_default_price_map_structure(self):
        """Verifies default fallback map contains all expected keys and valid ranges."""
        self.assertIn("DEFAULT", DEFAULT_DYNAMIC_PRICE_MAP)
        for sym, marks in DEFAULT_DYNAMIC_PRICE_MAP.items():
            self.assertIn("00", marks)
            self.assertIn("05", marks)
            for mark, bounds in marks.items():
                self.assertIn("min_buy", bounds)
                self.assertIn("max_buy", bounds)
                self.assertIn("tp", bounds)
                self.assertLess(bounds["min_buy"], bounds["max_buy"])
                self.assertLess(bounds["max_buy"], bounds["tp"])

    def test_should_refresh_dynamic_map_rules(self):
        """Tests cold start, 6-hour TTL, and clock marker trigger conditions."""
        # 1. Cold start: cache is empty -> Must refresh
        self.assertTrue(should_refresh_dynamic_map(cache_is_empty=True))

        # 2. Cache exists and last refresh was only 1 hour ago -> Should NOT refresh
        now = datetime(2026, 8, 14, 10, 30, 0, tzinfo=timezone.utc)
        one_hour_ago = (now - timedelta(hours=1)).timestamp()
        self.assertFalse(
            should_refresh_dynamic_map(
                now_utc=now,
                last_refresh_time=one_hour_ago,
                cache_is_empty=False,
            )
        )

        # 3. Cache exists but last refresh was 7 hours ago (> 6 hr TTL) -> Must refresh
        seven_hours_ago = (now - timedelta(hours=7)).timestamp()
        self.assertTrue(
            should_refresh_dynamic_map(
                now_utc=now,
                last_refresh_time=seven_hours_ago,
                cache_is_empty=False,
            )
        )

        # 4. Clock hits a 6-hour interval marker (00:00, 06:00, 12:00, 18:00 UTC) -> Must refresh
        clock_six_am = datetime(2026, 8, 14, 6, 2, 0, tzinfo=timezone.utc)
        earlier_today = datetime(2026, 8, 14, 5, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertTrue(
            should_refresh_dynamic_map(
                now_utc=clock_six_am,
                last_refresh_time=earlier_today,
                cache_is_empty=False,
            )
        )

    @patch("database.get_mongo_collection")
    def test_calibrate_with_mock_database(self, mock_get_coll):
        """Tests empirical calibration using mock snapshots and settlements."""
        # Create mock snapshots for high volatility ZEC and stable BTC
        mock_snaps = [
            {"sym": "ZEC", "tick": "KXZEC15M-1", "m": 0, "pct": 0.35, "trd": {"buy": 36, "side": "yes"}},
            {"sym": "ZEC", "tick": "KXZEC15M-2", "m": 0, "pct": -0.40, "trd": {"buy": 38, "side": "yes"}},
            {"sym": "ZEC", "tick": "KXZEC15M-3", "m": 0, "pct": 0.25, "trd": {"buy": 35, "side": "yes"}},
            {"sym": "ZEC", "tick": "KXZEC15M-4", "m": 0, "pct": -0.30, "trd": {"buy": 37, "side": "yes"}},
            {"sym": "ZEC", "tick": "KXZEC15M-5", "m": 0, "pct": 0.20, "trd": {"buy": 39, "side": "yes"}},
            {"sym": "BTC", "tick": "KXBTC15M-1", "m": 0, "pct": 0.03, "trd": {"buy": 44, "side": "no"}},
            {"sym": "BTC", "tick": "KXBTC15M-2", "m": 0, "pct": -0.04, "trd": {"buy": 45, "side": "no"}},
        ]
        mock_settles = [
            {"ticker": "KXZEC15M-1", "market_result": "yes"},
            {"ticker": "KXZEC15M-2", "market_result": "yes"},
            {"ticker": "KXZEC15M-3", "market_result": "yes"},
            {"ticker": "KXZEC15M-4", "market_result": "yes"},
            {"ticker": "KXZEC15M-5", "market_result": "yes"},
            {"ticker": "KXBTC15M-1", "market_result": "no"},
            {"ticker": "KXBTC15M-2", "market_result": "no"},
        ]

        def get_coll_side_effect(name):
            coll = MagicMock()
            if name == "market_snapshots":
                coll.find.return_value = mock_snaps
            elif name == "settlements":
                coll.find.return_value = mock_settles
            return coll

        mock_get_coll.side_effect = get_coll_side_effect

        calibrated = calibrate_dynamic_price_map_from_db()
        self.assertIn("ZEC", calibrated)
        self.assertIn("BTC", calibrated)

        # ZEC (high volatility) should have a dynamic TP higher or equal to base and clamped bounds
        zec_00 = calibrated["ZEC"]["00"]
        self.assertGreaterEqual(zec_00["tp"], 80)
        self.assertLessEqual(zec_00["max_buy"], 48)
        self.assertGreaterEqual(zec_00["min_buy"], 25)

    def test_get_dynamic_filter_for_market_lookups(self):
        """Tests helper function returns correct parameters for known and unknown symbols."""
        custom_map = {
            "SOL": {
                "00": {"min_buy": 33, "max_buy": 44, "tp": 84},
                "05": {"min_buy": 39, "max_buy": 45, "tp": 77},
            },
            "DEFAULT": {
                "00": {"min_buy": 35, "max_buy": 45, "tp": 82},
                "05": {"min_buy": 40, "max_buy": 47, "tp": 76},
            }
        }

        # Known symbol at :00 mark
        sol_00 = get_dynamic_filter_for_market("SOL", is_5min_mark=False, dynamic_map=custom_map)
        self.assertEqual(sol_00["min_buy"], 33)
        self.assertEqual(sol_00["max_buy"], 44)
        self.assertEqual(sol_00["tp"], 84)

        # Known symbol at :05 mark
        sol_05 = get_dynamic_filter_for_market("SOL", is_5min_mark=True, dynamic_map=custom_map)
        self.assertEqual(sol_05["min_buy"], 39)
        self.assertEqual(sol_05["max_buy"], 45)
        self.assertEqual(sol_05["tp"], 77)

        # Unknown symbol should gracefully fall back to DEFAULT
        xyz_00 = get_dynamic_filter_for_market("XYZ", is_5min_mark=False, dynamic_map=custom_map)
        self.assertEqual(xyz_00["min_buy"], 35)
        self.assertEqual(xyz_00["max_buy"], 45)
        self.assertEqual(xyz_00["tp"], 82)

    def test_runway_timing_clamp_and_fields(self):
        """Tests that Minute :05 entries are strictly clamped to <= 38¢ and contain all required dynamic fields."""
        for sym, marks in DEFAULT_DYNAMIC_PRICE_MAP.items():
            filter_00 = get_dynamic_filter_for_market(sym, is_5min_mark=False)
            filter_05 = get_dynamic_filter_for_market(sym, is_5min_mark=True)

            # Minute :05 max_buy must be tightened to <= 38¢ (Recommendation 1)
            self.assertLessEqual(filter_05["max_buy"], 38)
            self.assertLess(filter_05["max_buy"], filter_00["max_buy"])

            # Must contain dynamic stop loss, endgame floor, and middle zone fields
            for f in (filter_00, filter_05):
                self.assertIn("sl_threshold", f)
                self.assertIn("sl_floor", f)
                self.assertIn("cut_5min", f)
                self.assertIn("endgame_12_5_floor", f)
                self.assertIn("avoid_mid_zone", f)
                self.assertIn("mid_zone_min", f)
                self.assertIn("mid_zone_max", f)

    def test_asset_specific_stop_loss_defaults(self):
        """Tests that mean-reverting assets (DOGE, BNB) have wider SL bounds than trending assets (BTC, SOL)."""
        doge = get_dynamic_filter_for_market("DOGE", is_5min_mark=False)
        btc = get_dynamic_filter_for_market("BTC", is_5min_mark=False)

        # DOGE has wider tolerance (15¢ SL, 25¢ floor, 34¢ cut) vs BTC (10¢ SL, 30¢ floor, 40¢ cut)
        self.assertGreater(doge["sl_threshold"], btc["sl_threshold"])
        self.assertLess(doge["sl_floor"], btc["sl_floor"])
        self.assertLess(doge["cut_5min"], btc["cut_5min"])

    @patch("database.get_mongo_collection")
    def test_dynamic_calibration_false_alarm_adaptation(self, mock_get_coll):
        """Tests that high false-alarm rates dynamically trigger wider SL thresholds for that asset."""
        mock_snaps = [
            # DOGE had 4 early cut events where the trade settled winning (False alarms!)
            {"sym": "DOGE", "tick": "KXDOGE15M-1", "m": 0, "pct": 0.04, "trd": {"buy": 35, "side": "yes", "action": "buy_opened"}},
            {"sym": "DOGE", "tick": "KXDOGE15M-1", "m": 11, "pct": 0.04, "trd": {"side": "yes", "action": "early_loss_cut", "loss_cents": 10}},
            {"sym": "DOGE", "tick": "KXDOGE15M-2", "m": 0, "pct": 0.04, "trd": {"buy": 35, "side": "yes", "action": "buy_opened"}},
            {"sym": "DOGE", "tick": "KXDOGE15M-2", "m": 11, "pct": 0.04, "trd": {"side": "yes", "action": "early_loss_cut", "loss_cents": 10}},
            # BTC had 4 early cut events where the trade settled losing (Rightful saves!)
            {"sym": "BTC", "tick": "KXBTC15M-1", "m": 0, "pct": 0.02, "trd": {"buy": 42, "side": "yes", "action": "buy_opened"}},
            {"sym": "BTC", "tick": "KXBTC15M-1", "m": 11, "pct": 0.02, "trd": {"side": "yes", "action": "early_loss_cut", "loss_cents": 10}},
            {"sym": "BTC", "tick": "KXBTC15M-2", "m": 0, "pct": 0.02, "trd": {"buy": 42, "side": "yes", "action": "buy_opened"}},
            {"sym": "BTC", "tick": "KXBTC15M-2", "m": 11, "pct": 0.02, "trd": {"side": "yes", "action": "early_loss_cut", "loss_cents": 10}},
        ]
        mock_settles = [
            {"ticker": "KXDOGE15M-1", "market_result": "yes"},  # False alarm
            {"ticker": "KXDOGE15M-2", "market_result": "yes"},  # False alarm
            {"ticker": "KXBTC15M-1", "market_result": "no"},   # Rightful save
            {"ticker": "KXBTC15M-2", "market_result": "no"},   # Rightful save
        ]

        def get_coll_side_effect(name):
            coll = MagicMock()
            if name == "market_snapshots":
                coll.find.return_value = mock_snaps
            elif name == "settlements":
                coll.find.return_value = mock_settles
            return coll

        mock_get_coll.side_effect = get_coll_side_effect

        calibrated = calibrate_dynamic_price_map_from_db()
        self.assertIn("DOGE", calibrated)
        self.assertIn("BTC", calibrated)

        doge_calib = calibrated["DOGE"]["00"]
        btc_calib = calibrated["BTC"]["00"]

        # DOGE should dynamically get wider SL parameters
        self.assertEqual(doge_calib["sl_threshold"], 15)
        self.assertEqual(doge_calib["sl_floor"], 25)
        self.assertEqual(doge_calib["cut_5min"], 34)

        # BTC should keep tight SL parameters
        self.assertEqual(btc_calib["sl_threshold"], 10)
        self.assertEqual(btc_calib["sl_floor"], 30)
        self.assertEqual(btc_calib["cut_5min"], 40)

    def test_process_single_market_middle_zone_screening(self):
        """Tests that middle zone (37-43¢) prices are skipped unless spot momentum is strong."""
        from main import process_single_market

        client = MagicMock()
        client.exchange_name = "Kalshi"
        client.get_market_position.return_value = None
        client.has_existing_position_or_order.return_value = False
        client.has_profit_floor.return_value = False
        client.place_initial_buy_order.return_value = {"order_id": "test_order"}

        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Case 1: Mid-zone price (40¢) with weak spot momentum (0.01% dist) -> Should be skipped!
        weak_mkt = {
            "ticker": "KXBTC15M-MID",
            "series_ticker": "KXBTC15M",
            "yes_ask": 40,
            "yes_bid": 38,
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        snap1 = process_single_market(
            market=weak_mkt,
            is_trading_interval=True,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60005.0},  # +0.008% dist -> weak momentum
            dry_run=True,
        )
        self.assertNotIn("trd", snap1)

        # Case 2: Mid-zone price (40¢) with strong spot momentum (> 0.08% dist) -> Should be allowed!
        snap2 = process_single_market(
            market=weak_mkt,
            is_trading_interval=True,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60100.0},  # +0.16% dist -> strong momentum
            dry_run=True,
        )
        self.assertIn("trd", snap2)
        self.assertEqual(snap2["trd"]["action"], "buy_opened")

        # Case 3: Deep discount price (34¢) with normal momentum -> Always allowed!
        discount_mkt = {
            "ticker": "KXBTC15M-DISC",
            "series_ticker": "KXBTC15M",
            "yes_ask": 34,
            "yes_bid": 32,
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        snap3 = process_single_market(
            market=discount_mkt,
            is_trading_interval=True,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60005.0},
            dry_run=True,
        )
        self.assertIn("trd", snap3)
        self.assertEqual(snap3["trd"]["action"], "buy_opened")


if __name__ == "__main__":
    unittest.main()
