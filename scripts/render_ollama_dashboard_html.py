#!/usr/bin/env python3
"""
Build docs/ollama_runs.html from metrics CSV and companion paths.

Called from ollama_codex_loop.sh. Uses CSV-aware parsing, html.escape, and UTC
timestamps so the dashboard is correct on Linux and macOS (no date -r portability issues).
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _parse_metrics_rows(metrics_csv: Path, *, max_rows: int = 10) -> list[tuple[str, str, str]]:
    """Return up to max_rows newest rows as (ts_str, win_rate_str, pnl_str), newest first."""
    if not metrics_csv.is_file():
        return []
    raw: list[tuple[str, str, str]] = []
    try:
        text = metrics_csv.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 2)]
        if len(parts) != 3:
            continue
        ts_s, wr, pnl = parts
        try:
            float(ts_s)
        except ValueError:
            continue
        raw.append((ts_s, wr, pnl))
    tail = raw[-max_rows:]
    return list(reversed(tail))


def _format_ts_utc(ts_str: str) -> str:
    try:
        ts = float(ts_str)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError, OverflowError):
        return html.escape(ts_str)


def _display_wr(wr: str) -> str:
    w = wr.strip()
    if w == "null":
        return "null"
    return f"{html.escape(w)}%"


def _display_pnl(pnl: str) -> str:
    return html.escape(pnl.strip())


def render_html(
    *,
    metrics_rows: list[tuple[str, str, str]],
    metrics_csv_display: str,
    log_label: str,
    leaderboard_path: Path | None,
    blacklist_suggest_path: Path | None,
    leaderboard_hours: float,
    blacklist_min_trades: int,
) -> str:
    gen_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    esc_log = html.escape(log_label)
    esc_csv = html.escape(metrics_csv_display)

    lines = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'/>",
        "<title>Ollama optimization runs</title>",
        "<style>body{font-family:system-ui,sans-serif;} table{border-collapse:collapse;} "
        "td,th{padding:8px; border:1px solid #ccc;}</style>",
        "</head><body>",
        "<h1>Ollama optimization runs</h1>",
        "<p>Validated loop: backtest baseline + smoke compare_historical before commit. "
        "Each table row is one Ollama loop tick. <strong>Win rate</strong> and <strong>PnL ($)</strong> are for "
        "<strong>position evaluations</strong> (KEEP/SELL) <em>resolved in SQLite</em> during "
        "(previous row timestamp, this row timestamp], via <code>metrics_interval_position_eval.py</code>. "
        "If the script fails, the loop falls back to a snapshot from the latest eval log "
        "(<code>parse_compare_log_metrics.py</code>). Per-series iframe: <code>export_series_leaderboard.py</code>.</p>",
        "<p><small>Empty interval (no resolved position evals): win rate and PnL show as <code>null</code>.</small></p>",
        f"<p><small>Latest metrics log: {esc_log} · Generated {html.escape(gen_utc)} · metrics CSV: {esc_csv}</small></p>",
        "<table><tr><th>Timestamp (UTC)</th><th>Win rate</th><th>PnL ($)</th></tr>",
    ]
    for ts_s, wr, pnl in metrics_rows:
        lines.append(
            "<tr>"
            f"<td>{_format_ts_utc(ts_s)}</td>"
            f"<td>{_display_wr(wr)}</td>"
            f"<td>{_display_pnl(pnl)}</td>"
            "</tr>"
        )
    if not metrics_rows:
        lines.append("<tr><td colspan='3'><em>No metrics rows yet.</em></td></tr>")
    lines.append("</table>")

    lines.append(f"<h2>Series leaderboard ({leaderboard_hours:g}h lookback)</h2>")
    lines.append(
        '<p>Open <a href="series_leaderboard.html">series_leaderboard.html</a> (same folder). '
        "Sorted by PnL, then win rate; iframe below.</p>"
    )
    if leaderboard_path and leaderboard_path.is_file():
        lines.append(
            '<iframe src="series_leaderboard.html" title="Series leaderboard" width="100%" '
            'height="360" style="border:1px solid #ccc"></iframe>'
        )
    else:
        lines.append(
            "<p><em>series_leaderboard.html not found — export_series_leaderboard.py may have failed.</em></p>"
        )

    lines.append(
        f"<h2>Blacklist suggestions (WR &lt;30%, n≥{blacklist_min_trades} — manual review only)</h2>"
        '<pre style="white-space:pre-wrap">'
    )
    if blacklist_suggest_path and blacklist_suggest_path.is_file():
        try:
            sug = blacklist_suggest_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            sug = ""
        lines.append(html.escape(sug))
    lines.append("</pre>")
    lines.append("</body></html>")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render ollama_runs.html dashboard")
    ap.add_argument("--metrics-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--leaderboard", type=Path, default=None, help="Path to series_leaderboard.html (iframe if exists)")
    ap.add_argument("--blacklist-suggest", type=Path, default=None)
    ap.add_argument("--log-label", type=str, default="", help="Provenance: compare_historical log path")
    ap.add_argument("--leaderboard-hours", type=float, default=336.0)
    ap.add_argument("--blacklist-min-trades", type=int, default=50)
    ap.add_argument("--max-metric-rows", type=int, default=10)
    args = ap.parse_args()

    rows = _parse_metrics_rows(args.metrics_csv, max_rows=max(1, int(args.max_metric_rows)))
    html_out = render_html(
        metrics_rows=rows,
        metrics_csv_display=str(args.metrics_csv.resolve()),
        log_label=args.log_label or "",
        leaderboard_path=args.leaderboard,
        blacklist_suggest_path=args.blacklist_suggest,
        leaderboard_hours=float(args.leaderboard_hours),
        blacklist_min_trades=int(args.blacklist_min_trades),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_out, encoding="utf-8")
    print(f"Wrote {args.out} ({len(rows)} metrics row(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
