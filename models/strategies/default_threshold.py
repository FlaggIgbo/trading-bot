from __future__ import annotations

from dataclasses import dataclass
import math

from constants import REC_BUY_NO, REC_BUY_YES, REC_SKIP
from models.strategies.base import BaseStrategy


@dataclass
class DefaultThresholdStrategy(BaseStrategy):
    """
    Lightweight pluggable strategy scaffold.

    This mirrors the core threshold behavior used in compare_historical and is
    intended as a modular starting point for A/B strategy tests.
    """

    @staticmethod
    def _fee_per_contract(maker_fee_rate: float, p_dollars: float, min_fee_per_order: float) -> float:
        p = max(0.0, min(0.99, float(p_dollars)))
        rounded = (int((maker_fee_rate * p * (1.0 - p) + 0.009999) * 100.0) / 100.0)
        return max(rounded, max(0.0, float(min_fee_per_order)))

    @staticmethod
    def _clip01(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

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
        if market_yes <= 0:
            return REC_SKIP
        market_yes = max(0.0, min(0.99, float(market_yes)))
        market_no = 1.0 - market_yes
        fee_yes = self._fee_per_contract(maker_fee_rate, market_yes, min_fee_per_order)
        fee_no = self._fee_per_contract(maker_fee_rate, market_no, min_fee_per_order)
        edge_yes = float(p_yes) - market_yes - fee_yes
        edge_no = (1.0 - float(p_yes)) - market_no - fee_no
        req_yes = float(min_edge_yes) if min_edge_yes is not None else float(min_edge)
        req_no = float(min_edge_no) if min_edge_no is not None else float(min_edge)
        if (
            min_edge_easy_win is not None
            and float(p_yes) >= float(easy_win_min_p_win)
            and market_yes >= float(easy_win_min_market_p)
        ):
            req_yes = min(req_yes, float(min_edge_easy_win))
        if (
            min_edge_easy_win is not None
            and (1.0 - float(p_yes)) >= float(easy_win_min_p_win)
            and market_no >= float(easy_win_min_market_p)
        ):
            req_no = min(req_no, float(min_edge_easy_win))
        if consensus_sniper_enabled and consensus_sniper_min_edge is not None:
            gap = float(barbell_base_max_model_vs_market_gap)
            yb = float(market_yes)
            hp = float(p_yes)
            if (
                yb >= float(barbell_base_min_market_yes)
                and hp >= float(barbell_base_min_model_p)
                and hp + gap >= yb
                and yb <= float(barbell_base_max_yes_price)
            ):
                req_yes = min(req_yes, float(consensus_sniper_min_edge))
            nb = float(market_no)
            p_no = 1.0 - hp
            if (
                nb >= float(barbell_base_min_market_yes)
                and p_no >= float(barbell_base_min_model_p)
                and p_no + gap >= nb
                and nb <= float(barbell_base_max_no_price)
            ):
                req_no = min(req_no, float(consensus_sniper_min_edge))
        has_yes = edge_yes >= req_yes
        has_no = edge_no >= req_no
        if has_yes and has_no:
            return REC_BUY_YES if edge_yes >= edge_no else REC_BUY_NO
        if has_yes:
            return REC_BUY_YES
        if has_no:
            return REC_BUY_NO
        return REC_SKIP

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
        market_yes = max(0.0, min(0.99, float(market_yes)))
        market_no = 1.0 - market_yes
        fee_yes = self._fee_per_contract(maker_fee_rate, market_yes, min_fee_per_order)
        fee_no = self._fee_per_contract(maker_fee_rate, market_no, min_fee_per_order)
        edge_yes = float(p_yes) - market_yes - fee_yes
        edge_no = (1.0 - float(p_yes)) - market_no - fee_no
        req_yes = float(min_edge_yes) if min_edge_yes is not None else float(min_edge)
        req_no = float(min_edge_no) if min_edge_no is not None else float(min_edge)
        if (
            min_edge_easy_win is not None
            and float(p_yes) >= float(easy_win_min_p_win)
            and market_yes >= float(easy_win_min_market_p)
        ):
            req_yes = min(req_yes, float(min_edge_easy_win))
        if (
            min_edge_easy_win is not None
            and (1.0 - float(p_yes)) >= float(easy_win_min_p_win)
            and market_no >= float(easy_win_min_market_p)
        ):
            req_no = min(req_no, float(min_edge_easy_win))
        if consensus_sniper_enabled and consensus_sniper_min_edge is not None:
            gap = float(barbell_base_max_model_vs_market_gap)
            yb = float(market_yes)
            hp = float(p_yes)
            if (
                yb >= float(barbell_base_min_market_yes)
                and hp >= float(barbell_base_min_model_p)
                and hp + gap >= yb
                and yb <= float(barbell_base_max_yes_price)
            ):
                req_yes = min(req_yes, float(consensus_sniper_min_edge))
            nb = float(market_no)
            p_no = 1.0 - hp
            if (
                nb >= float(barbell_base_min_market_yes)
                and p_no >= float(barbell_base_min_model_p)
                and p_no + gap >= nb
                and nb <= float(barbell_base_max_no_price)
            ):
                req_no = min(req_no, float(consensus_sniper_min_edge))
        req_p_yes = float(min_p_win)
        req_p_no = float(min_p_win)
        if barbell_strict_relaxed_min_p_win is not None:
            relax = float(barbell_strict_relaxed_min_p_win)
            gap = float(barbell_base_max_model_vs_market_gap)
            hp = float(p_yes)
            yb = float(market_yes)
            if (
                yb >= float(barbell_base_min_market_yes)
                and hp >= float(barbell_base_min_model_p)
                and hp + gap >= yb
                and yb <= float(barbell_base_max_yes_price)
            ):
                req_p_yes = min(req_p_yes, relax)
            nb = float(market_no)
            p_no = 1.0 - hp
            if (
                nb >= float(barbell_base_min_market_yes)
                and p_no >= float(barbell_base_min_model_p)
                and p_no + gap >= nb
                and nb <= float(barbell_base_max_no_price)
            ):
                req_p_no = min(req_p_no, relax)
        yes_ok = float(p_yes) >= req_p_yes and edge_yes >= req_yes
        no_ok = (1.0 - float(p_yes)) >= req_p_no and edge_no >= req_no
        if yes_ok and no_ok:
            return REC_BUY_YES if edge_yes >= edge_no else REC_BUY_NO
        if yes_ok:
            return REC_BUY_YES
        if no_ok:
            return REC_BUY_NO
        return REC_SKIP

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
        rsi_14: float | None = None,
        price_history: list[float] | None = None,
    ) -> dict[str, float]:
        vol = self._clip01(vol_p if vol_p is not None else emp_p)
        mkt = self._clip01(market_yes)
        edge_yes = emp_p - mkt
        edge_no = (1.0 - emp_p) - (1.0 - mkt)
        if threshold > 0 and current_price > 0:
            moneyness = math.log(max(1e-9, current_price / threshold))
        else:
            moneyness = 0.0
        dte_norm = self._clip01((float(dte_days or 0.0)) / 30.0)
        spread_norm = self._clip01((float(spread_bps or 0.0)) / 10_000.0)
        consistency = 1.0
        if no_bid is not None:
            consistency = 1.0 - abs((mkt + self._clip01(float(no_bid))) - 1.0)
        rsi_norm = self._clip01((float(rsi_14) / 100.0) if rsi_14 is not None else 0.5)

        # Market-relative features (Alpha)
        vwap_dist = 0.0
        price_trend = 0.0
        if price_history and len(price_history) > 0:
            # Pseudo-VWAP (SMA of last 20)
            lookback = min(20, len(price_history))
            sma_20 = sum(price_history[-lookback:]) / lookback
            vwap_dist = (current_price - sma_20) / sma_20 if sma_20 > 0 else 0.0
            # Trend (current vs SMA of last 10)
            lookback_trend = min(10, len(price_history))
            sma_10 = sum(price_history[-lookback_trend:]) / lookback_trend
            price_trend = (current_price - sma_10) / sma_10 if sma_10 > 0 else 0.0

        return {
            "emp_p": self._clip01(emp_p),
            "vol_p": vol,
            "market_yes": mkt,
            "edge_yes": max(-1.0, min(1.0, edge_yes)),
            "edge_no": max(-1.0, min(1.0, edge_no)),
            "moneyness": max(-2.0, min(2.0, moneyness)),
            "dte_norm": dte_norm,
            "spread_norm": spread_norm,
            "liquidity_consistency": self._clip01(consistency),
            "macro_24h_ret": max(-0.1, min(0.1, float(macro_24h_ret))),
            "rsi_norm": rsi_norm,
            "vwap_dist": max(-0.2, min(0.2, vwap_dist)),
            "price_trend": max(-0.1, min(0.1, price_trend)),
        }

    def score_signal_probability(
        self,
        *,
        features: dict[str, float],
        signal_model_weights: dict[str, float] | None,
        ensemble_weights: list[dict[str, float]] | None = None,
        market_relative: bool = False,
    ) -> float:
        """
        Return P(YES) from signal model features.
        If market_relative=True, we model the residual (edge) instead of absolute probability.
        """
        def _score_one(w_dict: dict[str, float]) -> float:
            # Baseline weights for absolute probability
            base_w = {
                "bias": 0.0,
                "emp_p": 1.5,
                "vol_p": 1.0,
                "market_yes": 0.5,
                "edge_yes": 1.0,
                "edge_no": -0.5,
                "moneyness": 0.3,
                "dte_norm": 0.2,
                "spread_norm": -0.8,
                "liquidity_consistency": 0.5,
                "macro_24h_ret": 0.2,
                "rsi_norm": -0.5,
                "vwap_dist": 0.5,
                "price_trend": 0.3,
            }
            base_w.update(w_dict)
            
            if market_relative:
                # EDGE MODEL: target = outcome - market_prob
                # features should ideally be signals why market is wrong
                z = sum(float(base_w.get(k, 0.0)) * float(features.get(k, 0.0)) for k in base_w.keys() if k != "bias")
                z += float(base_w.get("bias", 0.0))
                # z here is the predicted 'edge residual'. 
                # We blend it back with market_yes to get a probability.
                mkt = float(features.get("market_yes", 0.5))
                # Simple sigmoid-like mapping of residual to prob shift
                edge_shift = 1.0 / (1.0 + math.exp(-z)) - 0.5
                return self._clip01(mkt + edge_shift)
            else:
                # PROBABILITY MODEL: target = outcome
                z = sum(float(base_w.get(k, 0.0)) * float(features.get(k, 0.0)) for k in base_w.keys() if k != "bias")
                z += float(base_w.get("bias", 0.0))
                return self._clip01(1.0 / (1.0 + math.exp(-z)))

        # Ensemble scoring
        if ensemble_weights and len(ensemble_weights) > 0:
            scores = [_score_one(ew) for ew in ensemble_weights]
            return sum(scores) / len(scores)
        
        return _score_one(signal_model_weights or {})

    def apply_bucket_penalty(
        self,
        *,
        hist_p: float,
        calibration: dict,
        use_bucket_penalty: bool,
        use_calibration: bool,
        min_bucket_samples: int,
        bucket_override_weight: float = 0.0,
    ) -> float:
        if not use_bucket_penalty or not use_calibration or not calibration:
            return float(hist_p)
        buckets = calibration.get("buckets", [])
        if not buckets:
            return float(hist_p)
        lo = round(float(hist_p) * 10) / 10
        hi = lo + 0.1
        midpoint = (lo + hi) / 2
        for b in buckets:
            if b.get("lo") == lo and b.get("hi") == hi:
                cnt = int(b.get("count", 0) or 0)
                wr = float(b.get("win_rate", midpoint) or midpoint)
                if cnt >= int(min_bucket_samples):
                    if bucket_override_weight > 0:
                        # EMPIRICAL OVERRIDE: model -> empirical reality -> override model
                        # Blend model prob with historical WR. weight=1.0 means full override.
                        w = min(1.0, max(0.0, bucket_override_weight))
                        # Pull hist_p towards historical WR observed in this bucket
                        new_p = (1.0 - w) * float(hist_p) + w * wr
                        return max(0.01, min(0.99, new_p))
                    elif wr < midpoint:
                        # Legacy penalty path
                        scale = wr / midpoint if midpoint > 0 else 1.0
                        return max(0.01, min(0.99, float(hist_p) * scale))
                break
        return float(hist_p)

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
        p_model = float(hist_p) if side == "no" else (1.0 - float(hist_p))
        p_market = float(yes_bid) if side == "no" else float(no_bid)
        max_w = float(p_lose_blend_market_max_weight)
        if max_w <= 0 or not (0 < float(yes_bid) < 1):
            return p_model
        dte = max(0.01, float(days_to_expiry)) if days_to_expiry is not None else 1.0
        half = max(0.01, float(p_lose_blend_market_half_life_days))
        consistency = 1.0 - abs((float(yes_bid) + float(no_bid)) - 1.0)
        consistency = max(0.0, min(1.0, consistency))
        weight_market = max_w * consistency * (1.0 / (1.0 + dte / half))
        return (1.0 - weight_market) * p_model + weight_market * p_market
