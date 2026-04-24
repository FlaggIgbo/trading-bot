#!/usr/bin/env python3
"""
Summarize barbell base vs alpha on stored market_snapshots (BUY YES / BUY NO only).

Uses the same _barbell_* helpers as compare_historical. Requires SQLite snapshots
from prior runs (storage enabled). With --demo, prints synthetic examples without DB.

Run:
  python3 scripts/barbell_backtest_report.py [--days 90]
  python3 scripts/barbell_backtest_report.py --demo
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config
from constants import PLATFORM_GEMINI, PLATFORM_KALSHI, REC_BUY_NO, REC_BUY_YES


def _load_compare_historical():
    name = "compare_historical_barbell_report"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / "compare_historical.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _maker_for_platform(sp, platform: str, yf_ticker: str | None) -> float:
    _idx = frozenset({"^GSPC", "GSPC", "^NDX", "NDX", "SPY", "QQQ"})
    if platform == PLATFORM_KALSHI and (yf_ticker or "").strip().upper() in _idx:
        return max(0.0, float(sp.kalshi_sp500_maker_fee_rate))
    if platform == PLATFORM_GEMINI:
        return max(0.0, float(sp.gemini_maker_fee_rate))
    return max(0.0, float(sp.kalshi_maker_fee_rate))


def main() -> int:
    parser = argparse.ArgumentParser(description="Barbell classification report on snapshot history")
    parser.add_argument("--days", type=int, default=90, help="Last N days of snapshots (0=all)")
    parser.add_argument("--demo", action="store_true", help="Synthetic examples only (no DB)")
    args = parser.parse_args()

    ch = _load_compare_historical()
    cfg = get_config() or {}
    strategy = dict(cfg.get("strategy") or {})
    sp = ch._load_strategy_params(strategy)

    if args.demo or not sp.barbell_enabled:
        if not sp.barbell_enabled:
            print("[Barbell report] barbell_enabled=false in config; showing --demo shapes only.\n")

    print("=" * 60)
    print("  BARBELL DEMO (synthetic prices, current strategy YAML)")
    print("=" * 60)
    fee_k = _maker_for_platform(sp, PLATFORM_KALSHI, None)
    demos = [
        ("BUY YES high-P consensus (base)", REC_BUY_YES, 0.88, 0.85, 0.15),
        ("BUY YES cheap tail (alpha)", REC_BUY_YES, 0.35, 0.12, 0.88),
        ("BUY NO high-P consensus (base)", REC_BUY_NO, 0.15, 0.16, 0.84),
        ("Middle book (no bucket)", REC_BUY_YES, 0.55, 0.48, 0.52),
    ]
    for label, rec, hist_p, yb, nb in demos:
        yb = float(yb)
        nb = float(nb)
        if rec == REC_BUY_YES:
            bucket = ch._barbell_yes_bucket(hist_p, yb, fee_k, sp)
        else:
            bucket = ch._barbell_no_bucket(hist_p, nb, fee_k, sp)
        print(f"  {label}")
        print(f"    rec={rec}  hist_p={hist_p:.2f}  yes_bid={yb:.2f}  no_bid={nb:.2f}  →  {bucket!r}")
    print()

    if args.demo:
        return 0

    try:
        from storage import get_market_snapshots_for_backtest
    except ImportError:
        print("storage not available; use --demo")
        return 1

    import time
    end_ts = time.time()
    start_ts = (end_ts - (max(0, args.days) * 86400)) if args.days > 0 else None

    rows = (
        get_market_snapshots_for_backtest(
            start_ts=start_ts,
            end_ts=end_ts,
            rec_filter=[REC_BUY_YES, REC_BUY_NO],
            config=cfg,
        )
        or []
    )
    if not rows:
        print("[Barbell report] No BUY snapshots in range; demo above shows classification logic.")
        return 0

    counts: dict[str, int] = {"base": 0, "alpha": 0, "none": 0, "barbell_off": 0}
    for row in rows:
        if not sp.barbell_enabled:
            counts["barbell_off"] += 1
            continue
        rec = row.get("rec") or ""
        hist_p = float(row.get("hist_p") or 0.0)
        yes_bid = float(row.get("yes_bid") or 0.0)
        no_bid = float(row.get("no_bid") or 0.0) or max(0.0, min(0.99, 1.0 - yes_bid))
        plat = str(row.get("platform") or PLATFORM_KALSHI)
        yf_t = row.get("yf_ticker")
        fee = _maker_for_platform(sp, plat, str(yf_t) if yf_t else None)
        if rec == REC_BUY_YES:
            b = ch._barbell_yes_bucket(hist_p, yes_bid, fee, sp)
        elif rec == REC_BUY_NO:
            b = ch._barbell_no_bucket(hist_p, no_bid, fee, sp)
        else:
            continue
        if b is None:
            counts["none"] += 1
        else:
            counts[b] = counts.get(b, 0) + 1

    print("=" * 60)
    print(f"  BARBELL ON SNAPSHOTS (last {args.days}d, BUY YES/NO only)")
    print("=" * 60)
    print(f"  Snapshots in window: {len(rows)}")
    print(f"  barbell base:   {counts['base']}")
    print(f"  barbell alpha:  {counts['alpha']}")
    print(f"  not base/alpha: {counts['none']}")
    if counts["barbell_off"]:
        print(f"  (barbell off):  {counts['barbell_off']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
