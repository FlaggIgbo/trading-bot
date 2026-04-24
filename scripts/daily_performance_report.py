#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
from datetime import datetime, timedelta
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config

try:
    from storage import get_bet_accuracy_stats
except ImportError:
    get_bet_accuracy_stats = None


def _load_backtest_module():
    bt_path = _ROOT / "scripts" / "backtest.py"
    spec = importlib.util.spec_from_file_location("tb_backtest", bt_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backtest module: {bt_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_range(bt_mod, cfg: dict, mode: str, days: int) -> dict:
    import time
    end_ts = time.time()
    start_ts = (end_ts - (days * 86400)) if days > 0 else None
    if mode == "synthetic":
        return bt_mod._run_synthetic(cfg, start_ts, end_ts)
    return bt_mod._run_real(cfg, start_ts, end_ts)


def _fmt_pct(x) -> str:
    return "n/a" if x is None else f"{float(x):.1%}"


def _fmt_num(x) -> str:
    return "n/a" if x is None else f"{float(x):.2f}"


def _fmt_pnl_cents(x) -> str:
    return "n/a" if x is None else f"${float(x)/100:,.2f}"


def _maker_fee_per_contract(price: float, maker_rate: float, min_fee: float = 0.01) -> float:
    """Per-contract maker fee in price units (same convention as compare_historical)."""
    p = float(price)
    if maker_rate <= 0 or p <= 0 or p >= 1:
        return 0.0
    fee = maker_rate * p * (1.0 - p)
    if fee <= 0:
        return 0.0
    rounded = math.ceil(fee * 100) / 100
    return max(rounded, max(0.0, float(min_fee)))


def _breakeven_win_rate(entry: float, maker_rate: float) -> float:
    """Approx. breakeven win rate for a directional binary at entry (ignoring exit fees)."""
    fee = _maker_fee_per_contract(entry, maker_rate)
    return max(0.0, min(0.999, float(entry) + fee))


def _append_risk_reward_section(
    lines: list[str],
    trade_details: list[dict],
    label: str,
    maker_rate: float,
) -> None:
    """Entry-price buckets, direction×side, and breakeven context (PnL-oriented reporting)."""
    if not trade_details:
        return
    decided = [t for t in trade_details if t.get("outcome") in ("win", "lose")]
    if not decided:
        return
    total_pnl = sum(float(t.get("pnl_cents") or 0) for t in decided)
    be_sum = 0.0
    for t in decided:
        ep = float(t.get("entry_price") or 0)
        if ep > 0:
            be_sum += _breakeven_win_rate(ep, maker_rate)
    avg_entry = sum(float(t.get("entry_price") or 0) for t in decided) / max(1, len(decided))
    avg_be = be_sum / max(1, len([t for t in decided if float(t.get("entry_price") or 0) > 0]))

    lines.append("")
    lines.append(f"Risk / reward context — {label}")
    lines.append("-" * 40)
    lines.append(f"Resolved trades (win/lose): {len(decided)} | Net PnL: {_fmt_pnl_cents(total_pnl)}")
    lines.append(f"Avg entry price: {avg_entry:.1%} | Approx breakeven WR @ avg entry: {avg_be:.1%}")

    bands = [
        (0.0, 0.15, "0–15¢"),
        (0.15, 0.30, "15–30¢"),
        (0.30, 0.45, "30–45¢"),
        (0.45, 0.60, "45–60¢"),
        (0.60, 0.75, "60–75¢"),
        (0.75, 1.01, "75–100¢"),
    ]
    lines.append("By entry-price band (win/lose only):")
    for lo, hi, name in bands:
        sub = [
            t
            for t in decided
            if lo <= float(t.get("entry_price") or 0) < hi
        ]
        if not sub:
            continue
        w = sum(1 for t in sub if t.get("outcome") == "win")
        el = sum(1 for t in sub if t.get("outcome") == "lose")
        pnl_b = sum(float(t.get("pnl_cents") or 0) for t in sub)
        wr = w / (w + el) if (w + el) else 0.0
        lines.append(f"  {name}: n={len(sub)} | WR {wr:.1%} | PnL {_fmt_pnl_cents(pnl_b)}")

    by_ds: dict[str, list[dict]] = {}
    for t in decided:
        d = (t.get("direction") or "?").strip() or "?"
        s = (t.get("side") or "?").strip() or "?"
        key = f"{d}+{s}"
        by_ds.setdefault(key, []).append(t)
    if len(by_ds) > 1 or (len(by_ds) == 1 and next(iter(by_ds)) != "?+?"):
        lines.append("By direction + side (win/lose):")
        for key in sorted(by_ds.keys()):
            sub = by_ds[key]
            w = sum(1 for t in sub if t.get("outcome") == "win")
            el = sum(1 for t in sub if t.get("outcome") == "lose")
            pnl_b = sum(float(t.get("pnl_cents") or 0) for t in sub)
            wr = w / (w + el) if (w + el) else 0.0
            lines.append(f"  {key}: n={len(sub)} | WR {wr:.1%} | PnL {_fmt_pnl_cents(pnl_b)}")


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
    strategy = (cfg or {}).get("strategy") or {}
    maker_rate = float(strategy.get("kalshi_maker_fee_rate", 0.0175))
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

    rs, rl = real_short.get("sharpe"), real_long.get("sharpe")
    ss = syn_long.get("sharpe")
    if rs is not None and ss is not None and rs > 0 and ss < 0:
        lines.append("")
        lines.append("Real vs synthetic (same window note)")
        lines.append("-" * 40)
        lines.append(
            "Real backtest Sharpe is positive while synthetic is negative — review fill/skip "
            "assumptions in synthetic mode or non-stationarity in the live sample."
        )

    # --- Bet Accuracy (distance between bet and resolution price) ---
    def _append_accuracy_section(lines: list[str], result: dict, label: str) -> None:
        acc = result.get("accuracy")
        if not acc:
            return
        n = acc.get("trades_with_resolution_price", 0)
        if n == 0:
            return
        lines.append("")
        lines.append(f"Bet Accuracy — {label}")
        lines.append("-" * 40)
        lines.append(f"Trades with resolution price: {n}")
        lines.append(f"Mean |distance|: {acc.get('mean_abs_distance_pct', 'n/a'):.2f}%")
        lines.append(f"Median |distance|: {acc.get('median_abs_distance_pct', 'n/a'):.2f}%")
        lines.append(f"Mean signed distance: {acc.get('mean_signed_distance_pct', 'n/a'):+.2f}%")
        lines.append(f"Correct side: {_fmt_pct(acc.get('pct_correct_side', 0) / 100.0 if acc.get('pct_correct_side') is not None else None)}")

    _append_accuracy_section(lines, real_short, f"Real {report_days_short}d")
    _append_accuracy_section(lines, real_long, f"Real {report_days_long}d")

    _append_risk_reward_section(
        lines, real_short.get("trade_details") or [], f"Real {report_days_short}d", maker_rate
    )
    _append_risk_reward_section(
        lines, real_long.get("trade_details") or [], f"Real {report_days_long}d", maker_rate
    )

    if get_bet_accuracy_stats:
        import time
        end_ts = time.time()
        start_short = end_ts - (report_days_short * 86400)
        start_long = end_ts - (report_days_long * 86400)
        acc_short = get_bet_accuracy_stats(cfg, start_ts=start_short, end_ts=end_ts)
        acc_long = get_bet_accuracy_stats(cfg, start_ts=start_long, end_ts=end_ts)
        for acc_data, label in [
            (acc_short, f"Storage {report_days_short}d"),
            (acc_long, f"Storage {report_days_long}d"),
        ]:
            summary = acc_data.get("summary") if acc_data else None
            if summary and acc_data.get("count", 0) > 0:
                lines.append("")
                lines.append(f"Bet Accuracy — {label}")
                lines.append("-" * 40)
                lines.append(f"Resolved trades: {summary.get('count', 0)}")
                lines.append(f"Mean |distance|: {_fmt_num(summary.get('mean_abs_distance_pct'))}%")
                lines.append(f"Median |distance|: {_fmt_num(summary.get('median_abs_distance_pct'))}%")
                lines.append(f"Mean signed distance: {_fmt_num(summary.get('mean_signed_distance_pct'))}%")
                lines.append(f"Std dev: {_fmt_num(summary.get('stddev_distance_pct'))}%")
                lines.append(f"Correct side: {_fmt_pct(summary.get('pct_correct_side', 0) / 100.0 if summary.get('pct_correct_side') is not None else None)}")
                lines.append(f"Avg |dist| when correct: {_fmt_num(summary.get('avg_distance_when_correct_pct'))}%")
                lines.append(f"Avg |dist| when wrong: {_fmt_num(summary.get('avg_distance_when_wrong_pct'))}%")

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
