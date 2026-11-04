"""
Step 1: Scanner
===============
Fetches open markets from Kalshi, filters for standalone binary markets, and upserts them into SQLite.
"""

from typing import Any, Optional

from db import Database
from ingestion.api.client import KalshiClient, is_binary_market


class Scanner:
    def __init__(self, client: Optional[KalshiClient], db: Optional[Database] = None) -> None:
        # Kalshi REST API client and SQLite database
        self.client: Optional[KalshiClient] = client
        self.db: Optional[Database] = db

    def filter_markets(self, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Count markets per event_ticker so we only keep standalone 1-market binary events
        event_counts: dict[str, int] = {}
        for market in markets:
            ev = market.get("event_ticker")
            if ev:
                event_counts[ev] = event_counts.get(ev, 0) + 1

        filtered: list[dict[str, Any]] = []
        for market in markets:
            ev = market.get("event_ticker")
            ev_count = event_counts.get(ev, 1) if ev else 1
            if is_binary_market(market, event_market_count=ev_count):
                filtered.append(market)

        return filtered

    def scan(
        self, series_list: Optional[list[str]] = None, limit_per_series: int = 20
    ) -> list[dict[str, Any]]:
        # 1. GET open markets from Kalshi API
        raw_markets = self.client.get_open_markets(
            series_list=series_list,
            limit_per_series=limit_per_series,
        )

        # 2. Filter to keep only standalone binary markets
        markets = self.filter_markets(raw_markets)

        # 3. Upsert filtered markets into SQLite
        if self.db is not None:
            self.db.upsert_markets(markets)

        return markets
