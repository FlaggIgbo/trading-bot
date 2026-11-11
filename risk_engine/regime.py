"""
Market Regime & Volatility Gate
===============================
Analyzes recent 15-minute market resolutions to detect:
1. Mean-Reversion Regime (negative autocorrelation / streaks >= 2).
2. Trending Breakout Regime (low oscillation + strong magnitude edge).
3. Choppy Noise Regime (alternating flips, low volatility) -> Abstains.

Provides high-edge execution signals for immediate taker IOC orders.
"""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple
from db import MarketResolution, PositionSide


@dataclass
class MarketRegimeResult:
    regime: str  # 'MEAN_REVERSION', 'TRENDING', 'CHOP_ABSTAIN'
    recommended_side: Optional[PositionSide]
    is_high_edge: bool
    streak: int
    flip_ratio: float
    edge_ratio: float
    avg_volatility: float
    reason: str

    @property
    def should_trade(self) -> bool:
        return self.recommended_side is not None and self.regime != "CHOP_ABSTAIN"


def detect_market_regime(
    past_results: Any,
    min_samples: int = 5,
    min_volatility: float = 0.03,
    min_edge_ratio: float = 0.20,
    max_trend_flip_ratio: float = 0.40,
) -> MarketRegimeResult:
    """
    Evaluate recent market resolutions through the volatility gate and regime detector.
    
    Returns:
        MarketRegimeResult containing regime classification, recommended trade side,
        and whether the setup qualifies for an immediate taker market order.
    """
    if not past_results or len(past_results) < min_samples:
        return MarketRegimeResult(
            regime="CHOP_ABSTAIN",
            recommended_side=None,
            is_high_edge=False,
            streak=0,
            flip_ratio=1.0,
            edge_ratio=0.0,
            avg_volatility=0.0,
            reason=f"Insufficient history ({len(past_results) if past_results else 0} < {min_samples})",
        )

    # 1. Clean results and calculate move magnitude weights
    raw_list = list(past_results)
    # Check if list is passed in reverse chronological order (newest first)
    first_ct = getattr(raw_list[0], "close_time", None) or (raw_list[0].get("close_time") if isinstance(raw_list[0], dict) else None)
    last_ct = getattr(raw_list[-1], "close_time", None) or (raw_list[-1].get("close_time") if isinstance(raw_list[-1], dict) else None)
    if first_ct and last_ct and str(first_ct) > str(last_ct):
        raw_list = list(reversed(raw_list))

    clean_results: List[str] = []
    vols: List[float] = []

    for item in raw_list:
        clean_res = None
        fs = 0.0
        ev = 0.0

        if isinstance(item, MarketResolution):
            clean_res = str(item.result).strip().lower()
            fs = float(item.floor_strike or 0)
            ev = float(item.expiration_value or 0)
        elif isinstance(item, dict):
            clean_res = str(item.get("result", "")).strip().lower()
            fs = float(item.get("floor_strike") or 0)
            ev = float(item.get("expiration_value") or 0)
        elif hasattr(item, "value"):
            clean_res = str(item.value).strip().lower()
        else:
            clean_res = str(item).strip().lower()

        if clean_res in ("yes", "no"):
            weight = abs(ev - fs) / fs * 100.0 if fs > 0 else 1.0
            clean_results.append(clean_res)
            vols.append(max(1e-4, weight))

    if len(clean_results) < min_samples:
        return MarketRegimeResult(
            regime="CHOP_ABSTAIN",
            recommended_side=None,
            is_high_edge=False,
            streak=0,
            flip_ratio=1.0,
            edge_ratio=0.0,
            avg_volatility=0.0,
            reason="Insufficient valid yes/no resolutions",
        )

    # 2. Volatility Gate: Average percentage move over recent 5 markets
    recent_vols = vols[-5:]
    avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0.0
    if avg_vol < min_volatility:
        return MarketRegimeResult(
            regime="CHOP_ABSTAIN",
            recommended_side=None,
            is_high_edge=False,
            streak=0,
            flip_ratio=1.0,
            edge_ratio=0.0,
            avg_volatility=avg_vol,
            reason=f"Volatility gate triggered: avg move {avg_vol:.4f}% < min {min_volatility:.2f}%",
        )

    # 3. Consecutive Streak Calculation at the tail of history
    last_res = clean_results[-1]
    streak = 0
    for r in reversed(clean_results):
        if r == last_res:
            streak += 1
        else:
            break

    # 4. Alternating Flip Ratio across the last 8 markets
    recent_8 = clean_results[-8:]
    flips = sum(1 for i in range(len(recent_8) - 1) if recent_8[i] != recent_8[i + 1])
    flip_ratio = flips / max(1, len(recent_8) - 1)

    # 5. Weighted Trend Magnitude across the last 21 markets
    recent_21_vols = vols[-21:]
    recent_21_res = clean_results[-21:]
    up_w = sum(w for w, r in zip(recent_21_vols, recent_21_res) if r == "yes")
    dn_w = sum(w for w, r in zip(recent_21_vols, recent_21_res) if r == "no")
    tot_w = up_w + dn_w
    edge_ratio = abs(up_w - dn_w) / tot_w if tot_w > 0 else 0.0

    # REGIME DECISION 1: Mean-Reversion Setup (Streak >= 2)
    # At 15-minute resolution, streaks of 2 reverse 56.5% of the time, and streaks of 3 reverse 61.4%
    if streak >= 2:
        fade_side = PositionSide.NO if last_res == "yes" else PositionSide.YES
        is_high_edge = (streak >= 2)
        return MarketRegimeResult(
            regime="MEAN_REVERSION",
            recommended_side=fade_side,
            is_high_edge=is_high_edge,
            streak=streak,
            flip_ratio=flip_ratio,
            edge_ratio=edge_ratio,
            avg_volatility=avg_vol,
            reason=f"Mean-reversion signal: {streak} consecutive {last_res.upper()}s -> fading with {fade_side.value}",
        )

    # REGIME DECISION 2: Trending Breakout Setup
    # Low alternating flips + dominant directional volume magnitude
    if flip_ratio <= max_trend_flip_ratio and edge_ratio >= min_edge_ratio:
        trend_side = PositionSide.YES if up_w > dn_w else PositionSide.NO
        is_high_edge = (edge_ratio >= 0.30)
        return MarketRegimeResult(
            regime="TRENDING",
            recommended_side=trend_side,
            is_high_edge=is_high_edge,
            streak=streak,
            flip_ratio=flip_ratio,
            edge_ratio=edge_ratio,
            avg_volatility=avg_vol,
            reason=f"Trending breakout: edge={edge_ratio:.2f}, flip_ratio={flip_ratio:.2f} -> following {trend_side.value}",
        )

    # REGIME DECISION 3: Choppy Noise Consolidation -> Abstain
    return MarketRegimeResult(
        regime="CHOP_ABSTAIN",
        recommended_side=None,
        is_high_edge=False,
        streak=streak,
        flip_ratio=flip_ratio,
        edge_ratio=edge_ratio,
        avg_volatility=avg_vol,
        reason=f"Choppy consolidation (flip_ratio={flip_ratio:.2f}, edge={edge_ratio:.2f}) -> abstaining",
    )
