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
  5. Regime Filter      – EMA alignment for BUY YES; BUY NO blocked on momentum
                          (price > short EMA + RSI); optional strong-trend routing
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
    
    # --- Linear Market Blend ---
    market_blend_enabled: bool = True
    market_blend_model_weight: float = 0.25 # Weight given to model (0.75 to market)

    # --- EWMA probability ---
    ewma_prob_enabled: bool = True
    ewma_prob_span: int = 14
    ewma_prob_min_samples: int = 10
    ewma_prob_blend_weight: float = 0.30

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
    adaptive_edge_otm_base_mult: float = 1.8
    adaptive_edge_trend_opposed_mult: float = 1.5
    adaptive_edge_strike_dist_mult: float = 1.2

    # --- Regime filter (EMA gate) ---
    regime_filter_enabled: bool = True
    regime_ema_period: int = 20
    regime_require_alignment: bool = True
    # Faster EMA + multi-factor trend score (level + slope + recent return)
    regime_ema_fast_period: int = 10
    trend_score_w_level: float = 1.0
    trend_score_w_slope: float = 0.8
    trend_score_w_ret: float = 0.5
    trend_score_up_threshold: float = 0.008
    trend_score_down_threshold: float = 0.008
    # Hard momentum gate for BUY NO (price above short EMA + elevated RSI)
    buy_no_momentum_gate_enabled: bool = True
    buy_no_momentum_ema_period: int = 10
    buy_no_momentum_rsi_period: int = 14
    buy_no_momentum_rsi_min: float = 60.0
    # Strong-trend routing (optional strict block lists)
    regime_routing_enabled: bool = True
    regime_block_buy_no_in_uptrend: bool = True
    regime_block_buy_yes_in_downtrend: bool = False


def load_anti_hallucination_config(strategy: dict) -> AntiHallucinationConfig:
    ah = strategy.get("anti_hallucination", {})
    return AntiHallucinationConfig(
        bayesian_enabled=bool(ah.get("bayesian_enabled", True)),
        bayesian_market_strength=float(ah.get("bayesian_market_strength", 2.0)),
        ewma_prob_enabled=bool(ah.get("ewma_prob_enabled", True)),
        ewma_prob_span=int(ah.get("ewma_prob_span", 14)),
        ewma_prob_min_samples=int(ah.get("ewma_prob_min_samples", 10)),
        ewma_prob_blend_weight=float(ah.get("ewma_prob_blend_weight", 0.30)),
        vol_zscore_enabled=bool(ah.get("vol_zscore_enabled", True)),
        vol_zscore_short_window=int(ah.get("vol_zscore_short_window", 24)),
        vol_zscore_long_window=int(ah.get("vol_zscore_long_window", 168)),
        vol_zscore_threshold=float(ah.get("vol_zscore_threshold", 2.0)),
        vol_zscore_tail_mult=float(ah.get("vol_zscore_tail_mult", 0.5)),
        adaptive_edge_enabled=bool(ah.get("adaptive_edge_enabled", True)),
        adaptive_edge_cheap_threshold=float(ah.get("adaptive_edge_cheap_threshold", 0.15)),
        adaptive_edge_expensive_threshold=float(ah.get("adaptive_edge_expensive_threshold", 0.85)),
        adaptive_edge_otm_base_mult=float(ah.get("adaptive_edge_otm_base_mult", 1.8)),
        adaptive_edge_trend_opposed_mult=float(ah.get("adaptive_edge_trend_opposed_mult", 1.5)),
        adaptive_edge_strike_dist_mult=float(ah.get("adaptive_edge_strike_dist_mult", 1.2)),
        regime_filter_enabled=bool(ah.get("regime_filter_enabled", True)),
        regime_ema_period=int(ah.get("regime_ema_period", 20)),
        regime_require_alignment=bool(ah.get("regime_require_alignment", True)),
        regime_ema_fast_period=int(ah.get("regime_ema_fast_period", 10)),
        trend_score_w_level=float(ah.get("trend_score_w_level", 1.0)),
        trend_score_w_slope=float(ah.get("trend_score_w_slope", 0.8)),
        trend_score_w_ret=float(ah.get("trend_score_w_ret", 0.5)),
        trend_score_up_threshold=float(ah.get("trend_score_up_threshold", 0.008)),
        trend_score_down_threshold=float(ah.get("trend_score_down_threshold", 0.008)),
        buy_no_momentum_gate_enabled=bool(ah.get("buy_no_momentum_gate_enabled", True)),
        buy_no_momentum_ema_period=int(ah.get("buy_no_momentum_ema_period", 10)),
        buy_no_momentum_rsi_period=int(ah.get("buy_no_momentum_rsi_period", 14)),
        buy_no_momentum_rsi_min=float(ah.get("buy_no_momentum_rsi_min", 60.0)),
        regime_routing_enabled=bool(ah.get("regime_routing_enabled", True)),
        regime_block_buy_no_in_uptrend=bool(ah.get("regime_block_buy_no_in_uptrend", True)),
        regime_block_buy_yes_in_downtrend=bool(ah.get("regime_block_buy_yes_in_downtrend", False)),
        market_blend_enabled=bool(ah.get("market_blend_enabled", True)),
        market_blend_model_weight=float(ah.get("market_blend_model_weight", 0.25)),
    )


# ---------------------------------------------------------------------------
# Layer 1: Bayesian Probability Update
# ---------------------------------------------------------------------------

def bayesian_update(
    model_p: float,
    market_p: float,
    market_strength: float = 2.0,
    liquidity_consistency: float = 1.0,
) -> float:
    """
    Bayesian update treating market price as a Beta-distributed prior and
    the model prediction as new evidence.

    market_strength controls how much weight the market prior gets.
    Higher values = more trust in the market.  At strength=2 the market
    acts like having observed 2 pseudo-samples.

    Now scales strength with liquidity_consistency: if the market is noisy
    (low consistency), we reduce its strength as a prior.
    """
    model_p = max(1e-6, min(1 - 1e-6, float(model_p)))
    market_p = max(1e-6, min(1 - 1e-6, float(market_p)))
    
    # Scale strength by consistency to avoid market 'crushing' model in illiquid tails
    strength = max(0.1, float(market_strength) * float(liquidity_consistency))

    # Rule 3: Trust market more at extremes (80%+ or <20%)
    if market_p > 0.80 or market_p < 0.20:
        strength *= 1.5

    alpha_prior = market_p * strength

    beta_prior = (1 - market_p) * strength

    alpha_post = alpha_prior + model_p
    beta_post = beta_prior + (1 - model_p)

    posterior = alpha_post / (alpha_post + beta_post)
    return max(1e-6, min(1 - 1e-6, posterior))

def market_linear_blend(
    model_p: float,
    market_p: float,
    model_weight: float = 0.25,
) -> float:
    """
    Linear blend between market probability and model probability.
    
    If model_weight = 0.25, the result is 75% market and 25% model.
    This anchors predictions to market reality while allowing the model 
    to 'tilt' the probability in the direction of its edge.
    """
    m_p = max(0.001, min(0.999, float(market_p)))
    p_p = max(0.001, min(0.999, float(model_p)))
    w = max(0.0, min(1.0, float(model_weight)))
    
    blended = (1 - w) * m_p + w * p_p
    return max(1e-6, min(1 - 1e-6, blended))

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
    otm_base_mult: float = 1.8,
    trend_opposed_mult: float = 1.5,
    strike_dist_mult: float = 1.2,
) -> float:
    """
    Dynamic edge requirement that scales with contract price and trend.

    For cheap contracts (< 15¢): require extra edge (scaled by
    otm_base_mult) because tail risk of a vol-spike sweep is high.

    For expensive contracts (> 85¢): also require more edge because
    you risk the full price for a small payout.

    When the spot price is trending AWAY from your strike (buying YES
    on "above" while price is falling), increase edge by
    trend_opposed_mult to avoid catching falling knives.
    """
    edge = float(base_edge)
    market_yes = max(0.01, min(0.99, float(market_yes)))

    if current_price < 0.02 or market_yes < 0.02:
        # Tight filter: require 0.35 net profit relative to $1.00 contract ($0.35/contract)
        # to ensure execution slippage/fees don't destroy the edge.
        edge = max(edge, 0.35)

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
            edge *= 1.0 + strike_dist * strike_dist_mult

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


def compute_rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    """Wilder-style RSI on last `period` closes (same construction as compare_historical._compute_rsi)."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss < 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def classify_trend_regime(
    closes: list[float],
    current_price: float,
    cfg: AntiHallucinationConfig,
) -> str:
    """
    Classify underlying trend as 'up', 'down', or 'neutral' using
    EMA level gap, fast-vs-slow EMA slope, and a short lookback return.
    """
    slow_p = max(2, int(cfg.regime_ema_period))
    fast_p = max(2, min(int(cfg.regime_ema_fast_period), slow_p - 1)) if slow_p > 3 else 2
    ema_s = compute_ema(closes, slow_p)
    ema_f = compute_ema(closes, fast_p)
    if ema_s is None or ema_f is None or ema_s <= 0:
        return "neutral"
    level = (float(current_price) - ema_s) / ema_s
    slope = (ema_f - ema_s) / ema_s
    ret_n = min(5, len(closes) - 1)
    rret = (
        (float(current_price) / float(closes[-1 - ret_n]) - 1.0)
        if ret_n > 0 and closes[-1 - ret_n] > 0
        else 0.0
    )
    w1, w2, w3 = cfg.trend_score_w_level, cfg.trend_score_w_slope, cfg.trend_score_w_ret
    score = w1 * level + w2 * slope + w3 * rret
    if score > float(cfg.trend_score_up_threshold):
        return "up"
    if score < -float(cfg.trend_score_down_threshold):
        return "down"
    return "neutral"


def directional_regime_should_skip(
    rec: str,
    direction: str,
    closes: list[float],
    current_price: float,
    cfg: AntiHallucinationConfig,
) -> tuple[bool, str]:
    """
    Post-recommendation gate: True → skip trade (counter-trend / momentum hazard).

    Order: BUY NO momentum (RSI + EMA), strong-trend routing, legacy BUY YES EMA alignment.
    """
    if not cfg.regime_filter_enabled:
        return False, ""
    if rec not in ("BUY YES", "BUY NO"):
        return False, ""

    regime = classify_trend_regime(closes, current_price, cfg)

    if rec == "BUY NO" and cfg.buy_no_momentum_gate_enabled:
        ema_gate = compute_ema(closes, max(2, int(cfg.buy_no_momentum_ema_period)))
        rsi = compute_rsi_wilder(closes, max(2, int(cfg.buy_no_momentum_rsi_period)))
        if (
            ema_gate is not None
            and rsi is not None
            and float(current_price) > ema_gate
            and rsi >= float(cfg.buy_no_momentum_rsi_min)
        ):
            return True, "buy_no_momentum_gate"

    if cfg.regime_routing_enabled:
        if regime == "up" and rec == "BUY NO" and cfg.regime_block_buy_no_in_uptrend:
            return True, "uptrend_block_buy_no"
        if regime == "down" and rec == "BUY YES" and cfg.regime_block_buy_yes_in_downtrend:
            return True, "downtrend_block_buy_yes"

    if cfg.regime_require_alignment and rec == "BUY YES":
        if regime_filter(closes, current_price, direction, rec, ema_period=cfg.regime_ema_period):
            return True, "ema_block_buy_yes_countertrend"

    return False, ""


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
    liquidity_consistency: float = 1.0,
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
            w = cfg.ewma_prob_blend_weight
            p = (1 - w) * p + w * ewma_p

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
        p = bayesian_update(p, market_p, cfg.bayesian_market_strength, liquidity_consistency=liquidity_consistency)

    if cfg.market_blend_enabled:
        p = market_linear_blend(p, market_p, model_weight=cfg.market_blend_model_weight)

    return max(1e-6, min(1 - 1e-6, p))

