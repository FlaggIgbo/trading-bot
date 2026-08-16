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
    is_default_exploration_window,
    should_refresh_dynamic_map,
)


class TestDynamicPriceFilters(unittest.TestCase):
    def setUp(self):
        import database
        database._DYNAMIC_PRICE_MAP_CACHE = None
        database._LAST_MAP_REFRESH_TIME = 0.0

    def tearDown(self):
        import database
        database._DYNAMIC_PRICE_MAP_CACHE = None
        database._LAST_MAP_REFRESH_TIME = 0.0

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

        # 4. Clock hits an anchor marker (00:00, 05:00, 07:00, 12:00, 18:00 UTC) -> Must refresh
        clock_seven_am = datetime(2026, 8, 14, 7, 2, 0, tzinfo=timezone.utc)
        earlier_today = datetime(2026, 8, 14, 6, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertTrue(
            should_refresh_dynamic_map(
                now_utc=clock_seven_am,
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

        # ZEC (high volatility) should have a dynamic TP and clamped bounds
        zec_00 = calibrated["ZEC"]["00"]
        self.assertGreaterEqual(zec_00["tp"], 58)
        self.assertLessEqual(zec_00["max_buy"], 44)
        self.assertGreaterEqual(zec_00["min_buy"], 18)

    def test_get_dynamic_filter_for_market_lookups(self):
        """Tests helper function returns correct parameters for known and unknown symbols."""
        custom_map = {
            "SOL": {
                "00": {"min_buy": 18, "max_buy": 44, "tp": 62},
                "05": {"min_buy": 18, "max_buy": 30, "tp": 58},
            },
            "DEFAULT": {
                "00": {"min_buy": 18, "max_buy": 44, "tp": 62},
                "05": {"min_buy": 18, "max_buy": 30, "tp": 58},
            }
        }

        # Known symbol at :00 mark
        sol_00 = get_dynamic_filter_for_market("SOL", is_5min_mark=False, dynamic_map=custom_map)
        self.assertEqual(sol_00["min_buy"], 18)
        self.assertEqual(sol_00["max_buy"], 44)
        self.assertEqual(sol_00["tp"], 62)

        # Known symbol at :05 mark
        sol_05 = get_dynamic_filter_for_market("SOL", is_5min_mark=True, dynamic_map=custom_map)
        self.assertEqual(sol_05["min_buy"], 18)
        self.assertEqual(sol_05["max_buy"], 30)
        self.assertEqual(sol_05["tp"], 58)

        # Unknown symbol should gracefully fall back to DEFAULT
        xyz_00 = get_dynamic_filter_for_market("XYZ", is_5min_mark=False, dynamic_map=custom_map)
        self.assertEqual(xyz_00["min_buy"], 18)
        self.assertEqual(xyz_00["max_buy"], 44)
        self.assertEqual(xyz_00["tp"], 62)

    def test_runway_timing_clamp_and_fields(self):
        """Tests that Minute :05 entries are strictly clamped to <= 30¢ and contain all required dynamic fields."""
        dt_test = datetime(2026, 8, 15, 1, 0, 0, tzinfo=timezone.utc)
        for sym, marks in DEFAULT_DYNAMIC_PRICE_MAP.items():
            filter_00 = get_dynamic_filter_for_market(sym, is_5min_mark=False, now_utc=dt_test)
            filter_05 = get_dynamic_filter_for_market(sym, is_5min_mark=True, now_utc=dt_test)

            # Minute :05 max_buy must be tightened to <= 30¢
            self.assertLessEqual(filter_05["max_buy"], 30)
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
        """Tests that dynamic stop-loss defaults use widened 25¢ threshold and 15¢ floor."""
        doge = get_dynamic_filter_for_market("DOGE", is_5min_mark=False)
        btc = get_dynamic_filter_for_market("BTC", is_5min_mark=False)

        self.assertEqual(doge["sl_threshold"], 25)
        self.assertEqual(doge["sl_floor"], 15)
        self.assertEqual(doge["cut_5min"], 15)
        self.assertEqual(btc["sl_threshold"], 25)
        self.assertEqual(btc["sl_floor"], 15)
        self.assertEqual(btc["cut_5min"], 15)

    @patch("database.get_mongo_collection")
    def test_dynamic_calibration_false_alarm_adaptation(self, mock_get_coll):
        """Tests that calibration loads data cleanly and produces updated deep discount thresholds."""
        mock_snaps = [
            {"sym": "DOGE", "tick": "KXDOGE15M-1", "m": 0, "pct": 0.04, "trd": {"buy": 30, "side": "yes", "action": "buy_opened"}},
            {"sym": "BTC", "tick": "KXBTC15M-1", "m": 0, "pct": 0.02, "trd": {"buy": 32, "side": "yes", "action": "buy_opened"}},
        ]
        mock_settles = [
            {"ticker": "KXDOGE15M-1", "market_result": "yes"},
            {"ticker": "KXBTC15M-1", "market_result": "no"},
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

        self.assertEqual(doge_calib["sl_threshold"], 25)
        self.assertEqual(doge_calib["sl_floor"], 15)
        self.assertEqual(doge_calib["cut_5min"], 15)
        self.assertEqual(btc_calib["sl_threshold"], 25)
        self.assertEqual(btc_calib["sl_floor"], 15)
        self.assertEqual(btc_calib["cut_5min"], 15)

    def test_process_single_market_middle_zone_screening(self):
        """Tests that above-range prices (> max_buy) are skipped and discount prices are bought."""
        from main import process_single_market

        client = MagicMock()
        client.exchange_name = "Kalshi"
        client.get_market_position.return_value = None
        client.has_existing_position_or_order.return_value = False
        client.has_profit_floor.return_value = False
        client.place_initial_buy_order.return_value = {"order_id": "test_order"}

        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Case 1: Expensive price (40¢) above max_buy (34¢) -> Should be skipped!
        expensive_mkt = {
            "ticker": "KXBTC15M-EXP",
            "series_ticker": "KXBTC15M",
            "yes_ask": 40,
            "yes_bid": 38,
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        snap1 = process_single_market(
            market=expensive_mkt,
            is_trading_interval=True,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60005.0},
            dry_run=True,
        )
        self.assertNotIn("trd", snap1)

        # Case 2: Deep discount price (30¢) with normal momentum -> Allowed!
        discount_mkt = {
            "ticker": "KXBTC15M-DISC",
            "series_ticker": "KXBTC15M",
            "yes_ask_dollars": "0.3000",
            "yes_bid_dollars": "0.2800",
            "no_ask_dollars": "0.7200",
            "no_bid_dollars": "0.7000",
            "floor_strike": 60000.0,
            "exchange": "kalshi",
        }
        snap2 = process_single_market(
            market=discount_mkt,
            is_trading_interval=True,
            client=client,
            now_utc=now,
            minute_past_hour=0,
            spot_cache={"BTC": 60005.0},
            dry_run=True,
        )
        self.assertIn("trd", snap2)
        self.assertEqual(snap2["trd"]["action"], "buy_opened")

    def test_is_default_exploration_window(self):
        """Tests that 05:00 - 07:00 UTC is recognized as the exploration window."""
        # 05:00 UTC -> True
        dt_0500 = datetime(2026, 8, 15, 5, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_default_exploration_window(dt_0500))

        # 06:30 UTC -> True
        dt_0630 = datetime(2026, 8, 15, 6, 30, 0, tzinfo=timezone.utc)
        self.assertTrue(is_default_exploration_window(dt_0630))

        # 07:00 UTC -> False
        dt_0700 = datetime(2026, 8, 15, 7, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(is_default_exploration_window(dt_0700))

        # 00:00 UTC -> False
        dt_0000 = datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(is_default_exploration_window(dt_0000))

        # 14:30 UTC -> False
        dt_1430 = datetime(2026, 8, 15, 14, 30, 0, tzinfo=timezone.utc)
        self.assertFalse(is_default_exploration_window(dt_1430))

    def test_should_refresh_at_0700_utc_anchor(self):
        """Tests that 07:00 UTC acts as an immediate post-exploration recalibration anchor."""
        # At 07:05 UTC, if last refresh was at 04:00 UTC -> should trigger refresh
        now_0705 = datetime(2026, 8, 15, 7, 5, 0, tzinfo=timezone.utc)
        last_refresh_0400 = datetime(2026, 8, 15, 4, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertTrue(should_refresh_dynamic_map(now_utc=now_0705, last_refresh_time=last_refresh_0400, cache_is_empty=False))

        # If already refreshed at 07:01 UTC -> should not re-trigger
        last_refresh_0701 = datetime(2026, 8, 15, 7, 1, 0, tzinfo=timezone.utc).timestamp()
        self.assertFalse(should_refresh_dynamic_map(now_utc=now_0705, last_refresh_time=last_refresh_0701, cache_is_empty=False))

    def test_get_dynamic_filter_exploration_window_fallback(self):
        """Tests that get_dynamic_filter_for_market returns defaults during 05:00-07:00 UTC window."""
        # 06:30 UTC -> Should use DEFAULT_DYNAMIC_PRICE_MAP and return is_exploration=True
        now_0630 = datetime(2026, 8, 15, 6, 30, 0, tzinfo=timezone.utc)
        filter_0630 = get_dynamic_filter_for_market("BTC", is_5min_mark=False, now_utc=now_0630)
        self.assertTrue(filter_0630["is_exploration"])
        self.assertEqual(filter_0630["max_buy"], DEFAULT_DYNAMIC_PRICE_MAP["BTC"]["00"]["max_buy"])

        # 14:30 UTC -> Should return is_exploration=False
        now_1430 = datetime(2026, 8, 15, 14, 30, 0, tzinfo=timezone.utc)
        filter_1430 = get_dynamic_filter_for_market("BTC", is_5min_mark=False, now_utc=now_1430)
        self.assertFalse(filter_1430["is_exploration"])


if __name__ == "__main__":
    unittest.main()
