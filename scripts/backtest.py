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
import json
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


def _compute_accuracy_summary(distances: list[float]) -> dict | None:
    """Summarize bet-vs-resolution distances (% from threshold, signed by direction)."""
    if not distances:
        return None
    n = len(distances)
    abs_dists = [abs(d) for d in distances]
    sorted_abs = sorted(abs_dists)
    median_abs = sorted_abs[n // 2] if n % 2 == 1 else (sorted_abs[n // 2 - 1] + sorted_abs[n // 2]) / 2.0
    mean_abs = sum(abs_dists) / n
    mean_signed = sum(distances) / n
    correct = sum(1 for d in distances if d >= 0)
    return {
        "trades_with_resolution_price": n,
        "mean_abs_distance_pct": round(mean_abs, 2),
        "median_abs_distance_pct": round(median_abs, 2),
        "mean_signed_distance_pct": round(mean_signed, 2),
        "pct_correct_side": round(correct / n * 100.0, 1) if n > 0 else None,
    }


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
    sold = sum(1 for p in preds if p.get("outcome") == "sold")
    total_pnl = sum(p.get("realized_pnl_cents", p.get("pnl_cents") or 0) or 0 for p in preds)

    trade_details = []
    accuracy_distances: list[float] = []
    for p in preds:
        pnl_c = p.get("realized_pnl_cents", p.get("pnl_cents") or 0) or 0
        detail: dict = {
            "ts": p.get("resolved_ts") or p.get("ts") or 0,
            "pnl_cents": float(pnl_c),
            "outcome": p.get("outcome", "unknown"),
            "ticker": p.get("ticker", ""),
            "side": (p.get("side") or "").lower().strip(),
            "direction": (p.get("direction") or "").lower().strip(),
            "entry_price": float(p.get("entry_price") or 0),
            "exit_price": float(p.get("exit_price") or 0),
        }
        threshold = p.get("threshold")
        direction = (p.get("direction") or "").lower().strip()
        res_price = p.get("resolution_price")
        if threshold and res_price and float(threshold) > 0:
            dist_pct = ((float(res_price) - float(threshold)) / float(threshold)) * 100.0
            if direction == "below":
                dist_pct = -dist_pct
            detail["distance_pct"] = round(dist_pct, 4)
            detail["resolution_price"] = float(res_price)
            detail["threshold"] = float(threshold)
            accuracy_distances.append(dist_pct)
        trade_details.append(detail)

    accuracy_summary = _compute_accuracy_summary(accuracy_distances)

    if pd is not None:
        df = pd.DataFrame(preds)
        if not df.empty:
            if "realized_pnl_cents" in df.columns:
                df["pnl_cents"] = df["realized_pnl_cents"].fillna(df["pnl_cents"]).fillna(0.0).astype(float)
            else:
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
            "sold_count": sold,
            "total_pnl_cents": total_pnl,
            "win_rate": wins / (wins + loses) if (wins + loses) > 0 else None,
            "sharpe": _compute_sharpe(returns) if returns else None,
            "max_drawdown_cents": max_dd,
            "trade_details": trade_details,
            "accuracy": accuracy_summary,
        }
    cumulative: list[float] = []
    run = 0
    for p in preds:
        run += p.get("realized_pnl_cents", p.get("pnl_cents") or 0) or 0
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
        "sold_count": sold,
        "total_pnl_cents": total_pnl,
        "win_rate": wins / (wins + loses) if (wins + loses) > 0 else None,
        "sharpe": _compute_sharpe(returns) if returns else None,
        "max_drawdown_cents": max_dd,
        "trade_details": trade_details,
        "accuracy": accuracy_summary,
    }


def _synthetic_rec_from_strategy(snap: dict, strategy_cfg: dict) -> str | None:
    """Recompute BUY YES/NO/SKIP from snapshot hist_p and book using threshold strategy math."""
    try:
        from strategies import DefaultThresholdStrategy

        min_edge = float(strategy_cfg.get("min_edge", 0.10))
        se = strategy_cfg.get("simple_ev_decision") or {}
        if isinstance(se, dict) and bool(se.get("enabled")):
            min_edge = float(se.get("min_edge", min_edge))
        min_fee = float(strategy_cfg.get("min_fee_per_order", 0.01))
        maker_fee = float(strategy_cfg.get("kalshi_maker_fee_rate", 0.0175))
        hist_p = float(snap.get("hist_p") or 0.5)
        yes_bid = float(snap.get("yes_bid") or 0.5)
        return DefaultThresholdStrategy().recommend(
            p_yes=hist_p,
            market_yes=yes_bid,
            min_edge=min_edge,
            maker_fee_rate=maker_fee,
            min_fee_per_order=min_fee,
        )
    except Exception:
        return None


def _run_synthetic(
    cfg: dict | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
    re_evaluate: bool = False,
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
    strategy_cfg = (cfg or {}).get("strategy", {})
    slippage_bps = float(bt_cfg.get("synthetic_slippage_bps", 100.0))  # 100 bps = 1%
    spread_soft = float(bt_cfg.get("synthetic_liquidity_spread_bps_soft", 1500.0))
    spread_hard = float(bt_cfg.get("synthetic_liquidity_spread_bps_hard", 4000.0))
    min_size_mult = float(bt_cfg.get("synthetic_liquidity_min_size_multiplier", 0.2))
    # Stress realism: tax winning trades by hard spread/slippage friction.
    win_slippage_tax_bps = float(
        bt_cfg.get(
            "synthetic_win_slippage_tax_bps",
            strategy_cfg.get("liquidity_spread_bps_hard", spread_hard),
        )
    )
    min_dwell_minutes = float(bt_cfg.get("synthetic_fill_min_dwell_minutes", 10.0))
    liquidity_friction_cents = float(bt_cfg.get("synthetic_liquidity_friction_cents", 0.75))
    assumed_order_size = int(bt_cfg.get("synthetic_assumed_order_size", 10))
    # Time-aware synthetic fill modeling: use full snapshot history per ticker.
    all_snaps = get_market_snapshots_for_backtest(
        start_ts=start_ts,
        end_ts=end_ts,
        rec_filter=rec_filter,
        config=cfg,
    ) or []
    last_per_ticker = get_last_snapshot_per_ticker(
        rec_filter=rec_filter,
        config=cfg,
    )
    resolved = get_resolved_outcomes(config=cfg)
    trades: list[dict] = []
    skipped_not_filled = 0

    by_ticker: dict[str, list[dict]] = {}
    for row in all_snaps:
        tk = str(row.get("ticker") or "")
        if not tk:
            continue
        by_ticker.setdefault(tk, []).append(row)
    for tk in by_ticker:
        by_ticker[tk].sort(key=lambda r: float(r.get("ts") or 0.0))

    def _side_price(s: dict, side: str) -> float:
        yes_bid = float(s.get("yes_bid") or 0.0)
        no_bid = float(s.get("no_bid") or 0.0) or (1.0 - yes_bid)
        return yes_bid if side == "yes" else no_bid

    def _fills_with_dwell(ticker: str, side: str, limit_price: float, placed_ts: float, resolution_ts: float | None) -> bool:
        needed = max(0.0, min_dwell_minutes) * 60.0
        if needed <= 0:
            return True
        snaps = by_ticker.get(ticker) or []
        if not snaps:
            return False
        horizon = float(resolution_ts) if resolution_ts else float("inf")
        window = [s for s in snaps if float(s.get("ts") or 0.0) >= placed_ts and float(s.get("ts") or 0.0) <= horizon]
        if not window:
            return False
        dwell = 0.0
        default_step = 15.0 * 60.0
        for i, snap in enumerate(window):
            ts_i = float(snap.get("ts") or 0.0)
            ts_next = float(window[i + 1].get("ts") or ts_i) if i + 1 < len(window) else (ts_i + default_step)
            dt = max(0.0, ts_next - ts_i)
            if _side_price(snap, side) <= limit_price:
                dwell += dt
                if dwell >= needed:
                    return True
            else:
                dwell = 0.0
        return False

    def _depth_available_contracts(snap: dict, side: str) -> float | None:
        keys_yes = ["yes_ask_size", "yes_depth", "yes_liquidity", "yes_available"]
        keys_no = ["no_ask_size", "no_depth", "no_liquidity", "no_available"]
        keys = keys_yes if side == "yes" else keys_no
        for k in keys:
            v = snap.get(k)
            if v is None:
                continue
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
        return None

    use_vectorized = False
    if use_vectorized and pd is not None:
        last_df = pd.DataFrame(
            [{"ticker": t, **snap} for t, snap in last_per_ticker.items()]
        )
        res_df = pd.DataFrame(
            [{"ticker": t, **row} for t, row in (resolved or {}).items()]
        )
        if not last_df.empty and not res_df.empty:
            df = last_df.merge(res_df[["ticker", "yes_settled"]], on="ticker", how="inner")
            df = df[df["rec"].isin(rec_filter)].copy()
            if not df.empty:
                df["side"] = df["rec"].map(lambda r: "yes" if r == REC_BUY_YES else "no")
                df["yes_bid"] = df["yes_bid"].fillna(0.0).astype(float)
                df["no_bid"] = df["no_bid"].fillna(0.0).astype(float)

                df["yes_ask_proxy"] = df.apply(
                    lambda r: (1.0 - r["no_bid"]) if (0 < r["no_bid"] < 1) else r["yes_bid"],
                    axis=1,
                )
                df["no_ask_proxy"] = df.apply(
                    lambda r: (1.0 - r["yes_bid"]) if (0 < r["yes_bid"] < 1) else r["no_bid"],
                    axis=1,
                )
                df["side_bid"] = df.apply(
                    lambda r: r["yes_bid"] if r["side"] == "yes" else r["no_bid"], axis=1
                )
                df["ask_proxy"] = df.apply(
                    lambda r: r["yes_ask_proxy"] if r["side"] == "yes" else r["no_ask_proxy"], axis=1
                )
                df["ask_proxy"] = df.apply(
                    lambda r: r["side_bid"] if r["ask_proxy"] <= 0 else r["ask_proxy"], axis=1
                )
                df = df[df["ask_proxy"] > 0].copy()

                df["theoretical_price"] = df["side_bid"].where(df["side_bid"] > 0)
                df["spread_bps"] = (
                    (df["ask_proxy"] - df["side_bid"]) / df["side_bid"].clip(lower=0.01) * 10_000.0
                )
                df.loc[(df["side_bid"] <= 0) | (df["ask_proxy"] < df["side_bid"]), "spread_bps"] = None

                def _size_mult(spread_val: float | None) -> float:
                    if spread_val is None or (isinstance(spread_val, float) and math.isnan(spread_val)):
                        return 1.0
                    if spread_val >= spread_hard:
                        return min_size_mult
                    if spread_val > spread_soft:
                        frac = (spread_val - spread_soft) / max(1e-9, (spread_hard - spread_soft))
                        return 1.0 - frac * (1.0 - min_size_mult)
                    return 1.0

                df["size_mult"] = df["spread_bps"].map(_size_mult).astype(float)
                df["entry_price"] = (
                    df["ask_proxy"] * (1.0 + slippage_bps / 10_000.0)
                ).clip(lower=0.01, upper=0.99)
                df["fill_slippage_bps"] = (
                    (df["entry_price"] - df["theoretical_price"]) / df["theoretical_price"] * 10_000.0
                )
                df.loc[df["theoretical_price"].isna() | (df["theoretical_price"] <= 0), "fill_slippage_bps"] = None

                df["yes_settled"] = df["yes_settled"].astype(bool)
                df["won"] = (
                    ((df["side"] == "yes") & df["yes_settled"])
                    | ((df["side"] == "no") & (~df["yes_settled"]))
                )
                def _pnl_row(r):
                    gross_win = (100 - r["entry_price"] * 100)
                    if r["won"]:
                        gross_win *= max(0.0, 1.0 - win_slippage_tax_bps / 10_000.0)
                        return gross_win * r["size_mult"]
                    return (-r["entry_price"] * 100) * r["size_mult"]

                df["pnl_cents"] = df.apply(_pnl_row, axis=1)
                trades = df[
                    [
                        "ticker",
                        "side",
                        "entry_price",
                        "theoretical_price",
                        "size_mult",
                        "spread_bps",
                        "fill_slippage_bps",
                        "won",
                        "pnl_cents",
                    ]
                ].to_dict(orient="records")

    if not trades:
        tickers_in_resolved = set(resolved.keys())
        for ticker, snap in last_per_ticker.items():
            if ticker not in tickers_in_resolved:
                continue
            ro = resolved[ticker]
            yes_settled = bool(ro.get("yes_settled"))
            
            if re_evaluate:
                rec = _synthetic_rec_from_strategy(snap, strategy_cfg)
                if rec is None:
                    rec = snap.get("rec")
            else:
                rec = snap.get("rec")

            if rec not in rec_filter:
                continue

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
            # Volume-weighted/partial-fill realism when depth is available.
            requested_contracts = int(snap.get("count") or assumed_order_size or 10)
            avail_depth = _depth_available_contracts(snap, side)
            if avail_depth is not None and requested_contracts > 0:
                fill_ratio = max(0.0, min(1.0, float(avail_depth) / float(requested_contracts)))
                size_mult *= fill_ratio
            if size_mult <= 0.0:
                skipped_not_filled += 1
                continue
            # Add conservative slippage to entry cost.
            actual_price = min(0.99, max(0.01, ask_proxy * (1.0 + slippage_bps / 10_000.0)))
            fill_slippage_bps = None
            if theoretical_price and theoretical_price > 0:
                fill_slippage_bps = ((actual_price - theoretical_price) / theoretical_price) * 10_000.0
            won = (side == "yes" and yes_settled) or (side == "no" and not yes_settled)
            placed_ts = float(snap.get("ts") or 0.0)
            resolution_ts = ro.get("resolution_ts")
            if not _fills_with_dwell(ticker, side, actual_price, placed_ts, resolution_ts):
                skipped_not_filled += 1
                continue
            gross_win = (100 - actual_price * 100)
            if won:
                gross_win *= max(0.0, 1.0 - win_slippage_tax_bps / 10_000.0)
                pnl_cents = (gross_win * size_mult) - (liquidity_friction_cents * size_mult)
            else:
                pnl_cents = ((-actual_price * 100) * size_mult) - (liquidity_friction_cents * size_mult)
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

    slippages = [
        float(t["fill_slippage_bps"])
        for t in trades
        if t.get("fill_slippage_bps") is not None and not math.isnan(float(t["fill_slippage_bps"]))
    ]
    spreads = [
        float(t["spread_bps"])
        for t in trades
        if t.get("spread_bps") is not None and not math.isnan(float(t["spread_bps"]))
    ]
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
        "skipped_not_filled": skipped_not_filled,
        "fill_min_dwell_minutes": min_dwell_minutes,
        "liquidity_friction_cents": liquidity_friction_cents,
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
    parser.add_argument(
        "--re-evaluate",
        action="store_true",
        help="Re-run strategy logic against snapshots (synthetic only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a single JSON object (machine-readable) to stdout; suppress human summary",
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
        out = _run_synthetic(cfg, start_ts, end_ts, re_evaluate=args.re_evaluate)

    if "error" in out:
        print(out["error"])
        return 1

    if args.json:
        export = {k: v for k, v in out.items() if k != "trade_details"}
        print(json.dumps(export, default=str))
        return 0

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
    if out.get("skipped_not_filled") is not None:
        print(f"  Skipped (not filled): {out['skipped_not_filled']}")
    if out.get("fill_min_dwell_minutes") is not None:
        print(f"  Fill dwell rule: >= {out['fill_min_dwell_minutes']:.1f} min")
    if out.get("liquidity_friction_cents") is not None:
        print(f"  Liquidity friction: {out['liquidity_friction_cents']:.2f} cents/trade")
    acc = out.get("accuracy")
    if acc and acc.get("trades_with_resolution_price", 0) > 0:
        print("-" * 60)
        print("  BET ACCURACY (distance: bet threshold vs resolution price)")
        print(f"  Trades w/ resolution price: {acc['trades_with_resolution_price']}")
        print(f"  Mean |distance|:   {acc['mean_abs_distance_pct']:.2f}%")
        print(f"  Median |distance|: {acc['median_abs_distance_pct']:.2f}%")
        print(f"  Mean signed dist:  {acc['mean_signed_distance_pct']:+.2f}%")
        if acc.get("pct_correct_side") is not None:
            print(f"  Correct side:      {acc['pct_correct_side']:.1f}%")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
