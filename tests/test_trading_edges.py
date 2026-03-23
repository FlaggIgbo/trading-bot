"""Unit tests for shared edge / fee / regime math (stdlib unittest, no pytest required)."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from constants import REC_BUY_NO, REC_BUY_YES, REC_SKIP
from models.trading_edges import (
    clip_probability,
    maker_fee_per_contract,
    net_edges_buy_legs,
    simple_ev_spend_fraction,
    vol_regime_edge_dip_size_mults,
)


class TestMakerFee(unittest.TestCase):
    def test_zero_rate_returns_zero(self) -> None:
        self.assertEqual(maker_fee_per_contract(0.5, 0.0, 0.01), 0.0)

    def test_invalid_price_returns_zero(self) -> None:
        self.assertEqual(maker_fee_per_contract(0.0, 0.0175, 0.01), 0.0)
        self.assertEqual(maker_fee_per_contract(1.0, 0.0175, 0.01), 0.0)

    def test_ceiling_to_cent(self) -> None:
        # rate * p * (1-p) then ceil to cents, then max with min_fee
        fee = maker_fee_per_contract(0.5, 0.0175, 0.01)
        self.assertGreaterEqual(fee, 0.01)


class TestNetEdges(unittest.TestCase):
    def test_symmetric_market_equal_edges_when_p_matches_mid(self) -> None:
        p = 0.5
        yb = 0.5
        nb = 0.5
        ey, en = net_edges_buy_legs(p, yb, nb, 0.0, 0.01)
        self.assertAlmostEqual(ey, en, places=6)

    def test_high_p_favors_yes_leg(self) -> None:
        ey, en = net_edges_buy_legs(0.85, 0.50, 0.48, 0.0, 0.0)
        self.assertGreater(ey, en)


class TestSimpleEvSpendFraction(unittest.TestCase):
    def test_monotonic_in_ev(self) -> None:
        a = simple_ev_spend_fraction(0.02, 0.40, kelly_dampen=0.25, max_bankroll_fraction=0.05)
        b = simple_ev_spend_fraction(0.04, 0.40, kelly_dampen=0.25, max_bankroll_fraction=0.05)
        self.assertGreater(b, a)

    def test_respects_max_bankroll_cap(self) -> None:
        huge = simple_ev_spend_fraction(1.0, 0.40, kelly_dampen=1.0, max_bankroll_fraction=0.02)
        self.assertLessEqual(huge, 0.02 + 1e-9)

    def test_kill_switch_still_positive_spend_with_huge_ev(self) -> None:
        """Even aggressive EV, capped Kelly should leave a positive spend fraction."""
        sf = simple_ev_spend_fraction(0.50, 0.35, kelly_dampen=0.25, max_bankroll_fraction=0.10)
        self.assertGreater(sf, 0.0)


class TestVolRegime(unittest.TestCase):
    def test_disabled_is_identity(self) -> None:
        em, dm, sm = vol_regime_edge_dip_size_mults(
            enabled=False,
            vol_ratio=99.0,
            edge_mult_per_x=1.0,
            max_edge_mult=10.0,
            dip_mult_per_x=1.0,
            max_dip_mult=10.0,
            size_mult_per_x=1.0,
            min_size_mult=0.01,
        )
        self.assertEqual((em, dm, sm), (1.0, 1.0, 1.0))

    def test_extreme_vol_ratio_finite(self) -> None:
        em, dm, sm = vol_regime_edge_dip_size_mults(
            enabled=True,
            vol_ratio=100.0,
            edge_mult_per_x=0.35,
            max_edge_mult=2.5,
            dip_mult_per_x=0.5,
            max_dip_mult=2.0,
            size_mult_per_x=0.5,
            min_size_mult=0.35,
        )
        self.assertTrue(math.isfinite(em) and math.isfinite(dm) and math.isfinite(sm))
        self.assertGreaterEqual(em, 1.0)
        self.assertLessEqual(em, 2.5 + 1e-9)


class TestClipProbability(unittest.TestCase):
    def test_bounds(self) -> None:
        self.assertEqual(clip_probability(0.99, 0.02, 0.90, enabled=True), 0.90)
        self.assertEqual(clip_probability(0.001, 0.02, 0.90, enabled=True), 0.02)


def _fee_per_contract(price: float, maker_rate: float, min_fee_per_order: float = 0.01) -> float:
    return maker_fee_per_contract(price, maker_rate, min_fee_per_order)


def _recommend(
    historical_p_yes: float,
    market_yes_price: float,
    min_edge: float,
    maker_fee_rate: float = 0.0,
    *,
    min_fee_per_order: float = 0.01,
) -> str:
    """Mirrors scripts/compare_historical._recommend (fee + net edge gates)."""
    if market_yes_price <= 0:
        return REC_SKIP
    no_price = 1.0 - market_yes_price
    fee_yes = _fee_per_contract(market_yes_price, maker_fee_rate, min_fee_per_order)
    fee_no = _fee_per_contract(no_price, maker_fee_rate, min_fee_per_order)
    net_edge_yes = historical_p_yes - market_yes_price - fee_yes
    net_edge_no = (1.0 - historical_p_yes) - no_price - fee_no
    req_yes = req_no = float(min_edge)
    has_yes = net_edge_yes >= req_yes
    has_no = net_edge_no >= req_no
    if has_yes and has_no:
        return REC_BUY_YES if net_edge_yes >= net_edge_no else REC_BUY_NO
    if has_yes:
        return REC_BUY_YES
    if has_no:
        return REC_BUY_NO
    return REC_SKIP


class TestRecommendScenarios(unittest.TestCase):
    def test_should_trade_high_edge(self) -> None:
        rec = _recommend(0.75, 0.50, min_edge=0.05, maker_fee_rate=0.0, min_fee_per_order=0.0)
        self.assertIn(rec, (REC_BUY_YES, REC_BUY_NO))

    def test_should_skip_low_edge(self) -> None:
        rec = _recommend(0.52, 0.50, min_edge=0.10, maker_fee_rate=0.0, min_fee_per_order=0.0)
        self.assertEqual(rec, REC_SKIP)


if __name__ == "__main__":
    unittest.main()
