"""
Stricter threshold strategy for A/B tests vs default_threshold / vol_regime.

Raises required edge by a configurable factor so multi-strategy compare can
surface real disagreements (vol_regime and default_threshold are currently identical).
"""
from __future__ import annotations

from dataclasses import dataclass

from models.strategies.default_threshold import DefaultThresholdStrategy


@dataclass
class StrictThresholdStrategy(DefaultThresholdStrategy):
    """Same as DefaultThresholdStrategy but requires ~25% higher edge to trade."""

    edge_scale: float = 1.25

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
        consensus_sniper_enabled: bool = False,
        consensus_sniper_min_edge: float | None = None,
        barbell_base_min_market_yes: float = 0.70,
        barbell_base_min_model_p: float = 0.78,
        barbell_base_max_model_vs_market_gap: float = 0.10,
        barbell_base_max_yes_price: float = 0.92,
        barbell_base_max_no_price: float = 0.92,
    ) -> str:
        s = max(1.0, float(self.edge_scale))
        if consensus_sniper_min_edge is None:
            cs = None
        elif consensus_sniper_min_edge < 0:
            # Negative = max allowed fee drag; stricter engine pulls toward 0 (needs more net edge).
            cs = consensus_sniper_min_edge / s
        else:
            cs = consensus_sniper_min_edge * s
        return super().recommend(
            p_yes=p_yes,
            market_yes=market_yes,
            min_edge=min_edge * s,
            maker_fee_rate=maker_fee_rate,
            min_fee_per_order=min_fee_per_order,
            min_edge_yes=(min_edge_yes * s) if min_edge_yes is not None else None,
            min_edge_no=(min_edge_no * s) if min_edge_no is not None else None,
            min_edge_easy_win=(min_edge_easy_win * s) if min_edge_easy_win is not None else None,
            easy_win_min_p_win=easy_win_min_p_win,
            easy_win_min_market_p=easy_win_min_market_p,
            consensus_sniper_enabled=consensus_sniper_enabled,
            consensus_sniper_min_edge=cs,
            barbell_base_min_market_yes=barbell_base_min_market_yes,
            barbell_base_min_model_p=barbell_base_min_model_p,
            barbell_base_max_model_vs_market_gap=barbell_base_max_model_vs_market_gap,
            barbell_base_max_yes_price=barbell_base_max_yes_price,
            barbell_base_max_no_price=barbell_base_max_no_price,
        )

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
        min_edge_easy_win: float | None = None,
        easy_win_min_p_win: float = 0.90,
        easy_win_min_market_p: float = 0.85,
        consensus_sniper_enabled: bool = False,
        consensus_sniper_min_edge: float | None = None,
        barbell_base_min_market_yes: float = 0.70,
        barbell_base_min_model_p: float = 0.78,
        barbell_base_max_model_vs_market_gap: float = 0.10,
        barbell_base_max_yes_price: float = 0.92,
        barbell_base_max_no_price: float = 0.92,
        barbell_strict_relaxed_min_p_win: float | None = None,
    ) -> str:
        s = max(1.0, float(self.edge_scale))
        if consensus_sniper_min_edge is None:
            cs = None
        elif consensus_sniper_min_edge < 0:
            cs = consensus_sniper_min_edge / s
        else:
            cs = consensus_sniper_min_edge * s
        return super().recommend_strict(
            p_yes=p_yes,
            market_yes=market_yes,
            min_p_win=min_p_win,
            min_edge=min_edge * s,
            maker_fee_rate=maker_fee_rate,
            min_fee_per_order=min_fee_per_order,
            min_edge_yes=(min_edge_yes * s) if min_edge_yes is not None else None,
            min_edge_no=(min_edge_no * s) if min_edge_no is not None else None,
            min_edge_easy_win=(min_edge_easy_win * s) if min_edge_easy_win is not None else None,
            easy_win_min_p_win=easy_win_min_p_win,
            easy_win_min_market_p=easy_win_min_market_p,
            consensus_sniper_enabled=consensus_sniper_enabled,
            consensus_sniper_min_edge=cs,
            barbell_base_min_market_yes=barbell_base_min_market_yes,
            barbell_base_min_model_p=barbell_base_min_model_p,
            barbell_base_max_model_vs_market_gap=barbell_base_max_model_vs_market_gap,
            barbell_base_max_yes_price=barbell_base_max_yes_price,
            barbell_base_max_no_price=barbell_base_max_no_price,
            barbell_strict_relaxed_min_p_win=barbell_strict_relaxed_min_p_win,
        )
