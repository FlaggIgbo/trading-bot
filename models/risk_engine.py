"""
Risk Management and Execution Logic: Sizing, Guardrails, and Exposure.

Provides safety blocks for OTM tail trades ('fat pitches') and applies
fractional Kelly sizing with confidence-aware multipliers.
"""
from __future__ import annotations

import math


def calculate_fractional_kelly_size(
    p_win: float,
    price: float,
    *,
    total_bankroll_cents: float,
    fraction: float = 0.25,
    max_trade_cents: float = 5000, # $50 max by default
    trust_score: float = 1.0,
) -> int:
    """
    Kelly Criterion: K = (p*b - q) / b where b is net odds (1/p - 1 per Kalshi/Gemini).
    Simplified: K = (p - price) / (1 - price).
    
    Returns the size in cents (contract count × price × 100).
    """
    if price <= 0 or price >= 1.0:
        return 0
    
    # 0.5 (model) - 0.1 (market) / 0.9 = 0.4 / 0.9 = 44%
    p = max(0.0, min(0.99, float(p_win)))
    k_raw = (p - float(price)) / (1.0 - float(price))
    
    if k_raw <= 0:
        return 0
    
    # Apply fractional Kelly (e.g. 0.25x) and trust score adjustment
    # If the model is underconfident (trust > 1.0), we increase size.
    k_adj = k_raw * float(fraction) * float(trust_score)
    k_adj = max(0.0, min(0.10, k_adj)) # Cap single-trade bankroll to 10%
    
    size_cents = int(total_bankroll_cents * k_adj)
    return min(int(max_trade_cents), size_cents)


def apply_fat_pitch_guardrail(
    *,
    net_edge: float,
    market_price: float,
    total_bankroll_cents: float,
    max_otm_bankroll_pct: float = 0.02, # Max 2% of bankroll on a single tail trade
) -> float:
    """
    When an EV-override (fat pitch) fires, limit the absolute risk to bankroll.
    Prediction market tails are fragile; we want many small bets, not one big one.
    
    Returns the maximum trade size in cents.
    """
    if market_price < 0.10: # Only applies to extreme tails
        # Max spend = 2% of total bankroll
        cap_cents = total_bankroll_cents * float(max_otm_bankroll_pct)
        return cap_cents
    
    return total_bankroll_cents * 0.10 # Standard 10% cap
