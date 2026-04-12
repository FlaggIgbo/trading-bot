"""
Risk Management and Execution Logic: Sizing, Guardrails, and Exposure.

Provides safety blocks for OTM tail trades ('fat pitches') and applies
fractional Kelly sizing with confidence-aware multipliers.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from constants import REC_BUY_YES, REC_BUY_NO
from models.strategy_config import StrategyParams
from models.trading_edges import maker_fee_per_contract as _fee_per_contract
from utils.market_analysis import clip_price, wide_book_lift_passive_raw_price, resolve_passive_quote_price


def calculate_fractional_kelly_size(
    p_win: float,
    price: float,
    *,
    total_bankroll_cents: float,
    fraction: float = 0.25,
    max_trade_cents: float = 5000,
    trust_score: float = 1.0,
    drawdown_pct: float = 0.0,
    drawdown_aggressive_delever_threshold: float = 0.15,
    regime_size_mult: float = 1.0,
) -> int:
    """
    Enhanced Kelly with drawdown-aware deleveraging and regime-based scaling.
    If drawdown exceeds threshold, we delever exponentially.
    """
    if price <= 0 or price >= 1.0:
        return 0
    
    p = max(0.0, min(0.99, float(p_win)))
    q = 1.0 - p
    b = (1.0 - float(price)) / float(price) # Odds
    
    # Kelly % = (bp - q) / b
    k_raw = (b * p - q) / b if b > 0 else 0
    
    if k_raw <= 0:
        return 0
    
    # Drawdown Scaling (Deleveraging)
    # 1. Linear scaling for small drawdowns
    # 2. Exponential deleveraging for significant ones
    delever_mult = 1.0
    if drawdown_pct > 0.02:
        if drawdown_pct < 0.10:
            # Linear fade: 100% at 2% DD -> 80% at 10% DD
            delever_mult = 1.0 - (drawdown_pct - 0.02) * 2.5
        else:
            # Quadratic fade: 80% at 10% DD -> 0% at ~40% DD
            delever_mult = 0.8 * (1.0 - (drawdown_pct - 0.10) / 0.30)**2
            
        if drawdown_pct >= drawdown_aggressive_delever_threshold:
            delever_mult *= 0.4  # Hard emergency haircut
            
    delever_mult = max(0.0, delever_mult)
            
    # Apply fractional Kelly, trust, regime, and deleveraging
    k_adj = k_raw * float(fraction) * float(trust_score) * float(regime_size_mult) * delever_mult
    
    # Cap single-trade bankroll risk to 10% (more conservative than 12%)
    k_adj = min(0.10, k_adj)
    
    size_cents = int(total_bankroll_cents * k_adj)
    return min(int(max_trade_cents), size_cents)


def get_ensemble_consensus_multiplier(
    p_model: float,
    p_vol: float | None,
    obi: float | None,
    momentum: float | None,
    sentiment: float | None = None,
) -> float:
    """
    Calculates a 'Consensus Multiplier' (0.5x to 1.5x) based on agreement strength.
    Covers technical (momentum, vol), flow (obi), and sentiment models.
    """
    checks = []
    
    # 1. Model vs Vol agreement (Technical/Model)
    if p_vol is not None:
        checks.append(abs(p_model - p_vol) < 0.10)
            
    # 2. Model vs OBI agreement (Flow)
    if obi is not None:
        model_bullish = p_model > 0.50
        obi_bullish = obi > 0.10
        # Check alignment: both bullish or both bearish
        checks.append(model_bullish == (obi > 0.05 if model_bullish else obi < -0.05))
            
    # 3. Model vs Momentum agreement (Technical)
    if momentum is not None:
        model_bullish = p_model > 0.50
        checks.append(model_bullish == (momentum > 0.0))
        
    # 4. Model vs Sentiment agreement (Sentiment)
    if sentiment is not None:
        model_bullish = p_model > 0.50
        sentiment_bullish = sentiment > 0.0
        checks.append(model_bullish == sentiment_bullish)
            
    if not checks:
        return 1.0
        
    agreement_rate = sum(1 for c in checks if c) / len(checks)
    
    # Sizing multiplier based on consensus
    if agreement_rate >= 0.9: return 1.50   # Full consensus -> big trades
    if agreement_rate >= 0.7: return 1.20   # Strong agreement
    if agreement_rate >= 0.4: return 1.00   # Partial agreement
    if agreement_rate >= 0.2: return 0.60   # Disagreement -> small size
    return 0.30                            # Strong conflict -> minimal size


def get_tiered_confidence_tier(p_win: float, consensus_mult: float) -> str:
    """
    Assigns a confidence tier for frequency control.
    """
    score = p_win * consensus_mult
    if score >= 0.85: return "HIGH"
    if score >= 0.65: return "MEDIUM"
    return "LOW"


def apply_fat_pitch_guardrail(
    *,
    net_edge: float,
    market_price: float,
    total_bankroll_cents: float,
    max_otm_bankroll_pct: float = 0.02,
) -> float:
    """
    When an EV-override (fat pitch) fires, limit the absolute risk to bankroll.
    """
    if market_price < 0.10:
        cap_cents = total_bankroll_cents * float(max_otm_bankroll_pct)
        return cap_cents
    return total_bankroll_cents * 0.10


def check_drawdown_block(
    platform: str,
    balance_cents: int,
    sp: Any,
    cfg: dict,
    get_recent_performance_fn: Callable | None = None,
    ignore_drawdown: bool = False,
) -> bool:
    """Returns True if the platform is currently in a drawdown-driven trading halt."""
    if ignore_drawdown: return False
    if get_recent_performance_fn is None: return False
    try:
        perf = get_recent_performance_fn(lookback_hours=8, platform=platform, config=cfg)
        pnl = int(perf.get("pnl_cents", 0))
        if pnl < 0 and balance_cents > 0:
            drawdown = abs(pnl) / float(balance_cents)
            if drawdown >= 0.20: # 20% hard halt
                return True
    except Exception:
        pass
    return False


def get_balance_regime_adjustments(
    balance_cents: int, sp: Any
) -> tuple[float, float, str]:
    return 1.0, 0.0, "normal"


def calculate_inventory_skew_factor(held_count: int, max_skew_pct: float) -> float:
    if held_count <= 0 or max_skew_pct <= 0: return 0.0
    return min(float(max_skew_pct), (held_count / 100.0) * float(max_skew_pct))


def get_adaptive_kelly_fraction(
    base_kelly: float, calibration: dict[str, Any], sp: Any
) -> float:
    return float(base_kelly)


def get_dynamic_kill_size_multiplier(
    series: str, series_pnl: dict[str, dict], sp: Any
) -> float:
    return 1.0


def get_adaptive_stress_size_multiplier(bump: float, sp: Any) -> float:
    """Shrink Kelly sizing when adaptive edge bump is large."""
    return 1.0
    thresh = float(sp.adaptive_stress_size_edge_threshold)
    if bump < thresh:
        return 1.0
    cap = max(1e-6, float(sp.adaptive_edge_max_extra) + float(sp.loss_streak_max_extra))
    floor_m = float(sp.adaptive_stress_size_min_mult)
    span = max(1e-6, cap - thresh)
    t = min(1.0, max(0.0, (float(bump) - thresh) / span))
    return 1.0 - t * (1.0 - floor_m)


def get_simple_ev_calibration_size_multiplier(
    calibration: dict[str, Any] | None, sp: StrategyParams
) -> float:
    if not sp.use_calibration or not calibration:
        return 1.0
    resolved = int(calibration.get("resolved_count", 0) or 0)
    wr = calibration.get("win_rate")
    if resolved < sp.min_calibration_samples_overall or wr is None:
        return 1.0
    if float(wr) < float(sp.simple_ev_calibration_wr_threshold):
        return float(sp.simple_ev_calibration_poor_size_mult)
    return 1.0


def get_dte_edge_scale_factor(dte_days: float, sp: StrategyParams) -> float:
    """
    Scale required min_edge: easier hurdle when DTE is short (less time for model error),
    harder when DTE is long ("dead zone" contracts).
    """
    if not sp.dte_edge_scaling_enabled:
        return 1.0
    d = max(0.0, float(dte_days))
    s_days = float(sp.dte_edge_short_threshold_days)
    l_days = max(s_days + 1e-6, float(sp.dte_edge_long_threshold_days))
    sm = float(sp.dte_edge_mult_short)
    lm = float(sp.dte_edge_mult_long)
    if d <= s_days:
        return sm
    if d >= l_days:
        return lm
    t = (d - s_days) / (l_days - s_days)
    return sm + t * (lm - sm)


def cap_buy_count_by_book_pct(count: int, book_bid_size: float | None, max_pct: float) -> int:
    """Cap order size vs visible same-side depth to reduce adverse selection and book impact."""
    if count <= 0 or max_pct <= 0:
        return count
    if book_bid_size is None:
        return count
    try:
        depth = float(book_bid_size)
    except (TypeError, ValueError):
        return count
    if depth <= 0:
        return count
    cap = max(1, int(depth * max_pct))
    return min(count, cap)


def compute_limit_price(
    *,
    bid: float,
    ask: float | None,
    p_win: float,
    maker_fee_rate: float,
    min_fee_per_order: float,
    mode: str,
    base_dip: float,
    tick_offset: int,
    held_same_side: int,
    held_opp_side: int,
    max_inventory_skew_pct: float,
    layered_order_levels: int,
    order_count: int,
    max_dip_cap: float = 0.25,
    passive_quote: str = "dip",
    net_edge: float | None = None,
    edge_aggression_enabled: bool = False,
    cross_spread_min_edge: float = 0.10,
    near_market_min_edge: float = 0.05,
    passive_only_below_edge: float = 0.0,
    ev_tier_pricing: bool = True,
    weak_bid_min_edge: float = 0.03,
    skip_below_weak_edge: bool = True,
    medium_quote_mode: str = "spread_fraction",
    medium_spread_fraction: float = 0.25,
    resting_order_age_minutes: float | None = None,
    escalation_price_enabled: bool = False,
    escalate_after_minutes_1: float = 5.0,
    escalate_after_minutes_2: float = 10.0,
    wide_book_coalesce_enabled: bool = False,
    wide_book_conservative: bool = False,
    wide_book_spread_rel: float = 0.22,
    wide_book_anchor_fraction: float = 0.38,
    fill_prob: float | None = None,
) -> tuple[float | None, float]:
    """
    Returns (limit_price, dip_used_proxy).

    Implements three execution regimes:
    1. Aggressive: Take liquidity (cross spread) for very high edge.
    2. Competitive: Join top of book or improve slightly for moderate edge.
    3. Passive: Deeper limit orders (dip-style) for low edge or wide spreads.
    """
    mode_norm = (mode or "fixed_pct").strip().lower()
    if mode_norm not in {"fixed_pct", "dynamic_tick", "layered"}:
        mode_norm = "fixed_pct"

    # Hard cap at fair value (P_win - fees)
    fair_cap = clip_price(float(p_win) - _fee_per_contract(float(p_win), maker_fee_rate, min_fee_per_order))
    if fair_cap <= 0.01:
        return 0.01, 0.0 # Stink bid floor

    _po = max(0.0, float(passive_only_below_edge or 0.0))
    ne = float(net_edge) if net_edge is not None else 0.0
    _ne_ok = ne > 0
    if _po > 0 and _ne_ok and ne < _po:
        _ne_ok = False

    # Dynamic Offset Logic
    tick = max(1, int(tick_offset)) * 0.01
    spread = (float(ask) - float(bid)) if (ask is not None and ask > bid) else 0.0
    
    # Execution Regime Selection
    if edge_aggression_enabled and _ne_ok:
        cross_th = max(0.0, float(cross_spread_min_edge))
        near_th = max(0.0, float(near_market_min_edge))
        weak_th = max(0.0, float(weak_bid_min_edge))
        if near_th > cross_th: near_th = cross_th
        if weak_th > near_th: weak_th = near_th

        # A. AGGRESSIVE (Take Liquidity / Cross Spread)
        if ne >= cross_th:
            if ask is not None and float(ask) > 0:
                # If very large edge, we take the ask; otherwise we join it
                target = min(float(ask), fair_cap)
            else:
                # No ask? Penny jump the bid
                target = min(float(bid) + tick, fair_cap)
            
            # Wide-book coalescing: if our target is below bid but we can afford bid, lift to bid
            if (wide_book_coalesce_enabled and not wide_book_conservative 
                and target + 1e-9 < float(bid) and fair_cap + 1e-9 >= float(bid)):
                target = min(float(bid), fair_cap)
                
            return clip_price(target), 0.0

        # B. COMPETITIVE (Join/Improve Top of Book)
        elif ne >= near_th:
            # We want to be at bid + small_offset
            # small_offset = min(tick, spread * 0.3, edge * 0.5)
            dynamic_offset = min(tick, spread * 0.3, ne * 0.5)
            target = float(bid) + dynamic_offset
            
            # If medium_quote_mode is spread_fraction, use that as a floor for aggression
            if medium_quote_mode == "spread_fraction" and spread > 0:
                frac_target = float(bid) + (spread * float(medium_spread_fraction))
                target = max(target, frac_target)
            
            target = min(target, fair_cap)
            if ask is not None and ask > 0:
                target = min(target, float(ask)) # Don't cross in competitive mode
            
            # Wide-book coalescing: if our target is below bid but we can afford bid, lift to bid
            if (wide_book_coalesce_enabled and not wide_book_conservative 
                and target + 1e-9 < float(bid) and fair_cap + 1e-9 >= float(bid)):
                target = min(float(bid), fair_cap)

            return clip_price(target), 0.0

        # NEW: WEAK TIER (Quote at Bid)
        elif ne >= weak_th:
            target = min(float(bid), fair_cap)
            return clip_price(target), 0.0

        # C. WEAK / PASSIVE GATE
        elif skip_below_weak_edge:
            return None, 0.0

    # C. PASSIVE (Dip / Provide Liquidity)
    if mode_norm == "fixed_pct":
        dip = max(0.0, min(max_dip_cap, float(base_dip)))
        raw_price = bid * (1.0 - dip) if dip > 0 else bid
    elif mode_norm == "dynamic_tick":
        # competitive-ish passive
        jump_price = bid + tick
        if ask is not None and ask > 0:
            jump_price = min(jump_price, max(0.01, float(ask) - 0.01))
        raw_price = min(jump_price, fair_cap)
        dip = 0.0
    else:
        # Layered: place passive limits BELOW bid
        dip = max(0.0, min(max_dip_cap, float(base_dip)))
        base_price = bid * (1.0 - dip) if dip > 0 else bid
        levels = max(1, int(layered_order_levels))
        if levels > 1 and order_count > 1:
            level_idx = min(order_count - 1, levels - 1)
            depth_per_level = max(0.01, bid * dip * 0.5) if dip > 0 else 0.01
            base_price -= level_idx * depth_per_level
        raw_price = min(base_price, fair_cap)

    # Wide Book Coalescing (Avoid dead zones)
    if mode_norm in ("fixed_pct", "layered"):
        raw_price = wide_book_lift_passive_raw_price(
            bid=bid,
            ask=ask,
            fair_cap=float(fair_cap),
            raw_price=float(raw_price),
            enabled=wide_book_coalesce_enabled,
            conservative=wide_book_conservative,
            spread_rel_threshold=wide_book_spread_rel,
            anchor_fraction=wide_book_anchor_fraction,
        )

    # Inventory Skew Adjustment
    same_skew = calculate_inventory_skew_factor(int(held_same_side), max_inventory_skew_pct)
    opp_skew = calculate_inventory_skew_factor(int(held_opp_side), max_inventory_skew_pct) * 0.5
    skewed = raw_price * (1.0 - same_skew + opp_skew)

    # Final resolution based on passive_quote mode
    merged = resolve_passive_quote_price(
        passive_quote=passive_quote,
        bid=bid,
        ask=ask,
        dip_based_price=skewed,
        fair_cap=fair_cap,
    )

    # D. ESCALATION (Aging Order Patience Exhausted)
    if escalation_price_enabled and resting_order_age_minutes is not None:
        age = float(resting_order_age_minutes)
        if age >= float(escalate_after_minutes_1):
            # Increase aggression: move towards competitive or mid
            # If we were passive, we might move to bid or bid+tick
            current_px = float(merged or 0)
            target_px = current_px
            if age >= float(escalate_after_minutes_2):
                # Second stage: move even closer to market
                if ask is not None and ask > bid:
                    target_px = (bid + ask) / 2.0
                else:
                    target_px = bid + (2 * tick)
            else:
                # First stage: move to bid or bid+tick
                target_px = max(current_px, bid + tick)
            
            merged = min(target_px, fair_cap)
            if ask is not None and ask > 0:
                merged = min(float(merged), float(ask))

    return merged, float(dip)


def allocate_budget_to_orders(
    pending_orders: list[dict],
    balance_cents: int,
    max_pct: float,
    filter_zero: bool = True,
) -> None:
    """
    Adjust order counts so total cost fits budget. Prefer buying 1 of many
    contracts over blowing the budget on a few. Modifies pending_orders in place.
    """
    if not pending_orders:
        return

    def _trace(stage: str, o: dict, before: int, after: int, note: str = "") -> None:
        if before == after:
            return
        traces = o.setdefault("_sizing_trace", [])
        traces.append({
            "stage": stage,
            "before": int(before),
            "after": int(after),
            "note": note,
        })

    available_cents = int(balance_cents * max_pct)
    total_cost_cents = sum(
        o.get("count", 10) * int(float(o.get("price", 0)) * 100)
        for o in pending_orders
    )
    if total_cost_cents <= available_cents:
        return
    # Scale down: prefer 1 of each for diversification
    cost_if_1_each = sum(int(float(o.get("price", 0)) * 100) for o in pending_orders)
    if cost_if_1_each <= available_cents:
        for o in pending_orders:
            before = int(o.get("count", 0) or 0)
            o["count"] = 1
            _trace("budget_one_each", o, before, 1, "force diversification under budget")
        return
    # Can't afford 1 of each: keep highest edge trades first (simplified ranking here)
    by_edge = sorted(
        pending_orders,
        key=lambda x: -float(x.get("edge", 0.0)),
    )
    running_cents = 0
    for o in by_edge:
        cost_1 = int(float(o.get("price", 0)) * 100)
        before = int(o.get("count", 0) or 0)
        if running_cents + cost_1 <= available_cents:
            o["count"] = 1
            running_cents += cost_1
            _trace("budget_rank_trim", o, before, 1, "ranked keep under max_total_buy_pct")
        else:
            o["count"] = 0
            _trace("budget_rank_trim", o, before, 0, "ranked out by budget")
    if filter_zero:
        pending_orders[:] = [o for o in by_edge if o.get("count", 0) > 0]


def cap_cross_platform_exposure(
    pending_orders: list[dict],
    total_balance_cents: int,
    max_cross_platform_exposure: float,
    get_resolution_key_fn: Callable[[dict], str | None],
) -> None:
    """
    Cap combined (Kalshi + Gemini) exposure per (yf_ticker, resolution key).
    """
    if not pending_orders or total_balance_cents <= 0:
        return
    cap_pct = max(0.0, min(1.0, float(max_cross_platform_exposure)))
    if cap_pct <= 0:
        return
    cap_cents = int(total_balance_cents * cap_pct)
    if cap_cents <= 0:
        return

    grouped: dict[tuple[str, str], list[dict]] = {}
    for o in pending_orders:
        yf = o.get("yf_ticker")
        res_key = get_resolution_key_fn(o)
        if not yf or not res_key:
            continue
        grouped.setdefault((str(yf), str(res_key)), []).append(o)

    for _, orders in grouped.items():
        trim_order_group_to_notional_cap(orders, cap_cents)


def cap_bankroll_exposure_buckets(
    pending_orders: list[dict],
    total_balance_cents: int,
    *,
    max_frac_per_direction: float,
    max_frac_per_underlying: float,
    logger: Any = print,
) -> None:
    """
    Bankroll-% risk buckets (Kalshi+Gemini combined notionals in this batch).
    """
    if not pending_orders or total_balance_cents <= 0:
        return
    fd = max(0.0, min(1.0, float(max_frac_per_direction)))
    fu = max(0.0, min(1.0, float(max_frac_per_underlying)))
    if fd <= 0 and fu <= 0:
        return

    def _risk_orders() -> list[dict]:
        out: list[dict] = []
        for o in pending_orders:
            if o.get("rec") not in (REC_BUY_YES, REC_BUY_NO):
                continue
            if not str(o.get("yf_ticker") or "").strip():
                continue
            if int(o.get("count", 0) or 0) <= 0:
                continue
            out.append(o)
        return out

    touched = False
    if fd > 0:
        cap_d = int(total_balance_cents * fd)
        if cap_d > 0:
            by_dir: dict[tuple[str, str], list[dict]] = {}
            for o in _risk_orders():
                yf = str(o.get("yf_ticker")).strip()
                side = "yes" if o.get("rec") == REC_BUY_YES else "no"
                by_dir.setdefault((yf, side), []).append(o)
            for grp in by_dir.values():
                if trim_order_group_to_notional_cap(grp, cap_d):
                    touched = True
    if fu > 0:
        cap_u = int(total_balance_cents * fu)
        if cap_u > 0:
            by_yf: dict[str, list[dict]] = {}
            for o in _risk_orders():
                yf = str(o.get("yf_ticker")).strip()
                by_yf.setdefault(yf, []).append(o)
            for grp in by_yf.values():
                if trim_order_group_to_notional_cap(grp, cap_u):
                    touched = True
    if touched:
        logger(
            "[Risk] Bankroll exposure buckets: trimmed pending BUY notionals "
            f"(per-direction cap={fd:.0%}, per-underlying cap={fu:.0%} of combined balance)."
        )

def _capital_efficiency_score_order(o: dict) -> float:
    """Edge-first order score with soft liquidity/time modifiers."""
    edge = max(0.0, float(o.get("edge", 0.0)))
    spread_bps = float(o.get("spread_bps_at_place", 100.0) or 100.0)
    liquidity_score = max(0.25, min(1.0, 1.0 - (max(0.0, spread_bps) / 10000.0)))
    ttl_h = o.get("expected_hours_to_resolution")
    # Fallback to 24h if missing to avoid heavy dependencies
    ttl_h = max(1.0, float(ttl_h) if ttl_h is not None else 24.0)
    time_decay = 1.0 / (1.0 + (ttl_h / 24.0))
    return edge * liquidity_score * time_decay


def trim_order_group_to_notional_cap(orders: list[dict], cap_cents: int) -> bool:
    """Shrink counts so sum(price*count) <= cap_cents. Returns True if any count changed."""
    if cap_cents <= 0 or not orders:
        return False
    total_cost = sum(
        int(float(o.get("price", 0)) * 100) * max(0, int(o.get("count", 0)))
        for o in orders
    )
    if total_cost <= cap_cents:
        return False
    # Sort by efficiency then edge for ranking
    ranked = sorted(
        orders,
        key=lambda x: (
            -_capital_efficiency_score_order(x),
            -float(x.get("edge", 0.0)),
        ),
    )
    used = 0
    changed = False
    for o in ranked:
        price_c = max(1, int(float(o.get("price", 0)) * 100))
        curr = max(0, int(o.get("count", 0)))
        if curr <= 0:
            continue
        room = max(0, cap_cents - used)
        max_count = room // price_c
        if curr > max_count:
            o["count"] = max_count
            changed = True
            traces = o.setdefault("_sizing_trace", [])
            traces.append({
                "stage": "notional_cap_trim",
                "before": int(curr),
                "after": int(max_count),
                "note": f"group cap ${cap_cents/100:.2f}",
            })
        used += price_c * max(0, int(o.get("count", 0)))
    return changed
