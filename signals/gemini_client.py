"""
Gemini Prediction Markets API Client (Commented Out)
=====================================================

Overview:
---------
Gemini provides prediction markets for cryptocurrencies (BTC, ETH, SOL, XRP, DOGE, etc.).
This file contains the client implementations for querying Gemini prediction markets.

Note:
-----
Per project instructions, all Gemini API code is currently COMMENTED OUT.
To enable Gemini calls in the future, uncomment the code blocks below.

Key Concepts if uncommented:
1. GeminiPredictionClient: Public REST client for fetching active prediction market events.
2. GeminiTradingClient: Authenticated REST client (using HMAC SHA384 signature headers)
   to place limit orders and query account USD balance.
"""

# import base64
# import hashlib
# import hmac
# import json
# import logging
# import os
# import threading
# import time
# from pathlib import Path
# import requests
# from dotenv import load_dotenv

# log = logging.getLogger(__name__)

# Base API URLs for Gemini Exchange
# PRODUCTION_BASE = "https://api.gemini.com"
# SANDBOX_BASE = "https://api.sandbox.gemini.com"

# ---------------------------------------------------------------------------
# Public Gemini Prediction Market Client (Commented Out)
# ---------------------------------------------------------------------------

# class GeminiPredictionClient:
#     """
#     Public API client to list events, prices, and 15-minute crypto markets on Gemini.
#     No API authentication required for public market data.
#     """
#     def __init__(self, base_url: str | None = None):
#         use_sandbox = os.getenv("GEMINI_USE_SANDBOX", "false").lower() == "true"
#         self.base_url = (base_url or (SANDBOX_BASE if use_sandbox else PRODUCTION_BASE)).rstrip("/")
#
#     def get_events(self, category: str = "crypto", status: str = "active") -> dict:
#         """Fetch prediction events from Gemini."""
#         url = f"{self.base_url}/v1/prediction-markets/events"
#         params = {"category": category, "status": status}
#         resp = requests.get(url, params=params, timeout=30)
#         resp.raise_for_status()
#         return resp.json() if resp.text else {"data": []}
#
#     def get_15m_crypto_events(self) -> list[dict]:
#         """
#         Retrieves active 15-minute crypto prediction events from Gemini.
#         Searches for 15M tickers (e.g. BTC15M, ETH15M, SOL15M, XRP15M).
#         """
#         events = []
#         for ticker_term in ("BTC15M", "ETH15M", "SOL15M", "XRP15M", "DOGE15M", "BNB15M"):
#             try:
#                 url = f"{self.base_url}/v1/prediction-markets/events"
#                 params = {"category": "crypto", "status": "active", "search": ticker_term}
#                 r = requests.get(url, params=params, timeout=30)
#                 if r.status_code == 200 and r.text:
#                     data = r.json().get("data", [])
#                     events.extend(data)
#             except Exception as e:
#                 log.warning(f"Error fetching Gemini 15m events for {ticker_term}: {e}")
#         return events

# ---------------------------------------------------------------------------
# Authenticated Gemini Trading Client (Commented Out)
# ---------------------------------------------------------------------------

# class GeminiTradingClient(GeminiPredictionClient):
#     """
#     Authenticated API client for placing orders and checking USD balance on Gemini.
#     Uses HMAC-SHA384 signatures passed via X-GEMINI-PAYLOAD headers.
#     """
#     def __init__(self, base_url: str | None = None):
#         super().__init__(base_url)
#         self.api_key = os.getenv("GEMINI_API_KEY")
#         self.api_secret = os.getenv("GEMINI_API_SECRET")
#
#     def get_balance(self) -> dict:
#         """Fetches available USD balance from Gemini account."""
#         return {"balance": 0}
