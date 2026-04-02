"""Per-underlying open contract cap (stdlib unittest)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.compare_historical import (
    _apply_max_open_contracts_per_underlying,
    _effective_open_contract_cap_for_order,
    _pending_order_is_ultra_short_horizon,
)


class TestUltraShortHorizonForCap(unittest.TestCase):
    def test_kalshi_15m_ticker_pattern(self) -> None:
        o = {"ticker": "KXBTC15M-26APR021030-30", "title": ""}
        self.assertTrue(_pending_order_is_ultra_short_horizon(o, 1.0))

    def test_dte_within_ultra_short_hours(self) -> None:
        o = {"ticker": "KXFOO", "title": "", "dte": 1.0 / 48.0}
        self.assertTrue(_pending_order_is_ultra_short_horizon(o, 24.0))

    def test_dte_outside_ultra_short_hours(self) -> None:
        o = {"ticker": "KXFOO", "title": "", "dte": 2.0}
        self.assertFalse(_pending_order_is_ultra_short_horizon(o, 1.0))


class TestEffectiveOpenContractCap(unittest.TestCase):
    def test_ultra_short_cap_when_zero(self) -> None:
        o = {"ticker": "KXBTC15M-X", "title": ""}
        self.assertEqual(
            _effective_open_contract_cap_for_order(o, 20, 0, 1.0),
            20,
        )

    def test_ultra_short_cap_applied(self) -> None:
        o = {"ticker": "KXBTC15M-X", "title": ""}
        self.assertEqual(
            _effective_open_contract_cap_for_order(o, 20, 35, 1.0),
            35,
        )


class TestApplyMaxOpenContracts(unittest.TestCase):
    def test_blocks_when_over_base_cap(self) -> None:
        def sums(_cfg):
            return {("kalshi", "BTC-USD"): 19}

        pending = [
            {
                "platform": "kalshi",
                "yf_ticker": "BTC-USD",
                "count": 5,
                "ticker": "KXBTCD-X",
                "title": "",
                "dte": 1.0,
            }
        ]
        _apply_max_open_contracts_per_underlying(
            pending,
            {},
            20,
            sums,
            logger=lambda *_a, **_k: None,
            ultra_short_cap=0,
            ultra_short_dte_hours=1.0,
        )
        self.assertEqual(pending, [])

    def test_ultra_short_cap_allows_headroom(self) -> None:
        def sums(_cfg):
            return {("kalshi", "BTC-USD"): 19}

        pending = [
            {
                "platform": "kalshi",
                "yf_ticker": "BTC-USD",
                "count": 2,
                "ticker": "KXBTC15M-X",
                "title": "",
            }
        ]
        _apply_max_open_contracts_per_underlying(
            pending,
            {},
            20,
            sums,
            logger=lambda *_a, **_k: None,
            ultra_short_cap=30,
            ultra_short_dte_hours=1.0,
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["count"], 2)


if __name__ == "__main__":
    unittest.main()
