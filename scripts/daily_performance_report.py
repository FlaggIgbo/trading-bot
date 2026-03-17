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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
