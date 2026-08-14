"""
Unit tests for the Dynamic Price Filters & 6-Hour In-Memory Calibration Engine.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from database import (
    DEFAULT_DYNAMIC_PRICE_MAP,
    calibrate_dynamic_price_map_from_db,
    get_dynamic_filter_for_market,
    get_dynamic_price_filter_map,
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


if __name__ == "__main__":
    unittest.main()
