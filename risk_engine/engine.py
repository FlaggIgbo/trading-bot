"""
Risk Engine
============
Logic:
If market is open (and when it opens), randomly bet 1 contract on one side ("YES", "NO").
Never sell, just hold it and hope for the best.
"""

import random
import time


class RiskEngine:
    def __init__(self, client):
        # Trading client to place orders
        self.client = client
        # Set of market tickers where we already placed our bet
        self.bet_markets = set()

    def is_market_open(self, market):
        # Check if market status is open or active
        status = market.get("status", "").lower()
        return status in ["open", "active"]

    def pick_random_side(self):
        # Randomly pick YES or NO
        return random.choice(["YES", "NO"])

    def process_market(self, market):
        # Read the market ticker
        ticker = market.get("ticker")
        if not ticker:
            return None

        # Check if the market is open
        if not self.is_market_open(market):
            return None

        # If we already bet on this market, skip it
        if ticker in self.bet_markets:
            return None

        # Randomly bet 1 contract on one side ("YES", "NO")
        side = self.pick_random_side()

        # Choose price (use ask if available, otherwise 50 cents)
        price_cents = 50
        if side == "YES" and market.get("yes_ask"):
            price_cents = market.get("yes_ask")
        elif side == "NO" and market.get("no_ask"):
            price_cents = market.get("no_ask")

        # Place the bet of 1 contract
        print(f"Market {ticker} is open! Randomly betting 1 contract on {side}.")
        result = self.client.place_order(
            ticker=ticker,
            side=side,
            count=1,
            price_cents=price_cents,
        )

        # Record that we bet on this market so we do not bet again
        # We never sell, just hold it and hope for the best!
        self.bet_markets.add(ticker)

        # Small pause to prevent hitting rate limits
        time.sleep(0.1)

        return result

    def evaluate_all(self, markets):
        # Check every market in the list
        results = []
        for market in markets:
            order = self.process_market(market)
            if order is not None:
                results.append(order)
        return results
