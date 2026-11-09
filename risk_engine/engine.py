"""
Step 2: Trader (Risk Engine)
============================
Reads candidate markets and open positions from SQLite, performs risk checks,
chooses a side (YES/up or NO/down) using a strategy function, and manages positions.
Supports last-5 resolved trend following, random selection, and sell-if-3x momentum strategy.
"""

from datetime import datetime, timezone, timedelta
import random
import re
import time
from typing import Any, Optional, Union

from db import Database, MarketResult, MarketStatus, PositionSide, PositionStatus, Strategy
from ingestion.api.client import is_binary_market
from logging.analytics import analytics


def choose_side_random(past_results=None) -> PositionSide:
    # Strategy 1: Randomly pick YES (up) or NO (down)
    return random.choice([PositionSide.YES, PositionSide.NO])


def choose_side_last_9(past_results) -> PositionSide:
    # Strategy 2: Look at the last 9 resolved markets for the same series.
    # "yes" = resolved UP (above start price), "no" = resolved DOWN (below start price).
    # If there were more ups than downs, bid YES (up). Otherwise, bid NO (down).
    last_9 = list(past_results or [])[:9]
    ups = 0
    downs = 0
    for result in last_9:
        clean = str(result).strip().lower()
        if clean == MarketResult.YES.value:
            ups += 1
        elif clean == MarketResult.NO.value:
            downs += 1

    if ups > downs:
        return PositionSide.YES
    return PositionSide.NO


# Backwards compatibility aliases
choose_side_last_5 = choose_side_last_9
choose_side_last_3 = choose_side_last_9


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
    stop_minute: float = 10.0,
    stop_price_threshold: int = 25,
    stop_ratio_threshold: float = 0.5,
    **kwargs,
) -> bool:
    """
    Check if a position should be liquidated under the stop-loss rule for 5m sell strategies:
    At minute >= 10, if the contract has not filled its resting order and:
      - is trading at <= 25c OR
      - current market price drops below 50% of entry price (< 0.5 * entry_price).
    """
    entry = kwargs.get("entry_price", entry_price_cents)
    current = kwargs.get("current_price", current_price_cents)

    if entry is None or current is None or entry <= 0:
        return False

    # Only exit at minute >= stop_minute (default 10.0)
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
            return (now_dt - open_dt).total_seconds() / 60.0

    if market_or_pos.get("close_time"):
        close_dt = _parse_time(market_or_pos["close_time"])
        if close_dt:
            start_dt = close_dt - timedelta(minutes=15)
            return (now_dt - start_dt).total_seconds() / 60.0

    return float(now_dt.minute % 15) + (now_dt.second / 60.0)


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


def is_entry_time(market: dict, strategy: Any = None, now: Any = None) -> bool:
    """Check if current time is within entry window for the strategy."""
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
    Strategy.LAST_3.value: choose_side_last_9,
    Strategy.LAST_5.value: choose_side_last_9,
    Strategy.LAST_9.value: choose_side_last_9,
    "last_9": choose_side_last_9,
    "last9": choose_side_last_9,
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


class RiskEngine:
    def __init__(self, client, db: Optional[Database] = None, strategy: Union[str, Strategy, Any] = Strategy.LAST_9, stop_loss: bool = False):
        # Kalshi REST API client and optional SQLite database
        self.client = client
        self.db: Optional[Database] = db
        self.strategy = strategy
        self.stop_loss: bool = bool(stop_loss or getattr(strategy, "stop_loss", False))

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

            # Switch statement for string names:
            # The '|' symbol means "OR" (matches any of the listed strings)
            match strat_lower:
                case "random":
                    self.strategy_name = Strategy.RANDOM

                case "last_9" | "last9":
                    self.strategy_name = Strategy.LAST_9

                case "last_5" | "last5":
                    self.strategy_name = Strategy.LAST_5

                case "last_3" | "last3":
                    self.strategy_name = Strategy.LAST_3

                case "sell_3n" | "sell3n" | "sell_if_3n":
                    self.strategy_name = Strategy.SELL_3N

                case "sell_2n_5m_35p" | "sell_2_n_5m_35p" | "sell2n_5m_35p":
                    self.strategy_name = Strategy.SELL_2N_5M_35P

                case "sell_2n_5m" | "sell_5m_2n" | "sell_2x_5m" | "sell_3n_2x":
                    self.strategy_name = Strategy.SELL_2N_5M

                case "sell_2n" | "sell2n" | "sell_if_2n":
                    self.strategy_name = Strategy.SELL_2N

                case "sell_1.5n_5m_35p" | "sell_1_5n_5m_35p" | "sell1.5n_5m_35p" | "sell1_5n_5m_35p":
                    self.strategy_name = Strategy.SELL_1_5N_5M_35P

                case "sell_1.5n_35p" | "sell_1_5n_35p" | "sell1.5n_35p" | "sell1_5n_35p":
                    self.strategy_name = Strategy.SELL_1_5N_35P

                case "sell_1.5n_5m" | "sell_1_5n_5m" | "sell1.5n_5m" | "sell1_5n_5m":
                    self.strategy_name = Strategy.SELL_1_5N_5M

                case "sell_1.5n" | "sell_1_5n" | "sell1.5n" | "sell1_5n":
                    self.strategy_name = Strategy.SELL_1_5N

                case "sell_2.5n" | "sell_2_5n" | "sell2.5n" | "sell2_5n" | "sell_2.5n_5m":
                    self.strategy_name = Strategy.SELL_2_5N

                case _:
                    # Default case: try converting directly to enum, fallback to LAST_5 if invalid
                    try:
                        self.strategy_name = Strategy(strat_lower)
                    except ValueError:
                        self.strategy_name = Strategy.LAST_5

        elif callable(strategy):
            # Switch statement for function references:
            # Matches whichever function is passed in
            match strategy:
                case _ if strategy is choose_side_random:
                    self.strategy_name = Strategy.RANDOM

                case _ if strategy in (sell_3n, choose_sell_if_3n, choose_side_sell_if_3n):
                    self.strategy_name = Strategy.SELL_3N

                case _ if strategy in (sell_2n_5m_35p, choose_sell_if_2n_5m_35p, choose_side_sell_2n_5m_35p):
                    self.strategy_name = Strategy.SELL_2N_5M_35P

                case _ if strategy in (sell_2n_5m, choose_sell_if_2n_5m, choose_side_sell_2n_5m):
                    self.strategy_name = Strategy.SELL_2N_5M

                case _ if strategy in (sell_2n, choose_sell_if_2n, choose_side_sell_2n):
                    self.strategy_name = Strategy.SELL_2N

                case _ if strategy in (sell_1_5n_35p, choose_sell_if_1_5n_35p, choose_side_sell_1_5n_35p):
                    self.strategy_name = Strategy.SELL_1_5N_35P

                case _ if strategy is sell_1_5n_5m:
                    self.strategy_name = Strategy.SELL_1_5N_5M

                case _ if strategy in (sell_1_5n, choose_sell_if_1_5n, choose_side_sell_1_5n):
                    self.strategy_name = Strategy.SELL_1_5N

                case _ if strategy in (sell_2_5n, choose_sell_if_2_5n, choose_side_sell_2_5n):
                    self.strategy_name = Strategy.SELL_2_5N

                case _:
                    # Default case: unknown function
                    self.strategy_name = Strategy.LAST_5

        else:
            # Default fallback if strategy is none of the above types
            self.strategy_name = Strategy.LAST_5

        self.is_sell_strategy = is_sell_strategy(self.strategy_name)
        self.max_entry_price = get_strategy_max_entry_price(self.strategy)

        # Set of market tickers where this strategy already placed its bet
        self.bet_markets = set()
        if self.db is not None:
            self.bet_markets = self.db.get_positioned_tickers(strategy=self.strategy_name)

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

    def get_last_9_results(self, ticker: str) -> list[str]:
        # Get the last 9 resolved results ('yes' or 'no') for this market's series (e.g. 'KXBTC15M')
        series = ticker.split("-")[0]
        results = []

        if self.db is not None:
            results = self.db.get_recent_results(series, limit=9)

        # If SQLite has fewer than 9 past resolved markets, check Kalshi API for recent settled markets
        if len(results) < 9 and self.client is not None and hasattr(self.client, "get_settled_markets"):
            settled_markets = self.client.get_settled_markets(series, limit=9)
            if settled_markets:
                if self.db is not None:
                    self.db.upsert_markets(settled_markets)
                api_results = [
                    m.get("result", "")
                    for m in settled_markets
                    if str(m.get("result", "")).strip().lower() in ("yes", "no")
                ]
                if api_results:
                    results = api_results[:9]

        return results

    # Aliases for backwards compatibility
    get_last_5_results = get_last_9_results
    get_last_3_results = get_last_9_results

    def choose_side(self, ticker: str, market: Optional[dict] = None) -> Optional[PositionSide]:
        # Run the configured strategy function to pick YES (up) or NO (down)
        if callable(self.strategy):
            strategy_fn = self.strategy
        else:
            strategy_fn = STRATEGIES.get(str(self.strategy), choose_side_last_9)

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

        past_results = self.get_last_9_results(ticker)
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

        # Cancel resting limit order if this is an early stop-loss exit
        if reason == "stop_loss" and position.get("resting_order_id") and self.client is not None and hasattr(self.client, "cancel_order"):
            try:
                self.client.cancel_order(position["resting_order_id"])
            except Exception as e:
                analytics.warning(f"Could not cancel resting order {position['resting_order_id']}: {e}")

        order_result = None
        is_live = bool(self.client and getattr(self.client, "live_orders", False))
        if is_live and (reason == "stop_loss" or not position.get("resting_order_id")):
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
                    analytics.warning(
                        f"Live {reason} order {sell_order_id} for {ticker} did not execute (status='{status}', fill_count={fill_count}). Position remains OPEN.",
                        ticker=ticker,
                        sell_order_id=sell_order_id,
                        status=status,
                        fill_count=fill_count,
                    )
                    return None

                # If executed, use actual fill price from Kalshi if available
                taker_cost = float(order_payload.get("taker_fill_cost_dollars") or 0)
                maker_cost = float(order_payload.get("maker_fill_cost_dollars") or 0)
                total_fill_dollars = taker_cost + maker_cost
                if total_fill_dollars > 0 and fill_count > 0:
                    actual_sell_price = int(round((total_fill_dollars / fill_count) * 100))
                    if 1 <= actual_sell_price <= 99:
                        current_price_cents = actual_sell_price

        pnl_cents = (current_price_cents - entry_price) * count

        if self.db is not None:
            self.db.settle_position(client_order_id=client_order_id, pnl_cents=pnl_cents)

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
        is_live = bool(self.client and getattr(self.client, "live_orders", False))

        for pos in open_positions:
            ticker = pos["ticker"]
            resting_order_id = pos.get("resting_order_id")

            # 1. LIVE RECONCILIATION: Check if resting order executed on Kalshi exchange
            if is_live and resting_order_id and hasattr(self.client, "get_order"):
                order_info = self.client.get_order(resting_order_id)
                if order_info and isinstance(order_info, dict):
                    status = order_info.get("status")
                    if status == "executed":
                        target_sell_price = min(99, int(round(multiplier * pos["price_cents"])))
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

            market = market_map.get(ticker)
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)

            if not market:
                continue

            current_bid = self._extract_bid_cents(market, pos["side"])
            if current_bid is None:
                current_bid = self._extract_ask_cents(market, pos["side"])
            target_sell_price = min(99, int(round(multiplier * pos["price_cents"])))
            minute = get_market_minute(market, now=now)
            if minute is None and pos.get("open_time"):
                minute = get_market_minute(pos, now=now)

            # In simulation mode (or if no resting order placed), check take-profit condition
            if not is_live and self.should_sell(pos["price_cents"], current_bid, multiplier=multiplier):
                sold_info = self.sell_position(pos, target_sell_price, reason="take_profit")
                if sold_info:
                    sold_positions.append(sold_info)
            elif self.stop_loss and current_bid is not None and should_stop_loss(pos["price_cents"], current_bid, minute=minute):
                sold_info = self.sell_position(pos, current_bid, reason="stop_loss")
                if sold_info:
                    sold_positions.append(sold_info)
            else:
                display_price = f"{current_bid}c" if current_bid is not None else "N/A"
                analytics.track(
                    "order_hold",
                    message=f"Holding {ticker} ({pos['side'].value}): current price {display_price} < target resting {target_sell_price}c ({multiplier}x entry {pos['price_cents']}c).",
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

        # If we already bet on this market, skip it
        if ticker in self.bet_markets:
            return None

        # For sell strategies (e.g. sell_3n, sell_2n, sell_2n_5m, sell_1.5n, sell_2.5n):
        if self.is_sell_strategy and check_timing:
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

        # For sell strategies: must be under max entry price (Zp, default 100/n)
        if self.is_sell_strategy and price_cents >= self.max_entry_price:
            return None

        # Generate client_order_id (tagged by strategy so parallel runs never collide)
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        safe_strategy = re.sub(r"[^a-zA-Z0-9_-]", "_", str(self.strategy_name.value))
        client_order_id = f"{safe_strategy}_buy_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"[:64]

        # Place the bet of 1 contract on Kalshi
        analytics.track(
            "order_intent",
            message=f"Market {ticker} is open! Betting 1 contract on {side.value} (strategy={self.strategy_name.value}).",
            ticker=ticker,
            side=side.value,
            strategy=self.strategy_name.value,
            price_cents=price_cents,
        )
        result = self.client.place_order(
            ticker=ticker,
            side=side,
            count=1,
            price_cents=price_cents,
            client_order_id=client_order_id,
            action="buy",
        )

        # If the API returned an error, do not record the position as OPEN
        if not result or (isinstance(result, dict) and "error" in result):
            return result

        # Check if entry order actually executed (or is simulated)
        is_live = bool(self.client and getattr(self.client, "live_orders", False))
        order_payload = result.get("order", result) if isinstance(result, dict) else {}
        order_status = str(order_payload.get("status") or "").lower()
        fill_count_str = str(order_payload.get("fill_count_fp") or order_payload.get("fill_count") or "0")
        try:
            fill_count = float(fill_count_str)
        except (ValueError, TypeError):
            fill_count = 0.0

        entry_filled = (not is_live) or (order_status == "executed") or (fill_count > 0)
        server_order_id = order_payload.get("order_id") or result.get("order_id") or client_order_id

        # If live order is resting, poll briefly up to 1.5s to verify if matching engine executed it
        if is_live and not entry_filled and order_status == "resting" and hasattr(self.client, "get_order"):
            for _ in range(3):
                time.sleep(0.5)
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
                        break
                    elif check_status == "canceled":
                        order_status = "canceled"
                        break

        # If live entry did NOT fill, cancel any lingering resting order and do NOT open position in SQLite
        if is_live and not entry_filled:
            if order_status == "resting" and hasattr(self.client, "cancel_order"):
                try:
                    self.client.cancel_order(server_order_id)
                    analytics.track(
                        "order_canceled",
                        message=f"Canceled unfilled resting entry order {server_order_id} for {ticker}.",
                        ticker=ticker,
                        order_id=server_order_id,
                    )
                except Exception as e:
                    analytics.warning(f"Could not cancel unfilled entry order {server_order_id}: {e}")

            analytics.warning(
                f"[LIVE ENTRY UNFILLED] Order {client_order_id} for {ticker} did not fill (status='{order_status}', fill_count={fill_count}). Position NOT opened.",
                ticker=ticker,
                order_id=server_order_id,
                status=order_status,
                fill_count=fill_count,
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
            taker_cost = float(order_payload.get("taker_fill_cost_dollars") or 0)
            maker_cost = float(order_payload.get("maker_fill_cost_dollars") or 0)
            total_fill_dollars = taker_cost + maker_cost
            if total_fill_dollars > 0 and fill_count > 0:
                actual_price = int(round((total_fill_dollars / fill_count) * 100))
                if 1 <= actual_price <= 99:
                    price_cents = actual_price

        # Immediately place resting sell order if strategy uses resting exit (SELL_3N=3x, SELL_2N=2x, etc.)
        # ONLY if entry order is filled (so we never sell before owning the contract)!
        resting_order = None
        resting_order_id = None
        multiplier = get_strategy_resting_multiplier(self.strategy_name)
        if multiplier is not None and entry_filled:
            resting_price_cents = min(99, int(round(multiplier * price_cents)))
            resting_order_id = f"{safe_strategy}_resting_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"[:64]
            analytics.track(
                "resting_order_placed",
                message=f"Market {ticker}: Placed resting sell order for 1 contract on {side.value} at {resting_price_cents}c ({multiplier}x entry {price_cents}c).",
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
        # If strategy is a sell strategy, check open positions to verify if resting orders filled
        if self.is_sell_strategy:
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
        # Run Step 2 (Trader): check open positions, evaluate candidates, place orders
        return self.evaluate_all(markets=markets, now=now, check_timing=check_timing)


# Alias Trader to RiskEngine so both names work cleanly
Trader = RiskEngine
