from __future__ import annotations

from dataclasses import dataclass

from strategies.default_threshold import DefaultThresholdStrategy


@dataclass
class VolRegimeStrategy(DefaultThresholdStrategy):
    """
    Regime-aware strategy facade.

    This currently reuses threshold+fee+easy-win logic and is intended to be the
    production default while volatility-regime controls stay centralized in
    compare_historical (where market context is available).
    """

