"""
Calibration and Trust Module: Analyzing realized vs predicted probabilities.

Enables the 'Learning Layer' to update strategy parameters (hurdles, confidence)
based on actual historical performance across probability buckets.
"""
from __future__ import annotations

import math
from typing import Any, Callable


def clip01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def fit_probability_calibrator(
    sp: Any,  # StrategyParams
    rows: list[dict],
) -> Callable[[float], float] | None:
    """
    Fit a lightweight sklearn calibrator on resolved predictions (hist_p -> outcome).
    Returns callable p->calibrated_p or None if unavailable/insufficient data.
    """
    try:
        from sklearn.isotonic import IsotonicRegression
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None

    try:
        train = [
            r for r in rows
            if r.get("hist_p") is not None and r.get("outcome") in ("win", "lose")
        ]
        if len(train) < max(10, sp.sklearn_calibration_min_samples):
            return None
        x = [float(r["hist_p"]) for r in train]
        y = [1 if r["outcome"] == "win" else 0 for r in train]
        method = (sp.sklearn_calibration_method or "isotonic").strip().lower()
        if method == "platt":
            lr = LogisticRegression(solver="lbfgs")
            x2 = [[v] for v in x]
            lr.fit(x2, y)

            def _calibrate(p: float) -> float:
                return clip01(float(lr.predict_proba([[float(p)]])[0][1]))

            return _calibrate
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(x, y)

        def _calibrate(p: float) -> float:
            return clip01(float(iso.predict([float(p)])[0]))

        return _calibrate
    except Exception:
        return None


class CalibrationEngine:
    def __init__(self, predictions: list[dict]):
        """
        predictions: List of dicts with 'hist_p', 'vol_p', 'side', 'outcome'.
        Outcome must be 'win' or 'lose' (or 'sold' treated as lose).
        """
        self.predictions = predictions

    def get_bucketed_calibration(self, n_bins: int = 10) -> list[dict[str, Any]]:
        """
        Groups predictions into N probability bins and computes win rate per bin.
        Returns a list of bin stats: {lo, hi, count, win_rate, expected_win_rate}.
        """
        if not self.predictions:
            return []

        bins = [[] for _ in range(n_bins)]
        for p in self.predictions:
            # Model P(Win)
            # If side is 'no', P(Win) = 1 - p_yes
            p_raw = float(p.get("hist_p") or 0.5)
            # EXCLUDE fallback noise (0.5 on Gemini usually means lost limit_orders context)
            if p_raw == 0.5:
                continue
                
            p_val = p_raw
            if p.get("side", "").lower() == "no":
                p_val = 1.0 - p_val
            
            bin_idx = min(n_bins - 1, int(p_val * n_bins))
            bins[bin_idx].append(p)

        stats = []
        for i in range(n_bins):
            lo = i / n_bins
            hi = (i + 1) / n_bins
            count = len(bins[i])
            if count == 0:
                continue
            
            wins = sum(1 for p in bins[i] if p.get("outcome") == "win")
            win_rate = wins / count
            expected_wr = sum((1.0 - float(p.get("hist_p") or 0.5) if p.get("side", "").lower() == "no" else float(p.get("hist_p") or 0.5)) for p in bins[i]) / count
            
            stats.append({
                "lo": lo,
                "hi": hi,
                "count": count,
                "win_rate": win_rate,
                "expected_win_rate": expected_wr,
                "calibration_gap": win_rate - expected_wr,
            })
        return stats

    def get_bucket_for_prob(self, p_win: float, n_bins: int = 10) -> dict[str, Any] | None:
        """Find the calibration stats for the bin containing p_win."""
        buckets = self.get_bucketed_calibration(n_bins)
        for b in buckets:
            if b['lo'] <= p_win <= b['hi']:
                return b
        return None

    def compute_trust_score(self, min_samples: int = 10) -> float:
        """
        Trust Score = Realized Edge / Predicted Edge.
        
        A value > 1.0 means the model is UNDER-confident (we win more than predicted).
        A value < 1.0 means the model is OVER-confident (hallucinating).
        
        This is the primary signal for adaptive 'edge' discounting.
        """
        total_p_win = 0.0
        total_realized_win = 0.0
        n = 0
        
        for p in self.predictions:
            p_raw = float(p.get("hist_p") or 0.5)
            if p_raw == 0.5:
                continue
            
            p_win = p_raw
            if p.get("side", "").lower() == "no":
                p_win = 1.0 - p_win
            
            total_p_win += p_win
            total_realized_win += 1.0 if p.get("outcome") == "win" else 0.0
            n += 1
            
        if n < min_samples or total_p_win <= 0:
            return 1.0
            
        score = total_realized_win / total_p_win
        return min(1.5, score)

    def compute_brier_score(self) -> float | None:
        """Mean Squared Error of predictions. 0.0 = perfect, 1.0 = total failure."""
        if not self.predictions:
            return None
        errors = []
        for p in self.predictions:
            p_raw = float(p.get("hist_p") or 0.5)
            if p_raw == 0.5:
                continue
                
            p_win = p_raw
            if p.get("side", "").lower() == "no":
                p_win = 1.0 - p_win
            outcome = 1.0 if p.get("outcome") == "win" else 0.0
            errors.append((p_win - outcome) ** 2)
        return sum(errors) / len(errors)

    def get_calibrated_probability(
        self, 
        p_win: float, 
        n_bins: int = 10, 
        min_bin_samples: int = 5,
        prior_weight: float = 1.0  # SUBSTANTIAL TUNING (was 5.0): trust actual data more to fix overconfidence
    ) -> float:
        """
        Maps a predicted win probability to its realized win rate using a 
        Bayesian-weighted smoothed blend.
        
        Formula: (sum_wins + prior_wins) / (count + prior_samples)
        where prior_wins = p_win * prior_weight.
        
        Ensures monotonicity by enforcing p_cal(i) >= p_cal(i-1).
        """
        p = max(0.0, min(1.0, float(p_win)))
        buckets = self.get_bucketed_calibration(n_bins)
        if not buckets:
            return p

        # 1. Calculate Bayesian-weighted WR for each bucket
        # We treat the model's p_win as the prior.
        cal_points = []
        for b in buckets:
            mid = (b["lo"] + b["hi"]) / 2.0
            # Bayesian blend: (actual_wins + prior_wins) / (actual_n + prior_n)
            # prior_wins = mid * prior_weight
            actual_n = float(b["count"])
            actual_wins = float(b["win_rate"]) * actual_n
            
            # Credibility-weighted win rate
            # As actual_n -> inf, blend -> actual_win_rate
            # As actual_n -> 0, blend -> mid (model's bucket prior)
            blend_wr = (actual_wins + (mid * prior_weight)) / (actual_n + prior_weight)
            cal_points.append({"p": mid, "wr": blend_wr})
            
        # 2. Enforce Monotonicity (Isotonic Smoothing)
        # If a higher bucket has lower WR than a lower bucket, we average them.
        # Simple Pool Adjacent Violators Algorithm (PAVA) lite
        for _ in range(len(cal_points)):
            changed = False
            for i in range(len(cal_points) - 1):
                if cal_points[i]["wr"] > cal_points[i+1]["wr"]:
                    # Violation of monotonicity: average the win rates
                    avg_wr = (cal_points[i]["wr"] + cal_points[i+1]["wr"]) / 2.0
                    cal_points[i]["wr"] = avg_wr
                    cal_points[i+1]["wr"] = avg_wr
                    changed = True
            if not changed:
                break

        # 3. Interpolate p in the smoothed monotonic curve
        # Find nearest points
        left = None
        right = None
        for i, pt in enumerate(cal_points):
            if pt["p"] <= p:
                left = pt
            if pt["p"] > p and right is None:
                right = pt
                break
        
        if left and right:
            t = (p - left["p"]) / (right["p"] - left["p"])
            return left["wr"] + t * (right["wr"] - left["wr"])
        elif left:
            # Extrapolate slightly or cap
            return left["wr"]
        elif right:
            return right["wr"]
        
        return p

    def get_series_trust_matrix(self) -> dict[str, float]:
        """Returns {series_prefix: trust_score}."""
        by_series: dict[str, list[dict]] = {}
        for p in self.predictions:
            ticker = p.get("ticker", "")
            prefix = ticker.split("-")[0] if "-" in ticker else ticker
            if prefix not in by_series:
                by_series[prefix] = []
            by_series[prefix].append(p)
            
        return {
            prefix: CalibrationEngine(preds).compute_trust_score(min_samples=5)
            for prefix, preds in by_series.items()
        }

    def get_reliability_curve(self, n_bins: int = 10) -> dict[str, list[float]]:
        """
        Calculates coordinates for a reliability diagram (calibration curve).
        Returns {'expected': [avg_p_in_bin], 'actual': [realized_win_rate_in_bin]}.
        """
        buckets = self.get_bucketed_calibration(n_bins)
        return {
            "expected": [b["expected_win_rate"] for b in buckets],
            "actual": [b["win_rate"] for b in buckets],
            "counts": [b["count"] for b in buckets]
        }
