#!/usr/bin/env python3
"""
Write per-series (Kalshi prefix) performance from SQLite into docs/series_leaderboard.html.
Used by ollama_codex_loop.sh (optional) or run manually after compare_historical fills predictions.

  python3 scripts/export_series_leaderboard.py [--hours 336] [--out docs/series_leaderboard.html]
"""
from __future__ import annotations

import argparse
import html
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
    ap.add_argument("--hours", type=float, default=336.0, help="Lookback hours (default 14d)")
    ap.add_argument("--out", type=Path, default=_ROOT / "docs" / "series_leaderboard.html")
    args = ap.parse_args()

    if not get_recent_performance_by_series:
        print("storage.get_recent_performance_by_series not available", file=sys.stderr)
        return 1

    cfg = get_config()
    perf = get_recent_performance_by_series(lookback_hours=args.hours, config=cfg) or {}
    rows = sorted(
        perf.items(),
        key=lambda kv: (kv[1].get("win_rate") or 0, kv[1].get("pnl_cents", 0)),
        reverse=True,
    )

    lines = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'/>",
        "<style>body{font-family:system-ui,sans-serif;} table{border-collapse:collapse;} th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;}</style>",
        "</head><body>",
        f"<h1>Series leaderboard ({args.hours:.0f}h)</h1>",
        "<p>From SQLite <code>predictions</code> (resolved). Use with <code>suggest_series_blacklist.py</code> for config hints.</p>",
        "<table><tr><th>Series</th><th>Trades</th><th>Win rate</th><th>PnL (¢)</th><th>Consec. losses</th></tr>",
    ]
    for series, p in rows:
        wr = p.get("win_rate")
        wr_s = f"{wr:.1%}" if wr is not None else "—"
        lines.append(
            "<tr>"
            f"<td>{html.escape(str(series))}</td>"
            f"<td>{int(p.get('total', 0))}</td>"
            f"<td>{wr_s}</td>"
            f"<td>{int(p.get('pnl_cents', 0))}</td>"
            f"<td>{int(p.get('consecutive_losses', 0))}</td>"
            "</tr>"
        )
    if not rows:
        lines.append("<tr><td colspan='5'>No resolved trades in window.</td></tr>")
    lines.append("</table></body></html>")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.out} ({len(rows)} series)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
