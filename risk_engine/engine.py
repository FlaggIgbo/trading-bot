"""
Step 2: Trader (Risk Engine)
============================
Reads candidate markets and open positions from SQLite, performs risk checks,
randomly bets 1 contract on YES or NO, and writes OPEN positions to SQLite.
Never sells, just holds and hopes for the best!
"""

import random
import re
import time

from ingestion.api.client import is_binary_market


class RiskEngine:
    def __init__(self, client, db=None):
        # Kalshi REST API client and optional SQLite database
        self.client = client
        self.db = db
        # Set of market tickers where we already placed our bet
        self.bet_markets = set()
        if self.db is not None:
            self.bet_markets = self.db.get_positioned_tickers()

    def is_market_open(self, market):
        # Check if market status is open or active
        status = market.get("status", "").lower()
        return status in ["open", "active"]

    def is_binary_market(self, market, event_market_count=None):
        # Check if market is a standalone binary market (only buy_yes / buy_no)
        return is_binary_market(market, event_market_count=event_market_count)

    def pick_random_side(self):
        # Randomly pick YES or NO
        return random.choice(["YES", "NO"])

    def _extract_ask_cents(self, market, side):
        # Extract ask price in cents (1-99), supporting both integer cents and dollar strings
        if side == "YES":
            if market.get("yes_ask") is not None:
                return int(market["yes_ask"])
            if market.get("yes_ask_dollars") is not None:
                return int(round(float(market["yes_ask_dollars"]) * 100))
        else:
            if market.get("no_ask") is not None:
                return int(market["no_ask"])
            if market.get("no_ask_dollars") is not None:
                return int(round(float(market["no_ask_dollars"]) * 100))
        return 50

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
            self.bet_markets.update(self.db.get_positioned_tickers())

        # If we already bet on this market, skip it
        if ticker in self.bet_markets:
            return None

        # Randomly bet 1 contract on one side ("YES", "NO")
        side = self.pick_random_side()

        # Choose price (use ask if available, otherwise 50 cents)
        price_cents = max(1, min(99, self._extract_ask_cents(market, side)))

        # Generate client_order_id
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        client_order_id = f"buy_{side.lower()}_{safe_ticker}_{int(time.time() * 1000)}"

        # Place the bet of 1 contract on Kalshi
        print(f"Market {ticker} is open! Randomly betting 1 contract on {side}.")
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
