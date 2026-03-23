from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.strategies.default_threshold import DefaultThresholdStrategy
from models.trading_edges import vol_regime_edge_dip_size_mults


@dataclass
class VolRegimeStrategy(DefaultThresholdStrategy):
    """
    Production default engine: same BUY YES / NO / SKIP math as DefaultThresholdStrategy.

    The name reflects *volatility-regime scaling elsewhere in the pipeline*, not a
    different recommend() formula. In compare_historical, per-series
    ``VolRegimeStrategy.volatility_regime_mults(sp, vol_ratio)`` supplies
    (edge_mult, dip_mult, size_mult) from realized vol; those feed execution sizing
    and (in legacy mode) stacked min_edge. Config aliases: ``vol_regime``,
    ``vol_regime_threshold`` (identical class).
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

