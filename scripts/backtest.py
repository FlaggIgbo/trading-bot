#!/usr/bin/env python3
"""
Backtest: run evaluation logic on historical data from SQLite storage.

Uses: market_snapshots (from compare_historical), predictions (real trades),
      resolved_outcomes (from settlement sync), yfinance for asset prices.

Modes:
  real      — Resolved predictions only. Win rate, total PnL, Sharpe, max drawdown.
  synthetic — Market snapshots + resolved_outcomes. Simulate "would have placed"
              at snapshot price; infer outcome from resolved_outcomes or yfinance.

Run: python3 scripts/backtest.py [--mode real|synthetic] [--days 90]
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta

from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config
from constants import REC_BUY_NO, REC_BUY_YES


def _compute_sharpe(returns: list[float], annualization: float = 252.0) -> float | None:
    """Annualized Sharpe (excess return / vol). Assumes daily returns."""
    if not returns or len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std <= 0:
        return None
    return (mean / std) * math.sqrt(annualization)


def _compute_max_drawdown(cumulative_pnl: list[float]) -> tuple[float, int, int]:
    """Returns (max_dd_cents, trough_idx, peak_idx)."""
    if not cumulative_pnl:
        return 0.0, 0, 0
    peak = cumulative_pnl[0]
    max_dd = 0.0
    trough_idx = 0
    peak_idx = 0
    for i, p in enumerate(cumulative_pnl):
        if p > peak:
            peak = p
            peak_idx = i
        dd = peak - p
        if dd > max_dd:
            max_dd = dd
            trough_idx = i
    return max_dd, trough_idx, peak_idx


def _run_real(cfg: dict, start_ts: float | None, end_ts: float | None) -> dict:
    """Backtest from real predictions (resolved trades)."""
    try:
        from storage import get_predictions_for_backtest
    except ImportError:
        print("storage module not available")
        return {"error": "storage not available"}

    preds = get_predictions_for_backtest(
        start_ts=start_ts,
        end_ts=end_ts,
        resolved_only=True,
        config=cfg,
    )
    if not preds:
        return {
            "mode": "real",
            "trades": 0,
            "win_count": 0,
            "lose_count": 0,
            "total_pnl_cents": 0,
            "win_rate": None,
            "sharpe": None,
            "max_drawdown_cents": None,
        }

    wins = sum(1 for p in preds if p.get("outcome") == "win")
    loses = sum(1 for p in preds if p.get("outcome") == "lose")
    total_pnl = sum(p.get("pnl_cents") or 0 for p in preds)
    if pd is not None:
        df = pd.DataFrame(preds)
        if not df.empty:
            df["pnl_cents"] = df["pnl_cents"].fillna(0.0).astype(float)
            cumulative = df["pnl_cents"].cumsum().tolist()
            returns = (
                df["pnl_cents"] / (df["pnl_cents"].cumsum().shift(1).abs() + 1e-9)
            ).replace([float("inf"), float("-inf")], 0.0).fillna(0.0).tolist()
        else:
            cumulative = []
            returns = []
        max_dd, _, _ = _compute_max_drawdown(cumulative)
        return {
            "mode": "real",
            "trades": len(preds),
            "win_count": wins,
            "lose_count": loses,
            "total_pnl_cents": total_pnl,
            "win_rate": wins / (wins + loses) if (wins + loses) > 0 else None,
            "sharpe": _compute_sharpe(returns) if returns else None,
            "max_drawdown_cents": max_dd,
        }
    cumulative: list[float] = []
    run = 0
    for p in preds:
        run += p.get("pnl_cents") or 0
        cumulative.append(float(run))

    returns: list[float] = []
    for i, p in enumerate(preds):
        ts = p.get("resolved_ts") or p.get("ts")
        if ts and i > 0:
            prev_ts = preds[i - 1].get("resolved_ts") or preds[i - 1].get("ts")
            if prev_ts and ts > prev_ts:
                days = (ts - prev_ts) / 86400
                if days > 0 and cumulative[i - 1] != 0:
                    ret = (cumulative[i] - cumulative[i - 1]) / (abs(cumulative[i - 1]) + 1e-9)
                    returns.append(ret)

    max_dd, _, _ = _compute_max_drawdown(cumulative)
    return {
        "mode": "real",
        "trades": len(preds),
        "win_count": wins,
        "lose_count": loses,
        "total_pnl_cents": total_pnl,
        "win_rate": wins / (wins + loses) if (wins + loses) > 0 else None,
        "sharpe": _compute_sharpe(returns) if returns else None,
        "max_drawdown_cents": max_dd,
    }


def _run_synthetic(
    cfg: dict,
    start_ts: float | None,
    end_ts: float | None,
) -> dict:
    """Backtest from market snapshots + resolved_outcomes.

    Uses a conservative ask proxy and slippage to better reflect executable fills.
    """
    try:
        from storage import (
            get_last_snapshot_per_ticker,
            get_market_snapshots_for_backtest,
            get_resolved_outcomes,
        )
    except ImportError:
        print("storage module not available")
        return {"error": "storage not available"}

    rec_filter = [REC_BUY_YES, REC_BUY_NO]
    bt_cfg = (cfg or {}).get("backtest", {})
    slippage_bps = float(bt_cfg.get("synthetic_slippage_bps", 100.0))  # 100 bps = 1%
    spread_soft = float(bt_cfg.get("synthetic_liquidity_spread_bps_soft", 1500.0))
    spread_hard = float(bt_cfg.get("synthetic_liquidity_spread_bps_hard", 4000.0))
    min_size_mult = float(bt_cfg.get("synthetic_liquidity_min_size_multiplier", 0.2))
    snaps = get_market_snapshots_for_backtest(
        start_ts=start_ts,
        end_ts=end_ts,
        rec_filter=rec_filter,
        config=cfg,
    )
    last_per_ticker = get_last_snapshot_per_ticker(
        rec_filter=rec_filter,
        config=cfg,
    )
    resolved = get_resolved_outcomes(config=cfg)
    tickers_in_resolved = set(resolved.keys())
    trades: list[dict] = []
    for ticker, snap in last_per_ticker.items():
        if ticker not in tickers_in_resolved:
            continue
        ro = resolved[ticker]
        yes_settled = bool(ro.get("yes_settled"))
        rec = snap.get("rec")
        side = "yes" if rec == REC_BUY_YES else "no"
        yes_bid = float(snap.get("yes_bid") or 0)
        no_bid = float(snap.get("no_bid") or 0)
        # Ask proxy from opposite bid for binary markets.
        yes_ask_proxy = (1.0 - no_bid) if (0 < no_bid < 1) else yes_bid
        no_ask_proxy = (1.0 - yes_bid) if (0 < yes_bid < 1) else no_bid
        side_bid = yes_bid if side == "yes" else no_bid
        theoretical_price = side_bid if side_bid > 0 else None
        ask_proxy = yes_ask_proxy if side == "yes" else no_ask_proxy
        if ask_proxy <= 0:
            # Fallback to side bid only if ask proxy is unavailable.
            ask_proxy = side_bid
        if ask_proxy <= 0:
            continue
        spread_bps = None
        if side_bid > 0 and ask_proxy >= side_bid:
            spread_bps = ((ask_proxy - side_bid) / max(0.01, side_bid)) * 10_000.0
        size_mult = 1.0
        if spread_bps is not None:
            if spread_bps >= spread_hard:
                size_mult = min_size_mult
            elif spread_bps > spread_soft:
                frac = (spread_bps - spread_soft) / max(1e-9, (spread_hard - spread_soft))
                size_mult = 1.0 - frac * (1.0 - min_size_mult)
        # Add conservative slippage to entry cost.
        actual_price = min(0.99, max(0.01, ask_proxy * (1.0 + slippage_bps / 10_000.0)))
        fill_slippage_bps = None
        if theoretical_price and theoretical_price > 0:
            fill_slippage_bps = ((actual_price - theoretical_price) / theoretical_price) * 10_000.0
        won = (side == "yes" and yes_settled) or (side == "no" and not yes_settled)
        pnl_cents = ((100 - actual_price * 100) if won else (-actual_price * 100)) * size_mult
        trades.append({
            "ticker": ticker,
            "side": side,
            "entry_price": actual_price,
            "theoretical_price": theoretical_price,
            "size_mult": size_mult,
            "spread_bps": spread_bps,
            "fill_slippage_bps": fill_slippage_bps,
            "won": won,
            "pnl_cents": pnl_cents,
        })

    wins = sum(1 for t in trades if t["won"])
    loses = len(trades) - wins
    total_pnl = sum(float(t["pnl_cents"]) for t in trades)
    cumulative = []
    run = 0
    for t in trades:
        run += float(t["pnl_cents"])
        cumulative.append(float(run))
    max_dd, _, _ = _compute_max_drawdown(cumulative)
    returns = []
    for i in range(1, len(trades)):
        ret = (cumulative[i] - cumulative[i - 1]) / (abs(cumulative[i - 1]) + 1e-9)
        returns.append(ret)

    slippages = [float(t["fill_slippage_bps"]) for t in trades if t.get("fill_slippage_bps") is not None]
    spreads = [float(t["spread_bps"]) for t in trades if t.get("spread_bps") is not None]
    return {
        "mode": "synthetic",
        "trades": len(trades),
        "win_count": wins,
        "lose_count": loses,
        "total_pnl_cents": int(round(total_pnl)),
        "win_rate": wins / len(trades) if trades else None,
        "sharpe": _compute_sharpe(returns) if returns else None,
        "max_drawdown_cents": max_dd,
        "avg_fill_slippage_bps": (sum(slippages) / len(slippages)) if slippages else None,
        "avg_spread_bps": (sum(spreads) / len(spreads)) if spreads else None,
    }


def main() -> int:
    cfg = get_config()
    parser = argparse.ArgumentParser(
        description="Backtest: real trades or synthetic from market snapshots"
    )
    parser.add_argument(
        "--mode",
        choices=["real", "synthetic"],
        default="real",
        help="real=resolved predictions; synthetic=snapshots+resolved_outcomes",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Limit to last N days (0=all)",
    )
    args = parser.parse_args()

    start_ts = None
    end_ts = None
    if args.days > 0:
        end_ts = datetime.utcnow().timestamp()
        start_ts = (datetime.utcnow() - timedelta(days=args.days)).timestamp()

    if args.mode == "real":
        out = _run_real(cfg, start_ts, end_ts)
    else:
        out = _run_synthetic(cfg, start_ts, end_ts)

    if "error" in out:
        print(out["error"])
        return 1

    print("\n" + "=" * 60)
    print("BACKTEST RESULT")
    print("=" * 60)
    print(f"  Mode:     {out['mode']}")
    print(f"  Trades:   {out['trades']}")
    print(f"  Wins:     {out['win_count']}")
    print(f"  Losses:   {out['lose_count']}")
    if out["win_rate"] is not None:
        print(f"  Win rate: {out['win_rate']:.1%}")
    print(f"  PnL:      ${out['total_pnl_cents'] / 100:,.2f}")
    if out.get("sharpe") is not None:
        print(f"  Sharpe:   {out['sharpe']:.2f}")
    if out.get("max_drawdown_cents") is not None:
        print(f"  Max DD:   ${out['max_drawdown_cents'] / 100:,.2f}")
    if out.get("avg_fill_slippage_bps") is not None:
        print(f"  Avg fill slippage: {out['avg_fill_slippage_bps']:.1f} bps")
    if out.get("avg_spread_bps") is not None:
        print(f"  Avg spread proxy:  {out['avg_spread_bps']:.1f} bps")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
