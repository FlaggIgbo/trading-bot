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


def choose_side_last_5(past_results) -> PositionSide:
    # Strategy 2: Look at the last 5 resolved markets for the same series.
    # "yes" = resolved UP (above start price), "no" = resolved DOWN (below start price).
    # If there were more ups than downs, bid YES (up). Otherwise, bid NO (down).
    last_5 = list(past_results or [])[:5]
    ups = 0
    downs = 0
    for result in last_5:
        clean = str(result).strip().lower()
        if clean == MarketResult.YES.value:
            ups += 1
        elif clean == MarketResult.NO.value:
            downs += 1

    if ups > downs:
        return PositionSide.YES
    return PositionSide.NO


# Backwards compatibility alias
choose_side_last_3 = choose_side_last_5


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


def choose_sell_if_3n(
    past_results_or_market: Any = None,
    market: Optional[dict] = None,
    *,
    yes_ask: Optional[int] = None,
    no_ask: Optional[int] = None,
) -> Optional[PositionSide]:
    """
    Strategy 3: Bet whichever side where the contract value is less than 33 cents.
    If none are less than 33 cents, return None.
    """
    target_market = past_results_or_market if isinstance(past_results_or_market, dict) else market

    yes_price = yes_ask
    if yes_price is None and target_market:
        yes_price = extract_contract_price(target_market, PositionSide.YES)

    no_price = no_ask
    if no_price is None and target_market:
        no_price = extract_contract_price(target_market, PositionSide.NO)

    yes_valid = yes_price is not None and 0 < yes_price < 33
    no_valid = no_price is not None and 0 < no_price < 33

    if yes_valid and no_valid:
        return PositionSide.YES if yes_price <= no_price else PositionSide.NO
    if yes_valid:
        return PositionSide.YES
    if no_valid:
        return PositionSide.NO
    return None


choose_side_sell_if_3n = choose_sell_if_3n


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


def is_entry_time(market: dict, now: Any = None) -> bool:
    """Enter market at 5 minutes past (between 5 and 10 minutes into the market)."""
    minute = get_market_minute(market, now=now)
    if minute is None:
        return True
    return 5.0 <= minute < 10.0


def is_checkin_time(market_or_pos: dict, now: Any = None) -> bool:
    """Check in at 10 minutes past into the market."""
    minute = get_market_minute(market_or_pos, now=now)
    if minute is None:
        return True
    return minute >= 10.0


def should_sell(entry_price_cents: int, current_price_cents: int) -> bool:
    """Check if current contract value is at least 3 times what we entered at."""
    if entry_price_cents <= 0:
        return False
    return current_price_cents >= 3 * entry_price_cents


STRATEGIES = {
    Strategy.RANDOM.value: choose_side_random,
    Strategy.LAST_3.value: choose_side_last_5,
    Strategy.LAST_5.value: choose_side_last_5,
    Strategy.SELL_3N.value: choose_sell_if_3n,
}


class RiskEngine:
    def __init__(self, client, db: Optional[Database] = None, strategy: Union[str, Strategy, Any] = Strategy.LAST_5):
        # Kalshi REST API client and optional SQLite database
        self.client = client
        self.db: Optional[Database] = db
        self.strategy = strategy
        if isinstance(strategy, Strategy):
            self.strategy_name = strategy
        elif isinstance(strategy, str):
            strat_lower = strategy.strip().lower()
            if strat_lower == "random":
                self.strategy_name = Strategy.RANDOM
            elif strat_lower in ("last_5", "last5"):
                self.strategy_name = Strategy.LAST_5
            elif strat_lower in ("last_3", "last3"):
                self.strategy_name = Strategy.LAST_3
            elif strat_lower in ("sell_3n", "sell3n", "sell_if_3n"):
                self.strategy_name = Strategy.SELL_3N
            else:
                try:
                    self.strategy_name = Strategy(strat_lower)
                except ValueError:
                    self.strategy_name = Strategy.LAST_5
        elif callable(strategy):
            if strategy is choose_side_random:
                self.strategy_name = Strategy.RANDOM
            elif strategy in (choose_sell_if_3n, choose_side_sell_if_3n):
                self.strategy_name = Strategy.SELL_3N
            else:
                self.strategy_name = Strategy.LAST_5
        else:
            self.strategy_name = Strategy.LAST_5

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

    def get_last_5_results(self, ticker: str) -> list[str]:
        # Get the last 5 resolved results ('yes' or 'no') for this market's series (e.g. 'KXBTC15M')
        series = ticker.split("-")[0]
        results = []

        if self.db is not None:
            results = self.db.get_recent_results(series, limit=5)

        # If SQLite has fewer than 5 past resolved markets, check Kalshi API for recent settled markets
        if len(results) < 5 and self.client is not None and hasattr(self.client, "get_settled_markets"):
            settled_markets = self.client.get_settled_markets(series, limit=5)
            if settled_markets:
                if self.db is not None:
                    self.db.upsert_markets(settled_markets)
                api_results = [
                    m.get("result", "")
                    for m in settled_markets
                    if str(m.get("result", "")).strip().lower() in ("yes", "no")
                ]
                if api_results:
                    results = api_results[:5]

        return results

    # Alias for backwards compatibility
    get_last_3_results = get_last_5_results

    def choose_side(self, ticker: str, market: Optional[dict] = None) -> Optional[PositionSide]:
        # Run the configured strategy function to pick YES (up) or NO (down)
        if callable(self.strategy):
            strategy_fn = self.strategy
        else:
            strategy_fn = STRATEGIES.get(str(self.strategy), choose_side_last_5)

        if strategy_fn is choose_side_random:
            return choose_side_random()

        if strategy_fn in (choose_sell_if_3n, choose_side_sell_if_3n):
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)
            return strategy_fn(market=market)

        past_results = self.get_last_5_results(ticker)
        return strategy_fn(past_results)

    def _extract_ask_cents(self, market, side):
        # Extract ask price in cents (1-99), supporting both integer cents and dollar strings.
        # If the orderbook has no ask (0 or 100 cents), default to 50 cents.
        cents = None
        if side == PositionSide.YES:
            if market.get("yes_ask") is not None:
                cents = int(market["yes_ask"])
            elif market.get("yes_ask_dollars") is not None:
                cents = int(round(float(market["yes_ask_dollars"]) * 100))
        else:
            if market.get("no_ask") is not None:
                cents = int(market["no_ask"])
            elif market.get("no_ask_dollars") is not None:
                cents = int(round(float(market["no_ask_dollars"]) * 100))

        if cents is None or cents <= 0 or cents >= 100:
            return 50
        return cents

    def should_sell(self, entry_price_cents: int, current_price_cents: int) -> bool:
        return should_sell(entry_price_cents, current_price_cents)

    def sell_position(self, position: dict, current_price_cents: int) -> dict:
        """Sell position at current contract value and settle in SQLite."""
        ticker = position["ticker"]
        side = position["side"]
        count = int(position.get("count", 1))
        entry_price = int(position["price_cents"])
        client_order_id = position["client_order_id"]
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        sell_order_id = f"{self.strategy_name.value}_sell_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"

        order_result = None
        if self.client is not None and hasattr(self.client, "place_order"):
            try:
                order_result = self.client.place_order(
                    ticker=ticker,
                    side=side,
                    count=count,
                    price_cents=current_price_cents,
                    client_order_id=sell_order_id,
                    action="sell",
                )
            except TypeError:
                order_result = self.client.place_order(
                    ticker=ticker,
                    side=side,
                    count=count,
                    price_cents=current_price_cents,
                    client_order_id=sell_order_id,
                )

        pnl_cents = (current_price_cents - entry_price) * count

        if self.db is not None:
            self.db.settle_position(client_order_id=client_order_id, pnl_cents=pnl_cents)

        analytics.track(
            "order_sold",
            message=f"Sold {count} contract on {side.value} for {ticker} at {current_price_cents}c (entered at {entry_price}c, 3x reached). PnL: {pnl_cents}c.",
            ticker=ticker,
            side=side.value,
            strategy=self.strategy_name.value,
            entry_price=entry_price,
            sell_price=current_price_cents,
            pnl_cents=pnl_cents,
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
            "order": order_result,
        }

    def check_open_positions(self, markets=None, now: Any = None, check_timing: bool = True) -> list[dict]:
        """
        Check in at 10 minutes past into market.
        If contract value is now 3 times what we entered at, sell. Else, hold.
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
        for pos in open_positions:
            ticker = pos["ticker"]
            market = market_map.get(ticker)
            if market is None and self.client is not None and hasattr(self.client, "get_market"):
                market = self.client.get_market(ticker)

            timing_target = market if market is not None else pos
            if check_timing and not is_checkin_time(timing_target, now=now):
                continue

            if not market:
                continue

            current_price = self._extract_ask_cents(market, pos["side"])
            if self.should_sell(pos["price_cents"], current_price):
                sold_info = self.sell_position(pos, current_price)
                sold_positions.append(sold_info)
            else:
                analytics.track(
                    "order_hold",
                    message=f"Holding {ticker} ({pos['side'].value}): current price {current_price}c < 3x entry ({pos['price_cents']}c).",
                    ticker=ticker,
                    side=pos["side"].value,
                    current_price=current_price,
                    entry_price=pos["price_cents"],
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

        # For SELL_3N: enter market at 5 minutes past (between 5 and 10 minutes)
        if self.strategy_name == Strategy.SELL_3N and check_timing:
            if not is_entry_time(market, now=now):
                return None

        # Choose side (PositionSide.YES or PositionSide.NO) using the active strategy
        side = self.choose_side(ticker, market=market)
        if side is None:
            return None

        # Choose price (use ask if available, otherwise 50 cents)
        price_cents = max(1, min(99, self._extract_ask_cents(market, side)))

        # For SELL_3N: must be under 33 cents
        if self.strategy_name == Strategy.SELL_3N and price_cents >= 33:
            return None

        # Generate client_order_id (tagged by strategy so parallel runs never collide)
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        client_order_id = f"{self.strategy_name.value}_buy_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"

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
        )

        # If the API returned an error, do not record the position as OPEN
        if isinstance(result, dict) and "error" in result:
            return result

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
            )

        # Small pause to prevent hitting rate limits
        time.sleep(0.1)

        return result

    def evaluate_all(self, markets=None, now: Any = None, check_timing: bool = True):
        # If strategy is SELL_3N, check open positions to sell if 3x at 10 minutes past
        if self.strategy_name == Strategy.SELL_3N:
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
