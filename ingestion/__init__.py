"""
Ingestion layer package.
"""

from ingestion.api.client import KalshiClient
from ingestion.scanner import Scanner
from ingestion.spot import SpotClient, extract_asset_symbol, extract_market_strike

__all__ = ["KalshiClient", "Scanner", "SpotClient", "extract_asset_symbol", "extract_market_strike"]
