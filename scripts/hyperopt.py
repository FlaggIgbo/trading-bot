#!/usr/bin/env python3
"""
Hyperopt: tune strategy/backtest params via backtest (Freqtrade-inspired).

Searches over param combinations and reports best by Sharpe or total PnL.
Strategy params (min_edge, limit_dip_pct) affect which trades are placed;
snapshots store decisions made at eval time, so strategy tuning would require
re-evaluating historical snapshots. This scaffold currently tunes backtest
realism params (slippage, dwell, friction) that affect fill simulation.

Usage:
  python scripts/hyperopt.py [--mode synthetic|real] [--days 90] [--trials 12]
"""
from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config


def _run_backtest(cfg: dict, mode: str, start_ts: float | None, end_ts: float | None) -> dict:
    """Run backtest; return result dict."""
    # Import from sibling module (scripts/ has no __init__.py)
    import importlib.util
    spec = importlib.util.spec_from_file_location("backtest", _ROOT / "scripts" / "backtest.py")
    backtest_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backtest_mod)
    if mode == "real":
        return backtest_mod._run_real(cfg, start_ts, end_ts)
    return backtest_mod._run_synthetic(cfg, start_ts, end_ts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hyperopt: tune params via backtest"
    )
    parser.add_argument("--mode", choices=["real", "synthetic"], default="synthetic")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--trials", type=int, default=12, help="Number of param combos to try")
    parser.add_argument("--objective", choices=["sharpe", "pnl", "win_rate"], default="sharpe")
    args = parser.parse_args()

    cfg = get_config()
    end_ts = datetime.utcnow().timestamp()
    start_ts = (datetime.utcnow() - timedelta(days=args.days)).timestamp() if args.days > 0 else None

    # Param grid: backtest realism params (affect fill simulation)
    backtest = cfg.get("backtest", {})
    slippage_vals = [50, 100, 150]
    dwell_vals = [5, 10, 15]
    friction_vals = [0.5, 0.75, 1.0]

    results: list[tuple[dict, dict]] = []
    trial = 0
    for slip in slippage_vals:
        for dwell in dwell_vals:
            for friction in friction_vals:
                if trial >= args.trials:
                    break
                trial += 1
                cfg_test = copy.deepcopy(cfg)
                bt = cfg_test.setdefault("backtest", {})
                bt["synthetic_slippage_bps"] = slip
                bt["synthetic_fill_min_dwell_minutes"] = dwell
                bt["synthetic_liquidity_friction_cents"] = friction
                try:
                    out = _run_backtest(cfg_test, args.mode, start_ts, end_ts)
                except Exception as e:
                    print(f"  Trial {trial}: FAILED ({e})")
                    continue
                if "error" in out:
                    print(f"  Trial {trial}: {out['error']}")
                    continue
                params = {"slippage_bps": slip, "dwell_min": dwell, "friction_cents": friction}
                results.append((params, out))

    if not results:
        print("No successful trials.")
        return 1

    # Rank by objective
    def score(item: tuple[dict, dict]) -> float:
        params, out = item
        if args.objective == "sharpe":
            s = out.get("sharpe")
            return float(s) if s is not None else -999.0
        if args.objective == "pnl":
            return float(out.get("total_pnl_cents", out.get("total_pnl", 0)) or 0)
        if args.objective == "win_rate":
            wr = out.get("win_rate")
            return float(wr) if wr is not None else 0.0
        return 0.0

    results.sort(key=score, reverse=True)
    best_params, best_out = results[0]

    print()
    print("=" * 60)
    print("  HYPEROPT RESULT")
    print("=" * 60)
    print(f"  Mode:       {args.mode}")
    print(f"  Objective:  {args.objective}")
    print(f"  Trials:     {len(results)}")
    print()
    print("  Best params:")
    for k, v in best_params.items():
        print(f"    {k}: {v}")
    print()
    print(f"  Best backtest:")
    print(f"    Trades:   {best_out.get('trades', 0)}")
    print(f"    Win rate: {best_out.get('win_rate'):.1%}" if best_out.get("win_rate") else "")
    print(f"    PnL:     ${(best_out.get('total_pnl_cents') or best_out.get('total_pnl', 0)) / 100:,.2f}")
    if best_out.get("sharpe") is not None:
        print(f"    Sharpe:  {best_out['sharpe']:.2f}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
