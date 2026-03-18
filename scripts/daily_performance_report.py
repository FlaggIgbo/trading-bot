#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config


def _load_backtest_module():
    bt_path = _ROOT / "scripts" / "backtest.py"
    spec = importlib.util.spec_from_file_location("tb_backtest", bt_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backtest module: {bt_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_range(bt_mod, cfg: dict, mode: str, days: int) -> dict:
    end_ts = datetime.utcnow().timestamp()
    start_ts = (datetime.utcnow() - timedelta(days=days)).timestamp() if days > 0 else None
    if mode == "synthetic":
        return bt_mod._run_synthetic(cfg, start_ts, end_ts)
    return bt_mod._run_real(cfg, start_ts, end_ts)


def _fmt_pct(x) -> str:
    return "n/a" if x is None else f"{float(x):.1%}"


def _fmt_num(x) -> str:
    return "n/a" if x is None else f"{float(x):.2f}"


def _fmt_pnl_cents(x) -> str:
    return "n/a" if x is None else f"${float(x)/100:,.2f}"


def _generate_performance_graph(
    trade_details: list[dict],
    out_path: Path,
    title: str = "Bot Performance",
) -> Path | None:
    """
    Generate a performance chart: X=time, Y=P&L per trade.
    Green bars for wins, red bars for losses, grey for sold/other.
    Also plots cumulative P&L as a line overlay.
    Returns the saved image path, or None on failure.
    """
    if not trade_details:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    trades = sorted(trade_details, key=lambda t: t.get("ts", 0))
    timestamps = [datetime.utcfromtimestamp(t["ts"]) for t in trades if t.get("ts", 0) > 0]
    pnl_dollars = [t["pnl_cents"] / 100.0 for t in trades if t.get("ts", 0) > 0]
    outcomes = [t.get("outcome", "unknown") for t in trades if t.get("ts", 0) > 0]

    if not timestamps:
        return None

    colors = []
    for o in outcomes:
        if o == "win":
            colors.append("#22c55e")
        elif o == "lose":
            colors.append("#ef4444")
        elif o == "sold":
            colors.append("#94a3b8")
        else:
            colors.append("#64748b")

    cum_pnl = []
    running = 0.0
    for p in pnl_dollars:
        running += p
        cum_pnl.append(running)

    fig, ax1 = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#0f172a")
    ax1.set_facecolor("#1e293b")

    bar_width = max(0.3, min(2.0, 50.0 / max(1, len(timestamps))))
    ax1.bar(timestamps, pnl_dollars, width=bar_width, color=colors, alpha=0.85, zorder=3)
    ax1.axhline(y=0, color="#475569", linewidth=0.8, linestyle="--", zorder=2)
    ax1.set_ylabel("P&L per Trade ($)", color="#e2e8f0", fontsize=11)
    ax1.tick_params(axis="y", colors="#94a3b8")
    ax1.tick_params(axis="x", colors="#94a3b8")

    ax2 = ax1.twinx()
    ax2.plot(timestamps, cum_pnl, color="#38bdf8", linewidth=2, alpha=0.9, label="Cumulative P&L", zorder=4)
    ax2.fill_between(
        timestamps,
        cum_pnl,
        [0] * len(cum_pnl),
        alpha=0.1,
        color="#38bdf8",
        zorder=1,
    )
    ax2.set_ylabel("Cumulative P&L ($)", color="#38bdf8", fontsize=11)
    ax2.tick_params(axis="y", colors="#38bdf8")

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)

    wins = sum(1 for o in outcomes if o == "win")
    losses = sum(1 for o in outcomes if o == "lose")
    total_pnl = sum(pnl_dollars)
    win_pct = f"{wins / (wins + losses):.0%}" if (wins + losses) > 0 else "n/a"

    subtitle = f"{len(trades)} trades | {wins}W {losses}L ({win_pct}) | Net: ${total_pnl:+,.2f}"
    ax1.set_title(f"{title}\n{subtitle}", color="#f1f5f9", fontsize=13, fontweight="bold", pad=12)

    from matplotlib.patches import Patch

    legend_elements = [
        Patch(facecolor="#22c55e", label="Win"),
        Patch(facecolor="#ef4444", label="Loss"),
        Patch(facecolor="#94a3b8", label="Sold"),
        plt.Line2D([0], [0], color="#38bdf8", linewidth=2, label="Cumulative"),
    ]
    ax1.legend(handles=legend_elements, loc="upper left", framealpha=0.7, fontsize=9,
               facecolor="#1e293b", edgecolor="#475569", labelcolor="#e2e8f0")

    ax1.grid(axis="y", color="#334155", alpha=0.5, linewidth=0.5)
    ax1.set_axisbelow(True)

    for spine in [*ax1.spines.values(), *ax2.spines.values()]:
        spine.set_color("#334155")

    plt.tight_layout()
    fig.savefig(str(out_path), dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def main() -> int:
    cfg = get_config()
    bt_cfg = (cfg or {}).get("backtest", {})
    report_days_short = int(bt_cfg.get("daily_report_days_short", 7))
    report_days_long = int(bt_cfg.get("daily_report_days_long", 30))
    out_dir = Path(
        bt_cfg.get(
            "daily_report_dir",
            "~/Desktop/trading-bot-reports",
        )
    ).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    bt_mod = _load_backtest_module()
    real_short = _run_range(bt_mod, cfg, "real", report_days_short)
    real_long = _run_range(bt_mod, cfg, "real", report_days_long)
    syn_long = _run_range(bt_mod, cfg, "synthetic", report_days_long)

    now = datetime.now()
    file_path = out_dir / f"performance_{now.strftime('%Y%m%d')}.txt"

    lines: list[str] = []
    lines.append("Trading Bot Daily Performance Report")
    lines.append("=" * 40)
    lines.append(f"Generated: {now.isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"Real Backtest ({report_days_short}d)")
    lines.append("-" * 40)
    lines.append(f"Trades: {real_short.get('trades', 0)}")
    lines.append(f"Wins: {real_short.get('win_count', 0)}")
    lines.append(f"Losses: {real_short.get('lose_count', 0)}")
    lines.append(f"Win rate: {_fmt_pct(real_short.get('win_rate'))}")
    lines.append(f"PnL: {_fmt_pnl_cents(real_short.get('total_pnl_cents'))}")
    lines.append(f"Sharpe: {_fmt_num(real_short.get('sharpe'))}")
    lines.append(f"Max drawdown: {_fmt_pnl_cents(real_short.get('max_drawdown_cents'))}")
    lines.append("")
    lines.append(f"Real Backtest ({report_days_long}d)")
    lines.append("-" * 40)
    lines.append(f"Trades: {real_long.get('trades', 0)}")
    lines.append(f"Wins: {real_long.get('win_count', 0)}")
    lines.append(f"Losses: {real_long.get('lose_count', 0)}")
    lines.append(f"Win rate: {_fmt_pct(real_long.get('win_rate'))}")
    lines.append(f"PnL: {_fmt_pnl_cents(real_long.get('total_pnl_cents'))}")
    lines.append(f"Sharpe: {_fmt_num(real_long.get('sharpe'))}")
    lines.append(f"Max drawdown: {_fmt_pnl_cents(real_long.get('max_drawdown_cents'))}")
    lines.append("")
    lines.append(f"Synthetic Backtest ({report_days_long}d)")
    lines.append("-" * 40)
    lines.append(f"Trades: {syn_long.get('trades', 0)}")
    lines.append(f"Wins: {syn_long.get('win_count', 0)}")
    lines.append(f"Losses: {syn_long.get('lose_count', 0)}")
    lines.append(f"Win rate: {_fmt_pct(syn_long.get('win_rate'))}")
    lines.append(f"PnL: {_fmt_pnl_cents(syn_long.get('total_pnl_cents'))}")
    lines.append(f"Sharpe: {_fmt_num(syn_long.get('sharpe'))}")
    lines.append(f"Max drawdown: {_fmt_pnl_cents(syn_long.get('max_drawdown_cents'))}")
    if syn_long.get("skipped_not_filled") is not None:
        lines.append(f"Skipped (not filled): {syn_long.get('skipped_not_filled')}")

    report = "\n".join(lines) + "\n"
    file_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved report: {file_path}")

    # --- Performance graphs ---
    graph_short_path = out_dir / f"performance_{now.strftime('%Y%m%d')}_{report_days_short}d.png"
    graph_long_path = out_dir / f"performance_{now.strftime('%Y%m%d')}_{report_days_long}d.png"

    short_details = real_short.get("trade_details", [])
    long_details = real_long.get("trade_details", [])

    if short_details:
        saved = _generate_performance_graph(
            short_details,
            graph_short_path,
            title=f"Bot Performance — Last {report_days_short} Days",
        )
        if saved:
            print(f"Saved graph: {saved}")

    if long_details:
        saved = _generate_performance_graph(
            long_details,
            graph_long_path,
            title=f"Bot Performance — Last {report_days_long} Days",
        )
        if saved:
            print(f"Saved graph: {saved}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
