#!/usr/bin/env python3
"""
Interval metrics for ollama_codex_loop: win rate + PnL for position_evaluations
resolved between the previous metrics CSV row timestamp and --now-ts.

Output (stdout): `<win_rate_percent> <pnl_dollars>` e.g. `29.4 0.50`
(win_rate is 0..100 for display; pnl_dollars has 2 decimal places).
If no position_evaluations resolved in the interval (n==0), prints `null null` so dashboards
don't show misleading 0% / $0.00.

If there is no prior row in metrics.csv, start_ts = now_ts - fallback_hours (env
OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS, default 24).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config  # noqa: E402

try:
    from storage import get_position_evaluation_interval_stats
except ImportError:
    get_position_evaluation_interval_stats = None  # type: ignore[misc,assignment]


def _last_metrics_ts(metrics_csv: Path) -> float | None:
    if not metrics_csv.is_file():
        return None
    try:
        text = metrics_csv.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    last_line = text.splitlines()[-1].strip()
    parts = [p.strip() for p in last_line.split(",")]
    if len(parts) < 1:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics-csv", type=Path, required=True)
    ap.add_argument(
        "--now-ts",
        type=float,
        default=None,
        help="Unix end of interval (default: time.time())",
    )
    ap.add_argument(
        "--fallback-hours",
        type=float,
        default=None,
        help="If no prior CSV row, window length ending at now (default: env or 24)",
    )
    args = ap.parse_args()

    if not get_position_evaluation_interval_stats:
        print("null null", flush=True)
        print("storage.get_position_evaluation_interval_stats not available", file=sys.stderr)
        return 0

    import time

    now_ts = float(args.now_ts) if args.now_ts is not None else time.time()
    fb_h = args.fallback_hours
    if fb_h is None:
        fb_h = float(os.environ.get("OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS", "24"))

    prev_ts = _last_metrics_ts(args.metrics_csv)
    if prev_ts is None:
        start_ts = now_ts - max(0.0, fb_h) * 3600.0
    else:
        start_ts = prev_ts

    if start_ts >= now_ts:
        start_ts = now_ts - 60.0

    cfg = get_config()
    stats = get_position_evaluation_interval_stats(cfg, start_ts=start_ts, end_ts=now_ts)
    if not stats.get("enabled"):
        print("null null", flush=True)
        return 0

    n = int(stats.get("n") or 0)
    if n == 0:
        print("null null", flush=True)
        if os.environ.get("OLLAMA_METRICS_INTERVAL_DEBUG", "").lower() in ("1", "true", "yes"):
            print(f"# interval ({start_ts:g}, {now_ts:g}] n=0 (no resolved position evals)", file=sys.stderr)
        return 0

    wr = stats.get("win_rate")
    wr_pct = (wr * 100.0) if wr is not None else 0.0
    pnl_cents = int(stats.get("pnl_cents") or 0)
    pnl_dollars = pnl_cents / 100.0

    print(f"{wr_pct:g} {pnl_dollars:.2f}", flush=True)
    if os.environ.get("OLLAMA_METRICS_INTERVAL_DEBUG", "").lower() in ("1", "true", "yes"):
        print(
            f"# interval ({start_ts:g}, {now_ts:g}] n={n} wins={stats.get('wins')} losses={stats.get('losses')}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
