"""
Market Analysis Utilities: Indicators, Volatility Metrics, and Dip Adjustments.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any, Callable

try:
    import numpy as np
except ImportError:
    np = None

try:
    from scipy.stats import norm as scipy_norm
    from scipy.stats import t as scipy_t
except ImportError:
    scipy_norm = None
    scipy_t = None

try:
    from arch import arch_model
except ImportError:
    arch_model = None

# Avoid circular imports by importing StrategyParams locally or using Any if needed,
# but the plan says use sp: StrategyParams consistently.
# We'll assume StrategyParams is available from models.strategy_config
from models.strategy_config import StrategyParams


_GARCH_VAR_CACHE: dict[tuple[int, int, int], float] = {}


def clip_price(v: float) -> float:
    return max(0.01, min(0.99, float(v)))


def empirical_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    *,
    recent_days: int = 90,
    recent_weight: float = 3.0,
    use_rolling_window: bool = True,
    weighting: str = "step_recent",
    exponential_halflife_bars: int = 14,
) -> float:
    """P(close above/below threshold) from historical closes with recency weighting."""
    if not closes:
        return 0.0
    data = closes[-recent_days:] if (use_rolling_window and recent_days > 0) else closes
    n = len(data)
    if n == 0:
        return 0.0
    wmode = (weighting or "step_recent").strip().lower()
    if wmode == "exponential":
        hl = max(1.0, float(exponential_halflife_bars))
        lam = math.log(2) / hl
        if np is not None:
            arr = np.asarray(data, dtype=float)
            # Newest observation gets highest weight: index n-1 has age 0
            ages = np.arange(n - 1, -1, -1, dtype=float)
            w = np.exp(-lam * ages)
            w = w / max(1e-12, float(w.sum()))
            if direction == "above":
                hits = (arr > threshold).astype(float)
            else:
                hits = (arr < threshold).astype(float)
            return float(np.dot(hits, w))
        tw = 0.0
        wins = 0.0
        for i, c in enumerate(data):
            age = n - 1 - i
            wi = math.exp(-lam * age)
            tw += wi
            if direction == "above":
                wins += wi if c > threshold else 0.0
            else:
                wins += wi if c < threshold else 0.0
        return float(wins / tw) if tw > 0 else 0.0
    if np is not None:
        arr = np.asarray(data, dtype=float)
        if recent_days <= 0 or recent_weight <= 1.0:
            if direction == "above":
                return float(np.mean(arr > threshold))
            return float(np.mean(arr < threshold))
        weights = np.ones(n, dtype=float)
        cutoff = max(0, n - recent_days)
        weights[cutoff:] = float(recent_weight)
        if direction == "above":
            wins = np.where(arr > threshold, weights, 0.0).sum()
        else:
            wins = np.where(arr < threshold, weights, 0.0).sum()
        total_w = float(weights.sum())
        return float(wins / total_w) if total_w > 0 else 0.0

    if recent_days <= 0 or recent_weight <= 1.0:
        if direction == "above":
            return sum(1 for c in data if c > threshold) / n
        return sum(1 for c in data if c < threshold) / n

    cutoff = max(0, n - recent_days)
    wins = 0.0
    total_w = 0.0
    for i, c in enumerate(data):
        w = recent_weight if i >= cutoff else 1.0
        total_w += w
        if direction == "above":
            wins += w if c > threshold else 0.0
        else:
            wins += w if c < threshold else 0.0
    return wins / total_w if total_w > 0 else 0.0


def empirical_probability_multi_horizon(
    closes: list[float],
    threshold: float,
    direction: str,
    *,
    short_days: int,
    medium_days: int,
    long_days: int,
    w_short: float,
    w_medium: float,
    w_long: float,
    recent_weight: float,
    use_rolling_window: bool,
    weighting: str,
    exponential_halflife_bars: int,
) -> float:
    """Blend empirical P from short / medium / long lookbacks (same weighting mode each)."""
    wsum = w_short + w_medium + w_long
    if wsum <= 0 or not closes:
        return empirical_probability(
            closes,
            threshold,
            direction,
            recent_days=max(short_days, 5),
            recent_weight=recent_weight,
            use_rolling_window=use_rolling_window,
            weighting=weighting,
            exponential_halflife_bars=exponential_halflife_bars,
        )
    n = len(closes)
    rd_s = max(5, min(short_days, n))
    rd_m = max(5, min(medium_days, n))
    rd_l = max(5, min(long_days, n))
    p_s = empirical_probability(
        closes,
        threshold,
        direction,
        recent_days=rd_s,
        recent_weight=recent_weight,
        use_rolling_window=use_rolling_window,
        weighting=weighting,
        exponential_halflife_bars=exponential_halflife_bars,
    )
    p_m = empirical_probability(
        closes,
        threshold,
        direction,
        recent_days=rd_m,
        recent_weight=recent_weight,
        use_rolling_window=use_rolling_window,
        weighting=weighting,
        exponential_halflife_bars=exponential_halflife_bars,
    )
    p_l = empirical_probability(
        closes,
        threshold,
        direction,
        recent_days=rd_l,
        recent_weight=recent_weight,
        use_rolling_window=use_rolling_window,
        weighting=weighting,
        exponential_halflife_bars=exponential_halflife_bars,
    )
    return (w_short * p_s + w_medium * p_m + w_long * p_l) / wsum


def ewma_variance(returns: list[float], lam: float = 0.94) -> float:
    """
    EWMA variance: sigma²_t = lam * sigma²_{t-1} + (1 - lam) * r²_t.
    Initialize with sample variance of first returns. lam ~ 0.94 is common (RiskMetrics).
    """
    if not returns or lam <= 0 or lam >= 1:
        return 0.0
    n = len(returns)
    if n < 3:
        return 0.0
    if np is not None:
        arr = np.asarray(returns, dtype=float)
        init_n = min(20, max(3, n // 2))
        var = float(np.mean(arr[:init_n] ** 2))
        for i in range(init_n, n):
            var = lam * var + (1 - lam) * float(arr[i] ** 2)
        return var
    # Initialize with sample variance of first half (or first 20)
    init_n = min(20, max(3, n // 2))
    init_var = sum(r**2 for r in returns[:init_n]) / init_n
    var = init_var
    for i in range(init_n, n):
        var = lam * var + (1 - lam) * (returns[i] ** 2)
    return var


def sample_variance(returns: list[float]) -> float:
    if len(returns) < 3:
        return 0.0
    mean_return = sum(returns) / len(returns)
    return sum((r - mean_return) ** 2 for r in returns) / len(returns)


def _garch_cache_key(returns: list[float]) -> tuple[int, int, int]:
    if not returns:
        return (0, 0, 0)
    tail = returns[-min(24, len(returns)) :]
    first = int(round(tail[0] * 1_000_000))
    last = int(round(tail[-1] * 1_000_000))
    return (len(returns), first, last)


def garch_variance(returns: list[float], min_returns: int = 80) -> float | None:
    """
    One-step-ahead GARCH(1,1) conditional variance estimate.
    Returns None when dependency/model fitting is unavailable.
    """
    if arch_model is None or len(returns) < max(30, int(min_returns)):
        return None
    key = _garch_cache_key(returns)
    cached = _GARCH_VAR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        series = [float(r) * 100.0 for r in returns]  # stabilize optimizer scale
        model = arch_model(series, p=1, q=1, mean="Zero", vol="GARCH", dist="normal", rescale=False)
        fit = model.fit(disp="off")
        forecast = fit.forecast(horizon=1)
        var_pct2 = float(forecast.variance.values[-1, 0])
        variance = max(0.0, var_pct2 / 10_000.0)
        _GARCH_VAR_CACHE[key] = variance
        return variance
    except Exception:
        return None


def volatility_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    days_to_expiry: float = 30.0,
    use_ewma: bool = False,
    ewma_lambda: float = 0.94,
    current_price: float | None = None,
    *,
    vol_model: str = "auto",
    garch_min_returns: int = 80,
    enable_otm_smile_adjustment: bool = True,
    otm_smile_strength: float = 0.35,
    otm_smile_max_mult: float = 2.0,
    distribution: str = "normal",
    t_df: float = 6.0,
    zero_drift: bool = False,
) -> float | None:
    """
    Log-normal / Black-Scholes style: P(price above/below K at expiry) using historical vol.
    """
    if not closes or len(closes) < 5 or threshold <= 0:
        return None
    S0 = float(current_price) if (current_price is not None and current_price > 0) else float(closes[-1])
    if S0 <= 0:
        return None
    # Log returns
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            r = math.log(closes[i] / closes[i - 1])
            returns.append(r)
    if len(returns) < 3:
        return None
    model = (vol_model or "auto").strip().lower()
    variance = 0.0
    if model in {"garch", "auto"}:
        gv = garch_variance(returns, min_returns=garch_min_returns)
        if gv is not None and gv > 0:
            variance = gv
    if variance <= 0 and (use_ewma or model in {"ewma", "auto"}):
        variance = ewma_variance(returns, ewma_lambda)
    if variance <= 0:
        variance = sample_variance(returns)
    sigma_daily = variance ** 0.5
    sigma_annual = sigma_daily * (252 ** 0.5)
    # OTM smile adjustment
    if enable_otm_smile_adjustment and sigma_annual > 0:
        m = math.log(max(1e-12, threshold / S0))
        is_otm_for_yes = (direction == "above" and threshold > S0) or (
            direction == "below" and threshold < S0
        )
        if is_otm_for_yes:
            mult = 1.0 + max(0.0, otm_smile_strength) * abs(m)
            mult = min(max(1.0, mult), max(1.0, otm_smile_max_mult))
            sigma_annual *= mult
    T = max(1e-6, days_to_expiry / 365.0)
    if sigma_annual < 1e-8:
        return None
    if zero_drift:
        d = (math.log(S0 / threshold)) / (sigma_annual * (T ** 0.5))
    else:
        d = (math.log(S0 / threshold) - 0.5 * sigma_annual**2 * T) / (sigma_annual * (T ** 0.5))
    dist = (distribution or "normal").strip().lower()
    if dist in ("student_t", "student-t", "t"):
        df = max(2.1, float(t_df))
        if scipy_t is not None:
            phi_d = float(scipy_t.cdf(d, df=df))
        elif scipy_norm is not None:
            scale = math.sqrt(df / max(1.0, (df - 2.0)))
            phi_d = float(scipy_norm.cdf(d / scale))
        else:
            phi_d = 0.5 * (1 + math.erf((d / math.sqrt(df / max(1.0, (df - 2.0)))) / (2 ** 0.5)))
    elif scipy_norm is not None:
        phi_d = float(scipy_norm.cdf(d))
    else:
        phi_d = 0.5 * (1 + math.erf(d / (2 ** 0.5)))
    phi_d = max(0.0, min(1.0, phi_d))
    if direction == "above":
        return phi_d
    return 1.0 - phi_d


def monte_carlo_probability(
    S0: float,
    sigma_annual: float,
    T: float,
    threshold: float,
    direction: str,
    n_paths: int = 10000,
    distribution: str = "normal",
    t_df: float = 6.0,
    zero_drift: bool = False,
) -> float:
    """Monte Carlo GBM simulation."""
    if S0 <= 0 or sigma_annual < 1e-8 or T <= 0 or threshold <= 0:
        return 0.5
    n_steps = max(1, round(T * 365))
    dt = T / n_steps
    drift = 0.0 if zero_drift else (-0.5 * sigma_annual ** 2 * dt)
    diffusion = sigma_annual * math.sqrt(dt)
    dist = (distribution or "normal").strip().lower()
    use_t = dist in ("student_t", "student-t", "t")
    df = max(2.1, float(t_df)) if use_t else 0.0
    t_scale = math.sqrt((df - 2.0) / df) if use_t and df > 2.0 else 1.0

    if np is not None:
        rng = np.random.default_rng()
        if use_t and scipy_t is not None:
            innovations = scipy_t.rvs(df, size=(n_paths, n_steps), random_state=rng) * t_scale
        elif use_t:
            innovations = rng.standard_normal((n_paths, n_steps)) * t_scale
        else:
            innovations = rng.standard_normal((n_paths, n_steps))
        log_returns = drift + diffusion * innovations
        log_S_T = math.log(S0) + np.sum(log_returns, axis=1)
        S_T = np.exp(log_S_T)
        if direction == "above":
            return float(np.mean(S_T >= threshold))
        return float(np.mean(S_T <= threshold))

    hits = 0
    for _ in range(n_paths):
        log_s = math.log(S0)
        for _ in range(n_steps):
            z = random.gauss(0, t_scale)
            log_s += drift + diffusion * z
        S_T = math.exp(log_s)
        if direction == "above":
            hits += (S_T >= threshold)
        else:
            hits += (S_T <= threshold)
    return hits / n_paths


def volatility_probability_with_mc(
    closes: list[float],
    threshold: float,
    direction: str,
    days_to_expiry: float = 30.0,
    use_ewma: bool = False,
    ewma_lambda: float = 0.94,
    current_price: float | None = None,
    *,
    vol_model: str = "auto",
    garch_min_returns: int = 80,
    enable_otm_smile_adjustment: bool = True,
    otm_smile_strength: float = 0.35,
    otm_smile_max_mult: float = 2.0,
    distribution: str = "normal",
    t_df: float = 6.0,
    mc_paths: int = 10000,
    mc_blend_weight: float = 0.3,
    zero_drift: bool = False,
) -> float | None:
    """Blended analytical + Monte Carlo volatility probability."""
    analytical = volatility_probability(
        closes, threshold, direction, days_to_expiry,
        use_ewma=use_ewma, ewma_lambda=ewma_lambda,
        current_price=current_price,
        vol_model=vol_model, garch_min_returns=garch_min_returns,
        enable_otm_smile_adjustment=enable_otm_smile_adjustment,
        otm_smile_strength=otm_smile_strength,
        otm_smile_max_mult=otm_smile_max_mult,
        distribution=distribution, t_df=t_df,
        zero_drift=zero_drift,
    )
    if analytical is None:
        return None
    if mc_paths <= 0 or mc_blend_weight <= 0:
        return analytical
    S0 = float(current_price) if (current_price is not None and current_price > 0) else float(closes[-1])
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))
    if len(returns) < 3:
        return analytical
    model = (vol_model or "auto").strip().lower()
    variance = 0.0
    if model in {"garch", "auto"}:
        gv = garch_variance(returns, min_returns=garch_min_returns)
        if gv is not None and gv > 0:
            variance = gv
    if variance <= 0 and (use_ewma or model in {"ewma", "auto"}):
        variance = ewma_variance(returns, ewma_lambda)
    if variance <= 0:
        variance = sample_variance(returns)
    sigma_daily = variance ** 0.5
    sigma_annual = sigma_daily * (252 ** 0.5)
    if enable_otm_smile_adjustment and sigma_annual > 0:
        m = math.log(max(1e-12, threshold / S0))
        is_otm_for_yes = (direction == "above" and threshold > S0) or (
            direction == "below" and threshold < S0
        )
        if is_otm_for_yes:
            mult = 1.0 + max(0.0, otm_smile_strength) * abs(m)
            mult = min(max(1.0, mult), max(1.0, otm_smile_max_mult))
            sigma_annual *= mult
    T = max(1e-6, days_to_expiry / 365.0)
    mc = monte_carlo_probability(
        S0, sigma_annual, T, threshold, direction,
        n_paths=mc_paths, distribution=distribution, t_df=t_df,
        zero_drift=zero_drift,
    )
    w = max(0.0, min(1.0, mc_blend_weight))
    return (1.0 - w) * analytical + w * mc


def compute_macro_signal_24h(sp: StrategyParams, cfg: dict) -> float:
    """
    Macro risk-on/risk-off feature from major indices.
    Returns average 24h return across configured tickers.
    """
    if not sp.macro_signal_enabled:
        return 0.0
    from yfinance_client import get_aggs as yf_get_aggs
    rets: list[float] = []
    for tk in sp.macro_signal_tickers:
        try:
            closes = yf_get_aggs(str(tk), days_back=5, config=cfg)
            if closes and len(closes) >= 2 and closes[-2] > 0:
                rets.append((float(closes[-1]) / float(closes[-2])) - 1.0)
        except Exception:
            continue
    if not rets:
        return 0.0
    return max(-0.10, min(0.10, sum(rets) / len(rets)))


def compute_lottery_limit_price(
    sp: StrategyParams,
    side: str,
    yes_bid: float,
    no_bid: float,
    yes_ask: float | None,
    no_ask: float | None,
) -> float:
    """
    Lottery resting bids: fixed micro price, or penny_jump (best bid on that side + 1¢).
    """
    mode = (sp.lottery_limit_price_mode or "fixed").strip().lower()
    if mode in ("", "fixed", "absolute"):
        return clip_price(float(sp.lottery_limit_price))
    if mode not in ("penny_jump", "bid_plus_tick", "bid_plus_one_tick", "jump"):
        return clip_price(float(sp.lottery_limit_price))
    tick = 0.01
    if side.lower() == "yes":
        bid = float(yes_bid)
        ask = yes_ask
    else:
        bid = float(no_bid)
        ask = no_ask
    # Match passive bid_plus_tick: front-of-queue without crossing the ask
    px = bid + tick
    if ask is not None and float(ask) > 0:
        px = min(px, max(bid, float(ask) - tick))
    th = float(sp.lottery_penny_jump_apply_cap_bid_below)
    if bid < th:
        px = min(px, float(sp.lottery_limit_max_price))
    return clip_price(px)


def wide_book_lift_passive_raw_price(
    *,
    bid: float,
    ask: float | None,
    fair_cap: float,
    raw_price: float,
    enabled: bool,
    conservative: bool,
    spread_rel_threshold: float,
    anchor_fraction: float,
) -> float:
    """On wide (ask-bid)/bid books, raise dip-based raw_price toward bid + anchor×(ask-bid)."""
    if not enabled or conservative or ask is None:
        return raw_price
    b = float(bid)
    a = float(ask)
    if b <= 0 or a <= b + 1e-9:
        return raw_price
    rel = (a - b) / b
    thr = max(0.0, float(spread_rel_threshold))
    if rel < thr:
        return raw_price
    frac = max(0.0, min(1.0, float(anchor_fraction)))
    anchor = b + frac * (a - b)
    cap = max(0.01, min(0.99, float(fair_cap)))
    lift = min(anchor, cap)
    return max(float(raw_price), lift)


def resolve_passive_quote_price(
    *,
    passive_quote: str,
    bid: float,
    ask: float | None,
    dip_based_price: float,
    fair_cap: float,
) -> float:
    """Micro-balance / maker-friendly quotes."""
    pq = (passive_quote or "dip").strip().lower()
    if pq in ("", "dip", "default", "below_bid", "layered"):
        return clip_price(min(float(dip_based_price), float(fair_cap)))
    fc = max(0.01, min(0.99, float(fair_cap)))
    if pq == "mid":
        if ask is not None and float(ask) > 0 and float(bid) > 0:
            mid = (float(bid) + float(ask)) / 2.0
            return clip_price(min(mid, fc))
        return clip_price(min(float(bid), fc))
    if pq in ("bid_plus_tick", "bid_plus_one_tick"):
        tick = 0.01
        j = float(bid) + tick
        if ask is not None and float(ask) > 0:
            j = min(j, max(float(bid), float(ask) - tick))
        return clip_price(min(j, fc))
    return clip_price(min(float(dip_based_price), fc))


def is_midprice_stable(
    *,
    ticker: str,
    platform: str,
    cfg: dict,
    min_stability_seconds: int,
    get_market_snapshots_fn: Callable | None = None,
    max_mid_move_pct: float = 0.03,
    skip_in_preview: bool = False,
) -> bool:
    """Anti-sniping guard."""
    if skip_in_preview or min_stability_seconds <= 0 or get_market_snapshots_fn is None:
        return True
    end_ts = time.time()
    start_ts = end_ts - max(float(min_stability_seconds + 60), 300.0)
    snaps = get_market_snapshots_fn(
        start_ts=start_ts,
        end_ts=end_ts,
        platform=platform,
        config=cfg,
    ) or []
    mids: list[tuple[float, float]] = []
    for s in snaps:
        if str(s.get("ticker") or "") != str(ticker):
            continue
        y = s.get("yes_bid")
        n = s.get("no_bid")
        try:
            yf = float(y) if y is not None else None
            nf = float(n) if n is not None else None
        except (TypeError, ValueError):
            continue
        if yf is None or nf is None or yf <= 0 or nf <= 0:
            continue
        mids.append((float(s.get("ts") or 0.0), (yf + nf) / 2.0))
    if len(mids) < 2:
        return False
    mids.sort(key=lambda x: x[0])
    if mids[-1][0] - mids[0][0] < float(min_stability_seconds):
        return False
    vals = [m for _, m in mids]
    lo, hi = min(vals), max(vals)
    move_pct = (hi - lo) / max(0.01, vals[-1])
    return move_pct <= max(0.0, float(max_mid_move_pct))


def limit_buy_wide_book_conservative(
    ticker: str,
    yf_ticker: str | None,
    substrings: list[str],
    yf_exact: list[str],
) -> bool:
    """When True, skip wide-book limit coalesce / EV touch-lift."""
    t = (ticker or "").upper()
    for s in substrings:
        su = (s or "").strip().upper()
        if su and su in t:
            return True
    yf = (yf_ticker or "").strip().upper()
    for x in yf_exact:
        if (x or "").strip().upper() == yf:
            return True
    return False


def calculate_adaptive_limit_dip(base_dip: float, closes: list[float], *, cap: float = 0.25) -> float:
    """
    ARR-inspired adaptive limit dip.
    Uses average absolute relative move as a practical volatility proxy and
    widens dip in higher-vol regimes: dip ~= max(base, 0.5 * ARR).
    """
    if len(closes) < 12:
        return max(0.0, base_dip)
    rel_moves: list[float] = []
    for i in range(1, min(len(closes), 72)):
        prev = float(closes[-(i + 1)] or 0.0)
        curr = float(closes[-i] or 0.0)
        if prev > 0:
            rel_moves.append(abs((curr - prev) / prev))
    if not rel_moves:
        return max(0.0, base_dip)
    arr = sum(rel_moves) / len(rel_moves)
    return min(max(0.0, cap), max(float(base_dip), 0.5 * arr))


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI on the underlying asset's close prices."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss < 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_rsi_dip_adjustment(base_dip: float, rsi: float | None, max_dip_cap: float = 0.25) -> float:
    """
    Modulate limit dip depth based on RSI.
    Oversold (RSI<30): tighten dip to fill faster on the bounce.
    Overbought (RSI>70): widen dip to wait for the correction.
    """
    if rsi is None:
        return base_dip
    if rsi < 30:
        return base_dip * 0.6
    if rsi > 70:
        return min(max_dip_cap, base_dip * 1.4)
    return base_dip


def calculate_dte_confidence_dip(
    base_dip: float,
    p_win: float,
    dte_days: float | None,
    sp: Any,  # StrategyParams
) -> float:
    """
    Scale limit order dip depth based on DTE × confidence interaction.

    When resolution is far out, contract prices are subject to uncertainty-driven dips
    even when the eventual outcome is near-certain. High-confidence, far-DTE trades
    benefit from deeper limits that catch these transient price drops.

    Returns the combined dip (base + DTE extra), capped at max_dip_cap.
    """
    if not sp.far_dte_dip_scaling_enabled:
        return base_dip
    if dte_days is None or dte_days <= sp.far_dte_threshold_days:
        return base_dip
    if p_win < sp.far_dte_min_confidence:
        return base_dip

    excess_days = dte_days - sp.far_dte_threshold_days
    extra_dip = min(sp.far_dte_max_extra_dip, excess_days * sp.far_dte_dip_scale_per_day)

    if p_win >= 0.90:
        extra_dip *= sp.far_dte_confidence_boost

    return min(sp.max_dip_cap, base_dip + extra_dip)


def calculate_volatility_spike_metrics(closes: list[float]) -> tuple[float | None, float | None]:
    """
    Return (current_vol, baseline_vol) using recent log-return windows.
    Uses short-vs-long realized vol as a regime-break proxy.
    """
    if not closes or len(closes) < 30:
        return None, None
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev and prev > 0 and curr > 0:
            rets.append(math.log(curr / prev))
    if len(rets) < 25:
        return None, None
    curr_win = rets[-6:]
    base_win = rets[-24:]
    curr_vol = math.sqrt(sum(r * r for r in curr_win) / max(1, len(curr_win)))
    base_vol = math.sqrt(sum(r * r for r in base_win) / max(1, len(base_win)))
    return curr_vol, base_vol


def calculate_vol_ratio(curr_vol: float | None, base_vol: float | None) -> float:
    if curr_vol is None or base_vol is None or base_vol <= 0:
        return 1.0
    return max(0.0, float(curr_vol) / float(base_vol))


def calculate_dynamic_stability_seconds(sp: Any, vol_ratio: float) -> int:
    """StrategyParams-aware dynamic stability window."""
    base = max(0, int(sp.min_stability_duration_seconds))
    if base <= 0 or not sp.enable_dynamic_stability_window:
        return base
    ref = max(1e-6, float(sp.stability_vol_ratio_ref))
    ratio = max(1.0, float(vol_ratio) / ref)
    mult = min(max(1.0, ratio), max(1.0, float(sp.stability_max_multiplier)))
    return int(round(base * mult))
