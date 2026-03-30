#!/usr/bin/env python3
"""
Print YAML comment lines suggesting `strategy.blacklist` entries for weak series.
Does not modify config automatically — review and paste into config.yaml.

  python3 scripts/suggest_series_blacklist.py [--hours 336] [--min-trades 80] [--max-win-rate 0.22]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config  # noqa: E402

try:
    from storage import get_recent_performance_by_series
except ImportError:
    get_recent_performance_by_series = None  # type: ignore[misc,assignment]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=336.0)
    ap.add_argument("--min-trades", type=int, default=80)
    ap.add_argument("--max-win-rate", type=float, default=0.22)
    args = ap.parse_args()

    if not get_recent_performance_by_series:
        print("storage not available", file=sys.stderr)
        return 1

    perf = get_recent_performance_by_series(lookback_hours=args.hours, config=get_config()) or {}
    bad: list[tuple[str, dict]] = []
    for s, p in perf.items():
        n = int(p.get("total", 0))
        wr = p.get("win_rate")
        if n < args.min_trades or wr is None:
            continue
        if wr < args.max_win_rate:
            bad.append((s, p))

    bad.sort(key=lambda x: (x[1].get("win_rate") or 0, x[1].get("total", 0)))
    print(f"# Suggested blacklist (lookback {args.hours:.0f}h, n>={args.min_trades}, WR<{args.max_win_rate:.0%})")
    if not bad:
        print("# (none)")
        return 0
    tickers = [b[0] for b in bad]
    print(f"# strategy:")
    print(f"#   blacklist: {tickers}")
    for s, p in bad:
        print(
            f"#   - {s}: n={p.get('total')} wr={p.get('win_rate'):.1%} pnl_cents={p.get('pnl_cents', 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
