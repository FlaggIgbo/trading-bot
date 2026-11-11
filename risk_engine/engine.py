"""
Step 2: Trader (Risk Engine)
============================
Reads candidate markets and open positions from SQLite, performs risk checks,
chooses a side (YES/up or NO/down) using a strategy function, and manages positions.
Supports last-5 resolved trend following, random selection, and sell-if-3x momentum strategy.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import random
import re
import time
from typing import Any, Callable, Optional, Protocol, Union

from db import (
    Database,
    MarketResolution,
    MarketResult,
    MarketStatus,
    PositionSide,
    PositionStatus,
    Strategy,
    normalize_strategy,
)
from ingestion.api.client import is_binary_market
from logging.analytics import analytics
from risk_engine.broker import Broker, KalshiBroker, SimulatedBroker
from risk_engine.decision import Decision
from risk_engine.reconciler import Reconciler
from risk_engine.rules import (
    DEFAULT_RISK_RULES,
    RiskContext,
    evaluate_risk_rules,
    rule_is_binary_market,
    rule_kill_switch_not_active,
    rule_market_is_open,
    rule_no_existing_position,
    rule_not_past_close,
    rule_under_daily_loss_cap,
    rule_under_max_open,
)


class StrategyProtocol(Protocol):
    """Protocol for pure strategy functions: recent history -> Side.YES, Side.NO, or None."""
    def __call__(
        self,
        past_results: Optional[list[str]] = None,
        market: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[PositionSide]:
        ...


def strategy_random(past_results=None, market=None, **kwargs) -> PositionSide:
    """Baseline strategy: randomly pick YES or NO."""
    return random.choice([PositionSide.YES, PositionSide.NO])


def calculate_weighted_trend(
    past_results: Any = None,
    limit: int = 21,
) -> tuple[Optional[PositionSide], float, float]:
    """
    Calculate magnitude-weighted trend from recent market resolutions.
    Each market's weight is its percentage return magnitude:
      weight = |expiration_value - floor_strike| / floor_strike * 100
    For example:
      - A 1% market move has weight 1.0
      - A 0.0001% market move has weight 0.0001
    If strike/expiration values are not available (e.g. unweighted strings in tests),
    weight defaults to 1.0.

    Returns:
      (candidate_side, up_weight, down_weight)
    """
    if not past_results:
        return None, 0.0, 0.0

    recent = list(past_results)[:limit]
    up_weight = 0.0
    down_weight = 0.0

    for item in recent:
        clean_result = None
        weight = 1.0

        if isinstance(item, MarketResolution):
            clean_result = item.result
            weight = getattr(item, "weight", 1.0)
        elif isinstance(item, dict):
            clean_result = str(item.get("result", "")).strip().lower()
            if "weight" in item and item["weight"] is not None:
                try:
                    weight = float(item["weight"])
                except (ValueError, TypeError):
                    weight = 1.0
            else:
                fs = item.get("floor_strike")
                ev = item.get("expiration_value")
                if fs is not None and ev is not None:
                    try:
                        fs_f = float(fs)
                        ev_f = float(ev)
                        if fs_f > 0:
                            weight = abs(ev_f - fs_f) / fs_f * 100.0
                    except (ValueError, TypeError):
                        weight = 1.0
        elif isinstance(item, tuple) and len(item) >= 2:
            clean_result = str(item[0]).strip().lower()
            try:
                weight = float(item[1])
            except (ValueError, TypeError):
                weight = 1.0
        else:
            clean_result = str(getattr(item, "value", item)).strip().lower()
            weight = 1.0

        effective_weight = max(1e-6, weight) if weight is not None else 1.0

        if clean_result in (MarketResult.YES.value, "yes"):
            up_weight += effective_weight
        elif clean_result in (MarketResult.NO.value, "no"):
            down_weight += effective_weight

    if up_weight > down_weight:
        side = PositionSide.YES
    elif down_weight > up_weight:
        side = PositionSide.NO
    else:
        side = None

    return side, up_weight, down_weight


def strategy_last_21(
    past_results: Any = None,
    market: Optional[dict] = None,
    min_samples: int = 21,
    **kwargs,
) -> Optional[PositionSide]:
    """
    Pure strategy: trend-following from the last 21 resolutions with magnitude weighting.
    Returns None if the series has fewer than min_samples resolutions (no opinion).
    """
    if not past_results:
        return None
    valid = [
        r for r in past_results
        if str(getattr(r, "value", r)).strip().lower() in (MarketResult.YES.value, MarketResult.NO.value)
    ]
    if len(valid) < min_samples:
        return None
    side, _, _ = calculate_weighted_trend(valid, limit=21)
    return side


def strategy_last_9(
    past_results: Any = None,
    market: Optional[dict] = None,
    min_samples: int = 9,
    limit: int = 21,
    **kwargs,
) -> Optional[PositionSide]:
    """
    Trend-following strategy from recent resolutions with magnitude weighting.
    Extended to 21 resolutions by default (or minimum 9 samples).
    Returns None if the series has fewer than min_samples resolutions (no opinion).
    """
    if not past_results:
        return None
    valid = [
        r for r in past_results
        if str(getattr(r, "value", r)).strip().lower() in (MarketResult.YES.value, MarketResult.NO.value)
    ]
    if len(valid) < min_samples:
        return None
    side, _, _ = calculate_weighted_trend(valid, limit=limit)
    return side


def choose_side_random(past_results=None) -> PositionSide:
    # Strategy 1: Randomly pick YES (up) or NO (down)
    return strategy_random(past_results=past_results)


def choose_side_last_21(
    past_results: Any = None,
    market: Optional[dict] = None,
    spot_client: Optional[Any] = None,
    check_spot: bool = False,
    min_entry_price: Optional[int] = None,
    max_entry_price: Optional[int] = None,
    require_spot_agreement: bool = True,
    limit: int = 21,
    **kwargs,
) -> Optional[PositionSide]:
    """
    Magnitude-Weighted Strategy (last_21):
    - Historical weighted trend: Analyzes the last 21 resolved markets weighted by
      percentage return magnitude: |expiration_value - floor_strike| / floor_strike * 100
      (e.g. 1% move vs 0.0001% move). Candidate is YES if up_weight > down_weight, else NO.
    - Real-time Spot Alignment: If check_spot=True and market is provided, compares
      real-time spot price against market strike (floor_strike). If spot conflicts, skips trade.
    - Price Cap & Value Filter: If min_entry_price or max_entry_price is set, ensures the
      contract ask price falls within acceptable risk/reward bounds (e.g. 20c - 65c).
    """
    hist_side, up_weight, down_weight = calculate_weighted_trend(past_results, limit=limit)
    if hist_side is None:
        hist_side = PositionSide.NO

    analytics.track(
        "trend_evaluation",
        message=f"Last {limit} weighted trend: up_weight={up_weight:.4f}, down_weight={down_weight:.4f} -> candidate={hist_side.value}",
        strategy=f"last_{limit}",
        up_weight=up_weight,
        down_weight=down_weight,
        candidate_side=hist_side.value,
    )

    # Exact legacy behavior if spot check and price filters are not active
    if not check_spot and min_entry_price is None and max_entry_price is None:
        return hist_side

    # Real-time Spot Trend Alignment Check
    if check_spot and market:
        if spot_client is None:
            from ingestion.spot import SpotClient
            spot_client = SpotClient()
        spot_side, spot_val, strike_val = spot_client.get_spot_trend(market)

        if spot_side is not None:
            if hist_side is None:
                candidate_side = spot_side
            elif require_spot_agreement and hist_side != spot_side:
                analytics.info(
                    f"Spot trend ({spot_side.value}, spot={spot_val} vs strike={strike_val}) "
                    f"conflicts with last_{limit} weighted historical ({hist_side.value}). Skipping trade."
                )
                return None
            else:
                candidate_side = spot_side
        else:
            candidate_side = hist_side or PositionSide.NO
    else:
        candidate_side = hist_side or PositionSide.NO

    # Price Cap & Value Filter
    if market and (min_entry_price is not None or max_entry_price is not None):
        ask_cents = extract_contract_price(market, candidate_side)
        if ask_cents is not None:
            if min_entry_price is not None and ask_cents < min_entry_price:
                analytics.info(f"Contract ask {ask_cents}c is below min entry price {min_entry_price}c. Skipping trade.")
                return None
            if max_entry_price is not None and ask_cents > max_entry_price:
                analytics.info(f"Contract ask {ask_cents}c exceeds max entry price {max_entry_price}c. Skipping trade.")
                return None

    return candidate_side


choose_side_last_9 = choose_side_last_21


def choose_side_regime_aware(
    past_results: Any = None,
    market: Optional[dict] = None,
    spot_client: Optional[Any] = None,
    check_spot: bool = False,
    min_entry_price: Optional[int] = 20,
    max_entry_price: Optional[int] = 65,
    min_volatility: float = 0.03,
    min_edge_ratio: float = 0.20,
    max_trend_flip_ratio: float = 0.40,
    require_spot_agreement: bool = False,
    **kwargs,
) -> Optional[PositionSide]:
    """
    Regime-Aware Strategy with Volatility Gate:
    1. Evaluates past market resolutions through detect_market_regime:
       - If streak >= 2: Fades the streak (Mean-Reversion, 57%+ win rate on 15m crypto).
       - If flip_ratio <= 0.40 and edge >= 0.20: Follows trending breakout.
       - If choppy noise or compressed volatility (< 0.03%): Abstains (returns None).
    2. Real-time Spot Trend Alignment (optional if check_spot=True).
    3. Price Cap & Value Filter: ensures contract ask is within bounds [min_entry_price, max_entry_price].
    """
    from risk_engine.regime import detect_market_regime

    regime_res = detect_market_regime(
        past_results=past_results,
        min_volatility=min_volatility,
        min_edge_ratio=min_edge_ratio,
        max_trend_flip_ratio=max_trend_flip_ratio,
    )

    analytics.track(
        "regime_evaluation",
        message=f"Market regime: {regime_res.regime} (streak={regime_res.streak}, flip_ratio={regime_res.flip_ratio:.2f}, edge={regime_res.edge_ratio:.2f}) -> {regime_res.reason}",
        regime=regime_res.regime,
        streak=regime_res.streak,
        flip_ratio=regime_res.flip_ratio,
        edge_ratio=regime_res.edge_ratio,
        recommended_side=regime_res.recommended_side.value if regime_res.recommended_side else None,
        is_high_edge=regime_res.is_high_edge,
    )

    if not regime_res.should_trade:
        return None

    candidate_side = regime_res.recommended_side

    # Spot Trend Check if enabled
    if check_spot and market:
        if spot_client is None:
            from ingestion.spot import SpotClient
            spot_client = SpotClient()
        spot_side, spot_val, strike_val = spot_client.get_spot_trend(market)
        if spot_side is not None and require_spot_agreement and spot_side != candidate_side:
            analytics.info(
                f"Spot trend ({spot_side.value}, spot={spot_val} vs strike={strike_val}) "
                f"conflicts with regime candidate ({candidate_side.value}). Skipping trade."
            )
            return None

    # Price bounds check
    if market and (min_entry_price is not None or max_entry_price is not None):
        ask_cents = extract_contract_price(market, candidate_side)
        if ask_cents is not None:
            if min_entry_price is not None and ask_cents < min_entry_price:
                return None
            if max_entry_price is not None and ask_cents > max_entry_price:
                return None

    return candidate_side


def regime_aware(
    past_results_or_market: Any = None,
    market: Optional[dict] = None,
    past_results: Any = None,
    exit_multiplier: Optional[Union[int, float]] = None,
    exit_price_cents: Optional[int] = None,
    n: Optional[Union[int, float]] = None,
    exit_price: Optional[int] = None,
    **kwargs,
) -> Any:
    """Regime-Aware strategy factory with optional TP / SL modifiers."""
    target_market = market
    target_past = past_results

    if isinstance(past_results_or_market, dict):
        target_market = past_results_or_market
    elif isinstance(past_results_or_market, (list, tuple)):
        target_past = past_results_or_market

    effective_multiplier = exit_multiplier if exit_multiplier is not None else (n if n is not None else kwargs.pop("n", None))
    effective_price = exit_price_cents if exit_price_cents is not None else (exit_price if exit_price is not None else kwargs.pop("exit_price", None))

    if target_market is None and target_past is None:
        def _bound_strategy(past_res=None, m=None, **kw):
            tm = m if m is not None else kw.pop("market", None)
            tp = past_res if past_res is not None else kw.pop("past_results", None)
            return regime_aware(
                market=tm,
                past_results=tp,
                exit_multiplier=effective_multiplier,
                exit_price_cents=effective_price,
                **kw,
            )

        _bound_strategy.exit_multiplier = effective_multiplier
        _bound_strategy.exit_price_cents = effective_price
        _bound_strategy.n = effective_multiplier
        _bound_strategy.taker_ioc = True
        return _bound_strategy

    kw = {
        "check_spot": kwargs.pop("check_spot", False),
        "min_entry_price": kwargs.pop("min_entry_price", 20),
        "max_entry_price": kwargs.pop("max_entry_price", 65),
        **kwargs,
    }
    return choose_side_regime_aware(past_results=target_past, market=target_market, **kw)


def regime_spot(past_results_or_market: Any = None, market: Optional[dict] = None, **kwargs) -> Any:
    """Regime-aware strategy with real-time spot alignment active."""
    return regime_aware(past_results_or_market=past_results_or_market, market=market, check_spot=True, **kwargs)


def last_21_spot(
    past_results_or_market: Any = None,
    market: Optional[dict] = None,
    past_results: Any = None,
    exit_multiplier: Optional[Union[int, float]] = None,
    exit_price_cents: Optional[int] = None,
    n: Optional[Union[int, float]] = None,
    exit_price: Optional[int] = None,
    **kwargs,
) -> Any:
    """
    last_21 strategy with magnitude weighting, real-time spot trend alignment, and value filter.
    Optional exit parameters for taking profit when winning:
    - exit_multiplier (or n): Multiplier threshold (e.g. 1.75 to sell if contract is worth 1.75x entry price).
    - exit_price_cents (or exit_price): Absolute price threshold (e.g. 85 or 90 to sell when contract passes target).
    """
    target_market = market
    target_past = past_results

    if isinstance(past_results_or_market, dict):
        target_market = past_results_or_market
    elif isinstance(past_results_or_market, (list, tuple)):
        target_past = past_results_or_market

    effective_multiplier = exit_multiplier if exit_multiplier is not None else (n if n is not None else kwargs.pop("n", None))
    effective_price = exit_price_cents if exit_price_cents is not None else (exit_price if exit_price is not None else kwargs.pop("exit_price", None))

    if target_market is None and target_past is None:
        def _bound_strategy(past_res=None, m=None, **kw):
            tm = m if m is not None else kw.pop("market", None)
            tp = past_res if past_res is not None else kw.pop("past_results", None)
            return last_21_spot(
                market=tm,
                past_results=tp,
                exit_multiplier=effective_multiplier,
                exit_price_cents=effective_price,
                **kw,
            )

        _bound_strategy.exit_multiplier = effective_multiplier
        _bound_strategy.exit_price_cents = effective_price
        _bound_strategy.n = effective_multiplier
        _bound_strategy.check_spot = True
        return _bound_strategy

    kw = {
        "check_spot": True,
        "min_entry_price": kwargs.pop("min_entry_price", 20),
        "max_entry_price": kwargs.pop("max_entry_price", 65),
        "limit": 21,
        **kwargs,
    }
    return choose_side_last_21(past_results=target_past, market=target_market, **kw)


last_9_spot = last_21_spot

# Deprecated backwards compatibility aliases (use choose_side_last_21)
choose_side_last_5 = choose_side_last_21
choose_side_last_3 = choose_side_last_21


def extract_contract_price(market: Optional[dict], side: PositionSide) -> Optional[int]:
    """Extract ask price in cents (1-99) for the given side."""
    if not isinstance(market, dict):
        return None
    cents = None
    if side == PositionSide.YES:
        if market.get("yes_ask") is not None:
            cents = int(market["yes_ask"])
        elif market.get("yes_ask_dollars") is not None:
            cents = int(round(float(market["yes_ask_dollars"]) * 100))
        elif market.get("yes_price") is not None:
            cents = int(market["yes_price"])
    else:
        if market.get("no_ask") is not None:
            cents = int(market["no_ask"])
        elif market.get("no_ask_dollars") is not None:
            cents = int(round(float(market["no_ask_dollars"]) * 100))
        elif market.get("no_price") is not None:
            cents = int(market["no_price"])

    if cents is not None and (cents <= 0 or cents >= 100):
        return None
    return cents


def sell_Xn_Ym(
    past_results_or_market: Any = None,
    n_or_market: Any = None,
    y_val: Optional[Union[int, float]] = None,
    z_val: Optional[Union[int, float]] = None,
    *,
    n: Optional[float] = None,
    y: Optional[float] = None,
    z: Optional[Union[int, float]] = None,
    zp: Optional[Union[int, float]] = None,
    p: Optional[Union[int, float]] = None,
    max_entry_price: Optional[Union[int, float]] = None,
    market: Optional[dict] = None,
    now: Any = None,
    check_timing: bool = False,
    yes_ask: Optional[int] = None,
    no_ask: Optional[int] = None,
    stop_loss: bool = False,
    **kwargs,
) -> Any:
    """
    Generalized sell_Xn_Ym_Zp momentum strategy:
    - n: resting sell order multiplier (e.g. 1.5x, 2x, 2.5x, 3x)
    - y: entry minute into the 15m cycle (e.g. 5m past, 10m past)
    - z: max entry price in cents (Zp). Optional; if not passed, defaults to int(100 / n) (e.g. 50 for 2n, 33 for 3n).
    - Selects whichever side has an ask price < z cents.
    - If both sides < z, selects the cheaper side.
    - If check_timing is True, ensures current minute into market is between y and y + 5m.
    """
    target_market = market
    if isinstance(past_results_or_market, dict):
        target_market = past_results_or_market
        if isinstance(n_or_market, (int, float)):
            n = float(n_or_market)
        if isinstance(y_val, (int, float)):
            y = float(y_val)
        if isinstance(z_val, (int, float)):
            z = float(z_val)
    elif isinstance(past_results_or_market, (int, float)):
        n = float(past_results_or_market)
        if isinstance(n_or_market, (int, float)):
            y = float(n_or_market)
        if isinstance(y_val, (int, float)):
            z = float(y_val)
    elif isinstance(n_or_market, dict):
        target_market = n_or_market

    n_num = float(n) if n is not None else 3.0
    y_num = float(y) if y is not None else 5.0

    z_input = z if z is not None else (zp if zp is not None else (p if p is not None else max_entry_price))
    if z_input is not None:
        effective_z = int(z_input)
    else:
        effective_z = int(100 / n_num) if n_num > 0 else 33

    # If called as factory without market/prices: e.g. sell_Xn_Ym(2, 5) or sell_Xn_Ym(2, 5, 45)
    if target_market is None and yes_ask is None and no_ask is None:
        def _bound_strategy(past_or_m=None, m=None, **kw):
            tm = past_or_m if isinstance(past_or_m, dict) else (m or target_market)
            return sell_Xn_Ym(tm, n=n_num, y=y_num, z=effective_z, **kw)
        _bound_strategy.n = n_num
        _bound_strategy.y = y_num
        _bound_strategy.z = effective_z
        _bound_strategy.stop_loss = stop_loss
        return _bound_strategy

    if check_timing and target_market:
        if not is_entry_time_ym(target_market, y_minutes=y_num, now=now):
            return None

    yes_price = yes_ask
    if yes_price is None and target_market:
        yes_price = extract_contract_price(target_market, PositionSide.YES)

    no_price = no_ask
    if no_price is None and target_market:
        no_price = extract_contract_price(target_market, PositionSide.NO)

    yes_valid = yes_price is not None and 0 < yes_price < effective_z
    no_valid = no_price is not None and 0 < no_price < effective_z

    if yes_valid and no_valid:
        return PositionSide.YES if yes_price <= no_price else PositionSide.NO
    if yes_valid:
        return PositionSide.YES
    if no_valid:
        return PositionSide.NO
    return None


sell_xn_ym = sell_Xn_Ym
sell_Xn_Ym_Zp = sell_Xn_Ym
sell_xn_ym_zp = sell_Xn_Ym


def sell_2n_5m(past_results_or_market: Any = None, market: Optional[dict] = None, z: Optional[Union[int, float]] = None, stop_loss: bool = False, **kwargs) -> Optional[PositionSide]:
    """sell_2n_5m strategy: n=2x resting sell order, y=5m entry, z=max entry price (default 100/2 = 50c)."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=2, y=5, z=z, stop_loss=stop_loss, **kwargs)


def sell_2n_5m_35p(past_results_or_market: Any = None, market: Optional[dict] = None, stop_loss: bool = False, **kwargs) -> Optional[PositionSide]:
    """sell_2n_5m_35p strategy: n=2x resting sell order, y=5m entry, z=35c max entry price, optional stop_loss."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=2, y=5, z=35, stop_loss=stop_loss, **kwargs)


def sell_3n(past_results_or_market: Any = None, market: Optional[dict] = None, z: Optional[Union[int, float]] = None, **kwargs) -> Optional[PositionSide]:
    """sell_3n strategy: n=3x resting sell order, y=5m entry, z=max entry price (default 100/3 = 33c)."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=3, y=5, z=z, **kwargs)


def sell_2n(past_results_or_market: Any = None, market: Optional[dict] = None, z: Optional[Union[int, float]] = None, **kwargs) -> Optional[PositionSide]:
    """sell_2n strategy: n=2x resting sell order, y=10m entry, z=max entry price (default 100/2 = 50c)."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=2, y=10, z=z, **kwargs)


def sell_1_5n(past_results_or_market: Any = None, market: Optional[dict] = None, z: Optional[Union[int, float]] = None, stop_loss: bool = False, **kwargs) -> Optional[PositionSide]:
    """sell_1.5n strategy: n=1.5x resting sell order, y=5m entry, z=max entry price (default 100/1.5 = 66c)."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=1.5, y=5, z=z, stop_loss=stop_loss, **kwargs)


def sell_1_5n_5m(past_results_or_market: Any = None, market: Optional[dict] = None, z: Optional[Union[int, float]] = None, stop_loss: bool = False, **kwargs) -> Optional[PositionSide]:
    """sell_1.5n_5m strategy: n=1.5x resting sell order, y=5m entry, max entry price z=35c (default 35c), optional stop_loss."""
    max_z = 35 if z is None else z
    return sell_Xn_Ym(past_results_or_market, market=market, n=1.5, y=5, z=max_z, stop_loss=stop_loss, **kwargs)


def sell_1_5n_5m_35p(past_results_or_market: Any = None, market: Optional[dict] = None, stop_loss: bool = False, **kwargs) -> Optional[PositionSide]:
    """sell_1.5n_5m_35p strategy: n=1.5x resting sell order, y=5m entry, z=35c max entry price, optional stop_loss."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=1.5, y=5, z=35, stop_loss=stop_loss, **kwargs)


def sell_1_5n_35p(past_results_or_market: Any = None, market: Optional[dict] = None, stop_loss: bool = False, **kwargs) -> Optional[PositionSide]:
    """sell_1.5n_35p strategy: n=1.5x resting sell order, y=5m entry, z=35c max entry price, optional stop_loss."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=1.5, y=5, z=35, stop_loss=stop_loss, **kwargs)


def should_stop_loss(
    entry_price_cents: Optional[int] = None,
    current_price_cents: Optional[int] = None,
    minute: Optional[float] = None,
    stop_minute: Optional[float] = None,
    stop_price_threshold: int = 25,
    stop_ratio_threshold: float = 0.5,
    strategy: Any = None,
    **kwargs,
) -> bool:
    """
    Check if a position should be liquidated under the stop-loss rule:
    For 5m sell strategies (entry at 5m): checks at minute >= 10.
    For direction strategies (last_9, random): checks at minute >= 5 (5m and 10m into the market).
    Triggers if:
      - is trading at <= 25c OR
      - current market price drops below 50% of entry price (< 0.5 * entry_price).
    """
    entry = kwargs.get("entry_price", entry_price_cents)
    current = kwargs.get("current_price", current_price_cents)
    strat = kwargs.get("strategy", strategy)

    if entry is None or current is None or entry <= 0:
        return False

    if stop_minute is None:
        strat_str = str(getattr(strat, "value", strat) or "").lower().strip()
        if strat_str in ("random", "last_9", "last_5", "last_3") or "random" in strat_str or "last" in strat_str:
            stop_minute = 5.0
        else:
            stop_minute = 10.0

    # Only exit at minute >= stop_minute
    if minute is None or minute < stop_minute:
        return False

    # Condition 1: trading at less than or equal to 25c
    if current <= stop_price_threshold:
        return True

    # Condition 2: current market price drops below 50% of entry price
    if current < (entry * stop_ratio_threshold):
        return True

    return False


def sell_2_5n(past_results_or_market: Any = None, market: Optional[dict] = None, z: Optional[Union[int, float]] = None, **kwargs) -> Optional[PositionSide]:
    """sell_2.5n strategy: n=2.5x resting sell order, y=5m entry, z=max entry price (default 100/2.5 = 40c)."""
    return sell_Xn_Ym(past_results_or_market, market=market, n=2.5, y=5, z=z, **kwargs)


# Backward compatibility aliases
choose_sell_if_3n = sell_3n
choose_side_sell_if_3n = sell_3n
choose_sell_if_2n = sell_2n
choose_side_sell_if_2n = sell_2n
choose_sell_if_2n_5m = sell_2n_5m
choose_side_sell_2n_5m = sell_2n_5m
choose_sell_if_2n_5m_35p = sell_2n_5m_35p
choose_side_sell_2n_5m_35p = sell_2n_5m_35p
choose_sell_if_1_5n = sell_1_5n
choose_side_sell_1_5n = sell_1_5n
choose_sell_if_1_5n_35p = sell_1_5n_35p
choose_side_sell_1_5n_35p = sell_1_5n_35p
choose_sell_if_2_5n = sell_2_5n
choose_side_sell_2_5n = sell_2_5n


def _parse_time(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if isinstance(val, str):
        cleaned = val.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def get_market_minute(market_or_pos: dict, now: Any = None) -> Optional[float]:
    """
    Calculate elapsed minutes into the 15-minute market.
    Supports explicit 'minute'/'minutes_past' fields, parses 'open_time'/'close_time',
    or falls back to wall-clock time aligned to the 15-minute cycle (:00, :15, :30, :45).
    """
    if not isinstance(market_or_pos, dict):
        return None

    if "minutes_past" in market_or_pos:
        return float(market_or_pos["minutes_past"])
    if "minute" in market_or_pos:
        return float(market_or_pos["minute"])

    now_dt = _parse_time(now) or datetime.now(timezone.utc)

    if market_or_pos.get("open_time"):
        open_dt = _parse_time(market_or_pos["open_time"])
        if open_dt:
            diff_min = (now_dt - open_dt).total_seconds() / 60.0
            if now is not None or (0 <= diff_min <= 30.0):
                return diff_min

    if market_or_pos.get("close_time"):
        close_dt = _parse_time(market_or_pos["close_time"])
        if close_dt:
            start_dt = close_dt - timedelta(minutes=15)
            diff_min = (now_dt - start_dt).total_seconds() / 60.0
            if now is not None or (0 <= diff_min <= 30.0):
                return diff_min

    if now is not None:
        return float(now_dt.minute % 15) + (now_dt.second / 60.0)

    return None


def is_entry_time_ym(market: dict, y_minutes: float = 5.0, window_minutes: float = 5.0, now: Any = None) -> bool:
    """Enter market at y_minutes past (between y and y + window_minutes into the 15m cycle)."""
    minute = get_market_minute(market, now=now)
    if minute is None:
        return True
    return float(y_minutes) <= minute < float(y_minutes + window_minutes)


def is_entry_time_5m(market: dict, now: Any = None) -> bool:
    """Enter market at 5 minutes past (between 5 and 10 minutes into the market)."""
    return is_entry_time_ym(market, y_minutes=5.0, now=now)


def is_entry_time_10m(market: dict, now: Any = None) -> bool:
    """Enter market at 10 minutes past (between 10 and 15 minutes into the market)."""
    return is_entry_time_ym(market, y_minutes=10.0, now=now)


def is_last_n_strategy(strategy: Any) -> bool:
    """Return True if strategy is a last_N direction strategy."""
    strat_clean = str(getattr(strategy, "value", strategy) or "").strip().lower()
    return (
        strat_clean.startswith("last_")
        or strat_clean.startswith("last")
        or strat_clean in ("last_21", "last_21_spot", "last_9", "last_9_spot", "last_5", "last_3", "regime_aware", "regime_spot", "regime")
        or strategy in (
            Strategy.LAST_21,
            Strategy.LAST_21_SPOT,
            Strategy.REGIME_AWARE,
            Strategy.REGIME_SPOT,
            Strategy.LAST_9,
            Strategy.LAST_9_SPOT,
            Strategy.LAST_5,
            Strategy.LAST_3,
            choose_side_regime_aware,
            regime_aware,
            regime_spot,
            choose_side_last_21,
            last_21_spot,
            choose_side_last_9,
            last_9_spot,
            strategy_last_21,
            strategy_last_9,
        )
    )


def is_last_n_entry_time(market: dict, now: Any = None) -> bool:
    """
    Check if current time is within valid entry windows for last_N strategies:
    - Initial entry: between 0 and 5 minutes into the 15m market (0.0 <= minute < 5.0)
    - 5-minute entry: between 5 and 10 minutes into the 15m market (5.0 <= minute < 10.0)
    Reject if >= 10 minutes past (final 5 minutes before expiration).
    """
    minute = get_market_minute(market, now=now)
    if minute is None:
        return True
    return 0.0 <= minute < 10.0


def is_entry_time(market: dict, strategy: Any = None, now: Any = None) -> bool:
    """Check if current time is within entry window for the strategy."""
    if is_last_n_strategy(strategy):
        return is_last_n_entry_time(market, now=now)
    y = get_strategy_entry_minute(strategy)
    if y is None:
        y = 5.0
    return is_entry_time_ym(market, y_minutes=float(y), now=now)


def is_checkin_time(market_or_pos: dict, now: Any = None) -> bool:
    """Check in at 10 minutes past into the market."""
    minute = get_market_minute(market_or_pos, now=now)
    if minute is None:
        return True
    return minute >= 10.0


def get_strategy_resting_multiplier(strategy: Any) -> Optional[Union[int, float]]:
    """Return resting sell order multiplier: 3 for SELL_3N, 2 for SELL_2N / SELL_2N_5M, 1.5 for SELL_1.5N, 2.5 for SELL_2.5N."""
    if hasattr(strategy, "n"):
        n = getattr(strategy, "n")
        return int(n) if float(n).is_integer() else float(n)
    strat_str = str(getattr(strategy, "value", strategy) or "").lower().strip()
    if not strat_str:
        return None
    if "1.5" in strat_str or "1_5" in strat_str:
        return 1.5
    if "2.5" in strat_str or "2_5" in strat_str:
        return 2.5
    if strat_str in ("sell_3n", "sell3n", "sell_if_3n") or "3n" in strat_str or "3x" in strat_str:
        return 3
    if "2n" in strat_str or "2x" in strat_str:
        return 2
    match = re.search(r"sell_?([0-9.]+)n", strat_str)
    if match:
        val = float(match.group(1))
        return int(val) if val.is_integer() else val
    return None


def get_strategy_entry_minute(strategy: Any) -> Optional[Union[int, float]]:
    """Return entry minute into 15m cycle: 10m for sell_2n, 5m for sell_3n, sell_2n_5m, sell_1.5n, sell_2.5n."""
    if hasattr(strategy, "y"):
        y = getattr(strategy, "y")
        return int(y) if float(y).is_integer() else float(y)
    strat_str = str(getattr(strategy, "value", strategy) or "").lower().strip()
    if not strat_str:
        return None
    if strat_str in ("sell_2n", "sell2n", "sell_if_2n", "sell_2n_10m"):
        return 10
    if "10m" in strat_str:
        return 10
    if "5m" in strat_str or "sell" in strat_str:
        return 5
    match = re.search(r"_([0-9.]+)m", strat_str)
    if match:
        val = float(match.group(1))
        return int(val) if val.is_integer() else val
    return None


def get_strategy_max_entry_price(strategy: Any) -> int:
    r"""Return max entry price (Zp) for strategy: explicit .z / '_(\d+)p', or defaults to int(100 / n)."""
    if hasattr(strategy, "z") and getattr(strategy, "z") is not None:
        return int(getattr(strategy, "z"))
    strat_str = str(getattr(strategy, "value", strategy) or "").lower().strip()
    match = re.search(r"(\d+)p\b", strat_str)
    if match:
        return int(match.group(1))
    if "sell_1.5n_5m" in strat_str or "sell_1_5n_5m" in strat_str or "sell1.5n_5m" in strat_str or "sell1_5n_5m" in strat_str:
        return 35
    if "sell_1.5n_35p" in strat_str or "sell_1_5n_35p" in strat_str:
        return 35
    n = get_strategy_resting_multiplier(strategy)
    if n and n > 0:
        return int(100 / n)
    return 33


def is_sell_strategy(strategy: Any) -> bool:
    """Check if strategy is any sell_Xn_Ym strategy with a resting multiplier."""
    return get_strategy_resting_multiplier(strategy) is not None


def should_sell(entry_price_cents: int, current_price_cents: Optional[int], multiplier: float = 3.0) -> bool:
    """Check if current contract value is at least multiplier times what we entered at."""
    if current_price_cents is None or entry_price_cents <= 0:
        return False
    return current_price_cents >= multiplier * entry_price_cents


STRATEGIES = {
    Strategy.RANDOM.value: choose_side_random,
    Strategy.LAST_21.value: choose_side_last_21,
    Strategy.LAST_21_SPOT.value: last_21_spot,
    Strategy.REGIME_AWARE.value: choose_side_regime_aware,
    Strategy.REGIME_SPOT.value: regime_spot,
    "regime_aware": choose_side_regime_aware,
    "regime": choose_side_regime_aware,
    "regime_spot": regime_spot,
    "regimespot": regime_spot,
    Strategy.LAST_3.value: choose_side_last_9,
    Strategy.LAST_5.value: choose_side_last_9,
    Strategy.LAST_9.value: choose_side_last_9,
    Strategy.LAST_9_SPOT.value: last_9_spot,
    "last_21": choose_side_last_21,
    "last21": choose_side_last_21,
    "last_21_strict": strategy_last_21,
    "last_21_spot": last_21_spot,
    "last21_spot": last_21_spot,
    "last_9": choose_side_last_9,
    "last9": choose_side_last_9,
    "last_9_strict": strategy_last_9,
    "last_9_spot": last_9_spot,
    "last9_spot": last_9_spot,
    "last_5": choose_side_last_9,
    "last5": choose_side_last_9,
    "last_3": choose_side_last_9,
    "last3": choose_side_last_9,
    Strategy.SELL_3N.value: sell_3n,
    Strategy.SELL_2N.value: sell_2n,
    Strategy.SELL_2N_5M.value: sell_2n_5m,
    Strategy.SELL_2N_5M_35P.value: sell_2n_5m_35p,
    Strategy.SELL_1_5N.value: sell_1_5n,
    Strategy.SELL_1_5N_5M.value: sell_1_5n_5m,
    Strategy.SELL_1_5N_5M_35P.value: sell_1_5n_5m_35p,
    Strategy.SELL_1_5N_35P.value: sell_1_5n_35p,
    Strategy.SELL_2_5N.value: sell_2_5n,
    "sell_2n_5m_35p": sell_2n_5m_35p,
    "sell_2_n_5m_35p": sell_2n_5m_35p,
    "sell_1_5n": sell_1_5n,
    "sell_1.5n_5m": sell_1_5n_5m,
    "sell_1_5n_5m": sell_1_5n_5m,
    "sell_1.5n_5m_35p": sell_1_5n_5m_35p,
    "sell_1_5n_5m_35p": sell_1_5n_5m_35p,
    "sell_1.5n_35p": sell_1_5n_35p,
    "sell_1_5n_35p": sell_1_5n_35p,
    "sell_2_5n": sell_2_5n,
}


@dataclass
class Config:
    """Configuration injected into TradeEngine and RiskEngine."""
    env: str = "demo"
    max_open_positions: int = 10
    daily_loss_cap_cents: int = 5000  # $50 daily loss cap
    kill_switch_path: str = "KILL_SWITCH"
    shadow_mode: bool = False
    stop_loss: bool = False
    check_spot: bool = False
    min_entry_price: Optional[int] = None
    max_entry_price: Optional[int] = None
    default_contract_price: int = 50
    live_orders: bool = False
    take_profit: Optional[int] = None
    regime_filter: bool = False
    taker_ioc: bool = False


class TradeEngine:
    """
    Pure orchestrator for evaluating markets, checking risk, and placing orders.
    Decoupled from direct I/O:
    - Logic (Strategy, RiskRules): pure functions, no network I/O
    - Database: injected repository (Database)
    - Execution: injected broker (KalshiBroker or SimulatedBroker)
    """
    def __init__(
        self,
        repo: Database,
        broker: Broker,
        strategy_fn: Any = choose_side_last_9,
        strategy_name: Union[str, Strategy] = Strategy.LAST_9,
        config: Optional[Config] = None,
        rules: Optional[list[Callable[[RiskContext], Optional[str]]]] = None,
    ):
        self.repo = repo
        self.broker = broker
        self.strategy_fn = strategy_fn
        self.strategy_name = (
            normalize_strategy(strategy_name)
            if isinstance(strategy_name, (str, Strategy))
            else Strategy.LAST_9
        )
        self.config = config or Config()
        self.rules = rules if rules is not None else DEFAULT_RISK_RULES
        self.reconciler = Reconciler(broker=self.broker, db=self.repo)

    def run_cycle(self) -> list[Decision]:
        """
        Orchestration only. Reads like a story:
        Query candidate markets -> process each market -> catch exceptions so one bad market never kills the cycle.
        """
        # Periodic reconciliation of any lingering PENDING positions
        try:
            self.reconciler.reconcile()
        except Exception as e:
            analytics.warning(f"Reconciliation error in run_cycle: {e}")

        candidates = self.repo.get_candidates(
            environment=self.config.env,
            strategy=self.strategy_name,
        )
        decisions: list[Decision] = []
        for market in candidates:
            try:
                decision = self._process(market)
                if decision:
                    decisions.append(decision)
            except Exception as e:
                ticker = market.get("ticker", "unknown")
                analytics.exception(
                    f"Market {ticker} failed during cycle: {e}",
                    extra={"ticker": ticker, "strategy": self.strategy_name.value},
                )
        return decisions

    def _process(self, market: dict[str, Any], now: Optional[datetime] = None) -> Optional[Decision]:
        ticker = market.get("ticker")
        if not ticker:
            return None

        # 1. Evaluate pure risk rules
        ctx = RiskContext(
            market=market,
            strategy_name=self.strategy_name.value,
            db=self.repo,
            max_open_positions=self.config.max_open_positions,
            daily_loss_cap_cents=self.config.daily_loss_cap_cents,
            kill_switch_path=self.config.kill_switch_path,
            now=now,
        )
        reason = evaluate_risk_rules(ctx, self.rules)
        if reason is not None:
            decision = Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=None,
                reason=reason,
            )
            self.repo.log_event(
                stage="risk_check",
                level="INFO",
                message=f"Market {ticker} skipped: {reason}",
                ticker=ticker,
                strategy=self.strategy_name.value,
            )
            return decision

        # 2. Get past resolutions for strategy
        series = ticker.split("-")[0]
        past_results = self.repo.get_recent_results(series, limit=21) if self.repo else []

        # 3. Pure strategy evaluation
        side = None
        if callable(self.strategy_fn):
            try:
                side = self.strategy_fn(
                    past_results=past_results,
                    market=market,
                    check_spot=self.config.check_spot,
                    min_entry_price=self.config.min_entry_price,
                    max_entry_price=self.config.max_entry_price,
                )
            except TypeError:
                side = self.strategy_fn(past_results=past_results)

        if side is None:
            return Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=None,
                reason="strategy_no_opinion",
            )

        # 4. Extract price in cents
        ask_cents = extract_contract_price(market, side)
        if ask_cents is None:
            price_cents = self.config.default_contract_price
        else:
            price_cents = max(1, min(99, ask_cents))

        # Check price bounds
        if self.config.min_entry_price is not None and price_cents < self.config.min_entry_price:
            return Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=side,
                price_cents=price_cents,
                reason=f"ask_{price_cents}c_below_min_{self.config.min_entry_price}c",
            )
        if self.config.max_entry_price is not None and price_cents > self.config.max_entry_price:
            return Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=side,
                price_cents=price_cents,
                reason=f"ask_{price_cents}c_above_max_{self.config.max_entry_price}c",
            )

        # 5. Shadow Mode: log decision without placing order
        if self.config.shadow_mode:
            self.repo.log_event(
                stage="trader",
                level="INFO",
                message=f"[SHADOW] Would bet 1 contract on {side.value} for {ticker} at {price_cents}c",
                ticker=ticker,
                strategy=self.strategy_name.value,
            )
            return Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=side,
                price_cents=price_cents,
                shadow=True,
            )

        # 6. Deterministic client_order_id: env + strategy + ticker + side
        safe_env = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.config.env).lower())
        safe_strategy = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.strategy_name.value).lower())
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        client_order_id = f"{safe_env}_{safe_strategy}_{safe_ticker}_{side.value.lower()}"[:64]

        # 7. Write intent before API call (State machine: PENDING)
        close_time = market.get("close_time", "")
        self.repo.insert_pending_position(
            client_order_id=client_order_id,
            ticker=ticker,
            side=side,
            count=1,
            price_cents=price_cents,
            close_time=close_time,
            environment=self.config.env,
            strategy=self.strategy_name,
        )
        self.repo.log_event(
            stage="trader",
            level="INFO",
            message=f"Write intent: PENDING 1 contract on {side.value} for {ticker} at {price_cents}c",
            ticker=ticker,
            strategy=self.strategy_name.value,
        )

        # 8. Submit order via Broker
        order_result = None
        try:
            order_result = self.broker.place_order(
                ticker=ticker,
                side=side,
                count=1,
                price_cents=price_cents,
                client_order_id=client_order_id,
                action="buy",
            )
        except Exception as e:
            self.repo.update_position_status(client_order_id, PositionStatus.FAILED)
            self.repo.log_event(
                stage="trader",
                level="ERROR",
                message=f"Broker error for {ticker}: {e}",
                ticker=ticker,
                strategy=self.strategy_name.value,
            )
            return Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=side,
                price_cents=price_cents,
                reason=f"broker_error_{e}",
            )

        # 9. Verify order success & update state machine
        if not order_result or (isinstance(order_result, dict) and "error" in order_result):
            self.repo.update_position_status(client_order_id, PositionStatus.FAILED)
            self.repo.log_event(
                stage="trader",
                level="WARNING",
                message=f"Order rejected on Kalshi for {ticker}: {order_result}. Marked FAILED.",
                ticker=ticker,
                strategy=self.strategy_name.value,
            )
            return Decision(
                ticker=ticker,
                market=market,
                strategy=self.strategy_name.value,
                side=side,
                price_cents=price_cents,
                reason="order_rejected_by_exchange",
            )

        # Transition PENDING -> OPEN in SQLite
        self.repo.update_position_status(client_order_id, PositionStatus.OPEN)
        self.repo.log_event(
            stage="trader",
            level="INFO",
            message=f"Order filled: OPEN 1 contract on {side.value} for {ticker} at {price_cents}c",
            ticker=ticker,
            strategy=self.strategy_name.value,
        )

        # 10. For momentum sell strategies or take_profit: place resting sell order if applicable
        multiplier = get_strategy_resting_multiplier(self.strategy_name)
        take_profit = getattr(self.config, "take_profit", None)
        if multiplier is not None or take_profit is not None:
            if take_profit is not None:
                resting_price_cents = min(99, int(take_profit))
            else:
                resting_price_cents = min(99, int(round(multiplier * price_cents)))
            resting_order_id = f"{safe_strategy}_resting_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"[:64]
            try:
                resting_res = self.broker.place_order(
                    ticker=ticker,
                    side=side,
                    count=1,
                    price_cents=resting_price_cents,
                    client_order_id=resting_order_id,
                    action="sell",
                    time_in_force="good_till_canceled",
                )
                if resting_res and isinstance(resting_res, dict) and "error" not in resting_res:
                    actual_rest_id = resting_res.get("order", {}).get("order_id") or resting_res.get("order_id") or resting_order_id
                    self.repo.update_position_status(client_order_id, PositionStatus.OPEN, resting_order_id=actual_rest_id)
            except Exception as e:
                analytics.warning(f"Could not place resting sell order for {ticker}: {e}")

        return Decision(
            ticker=ticker,
            market=market,
            strategy=self.strategy_name.value,
            side=side,
            price_cents=price_cents,
        )


class RiskEngine:
    def __init__(
        self,
        client,
        db: Optional[Database] = None,
        strategy: Union[str, Strategy, Any] = Strategy.LAST_9,
        stop_loss: bool = False,
        spot_client: Optional[Any] = None,
        check_spot: bool = False,
        min_entry_price: Optional[int] = None,
        max_entry_price: Optional[int] = None,
        take_profit: Optional[int] = None,
        broker: Optional[Broker] = None,
        config: Optional[Config] = None,
        entry_timeout_seconds: Optional[float] = None,
    ):
        # Kalshi REST API client and optional SQLite database
        self.client = client
        self.db: Optional[Database] = db
        self.strategy = strategy
        self.stop_loss: bool = bool(stop_loss or getattr(strategy, "stop_loss", False))
        self.spot_client = spot_client
        self.check_spot: bool = bool(check_spot or getattr(strategy, "check_spot", False))
        self.min_entry_price: Optional[int] = min_entry_price if min_entry_price is not None else getattr(strategy, "min_entry_price", None)
        self.max_entry_price: Optional[int] = max_entry_price if max_entry_price is not None else getattr(strategy, "max_entry_price", None)
        self.take_profit: Optional[int] = int(take_profit) if take_profit is not None else getattr(strategy, "take_profit", None)
        self.entry_timeout_seconds: Optional[float] = (
            float(entry_timeout_seconds) if entry_timeout_seconds is not None else getattr(strategy, "entry_timeout_seconds", None)
        )

        # Check what type of input 'strategy' is:
        # 1. An existing Strategy enum object
        # 2. A string name (e.g. "random", "last_9")
        # 3. A function/callable (e.g. choose_side_random)
        # 4. Anything else (default fallback)
        if isinstance(strategy, Strategy):
            self.strategy_name = strategy

        elif isinstance(strategy, str):
            strat_lower = strategy.strip().lower()

            # Enable stop-loss if common abbreviations appear in the name
            if "_sl" in strat_lower or "stop_loss" in strat_lower or "stoploss" in strat_lower:
                self.stop_loss = True

            # Enable take-profit if tp85 or tp_85 appear in the name
            m_tp = re.search(r"tp_?(\d+)", strat_lower)
            if m_tp and self.take_profit is None:
                self.take_profit = int(m_tp.group(1))

            if strat_lower == "random":
                self.strategy_name = Strategy.RANDOM
            elif strat_lower in ("last_21", "last21"):
                self.strategy_name = Strategy.LAST_21
            elif strat_lower in ("last_21_spot", "last21_spot", "last_21_cf", "last21_cf"):
                self.strategy_name = Strategy.LAST_21_SPOT
                self.check_spot = True
                if self.min_entry_price is None:
                    self.min_entry_price = 20
                if self.max_entry_price is None:
                    self.max_entry_price = 65
            elif strat_lower in ("regime_spot", "regimespot"):
                self.strategy_name = Strategy.REGIME_SPOT
                self.check_spot = True
                self.taker_ioc = True
                if self.min_entry_price is None:
                    self.min_entry_price = 20
                if self.max_entry_price is None:
                    self.max_entry_price = 65
            elif strat_lower in ("regime_aware", "regime", "regimeaware"):
                self.strategy_name = Strategy.REGIME_AWARE
                self.taker_ioc = True
                if self.min_entry_price is None:
                    self.min_entry_price = 20
                if self.max_entry_price is None:
                    self.max_entry_price = 65
            elif strat_lower in ("last_9", "last9"):
                self.strategy_name = Strategy.LAST_9
            elif strat_lower in ("last_9_spot", "last9_spot", "last_9_cf", "last9_cf"):
                self.strategy_name = Strategy.LAST_9_SPOT
                self.check_spot = True
                if self.min_entry_price is None:
                    self.min_entry_price = 20
                if self.max_entry_price is None:
                    self.max_entry_price = 65
            elif strat_lower in ("last_5", "last5", "last_3", "last3"):
                analytics.warning(f"Strategy '{strat_lower}' is deprecated. Using 'last_9'.")
                self.strategy_name = Strategy.LAST_9
            elif strat_lower in ("sell_3n", "sell3n", "sell_if_3n"):
                self.strategy_name = Strategy.SELL_3N
            elif strat_lower in ("sell_2n_5m_35p", "sell_2_n_5m_35p", "sell2n_5m_35p"):
                self.strategy_name = Strategy.SELL_2N_5M_35P
            elif strat_lower in ("sell_2n_5m", "sell_5m_2n", "sell_2x_5m", "sell_3n_2x"):
                self.strategy_name = Strategy.SELL_2N_5M
            elif strat_lower in ("sell_2n", "sell2n", "sell_if_2n"):
                self.strategy_name = Strategy.SELL_2N
            elif strat_lower in ("sell_1.5n_5m_35p", "sell_1_5n_5m_35p", "sell1.5n_5m_35p", "sell1_5n_5m_35p"):
                self.strategy_name = Strategy.SELL_1_5N_5M_35P
            elif strat_lower in ("sell_1.5n_35p", "sell_1_5n_35p", "sell1.5n_35p", "sell1_5n_35p"):
                self.strategy_name = Strategy.SELL_1_5N_35P
            elif strat_lower in ("sell_1.5n_5m", "sell_1_5n_5m", "sell1.5n_5m", "sell1_5n_5m"):
                self.strategy_name = Strategy.SELL_1_5N_5M
            elif strat_lower in ("sell_1.5n", "sell_1_5n", "sell1.5n", "sell1_5n"):
                self.strategy_name = Strategy.SELL_1_5N
            elif strat_lower in ("sell_2.5n", "sell_2_5n", "sell2.5n", "sell2_5n", "sell_2.5n_5m"):
                self.strategy_name = Strategy.SELL_2_5N
            else:
                try:
                    self.strategy_name = Strategy(strat_lower)
                except ValueError:
                    self.strategy_name = Strategy.LAST_5

        elif callable(strategy):
            if strategy is choose_side_random:
                self.strategy_name = Strategy.RANDOM
            elif strategy in (last_21_spot, last_9_spot):
                self.strategy_name = Strategy.LAST_21_SPOT if strategy is last_21_spot else Strategy.LAST_9_SPOT
                self.check_spot = True
                if self.min_entry_price is None:
                    self.min_entry_price = 20
                if self.max_entry_price is None:
                    self.max_entry_price = 65
            elif strategy in (regime_spot, choose_side_regime_aware, regime_aware):
                self.strategy_name = Strategy.REGIME_SPOT if strategy is regime_spot else Strategy.REGIME_AWARE
                self.taker_ioc = True
                if strategy is regime_spot:
                    self.check_spot = True
                if self.min_entry_price is None:
                    self.min_entry_price = 20
                if self.max_entry_price is None:
                    self.max_entry_price = 65
            elif strategy in (sell_3n, choose_sell_if_3n, choose_side_sell_if_3n):
                self.strategy_name = Strategy.SELL_3N
            elif strategy in (sell_2n_5m_35p, choose_sell_if_2n_5m_35p, choose_side_sell_2n_5m_35p):
                self.strategy_name = Strategy.SELL_2N_5M_35P
            elif strategy in (sell_2n_5m, choose_sell_if_2n_5m, choose_side_sell_2n_5m):
                self.strategy_name = Strategy.SELL_2N_5M
            elif strategy in (sell_2n, choose_sell_if_2n, choose_side_sell_2n):
                self.strategy_name = Strategy.SELL_2N
            elif strategy in (sell_1_5n_35p, choose_sell_if_1_5n_35p, choose_side_sell_1_5n_35p):
                self.strategy_name = Strategy.SELL_1_5N_35P
            elif strategy is sell_1_5n_5m:
                self.strategy_name = Strategy.SELL_1_5N_5M
            elif strategy in (sell_1_5n, choose_sell_if_1_5n, choose_side_sell_1_5n):
                self.strategy_name = Strategy.SELL_1_5N
            elif strategy in (sell_2_5n, choose_sell_if_2_5n, choose_side_sell_2_5n):
                self.strategy_name = Strategy.SELL_2_5N
            else:
                self.strategy_name = Strategy.LAST_5

        else:
            # Default fallback if strategy is none of the above types
            self.strategy_name = Strategy.LAST_5

        self.is_sell_strategy = is_sell_strategy(self.strategy_name)
        if self.is_sell_strategy:
            self.max_entry_price = get_strategy_max_entry_price(self.strategy)

        if self.check_spot and self.spot_client is None:
            from ingestion.spot import SpotClient
            self.spot_client = SpotClient()

        # Set of market tickers where this strategy already placed its bet
        self.bet_markets = set()
        self.open_contract_tickers = set()
        if self.db is not None:
            self.bet_markets = self.db.get_positioned_tickers(strategy=self.strategy_name)
            if hasattr(self.db, "get_open_positioned_tickers"):
                self.open_contract_tickers = self.db.get_open_positioned_tickers(strategy=self.strategy_name)

        if broker is not None:
            self.broker = broker
        elif client is not None:
            is_live = bool(getattr(client, "live_orders", False))
            self.broker = KalshiBroker(client) if is_live else SimulatedBroker(client)
        else:
            self.broker = SimulatedBroker()

        self.config = config or Config(
            env=getattr(self.db, "environment", "demo") if self.db else "demo",
            stop_loss=self.stop_loss,
            check_spot=self.check_spot,
            min_entry_price=self.min_entry_price,
            max_entry_price=self.max_entry_price,
            live_orders=bool(self.client and getattr(self.client, "live_orders", False)),
        )

        if self.db is not None:
            self.trade_engine = TradeEngine(
                repo=self.db,
                broker=self.broker,
                strategy_fn=self.strategy if callable(self.strategy) else STRATEGIES.get(str(self.strategy_name.value), choose_side_last_9),
                strategy_name=self.strategy_name,
                config=self.config,
            )
        else:
            self.trade_engine = None

    def _is_contract_held(self, ticker: str) -> bool:
        """Check if an open contract is currently being held for this market and strategy."""
        if self.db is not None:
            if hasattr(self.db, "has_open_position"):
                return self.db.has_open_position(ticker, strategy=self.strategy_name)
            try:
                open_pos = [
                    p for p in self.db.get_all_positions()
                    if str(p.get("status", "")).upper() in (PositionStatus.OPEN.value, PositionStatus.PENDING.value)
                    and p.get("strategy") == self.strategy_name
                    and p.get("ticker") == ticker
                ]
                return bool(open_pos)
            except Exception:
                pass
        return ticker in self.open_contract_tickers

    def is_market_open(self, market):
        # Check if market status is open or active
        status = str(market.get("status", "")).lower()
        return status in (MarketStatus.OPEN.value, MarketStatus.ACTIVE.value)

    def is_binary_market(self, market, event_market_count=None):
        # Check if market is a standalone binary market (only buy_yes / buy_no)
        return is_binary_market(market, event_market_count=event_market_count)

    def pick_random_side(self) -> PositionSide:
        # Randomly pick YES or NO
        return choose_side_random()

    def get_last_results(self, ticker: str, limit: int = 21) -> list[Any]:
        # Get the last N resolved results for this market's series (e.g. 'KXBTC15M')
        series = ticker.split("-")[0]
        results = []

        target_len = 21 if getattr(self, "strategy_name", None) in (Strategy.LAST_21, Strategy.LAST_21_SPOT, Strategy.REGIME_AWARE, Strategy.REGIME_SPOT) else 9
        target_limit = max(limit, target_len)

        # 1. First check SQLite database
        if self.db is not None:
            results = self.db.get_recent_results(series, limit=target_limit)

        # 2. If SQLite has fewer than target_len resolved markets, query Kalshi API for fresh settled markets
        if len(results) < target_len and self.client is not None and hasattr(self.client, "get_settled_markets"):
            settled_markets = self.client.get_settled_markets(series, limit=target_limit)
            if settled_markets:
                if self.db is not None:
                    self.db.upsert_markets(settled_markets)
                api_results = [
                    MarketResolution(
                        result=m.get("result", ""),
                        floor_strike=m.get("floor_strike"),
                        expiration_value=m.get("expiration_value"),
                        ticker=m.get("ticker"),
                    )
                    for m in settled_markets
                    if str(m.get("result", "")).strip().lower() in ("yes", "no")
                ]
                if api_results:
                    results = api_results[:target_limit]

        return results

    # Aliases for backwards compatibility
    get_last_21_results = get_last_results
    get_last_9_results = get_last_results
    get_last_5_results = get_last_results
    get_last_3_results = get_last_results

    def choose_side(self, ticker: str, market: Optional[dict] = None) -> Optional[PositionSide]:
        # Run the configured strategy function to pick YES (up) or NO (down)
        if callable(self.strategy):
            strategy_fn = self.strategy
        else:
            strategy_fn = STRATEGIES.get(str(self.strategy), choose_side_last_21)

        if strategy_fn is choose_side_random:
            return choose_side_random()

        if (
            self.is_sell_strategy
            or strategy_fn in (
                sell_Xn_Ym,
                sell_xn_ym,
                sell_Xn_Ym_Zp,
                sell_xn_ym_zp,
                sell_3n,
                sell_2n,
                sell_2n_5m,
                sell_1_5n,
                sell_2_5n,
                choose_sell_if_3n,
                choose_side_sell_if_3n,
                choose_sell_if_2n,
                choose_side_sell_if_2n,
                choose_sell_if_2n_5m,
                choose_side_sell_2n_5m,
                choose_sell_if_1_5n,
                choose_side_sell_1_5n,
                choose_sell_if_2_5n,
                choose_side_sell_2_5n,
            )
        ):
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)
            return strategy_fn(market=market)

        if (
            self.strategy_name in (Strategy.REGIME_AWARE, Strategy.REGIME_SPOT)
            or strategy_fn in (choose_side_regime_aware, regime_aware, regime_spot)
        ):
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)
            past_results = self.get_last_results(ticker, limit=21)
            return choose_side_regime_aware(
                past_results=past_results,
                market=market,
                spot_client=self.spot_client,
                check_spot=self.check_spot,
                min_entry_price=self.min_entry_price,
                max_entry_price=self.max_entry_price,
            )

        if (
            self.strategy_name in (Strategy.LAST_21, Strategy.LAST_21_SPOT, Strategy.LAST_9, Strategy.LAST_9_SPOT)
            or strategy_fn in (choose_side_last_21, last_21_spot, choose_side_last_9, last_9_spot)
            or self.check_spot
            or self.min_entry_price is not None
            or self.max_entry_price is not None
        ):
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)
            past_results = self.get_last_results(ticker, limit=21)
            return choose_side_last_21(
                past_results=past_results,
                market=market,
                spot_client=self.spot_client,
                check_spot=self.check_spot,
                min_entry_price=self.min_entry_price,
                max_entry_price=self.max_entry_price,
                limit=21,
            )

        past_results = self.get_last_results(ticker, limit=21)
        return strategy_fn(past_results)

    def _extract_ask_cents(self, market, side):
        # Extract ask price in cents (1-99), supporting both integer cents and dollar strings.
        # Returns None if the orderbook has no ask (never defaults to 50 cents).
        cents = None
        if side == PositionSide.YES:
            if market.get("yes_ask") is not None:
                cents = int(market["yes_ask"])
            elif market.get("yes_ask_dollars") is not None:
                cents = int(round(float(market["yes_ask_dollars"]) * 100))
            elif market.get("no_bid") is not None:
                cents = 100 - int(market["no_bid"])
            elif market.get("no_bid_dollars") is not None:
                cents = 100 - int(round(float(market["no_bid_dollars"]) * 100))
        else:
            if market.get("no_ask") is not None:
                cents = int(market["no_ask"])
            elif market.get("no_ask_dollars") is not None:
                cents = int(round(float(market["no_ask_dollars"]) * 100))
            elif market.get("yes_bid") is not None:
                cents = 100 - int(market["yes_bid"])
            elif market.get("yes_bid_dollars") is not None:
                cents = 100 - int(round(float(market["yes_bid_dollars"]) * 100))

        if cents is None or cents <= 0 or cents >= 100:
            return None
        return cents

    def _extract_bid_cents(self, market, side):
        # Extract bid price in cents (1-99), supporting both integer cents and dollar strings.
        # Returns None if the orderbook has no bid.
        cents = None
        if side == PositionSide.YES:
            if market.get("yes_bid") is not None:
                cents = int(market["yes_bid"])
            elif market.get("yes_bid_dollars") is not None:
                cents = int(round(float(market["yes_bid_dollars"]) * 100))
            elif market.get("no_ask") is not None:
                cents = 100 - int(market["no_ask"])
            elif market.get("no_ask_dollars") is not None:
                cents = 100 - int(round(float(market["no_ask_dollars"]) * 100))
            elif market.get("yes_ask") is not None:
                cents = int(market["yes_ask"])
        else:
            if market.get("no_bid") is not None:
                cents = int(market["no_bid"])
            elif market.get("no_bid_dollars") is not None:
                cents = int(round(float(market["no_bid_dollars"]) * 100))
            elif market.get("yes_ask") is not None:
                cents = 100 - int(market["yes_ask"])
            elif market.get("yes_ask_dollars") is not None:
                cents = 100 - int(round(float(market["yes_ask_dollars"]) * 100))
            elif market.get("no_ask") is not None:
                cents = int(market["no_ask"])

        if cents is None or cents <= 0 or cents >= 100:
            return None
        return cents

    def should_sell(self, entry_price_cents: int, current_price_cents: Optional[int], multiplier: float = 3.0) -> bool:
        return should_sell(entry_price_cents, current_price_cents, multiplier=multiplier)

    def sell_position(self, position: dict, current_price_cents: int, reason: str = "take_profit") -> Optional[dict]:
        """Sell position at current contract value and settle in SQLite."""
        ticker = position["ticker"]
        side = position["side"]
        count = int(position.get("count", 1))
        entry_price = int(position["price_cents"])
        client_order_id = position["client_order_id"]
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        safe_strategy = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.strategy_name.value))
        action_prefix = "sl" if reason == "stop_loss" else "sell"
        sell_order_id = f"{safe_strategy}_{action_prefix}_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"[:64]

        # Cancel resting limit order if this is an early exit (stop-loss or immediate market take-profit)
        if position.get("resting_order_id") and self.client is not None and hasattr(self.client, "cancel_order"):
            try:
                try:
                    self.client.cancel_order(position["resting_order_id"], ticker=ticker)
                except TypeError:
                    self.client.cancel_order(position["resting_order_id"])
                position["resting_order_id"] = None
            except Exception as e:
                analytics.warning(f"Could not cancel resting order {position['resting_order_id']}: {e}")

        order_result = None
        is_live = bool(self.client and getattr(self.client, "live_orders", False))
        if is_live:
            if hasattr(self.client, "place_order"):
                try:
                    order_result = self.client.place_order(
                        ticker=ticker,
                        side=side,
                        count=count,
                        price_cents=current_price_cents,
                        client_order_id=sell_order_id,
                        action="sell",
                        time_in_force="immediate_or_cancel",
                        reduce_only=True,
                    )
                except TypeError:
                    order_result = self.client.place_order(
                        ticker=ticker,
                        side=side,
                        count=count,
                        price_cents=current_price_cents,
                        client_order_id=sell_order_id,
                        action="sell",
                    )
                if not order_result or (isinstance(order_result, dict) and "error" in order_result):
                    analytics.error(
                        f"Failed to execute sell order on Kalshi for {ticker}: {order_result}. Position remains OPEN.",
                        ticker=ticker,
                        order_result=order_result,
                    )
                    return None

                # Check if the live order actually executed/filled on Kalshi
                # For IOC orders, if no liquidity matched, Kalshi returns status="canceled" with fill_count=0
                order_payload = order_result.get("order", order_result) if isinstance(order_result, dict) else {}
                status = str(order_payload.get("status") or "").lower()
                fill_count_str = str(order_payload.get("fill_count_fp") or order_payload.get("fill_count") or "0")
                try:
                    fill_count = float(fill_count_str)
                except (ValueError, TypeError):
                    fill_count = 0.0

                if status != "executed" and fill_count <= 0:
                    analytics.info(
                        f"Live {reason} order {sell_order_id} for {ticker} did not execute (status='{status}', fill_count={fill_count}). Position remains OPEN.",
                        event="order_unfilled",
                        ticker=ticker,
                        sell_order_id=sell_order_id,
                        status=status,
                        fill_count=fill_count,
                    )
                    return None

                # If executed, use actual fill price from Kalshi if available
                server_order_id = order_payload.get("order_id")
                net_fill_proceeds = None
                if is_live and server_order_id and hasattr(self.client, "get_fills"):
                    try:
                        time.sleep(0.3)
                        fills = self.client.get_fills(order_id=server_order_id)
                        if fills:
                            total_proceeds = 0.0
                            for f in fills:
                                fill_price = float(f.get("no_price_dollars") if side == PositionSide.NO else f.get("yes_price_dollars") or 0)
                                fill_fees = float(f.get("fee_cost") or 0)
                                total_proceeds += (fill_price - fill_fees)
                            if total_proceeds > 0:
                                net_fill_proceeds = total_proceeds
                                current_price_cents = round((total_proceeds / fill_count) * 100, 2)
                    except Exception as e:
                        analytics.warning(f"Could not fetch fills for sell order {server_order_id}: {e}")

                if net_fill_proceeds is None:
                    taker_cost = float(order_payload.get("taker_fill_cost_dollars") or 0)
                    maker_cost = float(order_payload.get("maker_fill_cost_dollars") or 0)
                    total_fill_dollars = taker_cost + maker_cost
                    if total_fill_dollars > 0 and fill_count > 0:
                        actual_sell_price = int(round((total_fill_dollars / fill_count) * 100))
                        if 1 <= actual_sell_price <= 99:
                            current_price_cents = actual_sell_price

        if is_live and net_fill_proceeds is not None:
            pnl_cents = int(round((net_fill_proceeds - (entry_price * count / 100)) * 100))
        else:
            pnl_cents = int(round((current_price_cents - entry_price) * count))

        if self.db is not None:
            self.db.settle_position(client_order_id=client_order_id, pnl_cents=pnl_cents)
        self.open_contract_tickers.discard(ticker)

        event_name = "order_stop_loss" if reason == "stop_loss" else "order_sold"
        action_label = "[STOP LOSS] Exited" if reason == "stop_loss" else "Sold"
        analytics.track(
            event_name,
            message=f"{action_label} {count} contract on {side.value} for {ticker} at {current_price_cents}c (entered at {entry_price}c). PnL: {pnl_cents}c.",
            ticker=ticker,
            side=side.value,
            strategy=self.strategy_name.value,
            entry_price=entry_price,
            sell_price=current_price_cents,
            pnl_cents=pnl_cents,
            reason=reason,
        )

        return {
            "client_order_id": client_order_id,
            "sell_order_id": sell_order_id,
            "ticker": ticker,
            "side": side,
            "entry_price": entry_price,
            "sell_price": current_price_cents,
            "pnl_cents": pnl_cents,
            "status": PositionStatus.SETTLED,
            "reason": reason,
            "order": order_result,
        }

    def check_open_positions(self, markets=None, now: Any = None, check_timing: bool = False) -> list[dict]:
        """
        Check in on open positions. If resting order executed on Kalshi (live) or market bid reached
        take-profit (simulation), mark position settled. Else check stop loss or hold.
        """
        if self.db is None:
            return []

        open_positions = [
            p
            for p in self.db.get_all_positions()
            if p["status"] == PositionStatus.OPEN and p.get("strategy") == self.strategy_name
        ]

        market_map = {}
        if markets:
            market_map = {m.get("ticker"): m for m in markets if isinstance(m, dict) and m.get("ticker")}

        sold_positions = []
        multiplier = get_strategy_resting_multiplier(self.strategy_name) or 3
        tp_target = getattr(self, "take_profit", None)
        is_live = bool(self.client and getattr(self.client, "live_orders", False))

        for pos in open_positions:
            ticker = pos["ticker"]
            resting_order_id = pos.get("resting_order_id")

            if tp_target is not None:
                target_sell_price = min(99, int(tp_target))
            elif multiplier is not None:
                target_sell_price = min(99, int(round(multiplier * pos["price_cents"])))
            else:
                target_sell_price = 85

            # 1. LIVE RECONCILIATION: Check if resting order executed on Kalshi exchange
            if is_live and resting_order_id and hasattr(self.client, "get_order"):
                order_info = self.client.get_order(resting_order_id)
                if order_info and isinstance(order_info, dict):
                    status = order_info.get("status")
                    if status == "executed":
                        pnl_cents = (target_sell_price - pos["price_cents"]) * int(pos.get("count", 1))
                        self.db.settle_position(client_order_id=pos["client_order_id"], pnl_cents=pnl_cents)
                        analytics.track(
                            "order_sold",
                            message=f"[LIVE EXECUTION] Resting limit order {resting_order_id} filled for {ticker}! Sold at {target_sell_price}c (entry {pos['price_cents']}c). PnL: {pnl_cents}c.",
                            ticker=ticker,
                            side=pos["side"].value,
                            strategy=self.strategy_name.value,
                            entry_price=pos["price_cents"],
                            sell_price=target_sell_price,
                            pnl_cents=pnl_cents,
                            reason="take_profit",
                        )
                        sold_positions.append({
                            "client_order_id": pos["client_order_id"],
                            "ticker": ticker,
                            "side": pos["side"],
                            "pnl_cents": pnl_cents,
                            "reason": "take_profit",
                        })
                        continue
                    elif status == "canceled":
                        pos["resting_order_id"] = None
                        resting_order_id = None
                        if self.db is not None:
                            self.db.update_position_status(
                                pos["client_order_id"], PositionStatus.OPEN, resting_order_id=""
                            )

            market = market_map.get(ticker)
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)

            if not market:
                continue

            current_bid = self._extract_bid_cents(market, pos["side"])
            if current_bid is None:
                current_bid = self._extract_ask_cents(market, pos["side"])
            minute = get_market_minute(market, now=now)
            if minute is None and pos.get("open_time"):
                minute = get_market_minute(pos, now=now)

            # 2. ACTIVE SAFETY NET: If take-profit is configured but resting_order_id is missing, place it immediately!
            has_take_profit = (tp_target is not None or self.is_sell_strategy)
            if has_take_profit and not pos.get("resting_order_id"):
                if current_bid is not None and current_bid >= target_sell_price:
                    # Current market price already at or above take-profit target: execute immediate exit!
                    sold_info = self.sell_position(pos, current_bid, reason="take_profit")
                    if sold_info:
                        sold_positions.append(sold_info)
                        continue
                else:
                    # Place missing resting limit sell order on Kalshi / simulate
                    side_val = pos["side"] if isinstance(pos["side"], PositionSide) else PositionSide(str(pos["side"]).upper())
                    safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
                    safe_strategy = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.strategy_name.value))
                    new_resting_client_id = f"{safe_strategy}_resting_{side_val.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"[:64]

                    if is_live and hasattr(self.client, "place_order"):
                        try:
                            resting_res = self.client.place_order(
                                ticker=ticker,
                                side=side_val,
                                count=int(pos.get("count", 1)),
                                price_cents=target_sell_price,
                                client_order_id=new_resting_client_id,
                                action="sell",
                                time_in_force="good_till_canceled",
                            )
                            if resting_res and isinstance(resting_res, dict) and "error" not in resting_res:
                                actual_id = (
                                    resting_res.get("order", {}).get("order_id")
                                    or resting_res.get("order_id")
                                    or new_resting_client_id
                                )
                                pos["resting_order_id"] = actual_id
                                if self.db is not None:
                                    self.db.update_position_status(
                                        pos["client_order_id"], PositionStatus.OPEN, resting_order_id=actual_id
                                    )
                                analytics.track(
                                    "resting_order_placed",
                                    message=f"Market {ticker}: Active safety net placed missing resting sell order for {pos.get('count', 1)} contract on {side_val.value} at {target_sell_price}c.",
                                    ticker=ticker,
                                    side=side_val.value,
                                    price_cents=target_sell_price,
                                )
                        except Exception as e:
                            analytics.warning(f"Could not place fallback resting order for {ticker}: {e}")
                    elif not is_live:
                        pos["resting_order_id"] = new_resting_client_id
                        if self.db is not None:
                            self.db.update_position_status(
                                pos["client_order_id"], PositionStatus.OPEN, resting_order_id=new_resting_client_id
                            )

            # 3. TAKE-PROFIT (simulation) / STOP-LOSS / HOLD EVALUATION
            if not is_live and has_take_profit and current_bid is not None and current_bid >= target_sell_price:
                sold_info = self.sell_position(pos, target_sell_price, reason="take_profit")
                if sold_info:
                    sold_positions.append(sold_info)
            elif self.stop_loss and current_bid is not None and should_stop_loss(pos["price_cents"], current_bid, minute=minute, strategy=self.strategy_name):
                sold_info = self.sell_position(pos, current_bid, reason="stop_loss")
                if sold_info:
                    sold_positions.append(sold_info)
            else:
                display_price = f"{current_bid}c" if current_bid is not None else "N/A"
                label = f"{tp_target}c take-profit" if tp_target is not None else f"{multiplier}x entry {pos['price_cents']}c"
                analytics.track(
                    "order_hold",
                    message=f"Holding {ticker} ({pos['side'].value}): current price {display_price} < target resting {target_sell_price}c ({label}).",
                    ticker=ticker,
                    side=pos["side"].value,
                    current_price=current_bid,
                    entry_price=pos["price_cents"],
                    target_sell_price=target_sell_price,
                )

        return sold_positions

    check_and_sell_positions = check_open_positions

    def process_market(self, market, event_market_count=None, now: Any = None, check_timing: bool = True):
        # Read the market ticker
        ticker = market.get("ticker")
        if not ticker:
            return None

        # Only bet on standalone binary markets (buy_yes / buy_no)
        if not self.is_binary_market(market, event_market_count=event_market_count):
            return None

        # Check if the market is open
        if not self.is_market_open(market):
            return None

        # Sync positioned tickers from SQLite if a DB is attached
        if self.db is not None:
            self.bet_markets.update(self.db.get_positioned_tickers(strategy=self.strategy_name))

        # If an open contract is held or market was already entered/traded, skip it
        if ticker in self.bet_markets or self._is_contract_held(ticker):
            return None

        # Check entry timing
        if is_last_n_strategy(self.strategy_name):
            if check_timing and not is_last_n_entry_time(market, now=now):
                return None
        elif self.is_sell_strategy and check_timing:
            if not is_entry_time(market, strategy=self.strategy_name, now=now):
                return None

        # Choose side (PositionSide.YES or PositionSide.NO) using the active strategy
        side = self.choose_side(ticker, market=market)
        if side is None:
            return None

        # Choose price (use ask if available, otherwise 50 cents for non-sell strategies)
        ask_cents = self._extract_ask_cents(market, side)
        if ask_cents is None:
            if not self.is_sell_strategy:
                price_cents = 50
            else:
                return None
        else:
            price_cents = max(1, min(99, ask_cents))

        # For non-sell strategies: apply value filter (price cap and floor) if configured
        if not self.is_sell_strategy:
            if self.min_entry_price is not None and price_cents < self.min_entry_price:
                return None
            if self.max_entry_price is not None and price_cents > self.max_entry_price:
                return None

        # For sell strategies: must be under max entry price (Zp, default 100/n)
        if self.is_sell_strategy and price_cents >= self.max_entry_price:
            return None

        # Generate client_order_id (deterministic prefix + unique millisecond timestamp to prevent 409 collisions)
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        safe_strategy = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.strategy_name.value))
        safe_env = re.sub(r"[^a-zA-Z0-9_-]", "_", str(getattr(self.db, "environment", "demo")).lower())
        ts_suffix = str(int(time.time() * 1000))[-6:]
        client_order_id = f"{safe_env}_{safe_strategy}_{safe_ticker}_{side.value.lower()}_{ts_suffix}"[:64]

        is_live = bool(self.client and getattr(self.client, "live_orders", False))

        # Margin guard: ensure available cash covers order cost to prevent 400 insufficient_balance
        if is_live and hasattr(self.client, "base_url") and self.client:
            try:
                now_t = time.time()
                if not hasattr(self, "_last_bal_check") or (now_t - getattr(self, "_last_bal_check", 0)) > 15:
                    self._last_bal_check = now_t
                    url = f"{self.client.base_url}/portfolio/balance"
                    headers = self.client._build_auth_headers("GET", url)
                    bal_res = self.client._request("GET", url, headers=headers, timeout=5)
                    if bal_res.status_code == 200:
                        bd = bal_res.json()
                        crypto_cash = 0.0
                        for b in bd.get("balance_breakdown", []):
                            if b.get("exchange_index") == 2:
                                crypto_cash = float(b.get("balance", 0.0))
                        total_cash = float(bd.get("balance_dollars", 0.0))
                        self._cached_avail_cash = crypto_cash if crypto_cash > 0 else total_cash
                cached_cash = getattr(self, "_cached_avail_cash", None)
                if cached_cash is not None and cached_cash < (price_cents / 100.0):
                    analytics.warning(
                        f"[MARGIN CHECK] Skipping {ticker}: available cash ${cached_cash:.2f} < contract cost ${price_cents/100:.2f}. Protecting account from 400 rejection.",
                        ticker=ticker,
                        available_cash=cached_cash,
                        price_cents=price_cents,
                    )
                    return None
            except Exception:
                pass

        # Write intent (PENDING) to SQLite before sending order to Kalshi
        if self.db is not None:
            close_time = market.get("close_time", "")
            self.db.insert_pending_position(
                client_order_id=client_order_id,
                ticker=ticker,
                side=side,
                count=1,
                price_cents=price_cents,
                close_time=close_time,
                strategy=self.strategy_name,
            )

        # Determine time_in_force: use IOC for regime_aware / taker strategies when edge is high
        use_ioc = getattr(self, "taker_ioc", False) or "regime" in str(self.strategy_name.value).lower() or getattr(self.strategy, "taker_ioc", False)
        time_in_force = "immediate_or_cancel" if use_ioc else "good_till_canceled"

        # Place the bet of 1 contract on Kalshi
        analytics.track(
            "order_intent",
            message=f"Market {ticker} is open! Betting 1 contract on {side.value} (strategy={self.strategy_name.value}, TIF={time_in_force}).",
            ticker=ticker,
            side=side.value,
            strategy=self.strategy_name.value,
            price_cents=price_cents,
            time_in_force=time_in_force,
        )
        result = self.client.place_order(
            ticker=ticker,
            side=side,
            count=1,
            price_cents=price_cents,
            client_order_id=client_order_id,
            action="buy",
            time_in_force=time_in_force,
        )

        if isinstance(result, dict):
            result["time_in_force"] = time_in_force

        # If the API returned an error, mark position as FAILED
        if not result or (isinstance(result, dict) and "error" in result):
            if not is_last_n_strategy(self.strategy_name):
                self.bet_markets.add(ticker)
            if self.db is not None:
                self.db.update_position_status(client_order_id, PositionStatus.FAILED)
            return result

        # Check if entry order actually executed (or is simulated)
        order_payload = result.get("order", result) if isinstance(result, dict) else {}
        order_status = str(order_payload.get("status") or "").lower()
        fill_count_str = str(order_payload.get("fill_count_fp") or order_payload.get("fill_count") or "0")
        try:
            fill_count = float(fill_count_str)
        except (ValueError, TypeError):
            fill_count = 0.0

        entry_filled = (not is_live) or (order_status == "executed") or (fill_count > 0)
        server_order_id = order_payload.get("order_id") or result.get("order_id") or client_order_id

        # For IOC taker orders: if not filled immediately, it was canceled by Kalshi without resting
        if is_live and use_ioc and not entry_filled:
            analytics.info(
                f"[IOC TAKER UNFILLED] Order {client_order_id} did not execute at ask {price_cents}c. Canceled immediately without adverse resting.",
                event="order_unfilled_ioc",
                ticker=ticker,
                order_id=server_order_id,
            )
            if self.db is not None:
                self.db.update_position_status(client_order_id, PositionStatus.FAILED)
            return result

        # Determine resting entry order timeout (default 60s for last_N strategies, 2.5s for fast simulation)
        resting_timeout = self.entry_timeout_seconds
        if resting_timeout is None:
            resting_timeout = 60.0 if is_last_n_strategy(self.strategy_name) else 2.5

        # If live order is not immediately filled, poll up to resting_timeout to verify if matching engine executed it
        if is_live and not entry_filled and hasattr(self.client, "get_order") and server_order_id:
            poll_interval = 1.0 if resting_timeout >= 10.0 else 0.5
            start_poll = time.time()
            analytics.info(
                f"[LIVE ENTRY RESTING] Order {client_order_id} ({server_order_id}) for {ticker} resting on book. Waiting up to {resting_timeout:.0f}s for match...",
                ticker=ticker,
                order_id=server_order_id,
                timeout=resting_timeout,
            )
            while (time.time() - start_poll) < resting_timeout:
                time.sleep(poll_interval)
                order_check = self.client.get_order(server_order_id)
                if order_check and isinstance(order_check, dict):
                    check_payload = order_check.get("order", order_check)
                    check_status = str(check_payload.get("status") or "").lower()
                    check_fill_str = str(check_payload.get("fill_count_fp") or check_payload.get("fill_count") or "0")
                    try:
                        check_fill = float(check_fill_str)
                    except (ValueError, TypeError):
                        check_fill = 0.0
                    if check_status == "executed" or check_fill > 0:
                        order_payload = check_payload
                        order_status = check_status
                        fill_count = check_fill
                        entry_filled = True
                        analytics.info(
                            f"[LIVE ENTRY FILLED] Order {client_order_id} filled after {time.time() - start_poll:.1f}s resting on book!",
                            ticker=ticker,
                            order_id=server_order_id,
                        )
                        break
                    elif check_status == "canceled":
                        order_status = "canceled"
                        break

        # If live entry did not fill immediately, attempt cancellation of the resting entry order
        if is_live and not entry_filled:
            if not is_last_n_strategy(self.strategy_name):
                self.bet_markets.add(ticker)
            if hasattr(self.client, "cancel_order") and server_order_id:
                try:
                    try:
                        self.client.cancel_order(server_order_id, ticker=ticker)
                    except TypeError:
                        self.client.cancel_order(server_order_id)
                    time.sleep(0.2)
                    # Check if cancellation actually succeeded or if order had already matched
                    if hasattr(self.client, "get_order"):
                        final_check = self.client.get_order(server_order_id)
                        if final_check and isinstance(final_check, dict):
                            fc_payload = final_check.get("order", final_check)
                            fc_status = str(fc_payload.get("status") or "").lower()
                            fc_fill = float(fc_payload.get("fill_count_fp") or fc_payload.get("fill_count") or 0.0)
                            if fc_status == "executed" or fc_fill > 0:
                                entry_filled = True
                                order_payload = fc_payload
                                fill_count = fc_fill
                except Exception as e:
                    analytics.warning(f"Error checking cancel status for {server_order_id}: {e}")

            if not entry_filled:
                if self.db is not None:
                    cursor = self.db.conn.cursor()
                    cursor.execute(
                        "DELETE FROM positions WHERE client_order_id = ? AND environment = ?",
                        (client_order_id, self.db.environment.value),
                    )
                    self.db.conn.commit()
                analytics.info(
                    f"[LIVE ENTRY UNFILLED] Order {client_order_id} for {ticker} canceled unfilled. Position NOT opened.",
                    ticker=ticker,
                    order_id=server_order_id,
                )
                return {
                    "order_id": server_order_id,
                    "client_order_id": client_order_id,
                    "ticker": ticker,
                    "side": side,
                    "status": "unfilled",
                    "entry_filled": False,
                }

        # Extract actual execution price from Kalshi if available
        if is_live:
            order_info = order_payload.get("order", order_payload) if isinstance(order_payload, dict) else {}
            taker_cost = float(order_info.get("taker_fill_cost_dollars") or 0)
            maker_cost = float(order_info.get("maker_fill_cost_dollars") or 0)
            fees = float(order_info.get("taker_fees_dollars") or 0) + float(order_info.get("maker_fees_dollars") or 0)
            total_fill_dollars = taker_cost + maker_cost + fees
            if total_fill_dollars == 0 and hasattr(self.client, "get_order") and server_order_id:
                try:
                    time.sleep(0.3)
                    fetched = self.client.get_order(server_order_id)
                    if fetched and isinstance(fetched, dict):
                        f_info = fetched.get("order", fetched)
                        t_cost = float(f_info.get("taker_fill_cost_dollars") or 0)
                        m_cost = float(f_info.get("maker_fill_cost_dollars") or 0)
                        f_fees = float(f_info.get("taker_fees_dollars") or 0) + float(f_info.get("maker_fees_dollars") or 0)
                        if t_cost + m_cost > 0:
                            total_fill_dollars = t_cost + m_cost + f_fees
                            order_payload = f_info
                except Exception:
                    pass
            if total_fill_dollars > 0 and fill_count > 0:
                actual_price = round((total_fill_dollars / fill_count) * 100, 2)
                if 1 <= actual_price <= 99:
                    price_cents = actual_price

        # Immediately place resting sell order if strategy uses resting exit (SELL_3N=3x, SELL_2N=2x, etc.)
        # OR if take_profit is configured (e.g. 85c resting limit order)
        # ONLY if entry order is filled (so we never sell before owning the contract)!
        resting_order = None
        resting_order_id = None
        multiplier = get_strategy_resting_multiplier(self.strategy_name)
        take_profit = getattr(self, "take_profit", None)
        if (multiplier is not None or take_profit is not None) and entry_filled:
            if take_profit is not None:
                resting_price_cents = min(99, int(take_profit))
                mult_label = f"target {resting_price_cents}c"
            else:
                resting_price_cents = min(99, int(round(multiplier * price_cents)))
                mult_label = f"{multiplier}x entry {price_cents}c"
            resting_order_id = f"{safe_strategy}_resting_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"[:64]
            analytics.track(
                "resting_order_placed",
                message=f"Market {ticker}: Placed resting sell order for 1 contract on {side.value} at {resting_price_cents}c ({mult_label}).",
                ticker=ticker,
                side=side.value,
                strategy=self.strategy_name.value,
                entry_price=price_cents,
                resting_price=resting_price_cents,
                multiplier=multiplier,
            )
            if self.client is not None and hasattr(self.client, "place_order"):
                try:
                    resting_order = self.client.place_order(
                        ticker=ticker,
                        side=side,
                        count=1,
                        price_cents=resting_price_cents,
                        client_order_id=resting_order_id,
                        action="sell",
                        time_in_force="good_till_canceled",
                    )
                except TypeError:
                    resting_order = self.client.place_order(
                        ticker=ticker,
                        side=side,
                        count=1,
                        price_cents=resting_price_cents,
                        client_order_id=resting_order_id,
                        action="sell",
                    )
            if isinstance(resting_order, dict) and "error" not in resting_order:
                server_order_id = resting_order.get("order", {}).get("order_id") or resting_order.get("order_id")
                if server_order_id:
                    resting_order_id = server_order_id
            else:
                resting_order_id = None

            if isinstance(result, dict):
                result["resting_order"] = resting_order
                result["resting_price_cents"] = resting_price_cents
                result["resting_order_id"] = resting_order_id

        # Record that we bet on this market so we do not bet again
        self.bet_markets.add(ticker)
        self.open_contract_tickers.add(ticker)

        # Write OPEN position to SQLite
        if self.db is not None:
            order_id = result.get("client_order_id") or result.get("order_id") or client_order_id
            close_time = market.get("close_time", "")
            self.db.insert_open_position(
                client_order_id=order_id,
                ticker=ticker,
                side=side,
                count=1,
                price_cents=price_cents,
                close_time=close_time,
                status=PositionStatus.OPEN,
                strategy=self.strategy_name,
                resting_order_id=resting_order_id,
            )

        # Small pause to prevent hitting rate limits
        time.sleep(0.1)

        return result

    def evaluate_all(self, markets=None, now: Any = None, check_timing: bool = True):
        # If strategy is a sell strategy or stop-loss is enabled, check open positions
        if self.is_sell_strategy or self.stop_loss:
            self.check_open_positions(markets=markets, now=now, check_timing=check_timing)

        # If no markets list was passed in, read open candidate markets from SQLite
        if markets is None and self.db is not None:
            markets = self.db.get_open_markets()
        markets = markets or []

        # Count markets per event_ticker to reject multi-market events
        event_counts = {}
        for market in markets:
            ev = market.get("event_ticker")
            if ev:
                event_counts[ev] = event_counts.get(ev, 0) + 1

        # Check every market in the list
        results = []
        for market in markets:
            ev = market.get("event_ticker")
            ev_count = event_counts.get(ev, 1) if ev else 1
            order = self.process_market(market, event_market_count=ev_count, now=now, check_timing=check_timing)
            if order is not None:
                results.append(order)
        return results

    def trade(self, markets=None, now: Any = None, check_timing: bool = True):
        # Run Step 2 (Trader): reconcile pending orders, check open positions, evaluate candidates, place orders
        if getattr(self, "trade_engine", None) is not None:
            try:
                self.trade_engine.reconciler.reconcile()
            except Exception as e:
                analytics.warning(f"Reconciliation error in trade(): {e}")
        return self.evaluate_all(markets=markets, now=now, check_timing=check_timing)


# Alias Trader to RiskEngine so both names work cleanly
Trader = RiskEngine
