"""
Alpha-generation and risk-management upgrades.

Provides four composable modules:
  1. Order Book Imbalance (OBI) — directional signal from bid/ask depth
  2. Markout PnL / Toxic Flow Detection — post-fill adverse selection tracking
  3. Volatility-Adjusted Kelly — scale Kelly sizing inversely with realised vol
  4. Lead-Lag Signal — use correlated asset moves (e.g. SPY→KXINXU) as a leading indicator
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class AlphaEngineConfig:
    """All tunables for the alpha / risk engine."""

    # --- Order Book Imbalance (OBI) ---
    obi_enabled: bool = True
    obi_edge_bonus_max: float = 0.02
    obi_strong_threshold: float = 0.40
    obi_min_total_size: float = 5.0

    # --- Markout PnL / Toxic Flow ---
    markout_enabled: bool = True
    markout_windows_minutes: list[int] = field(default_factory=lambda: [1, 5, 15])
    markout_toxic_threshold_bps: float = -50.0
    markout_edge_penalty: float = 0.05
    markout_penalty_multiplier: float = 1.0  # Multiplier for continuous penalty scaling
    markout_min_samples: int = 5

    # --- Volatility-Adjusted Kelly ---
    vol_kelly_enabled: bool = True
    vol_kelly_baseline_vol: float = 0.02
    vol_kelly_max_shrink: float = 0.25
    vol_kelly_lookback_bars: int = 24

    # --- Lead-Lag Signal ---
    lead_lag_enabled: bool = True
    lead_lag_pairs: dict[str, str] = field(default_factory=lambda: {
        "^GSPC": "KXINXU",
        "^IXIC": "KXINXU",
        "^NDX": "KXNASDAQ100U",
    })
    lead_lag_min_move_pct: float = 0.003
    lead_lag_edge_bonus: float = 0.02
    lead_lag_lookback_bars: int = 5

    # --- GBDT Signal Model ---
    gbdt_enabled: bool = True
    gbdt_min_samples: int = 250
    gbdt_params: dict = field(default_factory=lambda: {
        "n_estimators": 100,
        "max_depth": 4,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
    })
    gbdt_retrain_interval_trades: int = 250
    regime_vol_buckets: list[float] = field(default_factory=lambda: [0.3, 0.7, 1.2, 1.8])

    # --- Regime Filters ---
    regime_enabled: bool = True
    regime_vol_buckets: list[float] = field(default_factory=lambda: [0.01, 0.02, 0.04])
    regime_trend_ema_span: int = 24


_DEFAULT_LEAD_LAG_PAIRS = {"^GSPC": "KXINXU", "^IXIC": "KXINXU", "^NDX": "KXNASDAQ100U"}
_DEFAULT_MARKOUT_WINDOWS = [1, 5, 15]


def load_alpha_engine_config(strategy: dict) -> AlphaEngineConfig:
    ae = strategy.get("alpha_engine", {})
    pairs_raw = ae.get("lead_lag_pairs")
    pairs = dict(pairs_raw) if isinstance(pairs_raw, dict) else dict(_DEFAULT_LEAD_LAG_PAIRS)
    windows_raw = ae.get("markout_windows_minutes")
    windows = list(windows_raw) if isinstance(windows_raw, (list, tuple)) else list(_DEFAULT_MARKOUT_WINDOWS)
    return AlphaEngineConfig(
        obi_enabled=bool(ae.get("obi_enabled", True)),
        obi_edge_bonus_max=float(ae.get("obi_edge_bonus_max", 0.02)),
        obi_strong_threshold=float(ae.get("obi_strong_threshold", 0.40)),
        obi_min_total_size=float(ae.get("obi_min_total_size", 5.0)),
        markout_enabled=bool(ae.get("markout_enabled", True)),
        markout_windows_minutes=[int(w) for w in windows],
        markout_toxic_threshold_bps=float(ae.get("markout_toxic_threshold_bps", -50.0)),
        markout_edge_penalty=float(ae.get("markout_edge_penalty", 0.03)),
        markout_penalty_multiplier=float(ae.get("markout_penalty_multiplier", 1.0)),
        markout_min_samples=int(ae.get("markout_min_samples", 5)),
        vol_kelly_enabled=bool(ae.get("vol_kelly_enabled", True)),
        vol_kelly_baseline_vol=float(ae.get("vol_kelly_baseline_vol", 0.02)),
        vol_kelly_max_shrink=float(ae.get("vol_kelly_max_shrink", 0.25)),
        vol_kelly_lookback_bars=int(ae.get("vol_kelly_lookback_bars", 24)),
        lead_lag_enabled=bool(ae.get("lead_lag_enabled", True)),
        lead_lag_pairs=pairs,
        lead_lag_min_move_pct=float(ae.get("lead_lag_min_move_pct", 0.003)),
        lead_lag_edge_bonus=float(ae.get("lead_lag_edge_bonus", 0.02)),
        lead_lag_lookback_bars=int(ae.get("lead_lag_lookback_bars", 5)),
        gbdt_enabled=bool(ae.get("gbdt_enabled", True)),
        gbdt_min_samples=int(ae.get("gbdt_min_samples", 250)),
        gbdt_params=dict(ae.get("gbdt_params", {})),
        regime_enabled=bool(ae.get("regime_enabled", True)),
        regime_vol_buckets=list(ae.get("regime_vol_buckets", [0.01, 0.02, 0.04])),
        regime_trend_ema_span=int(ae.get("regime_trend_ema_span", 24)),
    )


# ---------------------------------------------------------------------------
# Layer 1: Order Book Imbalance (OBI)
# ---------------------------------------------------------------------------

def compute_obi(
    bid_size: float | None,
    ask_size: float | None,
    min_total_size: float = 5.0,
) -> float | None:
    """
    Order Book Imbalance = (V_bid - V_ask) / (V_bid + V_ask).

    Returns a value in [-1, +1]:
      +1 = all depth on the bid (upward pressure)
      -1 = all depth on the ask (downward pressure)
      None = insufficient data

    When bid_size >> ask_size, the price is likely to tick up because
    there is strong demand relative to supply at the top of the book.
    """
    if bid_size is None or ask_size is None:
        return None
    bid_size = max(0.0, float(bid_size))
    ask_size = max(0.0, float(ask_size))
    total = bid_size + ask_size
    if total < min_total_size:
        return None
    return (bid_size - ask_size) / total


def obi_edge_adjustment(
    obi: float | None,
    rec: str,
    *,
    strong_threshold: float = 0.40,
    max_bonus: float = 0.05, # Increased for continuous scaling (was 0.02)
) -> float:
    """
    Adjust effective edge requirement based on order book imbalance.

    When OBI strongly favors our trade direction, reduce the required edge
    (negative return = bonus). When OBI opposes us, increase it (positive return = penalty).

    For BUY YES: positive OBI (buy pressure) → bonus; negative OBI → penalty.
    For BUY NO: negative OBI (sell pressure) → bonus; positive OBI → penalty.
    
    Using a continuous linear scale from 0.0 to 1.0 OBI.
    """
    if obi is None:
        return 0.0

    if rec == "BUY YES":
        aligned = obi
    elif rec == "BUY NO":
        aligned = -obi
    else:
        return 0.0

    # Continuous scaling: no hard threshold hurdle, but we still scale by magnitude
    # Penalty = OBI * max_bonus
    # If OBI is -0.8 (opposed), penalty is +0.8 * 0.05 = +0.04 (4% edge)
    return -aligned * max_bonus


# ---------------------------------------------------------------------------
# Layer 2: Markout PnL / Toxic Flow Detection
# ---------------------------------------------------------------------------

def compute_markout_bps(
    entry_price: float,
    price_after: float,
    side: str,
) -> float:
    """
    Markout in basis points from our perspective.

    For YES buys: if price dropped after our fill, markout is negative (toxic).
    For NO buys: inverted — price rising after our fill is bad for us.

    Returns the PnL in basis points relative to the contract price.
    """
    if entry_price <= 0:
        return 0.0
    if side.lower() in ("yes", "buy_yes"):
        return (price_after - entry_price) / entry_price * 10_000
    else:
        return (entry_price - price_after) / entry_price * 10_000


def is_ticker_toxic(
    markout_history: list[float],
    threshold_bps: float = -50.0,
    min_samples: int = 5,
) -> bool:
    """
    A ticker is "toxic" if the average markout across recent fills is
    below the threshold. This means we are consistently getting filled
    right before the price moves against us — a sign of adverse selection.
    """
    if len(markout_history) < min_samples:
        return False
    avg = sum(markout_history) / len(markout_history)
    return avg < threshold_bps


def toxic_flow_edge_penalty(
    markout_history: list[float],
    *,
    threshold_bps: float = -50.0,
    min_samples: int = 5,
    base_penalty: float = 0.03,
    multiplier: float = 1.0,
) -> float:
    """
    If a ticker is toxic, return a positive edge penalty so the strategy
    demands more edge before entering again.

    Now using a continuous scaling function:
    Penalty = base_penalty * (1 + multiplier * abs(avg - threshold) / 100)
    This makes the bot progressively more cautious as adverse selection increases.
    """
    if len(markout_history) < min_samples:
        return 0.0
    avg = sum(markout_history) / len(markout_history)
    if avg >= threshold_bps:
        return 0.0

    # Calculate "excess toxicity" in bps
    excess = abs(avg - threshold_bps)
    # Scale: for every 100 bps (1%) of excess toxicity, add 'multiplier' * base_penalty
    scaling_factor = 1.0 + (multiplier * (excess / 100.0))
    return base_penalty * scaling_factor


# ---------------------------------------------------------------------------
# Layer 3: Volatility-Adjusted Kelly Sizing
# ---------------------------------------------------------------------------

def vol_adjusted_kelly(
    base_kelly: float,
    closes: list[float],
    lookback: int = 24,
    baseline_vol: float = 0.02,
    max_shrink: float = 0.25,
) -> float:
    """
    Scale Kelly fraction inversely with recent realized volatility.
    
    If current vol > baseline, shrink position size.
    Size = base_kelly * (baseline / current_vol)
    
    Capped at max_shrink * base_kelly.
    """
    if not closes or len(closes) < 2:
        return base_kelly

    rets = []
    # Use log returns for volatility
    for i in range(max(1, len(closes) - lookback), len(closes)):
        if closes[i-1] > 0:
            rets.append(math.log(closes[i] / closes[i-1]))
    
    if not rets:
        return base_kelly
        
    current_vol = math.sqrt(sum(r*r for r in rets) / len(rets))
    if current_vol <= baseline_vol:
        return base_kelly
        
    ratio = baseline_vol / current_vol
    ratio = max(max_shrink, ratio)
    return base_kelly * ratio


# ---------------------------------------------------------------------------
# Layer 4: Regime State & Filters (Phase 3)
# ---------------------------------------------------------------------------

@dataclass
class RegimeState:
    vol_bucket: str      # 'Low', 'Medium', 'High', 'Extreme'
    trend: str           # 'Uptrend', 'Downtrend', 'Sideways'
    vol_zscore: float    # Z-score of realized volatility
    is_volatile: bool    # True if vol > high threshold
    trend_score: float   # Momentum score normalized to [-1, 1]


def compute_regime_state(
    closes: list[float],
    lookback_vol: int = 24,
    lookback_trend: int = 24,
    vol_buckets: list[float] = None,
) -> RegimeState:
    """
    Identifies the current market regime based on volatility and trend.
    """
    if vol_buckets is None:
        vol_buckets = [0.01, 0.02, 0.04]
        
    if not closes or len(closes) < 2:
        return RegimeState("Medium", "Sideways", 0.0, False, 0.0)

    # 1. Volatility
    rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            rets.append(math.log(closes[i] / closes[i-1]))
    
    if not rets:
        return RegimeState("Medium", "Sideways", 0.0, False, 0.0)
        
    current_vol = math.sqrt(sum(r*r for r in rets[-lookback_vol:]) / len(rets[-lookback_vol:]))
    
    # Simple Z-score (vs full history)
    mean_vol = math.sqrt(sum(r*r for r in rets) / len(rets))
    vol_z = (current_vol - mean_vol) / (mean_vol + 1e-9)
    
    bucket = "Low"
    if current_vol > vol_buckets[2]: bucket = "Extreme"
    elif current_vol > vol_buckets[1]: bucket = "High"
    elif current_vol > vol_buckets[0]: bucket = "Medium"
    
    # 2. Trend
    recent = closes[-lookback_trend:]
    if len(recent) < lookback_trend:
        trend = "Sideways"
        trend_score = 0.0
    else:
        # Simple slope / return
        total_ret = (recent[-1] - recent[0]) / recent[0]
        trend_score = max(-1.0, min(1.0, total_ret / 0.02)) # Normalized by ~2% move
        
        if trend_score > 0.35: trend = "Uptrend"
        elif trend_score < -0.35: trend = "Downtrend"
        else: trend = "Sideways"
        
    return RegimeState(
        vol_bucket=bucket,
        trend=trend,
        vol_zscore=vol_z,
        is_volatile=current_vol > vol_buckets[1],
        trend_score=trend_score
    )


# ---------------------------------------------------------------------------
# Layer 4: Lead-Lag Signal
# ---------------------------------------------------------------------------

def lead_lag_signal(
    leader_closes: list[float],
    lookback: int = 5,
    min_move_pct: float = 0.003,
) -> float:
    """
    Compute short-term momentum of a leading asset.

    Returns the return over the last `lookback` bars.
    If the absolute return exceeds min_move_pct, the signal is live;
    otherwise it returns 0.0 (no actionable signal).

    Positive return on SPY → bullish for KXINXU "above" contracts.
    Negative return on SPY → bearish for KXINXU "above" contracts.
    """
    if not leader_closes or len(leader_closes) < lookback + 1:
        return 0.0

    old = leader_closes[-(lookback + 1)]
    new = leader_closes[-1]
    if old <= 0:
        return 0.0
    ret = (new - old) / old
    if abs(ret) < min_move_pct:
        return 0.0
    return ret


def lead_lag_edge_adjustment(
    signal: float,
    direction: str,
    rec: str,
    edge_bonus: float = 0.02,
) -> float:
    """
    Adjust edge requirement when a leading asset has moved.

    If SPY is sharply up and we're considering BUY YES on "above" contract,
    reduce required edge (bonus). If SPY is sharply down and we're buying
    YES on "above", increase edge (penalty).
    
    Also implements 'fighting the tape' filter: if signal opposes our direction
    by more than min_move_pct * 3, we return a very high penalty to force a SKIP.

    Returns edge adjustment: negative = bonus, positive = penalty.
    """
    if signal == 0.0 or rec not in ("BUY YES", "BUY NO"):
        return 0.0

    # Determine if trade is 'bullish' (BUY YES above / BUY NO below) or 'bearish'
    is_bullish_trade = False
    if rec == "BUY YES":
        is_bullish_trade = (direction == "above")
    elif rec == "BUY NO":
        is_bullish_trade = (direction == "below")

    # If SPY is up (signal > 0) and we're bullish, that's alignment.
    alignment = 1.0 if (signal > 0) == is_bullish_trade else -1.0
    
    # Fighting the tape protection:
    # If signal is against us and deep (e.g. signal -0.5% while we want to BUY YES above)
    if alignment < 0 and abs(signal) > 0.005: 
        return 0.50 # Massive penalty to block the trade

    # Standard adjustment
    return -edge_bonus * alignment if alignment > 0 else edge_bonus * abs(alignment / 0.003) * edge_bonus


# ---------------------------------------------------------------------------
# Layer 5: Machine Learning Signal Model
# ---------------------------------------------------------------------------

@dataclass
class SignalFeatures:
    emp_p: float
    vol_p: float
    market_yes: float
    edge_yes: float
    edge_no: float
    moneyness: float
    dte_norm: float
    spread_norm: float
    liquidity_consistency: float
    # New Regime Features
    vol_regime: float  # Z-scored vol
    trend_score: float # Momentum score
    rsi: float        # Relative Strength Index


def fit_signal_model_weights(
    sp: Any,  # StrategyParams
    rows: list[dict],
) -> dict[str, Any] | None:
    """
    Learn signal-model feature weights (Logistic or GBDT) from resolved predictions.
    """
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        
        # Phase 3: GBDT support
        use_gbdt = getattr(sp.ae_cfg, "gbdt_enabled", False)
        if use_gbdt:
            try:
                from xgboost import XGBClassifier
            except ImportError:
                from sklearn.ensemble import HistGradientBoostingClassifier as XGBClassifier
    except ImportError:
        return None

    try:
        train = [
            r for r in rows
            if r.get("hist_p") is not None and r.get("outcome") in ("win", "lose")
        ]
        
        min_samples = sp.signal_weight_fit_min_samples
        if use_gbdt:
            min_samples = max(min_samples, getattr(sp.ae_cfg, "gbdt_min_samples", 250))
            
        if len(train) < max(30, min_samples):
            return None

        # Feature schema mirrors the runtime scorer where possible.
        feat_names = [
            "emp_p", "vol_p", "market_yes", "edge_yes", "edge_no", 
            "spread_norm", "liquidity_consistency", "macro_24h_ret",
            "vol_regime", "trend_score", "rsi"
        ]
        x_rows: list[list[float]] = []
        y_rows: list[int] = []

        for r in train:
            emp_p = max(0.0, min(1.0, float(r.get("hist_p") or 0.5)))
            vol_p = max(0.0, min(1.0, float(r.get("vol_p") if r.get("vol_p") is not None else emp_p)))
            market_yes = float(r.get("bid_at_place") or r.get("theoretical_price") or r.get("entry_price") or 0.5)
            spread_bps = float(r.get("spread_bps_at_place") or 0.0)
            
            # Basic features
            feats = [
                emp_p,
                vol_p,
                max(0.0, min(1.0, market_yes)),
                max(-1.0, min(1.0, emp_p - market_yes)),
                max(-1.0, min(1.0, (1.0 - emp_p) - (1.0 - market_yes))),
                max(0.0, min(1.0, spread_bps / 10_000.0)),
                1.0, # consistency placeholder
                float(r.get("macro_24h_ret") or 0.0),
                # Regime placeholders (historical data may not have these yet)
                float(r.get("vol_zscore") or 0.0),
                float(r.get("trend_score") or 0.0),
                float(r.get("rsi") or 0.5),
            ]
            x_rows.append(feats)
            y_rows.append(1 if r.get("outcome") == "win" else 0)

        X = np.array(x_rows)
        y = np.array(y_rows)

        if use_gbdt:
            params = getattr(sp.ae_cfg, "gbdt_params", {})
            model = XGBClassifier(**params)
            model.fit(X, y)
            return {"type": "gbdt", "model": model, "features": feat_names}
        else:
            lr = LogisticRegression(
                solver="lbfgs",
                max_iter=1000,
                C=max(1e-4, float(sp.signal_weight_fit_l2_c)),
                class_weight="balanced",
            )
            lr.fit(X, y)
            coefs = lr.coef_[0]
            learned = {"type": "logistic", "bias": float(lr.intercept_[0])}
            for i, name in enumerate(feat_names):
                learned[name] = float(coefs[i])
            return learned
    except Exception:
        return None


def build_signal_features(
    *,
    emp_p: float,
    vol_p: float | None,
    market_yes: float,
    threshold: float,
    current_price: float,
    dte_days: float | None,
    spread_bps: float | None,
    no_bid: float | None,
    vol_regime: float = 0.0,
    trend_score: float = 0.0,
    rsi: float = 0.5,
) -> SignalFeatures:
    def _clip01(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    vol = _clip01(vol_p if vol_p is not None else emp_p)
    mkt = _clip01(market_yes)
    edge_yes = emp_p - mkt
    edge_no = (1.0 - emp_p) - (1.0 - mkt)
    if threshold > 0 and current_price > 0:
        moneyness = math.log(max(1e-9, current_price / threshold))
    else:
        moneyness = 0.0
    dte_norm = _clip01((float(dte_days or 0.0)) / 30.0)
    spread_norm = _clip01((float(spread_bps or 0.0)) / 10_000.0)
    consistency = 1.0
    if no_bid is not None:
        consistency = 1.0 - abs((mkt + _clip01(float(no_bid))) - 1.0)
    return SignalFeatures(
        emp_p=_clip01(emp_p),
        vol_p=vol,
        market_yes=mkt,
        edge_yes=max(-1.0, min(1.0, edge_yes)),
        edge_no=max(-1.0, min(1.0, edge_no)),
        moneyness=max(-2.0, min(2.0, moneyness)),
        dte_norm=dte_norm,
        spread_norm=spread_norm,
        liquidity_consistency=_clip01(consistency),
        vol_regime=float(vol_regime),
        trend_score=float(trend_score),
        rsi=float(rsi),
    )


def score_signal_model(features: SignalFeatures, sp: Any) -> float:
    """
    Interpretable signal model (Logistic) or GBDT.
    """
    w_fit = sp.signal_model_weights or {}
    model_type = w_fit.get("type", "logistic")

    if model_type == "gbdt":
        model = w_fit.get("model")
        if model:
            import numpy as np
            X = np.array([[
                features.emp_p, features.vol_p, features.market_yes,
                features.edge_yes, features.edge_no, features.spread_norm,
                features.liquidity_consistency, 0.0, # macro placeholder
                features.vol_regime, features.trend_score, features.rsi
            ]])
            # Return probability of class 1
            return float(model.predict_proba(X)[0][1])

    # Fallback to Logistic
    w = {
        "bias": 0.0,
        "emp_p": 1.8,
        "vol_p": 1.3,
        "market_yes": 0.3,
        "edge_yes": 1.2,
        "edge_no": -0.8,
        "moneyness": 0.4,
        "dte_norm": 0.2,
        "spread_norm": -0.5,
        "liquidity_consistency": 0.6,
        "macro_24h_ret": 0.3,
        "vol_regime": 0.5,
        "trend_score": 0.5,
        "rsi": -0.3,
    }
    # Update with strategy overrides or learned weights
    w.update({k: v for k, v in w_fit.items() if k != "type"})

    score = w.get("bias", 0.0)
    score += features.emp_p * w.get("emp_p", 0.0)
    score += features.vol_p * w.get("vol_p", 0.0)
    score += features.market_yes * w.get("market_yes", 0.0)
    score += features.edge_yes * w.get("edge_yes", 0.0)
    score += features.edge_no * w.get("edge_no", 0.0)
    score += features.moneyness * w.get("moneyness", 0.0)
    score += features.dte_norm * w.get("dte_norm", 0.0)
    score += features.spread_norm * w.get("spread_norm", 0.0)
    score += features.liquidity_consistency * w.get("liquidity_consistency", 0.0)
    score += features.vol_regime * w.get("vol_regime", 0.0)
    score += features.trend_score * w.get("trend_score", 0.0)
    score += features.rsi * w.get("rsi", 0.0)
    
    # Sigmoid to get probability-like value
    try:
        return 1.0 / (1.0 + math.exp(-score))
    except OverflowError:
        return 1.0 if score > 0 else 0.0


def estimate_fill_probability(
    spread_bps: float | None,
    obi: float | None,
    side_is_aligned: bool,
) -> float:
    """
    Heuristic to estimate the probability of a limit order being filled.
    Decreases with wider spreads and increases when OBI is favorable (against us).
    Wait, if OBI is favorable to US (buying pressure), our BUY limit is LESS likely to be filled
    because the price moves away. If OBI is AGAINST us, we are MORE likely to be filled.
    """
    base_p = 0.75  # Base case fill probability for a reasonably liquid market
    
    # 1. Spread penalty
    if spread_bps is not None:
        # For every 100 bps of spread, reduce fill prob by 5%
        spread_penalty = (spread_bps / 100.0) * 0.05
        base_p -= spread_penalty
        
    # 2. OBI alignment (Adverse selection)
    if obi is not None:
        # If OBI is +0.5 (strong buy pressure) and we are BUYing YES, we are less likely to get filled
        # If OBI is -0.5 (sell pressure) and we are BUYing YES, we are MORE likely to get filled (but it's toxic)
        obi_effect = -obi if side_is_aligned else obi
        base_p += obi_effect * 0.25
        
    return max(0.05, min(0.95, base_p))


def calculate_expected_value(
    p_win: float,
    p_fill: float,
    entry_price_cents: float,
    maker_fee_cents: float,
) -> float:
    """
    EV = P(fill) * [ P(win) * (100 - entry - fee) - P(lose) * (entry + fee) ]
    """
    reward = 100.0 - entry_price_cents - maker_fee_cents
    risk = entry_price_cents + maker_fee_cents
    p_lose = 1.0 - p_win
    
    expected_pnl_at_fill = (p_win * reward) - (p_lose * risk)
    return p_fill * expected_pnl_at_fill
