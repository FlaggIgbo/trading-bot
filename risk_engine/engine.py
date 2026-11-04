"""
Step 2: Trader (Risk Engine)
============================
Reads candidate markets and open positions from SQLite, performs risk checks,
chooses a side (YES/up or NO/down) using a strategy function, and writes OPEN positions to SQLite.
Never sells, just holds and hopes for the best!
"""

import random
import re
import time
from typing import Optional

from db import Database, MarketResult, MarketStatus, PositionSide, PositionStatus, Strategy
from ingestion.api.client import is_binary_market


def choose_side_random(past_results=None) -> PositionSide:
    # Strategy 1: Randomly pick YES (up) or NO (down)
    return random.choice([PositionSide.YES, PositionSide.NO])


def choose_side_last_3(past_results) -> PositionSide:
    # Strategy 2: Look at the last 3 resolved markets for the same series.
    # "yes" = resolved UP (above start price), "no" = resolved DOWN (below start price).
    # If there were more ups than downs, bid YES (up). Otherwise, bid NO (down).
    last_3 = list(past_results or [])[:3]
    ups = 0
    downs = 0
    for result in last_3:
        clean = str(result).strip().lower()
        if clean == MarketResult.YES.value:
            ups += 1
        elif clean == MarketResult.NO.value:
            downs += 1

    if ups > downs:
        return PositionSide.YES
    return PositionSide.NO


STRATEGIES = {
    Strategy.RANDOM.value: choose_side_random,
    Strategy.LAST_3.value: choose_side_last_3,
}


class RiskEngine:
    def __init__(self, client, db: Optional[Database] = None, strategy=Strategy.LAST_3):
        # Kalshi REST API client and optional SQLite database
        self.client = client
        self.db: Optional[Database] = db
        self.strategy = strategy
        self.strategy_name = Strategy.RANDOM if strategy in ("random", choose_side_random) else Strategy.LAST_3
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

    def get_last_3_results(self, ticker: str) -> list[str]:
        # Get the last 3 resolved results ('yes' or 'no') for this market's series (e.g. 'KXBTC15M')
        series = ticker.split("-")[0]
        results = []

        if self.db is not None:
            results = self.db.get_recent_results(series, limit=3)

        # If SQLite has fewer than 3 past resolved markets, check Kalshi API for recent settled markets
        if len(results) < 3 and self.client is not None and hasattr(self.client, "get_settled_markets"):
            settled_markets = self.client.get_settled_markets(series, limit=3)
            if self.db is not None and settled_markets:
                self.db.upsert_markets(settled_markets)
            results = [
                m.get("result", "")
                for m in settled_markets
                if str(m.get("result", "")).strip().lower() in ("yes", "no")
            ][:3]

        return results

    def choose_side(self, ticker: str) -> PositionSide:
        # Run the configured strategy function to pick YES (up) or NO (down)
        if callable(self.strategy):
            strategy_fn = self.strategy
        else:
            strategy_fn = STRATEGIES.get(str(self.strategy), choose_side_last_3)

        if strategy_fn is choose_side_random:
            return choose_side_random()

        past_results = self.get_last_3_results(ticker)
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

    def process_market(self, market, event_market_count=None):
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

        # Choose side (PositionSide.YES or PositionSide.NO) using the active strategy
        side = self.choose_side(ticker)

        # Choose price (use ask if available, otherwise 50 cents)
        price_cents = max(1, min(99, self._extract_ask_cents(market, side)))

        # Generate client_order_id (tagged by strategy so parallel runs never collide)
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        client_order_id = f"{self.strategy_name.value}_buy_{side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"

        # Place the bet of 1 contract on Kalshi
        print(f"Market {ticker} is open! Betting 1 contract on {side.value} (strategy={self.strategy_name.value}).")
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
        # We never sell, just hold it and hope for the best!
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

    def evaluate_all(self, markets=None):
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
            order = self.process_market(market, event_market_count=ev_count)
            if order is not None:
                results.append(order)
        return results

    def trade(self):
        # Run Step 2 (Trader): read candidates + open positions from DB, place orders, and write OPEN to DB
        return self.evaluate_all()


# Alias Trader to RiskEngine so both names work cleanly
Trader = RiskEngine
