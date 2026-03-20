#!/usr/bin/env python3
"""
Write underperforming Kalshi series prefixes to a newline-separated file for runtime merge.

compare_historical.py merges `strategy.blacklist_extra_file` into the YAML blacklist on each run.
This avoids corrupting config.yaml with `echo >>` (YAML comments/structure preserved).

  python3 scripts/apply_series_blacklist.py --hours 168 --min-trades 20 --max-win-rate 0.25 \\
      --out ~/.trading-bot-data/series_blacklist_auto.txt --merge
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


def bad_series(
    *,
    lookback_hours: float,
    min_trades: int,
    max_win_rate: float,
) -> list[str]:
    if not get_recent_performance_by_series:
        return []
    perf = get_recent_performance_by_series(lookback_hours=lookback_hours, config=get_config()) or {}
    out: list[str] = []
    for s, p in perf.items():
        n = int(p.get("total", 0))
        wr = p.get("win_rate")
        if n < min_trades or wr is None:
            continue
        if wr < max_win_rate:
            out.append(str(s).strip().upper())
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="Write auto-blacklist series file (merged at runtime by compare_historical).")
    ap.add_argument("--hours", type=float, default=168.0, help="Lookback hours for performance stats")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--max-win-rate", type=float, default=0.25)
    ap.add_argument("--out", type=Path, required=True, help="Output path (newline-separated series)")
    ap.add_argument(
        "--merge",
        action="store_true",
        help="Union with existing lines in --out (keeps prior bans)",
    )
    args = ap.parse_args()

    bad = set(bad_series(lookback_hours=args.hours, min_trades=args.min_trades, max_win_rate=args.max_win_rate))
    out_path = args.out.expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing: set[str] = set()
    if args.merge and out_path.is_file():
        for line in out_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.split("#", 1)[0].strip().upper()
            if s:
                existing.add(s)

    final = sorted(existing | bad)
    out_path.write_text("\n".join(final) + ("\n" if final else ""), encoding="utf-8")
    print(f"Wrote {out_path} ({len(final)} series, {len(bad)} newly flagged this run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
