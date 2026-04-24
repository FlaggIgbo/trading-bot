#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

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


def _format_metric(name: str, value) -> str:
    if value is None:
        return f"{name}=n/a"
    if isinstance(value, float):
        if "rate" in name:
            return f"{name}={value:.2%}"
        return f"{name}={value:.3f}"
    return f"{name}={value}"


def main() -> int:
    cfg = get_config()
    bt_cfg = (cfg or {}).get("backtest", {})

    enabled = bool(bt_cfg.get("pre_push_enabled", True))
    if not enabled:
        print("[Backtest Guard] Skipped: pre_push_enabled=false")
        return 0

    mode = str(bt_cfg.get("pre_push_mode", "real")).strip().lower()
    days = int(bt_cfg.get("pre_push_days", 30))
    min_trades = int(bt_cfg.get("pre_push_min_trades", 20))
    min_win_rate = float(bt_cfg.get("pre_push_min_win_rate", 0.50))
    min_pnl_cents = int(bt_cfg.get("pre_push_min_pnl_cents", 0))
    require_positive_sharpe = bool(bt_cfg.get("pre_push_require_positive_sharpe", False))
    fail_on_insufficient = bool(bt_cfg.get("pre_push_fail_on_insufficient_trades", False))

    import time
    end_ts = time.time()
    start_ts = (end_ts - (max(0, days) * 86400)) if days > 0 else None

    bt = _load_backtest_module()
    if mode == "synthetic":
        out = bt._run_synthetic(cfg, start_ts, end_ts)
    else:
        out = bt._run_real(cfg, start_ts, end_ts)

    if "error" in out:
        print(f"[Backtest Guard] Error: {out['error']}")
        return 1

    trades = int(out.get("trades") or 0)
    win_rate = out.get("win_rate")
    pnl_cents = int(out.get("total_pnl_cents") or 0)
    sharpe = out.get("sharpe")

    print(
        "[Backtest Guard] "
        + ", ".join(
            [
                f"mode={mode}",
                f"days={days}",
                _format_metric("trades", trades),
                _format_metric("win_rate", win_rate),
                _format_metric("total_pnl_cents", pnl_cents),
                _format_metric("sharpe", sharpe),
            ]
        )
    )

    failures: list[str] = []
    if trades < min_trades:
        msg = f"trades {trades} < min_trades {min_trades}"
        if fail_on_insufficient:
            failures.append(msg)
        else:
            print(f"[Backtest Guard] Warning: {msg} (allowed)")
            return 0
    if win_rate is None or win_rate < min_win_rate:
        failures.append(f"win_rate {win_rate} < min_win_rate {min_win_rate}")
    if pnl_cents < min_pnl_cents:
        failures.append(f"pnl_cents {pnl_cents} < min_pnl_cents {min_pnl_cents}")
    if require_positive_sharpe and (sharpe is None or sharpe <= 0):
        failures.append(f"sharpe {sharpe} must be > 0")

    if failures:
        print("[Backtest Guard] FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("[Backtest Guard] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
