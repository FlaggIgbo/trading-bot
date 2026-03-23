"""Config deep-merge and overlay toggle (stdlib unittest)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import deep_merge_dict, get_config


class TestDeepMerge(unittest.TestCase):
    def test_nested_overlay_wins(self) -> None:
        base = {"strategy": {"min_edge": 0.10, "simple_ev_decision": {"min_edge": 0.10}}}
        over = {"strategy": {"simple_ev_decision": {"min_edge": 0.08}}}
        m = deep_merge_dict(base, over)
        self.assertEqual(m["strategy"]["min_edge"], 0.10)
        self.assertEqual(m["strategy"]["simple_ev_decision"]["min_edge"], 0.08)

    def test_top_level_replace(self) -> None:
        m = deep_merge_dict({"poll_interval_seconds": 60}, {"poll_interval_seconds": 120})
        self.assertEqual(m["poll_interval_seconds"], 120)


class TestGetConfigUsesOverlay(unittest.TestCase):
    def test_merged_simple_ev_min_edge(self) -> None:
        old = os.environ.pop("TRADING_BOT_MINIMAL_OVERLAY", None)
        try:
            cfg = get_config()
            sed = (cfg.get("strategy") or {}).get("simple_ev_decision") or {}
            self.assertEqual(sed.get("min_edge"), 0.08)
        finally:
            if old is not None:
                os.environ["TRADING_BOT_MINIMAL_OVERLAY"] = old

    def test_overlay_disabled_env(self) -> None:
        old = os.environ.get("TRADING_BOT_MINIMAL_OVERLAY")
        try:
            os.environ["TRADING_BOT_MINIMAL_OVERLAY"] = "0"
            cfg = get_config()
            sed = (cfg.get("strategy") or {}).get("simple_ev_decision") or {}
            # Base config.yaml keeps 0.10 when overlay off
            self.assertEqual(sed.get("min_edge"), 0.10)
        finally:
            if old is None:
                os.environ.pop("TRADING_BOT_MINIMAL_OVERLAY", None)
            else:
                os.environ["TRADING_BOT_MINIMAL_OVERLAY"] = old


if __name__ == "__main__":
    unittest.main()
