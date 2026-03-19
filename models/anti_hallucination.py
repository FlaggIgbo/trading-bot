"""
Anti-hallucination layers for probability estimation.

Addresses the core problem: model assigns 80%+ probability to contracts
the market prices at 5% because it treats historical data as stationary.
These layers bridge the gap between model confidence and market reality.

Layers:
  1. Bayesian Update   – treat market price as prior, model P as evidence
  2. EWMA Probability  – exponentially weight recent price data
  3. Volatility Z-Score – fatten tails when current vol is anomalous
  4. Adaptive Edge      – require larger edge for cheap/expensive contracts
  5. Regime Filter      – block trend-opposed trades via EMA gate
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class AntiHallucinationConfig:
    """All tunables for the anti-hallucination pipeline."""

    # --- Bayesian update ---
    bayesian_enabled: bool = True
    bayesian_market_strength: float = 2.0

    # --- EWMA probability ---
    ewma_prob_enabled: bool = True
    ewma_prob_span: int = 14
    ewma_prob_min_samples: int = 10

    # --- Volatility Z-score gate ---
    vol_zscore_enabled: bool = True
    vol_zscore_short_window: int = 24
    vol_zscore_long_window: int = 168
    vol_zscore_threshold: float = 2.0
    vol_zscore_tail_mult: float = 0.5

    # --- Adaptive edge ---
    adaptive_edge_enabled: bool = True
    adaptive_edge_cheap_threshold: float = 0.15
    adaptive_edge_expensive_threshold: float = 0.85
    adaptive_edge_otm_base_mult: float = 2.5
    adaptive_edge_trend_opposed_mult: float = 2.0

    # --- Regime filter (EMA gate) ---
    regime_filter_enabled: bool = True
    regime_ema_period: int = 20
    regime_require_alignment: bool = True


def load_anti_hallucination_config(strategy: dict) -> AntiHallucinationConfig:
    ah = strategy.get("anti_hallucination", {})
    return AntiHallucinationConfig(
        bayesian_enabled=bool(ah.get("bayesian_enabled", True)),
        bayesian_market_strength=float(ah.get("bayesian_market_strength", 2.0)),
        ewma_prob_enabled=bool(ah.get("ewma_prob_enabled", True)),
        ewma_prob_span=int(ah.get("ewma_prob_span", 14)),
        ewma_prob_min_samples=int(ah.get("ewma_prob_min_samples", 10)),
        vol_zscore_enabled=bool(ah.get("vol_zscore_enabled", True)),
        vol_zscore_short_window=int(ah.get("vol_zscore_short_window", 24)),
        vol_zscore_long_window=int(ah.get("vol_zscore_long_window", 168)),
        vol_zscore_threshold=float(ah.get("vol_zscore_threshold", 2.0)),
        vol_zscore_tail_mult=float(ah.get("vol_zscore_tail_mult", 0.5)),
        adaptive_edge_enabled=bool(ah.get("adaptive_edge_enabled", True)),
        adaptive_edge_cheap_threshold=float(ah.get("adaptive_edge_cheap_threshold", 0.15)),
        adaptive_edge_expensive_threshold=float(ah.get("adaptive_edge_expensive_threshold", 0.85)),
        adaptive_edge_otm_base_mult=float(ah.get("adaptive_edge_otm_base_mult", 2.5)),
        adaptive_edge_trend_opposed_mult=float(ah.get("adaptive_edge_trend_opposed_mult", 2.0)),
        regime_filter_enabled=bool(ah.get("regime_filter_enabled", True)),
        regime_ema_period=int(ah.get("regime_ema_period", 20)),
        regime_require_alignment=bool(ah.get("regime_require_alignment", True)),
    )


# ---------------------------------------------------------------------------
# Layer 1: Bayesian Probability Update
# ---------------------------------------------------------------------------

def bayesian_update(
    model_p: float,
    market_p: float,
    market_strength: float = 2.0,
) -> float:
    """
    Bayesian update treating market price as a Beta-distributed prior and
    the model prediction as new evidence.

    market_strength controls how much weight the market prior gets.
    Higher values = more trust in the market.  At strength=2 the market
    acts like having observed 2 pseudo-samples.

    When model says 86% and market says 8%, with strength=2 the posterior
    lands around 30% instead of blindly trusting 86%.
    """
    model_p = max(1e-6, min(1 - 1e-6, float(model_p)))
    market_p = max(1e-6, min(1 - 1e-6, float(market_p)))
    strength = max(0.0, float(market_strength))
    if strength <= 0:
        return model_p

    alpha_prior = market_p * strength
    beta_prior = (1 - market_p) * strength

    alpha_post = alpha_prior + model_p
    beta_post = beta_prior + (1 - model_p)

    posterior = alpha_post / (alpha_post + beta_post)
    return max(0.01, min(0.99, posterior))


# ---------------------------------------------------------------------------
# Layer 2: EWMA Probability Estimation
# ---------------------------------------------------------------------------

def ewma_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    span: int = 14,
    min_samples: int = 10,
) -> float | None:
    """
    Exponentially weighted probability that price is above/below threshold.

    Instead of giving equal weight to all historical days, recent days
    get exponentially more weight.  A 14-day span means yesterday's
    observation is worth ~2x an observation from 2 weeks ago.

    Returns None if insufficient data.
    """
    if not closes or len(closes) < min_samples:
        return None
    decay = 2.0 / (span + 1)
    weighted_hits = 0.0
    total_weight = 0.0
    for i, price in enumerate(closes):
        age = len(closes) - 1 - i
        w = (1 - decay) ** age
        total_weight += w
        hit = (price > threshold) if direction == "above" else (price < threshold)
        if hit:
            weighted_hits += w
    if total_weight <= 0:
        return None
    return max(0.01, min(0.99, weighted_hits / total_weight))


# ---------------------------------------------------------------------------
# Layer 3: Volatility Z-Score Gate
# ---------------------------------------------------------------------------

def volatility_zscore(
    closes: list[float],
    short_window: int = 24,
    long_window: int = 168,
) -> float | None:
    """
    Z-score of current short-window realized volatility vs long-window baseline.

    Returns None if insufficient data.  A Z-score > 2.0 means current
    vol is more than 2 standard deviations above its recent average,
    signaling a regime break where model confidence should be dampened.
    """
    if not closes or len(closes) < long_window + 2:
        return None
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < long_window:
        return None

    short_rets = rets[-short_window:]
    short_vol = math.sqrt(sum(r * r for r in short_rets) / len(short_rets))

    chunk_size = short_window
    n_chunks = len(rets) // chunk_size
    if n_chunks < 3:
        return None
    chunk_vols = []
    for c in range(n_chunks):
        chunk = rets[c * chunk_size : (c + 1) * chunk_size]
        if chunk:
            chunk_vols.append(math.sqrt(sum(r * r for r in chunk) / len(chunk)))
    if len(chunk_vols) < 3:
        return None

    mean_vol = sum(chunk_vols) / len(chunk_vols)
    std_vol = math.sqrt(sum((v - mean_vol) ** 2 for v in chunk_vols) / len(chunk_vols))
    if std_vol < 1e-12:
        return 0.0
    return (short_vol - mean_vol) / std_vol


def apply_vol_zscore_dampening(
    model_p: float,
    zscore: float | None,
    threshold_z: float = 2.0,
    tail_mult: float = 0.5,
) -> float:
    """
    When vol Z-score exceeds threshold, pull probability toward 0.5
    (fatten tails / reduce extreme confidence).

    tail_mult controls how aggressively we dampen: 0.5 means we move
    halfway toward 0.5 when Z >= threshold.
    """
    if zscore is None or zscore < threshold_z:
        return model_p
    excess = min(zscore - threshold_z, 3.0)
    blend = min(1.0, tail_mult * (1.0 + excess * 0.3))
    dampened = model_p * (1 - blend) + 0.5 * blend
    return max(0.01, min(0.99, dampened))


# ---------------------------------------------------------------------------
# Layer 4: Adaptive Edge Requirements
# ---------------------------------------------------------------------------

def adaptive_min_edge(
    base_edge: float,
    market_yes: float,
    *,
    current_price: float,
    threshold: float,
    direction: str,
    momentum_ret: float = 0.0,
    cheap_threshold: float = 0.15,
    expensive_threshold: float = 0.85,
    otm_base_mult: float = 2.5,
    trend_opposed_mult: float = 2.0,
) -> float:
    """
    Dynamic edge requirement that scales with contract price and trend.

    For cheap contracts (< 15¢): require 2.5x base edge because the
    tail risk of a vol-spike sweep is enormous.

    For expensive contracts (> 85¢): also require more edge because
    you risk the full price for a small payout.

    When the spot price is trending AWAY from your strike (buying YES
    on "above" while price is falling), double the edge to prevent
    catching falling knives.
    """
    edge = float(base_edge)
    market_yes = max(0.01, min(0.99, float(market_yes)))

    if market_yes < cheap_threshold:
        depth = (cheap_threshold - market_yes) / cheap_threshold
        edge *= 1.0 + (otm_base_mult - 1.0) * depth
    elif market_yes > expensive_threshold:
        depth = (market_yes - expensive_threshold) / (1.0 - expensive_threshold)
        edge *= 1.0 + (otm_base_mult - 1.0) * depth

    trend_opposed = False
    if direction == "above" and momentum_ret < -0.005:
        trend_opposed = True
    elif direction == "below" and momentum_ret > 0.005:
        trend_opposed = True

    if current_price > 0 and threshold > 0:
        strike_dist = abs(current_price - threshold) / current_price
        if strike_dist > 0.05:
            edge *= 1.0 + strike_dist * 2.0

    if trend_opposed:
        edge *= trend_opposed_mult

    return edge


# ---------------------------------------------------------------------------
# Layer 5: Regime Detection Filter (EMA Gate)
# ---------------------------------------------------------------------------

def compute_ema(closes: list[float], period: int) -> float | None:
    """EMA of the last `period` prices. Returns None if insufficient data."""
    if not closes or len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = float(closes[0])
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def regime_filter(
    closes: list[float],
    current_price: float,
    direction: str,
    rec: str,
    ema_period: int = 20,
) -> bool:
    """
    Return True if the trade should be BLOCKED by the regime filter.

    Logic:
      - If asset is BELOW its EMA and rec is BUY YES on an "above" contract → block
      - If asset is ABOVE its EMA and rec is BUY YES on a "below" contract → block
      - BUY NO is not blocked (it's trend-aligned in those scenarios)

    This creates an AND gate: (Model says Buy) AND (Trend is not opposed).
    """
    if rec != "BUY YES":
        return False
    ema = compute_ema(closes, ema_period)
    if ema is None:
        return False
    if direction == "above" and current_price < ema:
        return True
    if direction == "below" and current_price > ema:
        return True
    return False


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def apply_anti_hallucination_pipeline(
    *,
    model_p: float,
    market_p: float,
    closes: list[float],
    threshold: float,
    direction: str,
    current_price: float,
    momentum_ret: float,
    cfg: AntiHallucinationConfig,
) -> float:
    """
    Run all probability-adjustment layers and return the corrected P(YES).

    This is the main entry point. Call after computing the raw model
    probability (empirical/vol blend) and before feeding into the
    recommendation engine.
    """
    p = float(model_p)

    if cfg.ewma_prob_enabled:
        ewma_p = ewma_probability(
            closes, threshold, direction,
            span=cfg.ewma_prob_span,
            min_samples=cfg.ewma_prob_min_samples,
        )
        if ewma_p is not None:
            p = 0.5 * p + 0.5 * ewma_p

    if cfg.vol_zscore_enabled:
        z = volatility_zscore(
            closes,
            short_window=cfg.vol_zscore_short_window,
            long_window=cfg.vol_zscore_long_window,
        )
        p = apply_vol_zscore_dampening(
            p, z,
            threshold_z=cfg.vol_zscore_threshold,
            tail_mult=cfg.vol_zscore_tail_mult,
        )

    if cfg.bayesian_enabled:
        p = bayesian_update(p, market_p, cfg.bayesian_market_strength)

    return max(0.01, min(0.99, p))
