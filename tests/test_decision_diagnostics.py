"""Tests for decision_diag required-edge JSON helper."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from models.decision_diagnostics import build_required_edge_breakdown
from models.strategy_registry import STRATEGY_REGISTRY, build_strategy_engine_by_name


class TestBuildRequiredEdgeBreakdown(unittest.TestCase):
    def test_simple_ev_chain(self) -> None:
        r = build_required_edge_breakdown(
            simple_ev=True,
            effective_min_final=0.09,
            effective_min_yes=0.09,
            effective_min_no=0.09,
            min_p_win=0.67,
            us_detail={"base": 0.08, "horizon_mult": 1.2, "late_mult": 1.0},
            eff_after_simple_ev_bundle=0.096,
            proxy_basis_add=0.0,
            legacy_core_before_parity_obi=None,
        )
        self.assertEqual(r["eff_min_final"], 0.09)
        self.assertIn("simple_ev", r)
        self.assertEqual(r["simple_ev"]["base_from_config"], 0.08)
        self.assertEqual(r["simple_ev"]["after_ultra_short_late_mult"], 0.096)
        self.assertEqual(r["simple_ev"]["final"], 0.09)
        self.assertEqual(r["max_side_req_pre_recommend"], 0.09)

    def test_legacy_core(self) -> None:
        r = build_required_edge_breakdown(
            simple_ev=False,
            effective_min_final=0.12,
            effective_min_yes=0.14,
            effective_min_no=0.11,
            min_p_win=0.75,
            us_detail=None,
            eff_after_simple_ev_bundle=None,
            proxy_basis_add=0.0,
            legacy_core_before_parity_obi=0.10,
        )
        self.assertIn("legacy", r)
        self.assertEqual(r["legacy"]["core_before_parity_obi"], 0.10)
        self.assertEqual(r["max_side_req_pre_recommend"], 0.14)


class TestVolRegimeAlias(unittest.TestCase):
    def test_registry_alias(self) -> None:
        self.assertIs(STRATEGY_REGISTRY["vol_regime"], STRATEGY_REGISTRY["vol_regime_threshold"])
        a = build_strategy_engine_by_name("vol_regime")
        b = build_strategy_engine_by_name("vol_regime_threshold")
        self.assertEqual(type(a), type(b))


if __name__ == "__main__":
    unittest.main()
