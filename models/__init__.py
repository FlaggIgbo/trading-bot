"""
Models layer: ML strategies, signal scoring, and calibration.

Contains the COMPUTE layer of the ML pipeline — takes features derived from
signals and produces trade recommendations (BUY YES / BUY NO / SKIP).
"""
from models.strategies import BaseStrategy, DefaultThresholdStrategy, VolRegimeStrategy
from models.anti_hallucination import (
    AntiHallucinationConfig,
    adaptive_min_edge,
    apply_anti_hallucination_pipeline,
    apply_vol_zscore_dampening,
    bayesian_update,
    compute_ema,
    ewma_probability,
    load_anti_hallucination_config,
    regime_filter,
    volatility_zscore,
)
from models.alpha_engine import (
    AlphaEngineConfig,
    compute_markout_bps,
    compute_obi,
    is_ticker_toxic,
    lead_lag_edge_adjustment,
    lead_lag_signal,
    load_alpha_engine_config,
    obi_edge_adjustment,
    toxic_flow_edge_penalty,
    vol_adjusted_kelly,
)

__all__ = [
    "BaseStrategy",
    "DefaultThresholdStrategy",
    "VolRegimeStrategy",
    "AntiHallucinationConfig",
    "adaptive_min_edge",
    "apply_anti_hallucination_pipeline",
    "apply_vol_zscore_dampening",
    "bayesian_update",
    "compute_ema",
    "ewma_probability",
    "load_anti_hallucination_config",
    "regime_filter",
    "volatility_zscore",
    "AlphaEngineConfig",
    "compute_markout_bps",
    "compute_obi",
    "is_ticker_toxic",
    "lead_lag_edge_adjustment",
    "lead_lag_signal",
    "load_alpha_engine_config",
    "obi_edge_adjustment",
    "toxic_flow_edge_penalty",
    "vol_adjusted_kelly",
]
