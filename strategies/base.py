from __future__ import annotations

from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """
    Minimal pluggable strategy interface (Freqtrade-style direction).

    Current compare_historical flow can call implementations of this interface
    for recommendation decisions while keeping execution logic unchanged.
    """

    @abstractmethod
    def recommend(
        self,
        *,
        p_yes: float,
        market_yes: float,
        min_edge: float,
        maker_fee_rate: float,
        min_fee_per_order: float,
        min_edge_yes: float | None = None,
        min_edge_no: float | None = None,
        min_edge_easy_win: float | None = None,
        easy_win_min_p_win: float = 0.90,
        easy_win_min_market_p: float = 0.85,
    ) -> str:
        """Return one of: BUY YES, BUY NO, SKIP for normal markets."""
        raise NotImplementedError

    @abstractmethod
    def recommend_strict(
        self,
        *,
        p_yes: float,
        market_yes: float,
        min_p_win: float,
        min_edge: float,
        maker_fee_rate: float,
        min_fee_per_order: float,
        min_edge_yes: float | None = None,
        min_edge_no: float | None = None,
    ) -> str:
        """Return one of: BUY YES, BUY NO, SKIP for strict intraday markets."""
        raise NotImplementedError

    @abstractmethod
    def build_signal_features(
        self,
        *,
        emp_p: float,
        vol_p: float | None,
        market_yes: float,
        threshold: float,
        current_price: float,
        dte_days: float | None,
        spread_bps: float | None,
        no_bid: float | None,
        macro_24h_ret: float = 0.0,
    ) -> dict[str, float]:
        """Build normalized signal features for scoring."""
        raise NotImplementedError

    @abstractmethod
    def score_signal_probability(
        self,
        *,
        features: dict[str, float],
        signal_model_weights: dict[str, float] | None,
    ) -> float:
        """Return P(YES) from signal model features."""
        raise NotImplementedError

    @abstractmethod
    def apply_bucket_penalty(
        self,
        *,
        hist_p: float,
        calibration: dict,
        use_bucket_penalty: bool,
        use_calibration: bool,
        min_bucket_samples: int,
    ) -> float:
        """Apply calibration bucket penalty to probability."""
        raise NotImplementedError

    @abstractmethod
    def compute_p_lose(
        self,
        *,
        hist_p: float,
        yes_bid: float,
        no_bid: float,
        side: str,
        days_to_expiry: float | None,
        p_lose_blend_market_max_weight: float,
        p_lose_blend_market_half_life_days: float,
    ) -> float:
        """Compute probability current position loses."""
        raise NotImplementedError
