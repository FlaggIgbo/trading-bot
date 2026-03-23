from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.strategies.default_threshold import DefaultThresholdStrategy
from models.trading_edges import vol_regime_edge_dip_size_mults


@dataclass
class VolRegimeStrategy(DefaultThresholdStrategy):
    """
    Regime-aware strategy facade.

    BUY/SELL threshold logic matches DefaultThresholdStrategy. Volatility-regime
    *scaling* (edge / dip / size multipliers from short-vs-long vol) lives here and
    is invoked from compare_historical once per series so behavior is traceable to
    this class rather than a loose helper.
    """

    @classmethod
    def volatility_regime_mults(cls, sp: Any, vol_ratio: float) -> tuple[float, float, float]:
        """(edge_mult, dip_mult, size_mult) from config + realized vol ratio."""
        return vol_regime_edge_dip_size_mults(
            enabled=bool(getattr(sp, "enable_vol_regime_scaling", False)),
            vol_ratio=float(vol_ratio),
            edge_mult_per_x=float(getattr(sp, "vol_regime_edge_mult_per_x", 0.35)),
            max_edge_mult=float(getattr(sp, "vol_regime_max_edge_mult", 2.5)),
            dip_mult_per_x=float(getattr(sp, "vol_regime_dip_mult_per_x", 0.5)),
            max_dip_mult=float(getattr(sp, "vol_regime_max_dip_mult", 2.0)),
            size_mult_per_x=float(getattr(sp, "vol_regime_size_mult_per_x", 0.5)),
            min_size_mult=float(getattr(sp, "vol_regime_min_size_mult", 0.35)),
        )

