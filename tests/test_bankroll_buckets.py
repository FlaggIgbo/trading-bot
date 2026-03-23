"""Bankroll exposure bucket trimming (stdlib unittest)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from constants import REC_BUY_NO, REC_BUY_YES

from scripts.compare_historical import (
    _cap_bankroll_exposure_buckets,
    _compute_limit_price,
    _trim_order_group_to_notional_cap,
)


class TestTrimNotionalCap(unittest.TestCase):
    def test_no_change_when_under_cap(self) -> None:
        orders = [{"price": 0.5, "count": 4, "edge": 0.1, "yf_ticker": "X"}]
        self.assertFalse(_trim_order_group_to_notional_cap(orders, 500))
        self.assertEqual(orders[0]["count"], 4)

    def test_trims_to_cap_by_efficiency(self) -> None:
        hi = {"price": 0.5, "count": 10, "edge": 0.20, "yf_ticker": "X", "expected_hours_to_resolution": 1.0}
        lo = {"price": 0.5, "count": 10, "edge": 0.05, "yf_ticker": "X", "expected_hours_to_resolution": 1.0}
        orders = [lo, hi]
        # 250¢ cap: keep high-edge leg full (500¢) doesn't fit — both 50¢/contract
        self.assertTrue(_trim_order_group_to_notional_cap(orders, 250))
        total = sum(int(o["price"] * 100) * o["count"] for o in orders)
        self.assertLessEqual(total, 250)
        self.assertEqual(hi["count"], 5)
        self.assertEqual(lo["count"], 0)


class TestCapBankrollBuckets(unittest.TestCase):
    def test_per_direction_then_underlying(self) -> None:
        pending = [
            {"rec": REC_BUY_YES, "yf_ticker": "BTC-USD", "price": 0.4, "count": 10, "edge": 0.2},
            {"rec": REC_BUY_NO, "yf_ticker": "BTC-USD", "price": 0.4, "count": 10, "edge": 0.1},
        ]
        _cap_bankroll_exposure_buckets(
            pending,
            1000,
            max_frac_per_direction=0.1,
            max_frac_per_underlying=0.0,
            logger=lambda *_a, **_k: None,
        )
        # 10% of 1000 = 100¢ per side; 40¢/contract → max 2 contracts each
        self.assertEqual(pending[0]["count"], 2)
        self.assertEqual(pending[1]["count"], 2)


class TestPassiveOnlyBelowEdge(unittest.TestCase):
    def test_thin_edge_stays_passive(self) -> None:
        px, _ = _compute_limit_price(
            bid=0.40,
            ask=0.42,
            p_win=0.55,
            maker_fee_rate=0.0,
            min_fee_per_order=0.0,
            mode="layered",
            base_dip=0.05,
            tick_offset=1,
            held_same_side=0,
            held_opp_side=0,
            max_inventory_skew_pct=0.1,
            layered_order_levels=2,
            order_count=1,
            max_dip_cap=0.25,
            passive_quote="dip",
            net_edge=0.06,
            edge_aggression_enabled=True,
            cross_spread_min_edge=0.12,
            near_market_min_edge=0.08,
            passive_only_below_edge=0.075,
        )
        # Layered dip: below bid, not ask-1¢ aggressive
        self.assertLess(px, 0.40)

    def test_strong_edge_can_cross_when_above_passive_floor(self) -> None:
        px, _ = _compute_limit_price(
            bid=0.40,
            ask=0.42,
            p_win=0.70,
            maker_fee_rate=0.0,
            min_fee_per_order=0.0,
            mode="layered",
            base_dip=0.05,
            tick_offset=1,
            held_same_side=0,
            held_opp_side=0,
            max_inventory_skew_pct=0.1,
            layered_order_levels=2,
            order_count=1,
            max_dip_cap=0.25,
            passive_quote="dip",
            net_edge=0.15,
            edge_aggression_enabled=True,
            cross_spread_min_edge=0.12,
            near_market_min_edge=0.08,
            passive_only_below_edge=0.075,
        )
        self.assertGreaterEqual(px, 0.40)


class TestEvTierLimitPricing(unittest.TestCase):
    def test_medium_tier_uses_spread_fraction(self) -> None:
        px, _ = _compute_limit_price(
            bid=0.40,
            ask=0.44,
            p_win=0.60,
            maker_fee_rate=0.0,
            min_fee_per_order=0.0,
            mode="layered",
            base_dip=0.10,
            tick_offset=1,
            held_same_side=0,
            held_opp_side=0,
            max_inventory_skew_pct=0.1,
            layered_order_levels=2,
            order_count=1,
            max_dip_cap=0.25,
            passive_quote="dip",
            net_edge=0.07,
            edge_aggression_enabled=True,
            cross_spread_min_edge=0.10,
            near_market_min_edge=0.05,
            passive_only_below_edge=0.0,
            ev_tier_pricing=True,
            weak_bid_min_edge=0.03,
            skip_below_weak_edge=True,
            medium_quote_mode="spread_fraction",
            medium_spread_fraction=0.25,
        )
        self.assertIsNotNone(px)
        assert px is not None
        # bid + 0.25 * 0.04 = 0.41
        self.assertAlmostEqual(px, 0.41, places=2)

    def test_weak_tier_at_bid(self) -> None:
        px, _ = _compute_limit_price(
            bid=0.35,
            ask=0.40,
            p_win=0.55,
            maker_fee_rate=0.0,
            min_fee_per_order=0.0,
            mode="layered",
            base_dip=0.10,
            tick_offset=1,
            held_same_side=0,
            held_opp_side=0,
            max_inventory_skew_pct=0.1,
            layered_order_levels=2,
            order_count=1,
            max_dip_cap=0.25,
            passive_quote="dip",
            net_edge=0.04,
            edge_aggression_enabled=True,
            cross_spread_min_edge=0.10,
            near_market_min_edge=0.05,
            passive_only_below_edge=0.0,
            ev_tier_pricing=True,
            weak_bid_min_edge=0.03,
            skip_below_weak_edge=True,
        )
        self.assertIsNotNone(px)
        assert px is not None
        self.assertAlmostEqual(px, 0.35, places=2)

    def test_skip_when_below_weak_and_skip_enabled(self) -> None:
        px, dip = _compute_limit_price(
            bid=0.40,
            ask=0.42,
            p_win=0.55,
            maker_fee_rate=0.0,
            min_fee_per_order=0.0,
            mode="layered",
            base_dip=0.05,
            tick_offset=1,
            held_same_side=0,
            held_opp_side=0,
            max_inventory_skew_pct=0.1,
            layered_order_levels=2,
            order_count=1,
            max_dip_cap=0.25,
            passive_quote="dip",
            net_edge=0.02,
            edge_aggression_enabled=True,
            cross_spread_min_edge=0.10,
            near_market_min_edge=0.05,
            passive_only_below_edge=0.0,
            ev_tier_pricing=True,
            weak_bid_min_edge=0.03,
            skip_below_weak_edge=True,
        )
        self.assertIsNone(px)
        self.assertEqual(dip, 0.0)


if __name__ == "__main__":
    unittest.main()
