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
