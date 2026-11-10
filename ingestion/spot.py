"""
Real-time Crypto Spot Price Client
==================================
Fetches real-time spot prices for crypto assets (BTC, ETH, SOL, XRP, DOGE, BNB)
from CF Benchmarks constituent exchanges (Coinbase primary, Kraken fallback).
Used for trend alignment against Kalshi 15m binary option strikes.
"""

import re
import time
from typing import Optional, Union
import requests

from db import PositionSide
from logging.analytics import analytics

COINBASE_SYMBOLS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "BNB": "BNB-USD",
}

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD",
    "ETH": "XETHZUSD",
    "SOL": "SOLUSD",
    "XRP": "XXRPZUSD",
    "DOGE": "XDGUSD",
}


def extract_asset_symbol(ticker_or_series: str) -> Optional[str]:
    """
    Extract crypto base symbol from ticker or series string.
    e.g., 'KXBTC15M-26NOV100015-15' -> 'BTC', 'KXETH15M' -> 'ETH'.
    """
    if not ticker_or_series or not isinstance(ticker_or_series, str):
        return None
    clean = ticker_or_series.split("-")[0].upper().strip()
    if clean.startswith("KX"):
        clean = clean[2:]
    for crypto in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
        if clean.startswith(crypto):
            return crypto
    return None


def extract_market_strike(market: dict) -> Optional[float]:
    """
    Extract the numeric strike price (floor_strike or Target Price) from a Kalshi market dictionary.
    """
    if not isinstance(market, dict):
        return None

    floor = market.get("floor_strike")
    if floor is not None:
        try:
            val = float(floor)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass

    # Fallback to yes_sub_title (e.g. 'Target Price: $74,697.25')
    sub_title = market.get("yes_sub_title", "")
    if sub_title:
        match = re.search(r"(\d[\d,]*\.?\d*)", sub_title)
        if match:
            try:
                val = float(match.group(1).replace(",", ""))
                if val > 0:
                    return val
            except (ValueError, TypeError):
                pass

    return None


class SpotClient:
    """
    Client for fetching real-time cryptocurrency spot prices.
    Uses Coinbase as primary data provider (CF Benchmarks constituent) and Kraken as fallback.
    Includes in-memory TTL caching to avoid redundant calls within the same scan interval.
    """

    def __init__(self, session: Optional[requests.Session] = None, cache_ttl_seconds: float = 3.0, timeout: float = 2.5):
        self.session = session or requests.Session()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout = timeout
        self._price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)

    def get_spot_price(self, symbol_or_ticker: str) -> Optional[float]:
        """
        Get current spot price in USD for a crypto asset symbol or Kalshi ticker.
        """
        symbol = extract_asset_symbol(symbol_or_ticker) or str(symbol_or_ticker).upper().strip()
        if not symbol:
            return None

        # Check in-memory cache
        now = time.time()
        if symbol in self._price_cache:
            price, ts = self._price_cache[symbol]
            if (now - ts) < self.cache_ttl_seconds:
                return price

        # 1. Primary: Coinbase Public Spot API
        price = self._fetch_coinbase(symbol)
        if price is not None:
            self._price_cache[symbol] = (price, now)
            return price

        # 2. Fallback: Kraken Public Ticker API
        price = self._fetch_kraken(symbol)
        if price is not None:
            self._price_cache[symbol] = (price, now)
            return price

        return None

    def _fetch_coinbase(self, symbol: str) -> Optional[float]:
        pair = COINBASE_SYMBOLS.get(symbol)
        if not pair:
            return None
        url = f"https://api.coinbase.com/v2/prices/{pair}/spot"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                amt = data.get("data", {}).get("amount")
                if amt is not None:
                    return float(amt)
        except Exception as e:
            analytics.debug(f"Coinbase spot fetch failed for {symbol}: {e}")
        return None

    def _fetch_kraken(self, symbol: str) -> Optional[float]:
        pair = KRAKEN_PAIRS.get(symbol)
        if not pair:
            return None
        url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result", {})
                for k, v in result.items():
                    c_list = v.get("c")
                    if c_list and len(c_list) > 0:
                        return float(c_list[0])
        except Exception as e:
            analytics.debug(f"Kraken spot fetch failed for {symbol}: {e}")
        return None

    def get_spot_trend(
        self,
        market: dict,
        current_spot: Optional[float] = None,
    ) -> tuple[Optional[PositionSide], Optional[float], Optional[float]]:
        """
        Compare current spot price against the market's strike price.
        Returns:
            (trend_side, spot_price, strike_price)
            - trend_side: PositionSide.YES if spot >= strike, PositionSide.NO if spot < strike, or None if unavailable.
        """
        ticker = market.get("ticker", "")
        strike = extract_market_strike(market)
        if strike is None:
            return None, None, None

        spot = current_spot
        if spot is None:
            spot = self.get_spot_price(ticker)

        if spot is None:
            return None, None, strike

        # On Kalshi: If CF Benchmarks RTI >= strike, resolves YES. If < strike, resolves NO.
        if spot >= strike:
            return PositionSide.YES, spot, strike
        else:
            return PositionSide.NO, spot, strike
