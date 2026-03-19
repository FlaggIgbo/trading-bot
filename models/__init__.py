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
]
