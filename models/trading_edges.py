"""
Shared fee, net-edge, Kelly spend, and vol-regime scaling math.

Used by compare_historical (and tests) so edge invariants stay consistent in one place.
"""
from __future__ import annotations

import math


def maker_fee_per_contract(price: float, maker_rate: float, min_fee_per_order: float = 0.01) -> float:
    """
    Maker fee per contract: rate × P × (1−P). Kalshi & Gemini use this shape.
    Rounded up to next cent per exchange docs. Returns 0 if rate=0 or invalid price.
    """
    if maker_rate <= 0 or price <= 0 or price >= 1:
        return 0.0
    fee = maker_rate * price * (1.0 - price)
    if fee <= 0:
        return 0.0
    rounded = math.ceil(fee * 100) / 100
    return max(rounded, max(0.0, float(min_fee_per_order)))


def net_edges_buy_legs(
    p_yes: float,
    yes_bid: float,
    no_bid: float,
    maker_fee_rate: float,
    min_fee_per_order: float,
) -> tuple[float, float]:
    """
    Fee-adjusted edge for each leg (single source for BUY YES vs BUY NO symmetry):
      edge_yes = P(YES) − yes_bid − fee_yes
      edge_no  = P(NO) − no_bid − fee_no  with P(NO) = 1 − P(YES)
    """
    yb = max(0.0, min(0.99, float(yes_bid)))
    nb = max(1e-6, min(0.99, float(no_bid)))
    p = max(0.0, min(1.0, float(p_yes)))
    fy = maker_fee_per_contract(yb, maker_fee_rate, min_fee_per_order)
    fn = maker_fee_per_contract(nb, maker_fee_rate, min_fee_per_order)
    return (p - yb - fy, (1.0 - p) - nb - fn)


def simple_ev_spend_fraction(
    ev_net: float,
    price: float,
    *,
    kelly_dampen: float,
    max_bankroll_fraction: float,
) -> float:
    """
    Fraction of bankroll to allocate: min(cap, dampen * EV / (p*(1-p))).
    ev_net and price are for the bought contract (YES or NO); price is the bid paid.
    """
    p = max(1e-6, min(1.0 - 1e-6, float(price)))
    denom = p * (1.0 - p)
    k_raw = float(ev_net) / denom if denom > 0 else 0.0
    k_raw = max(0.0, k_raw)
    return min(float(max_bankroll_fraction), k_raw * float(kelly_dampen))


def vol_regime_edge_dip_size_mults(
    *,
    enabled: bool,
    vol_ratio: float,
    edge_mult_per_x: float,
    max_edge_mult: float,
    dip_mult_per_x: float,
    max_dip_mult: float,
    size_mult_per_x: float,
    min_size_mult: float,
) -> tuple[float, float, float]:
    """
    Return (edge_mult, dip_mult, size_mult) from continuous volatility ratio.
    Higher vol raises edge thresholds, deepens limits, and reduces size.
    """
    if not enabled:
        return 1.0, 1.0, 1.0
    excess = max(0.0, float(vol_ratio) - 1.0)
    edge_mult = 1.0 + max(0.0, float(edge_mult_per_x)) * excess
    edge_mult = min(max(1.0, edge_mult), max(1.0, float(max_edge_mult)))
    dip_mult = 1.0 + max(0.0, float(dip_mult_per_x)) * excess
    dip_mult = min(max(1.0, dip_mult), max(1.0, float(max_dip_mult)))
    size_mult = 1.0 / (1.0 + max(0.0, float(size_mult_per_x)) * excess)
    size_mult = max(min(1.0, size_mult), max(0.01, float(min_size_mult)))
    return edge_mult, dip_mult, size_mult


def clip_probability(p: float, lo: float, hi: float, *, enabled: bool = True) -> float:
    """Clamp model probability when hard-cap is enabled (matches compare_historical sanitize)."""
    if not enabled:
        return float(p)
    return max(float(lo), min(float(hi), float(p)))


def brier_score(predictions: list[float], outcomes: list[float]) -> float | None:
    """
    Mean Squared Error of probabilities: 1/N * sum((p - o)^2).
    Outcomes should be 1.0 (win) or 0.0 (loss). Lower is better (0.0 = perfect).
    """
    if not predictions or len(predictions) != len(outcomes):
        return None
    total_sq_error = sum((p - o) ** 2 for p, o in zip(predictions, outcomes))
    return total_sq_error / len(predictions)


def expected_calibration_error(
    predictions: list[float], outcomes: list[float], n_bins: int = 10
) -> float | None:
    """
    ECE: Weighted average of |accuracy - confidence| across probability bins.
    Simplified version using 10 fixed bins (0-10%, 10-20%, etc).
    """
    if not predictions or len(predictions) != len(outcomes):
        return None
    
    bin_counts = [0] * n_bins
    bin_confidences = [0.0] * n_bins
    bin_accuracies = [0.0] * n_bins
    
    for p, o in zip(predictions, outcomes):
        bin_idx = min(n_bins - 1, int(p * n_bins))
        bin_counts[bin_idx] += 1
        bin_confidences[bin_idx] += p
        bin_accuracies[bin_idx] += o
        
    total_ece = 0.0
    n_total = len(predictions)
    for i in range(n_bins):
        if bin_counts[i] > 0:
            avg_conf = bin_confidences[i] / bin_counts[i]
            avg_acc = bin_accuracies[i] / bin_counts[i]
            total_ece += (bin_counts[i] / n_total) * abs(avg_conf - avg_acc)
            
    return total_ece


def horizon_adjusted_required_edge(
    base_edge: float,
    minutes_left: float | None,
    spread: float = 0.02,
    k_spread: float = 0.5,
    latency_risk: float = 0.015,
) -> float:
    """
    Dynamic required edge threshold based on time remaining.
    Instead of a blunt multiplier, use additive risk factors.
    As time -> 0, liquidity and latency risks dominate.
    """
    if minutes_left is None or minutes_left > 60:
        return base_edge
    
    # Linear ramp for latency risk as we get very close to resolution
    # (e.g. at 0m it's full latency_risk, at 60m it's 0)
    risk_factor = max(0.0, (60.0 - minutes_left) / 60.0)
    
    # required_edge = base + k*spread + ramped_latency_risk
    # We add the spread-based hurdle to ensure we aren't just crossing wide books blindly
    return base_edge + (k_spread * spread) + (risk_factor * latency_risk)


def liquidity_size_multiplier(
    price: float,
    min_price_threshold: float = 0.01,
    floor_mult: float = 0.1,
) -> float:
    """
    Instead of a hard filter for cheap contracts (e.g. < 5¢), 
    linearly scale down the position size.
    
    Returns 1.0 for price >= threshold.
    Returns floor_mult for price close to 0.
    """
    if price >= min_price_threshold:
        return 1.0
    if price <= 0:
        return floor_mult
    
    # Linear decay from 1.0 to floor_mult
    ratio = price / min_price_threshold
    return floor_mult + (1.0 - floor_mult) * ratio
