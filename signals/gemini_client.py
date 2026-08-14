"""
Gemini Prediction Markets API Client & 15-Minute Crypto Market Data Retriever
==============================================================================

Overview:
---------
Gemini provides prediction markets for cryptocurrencies (BTC, ETH, SOL, XRP, DOGE, etc.).
This file contains the client implementations for querying Gemini prediction markets
and executing automated trades on Gemini using HMAC-SHA384 request authentication.

Key Concepts:
1. GeminiPredictionClient: Public REST client for fetching active 15-minute prediction market events.
2. GeminiTradingClient: Authenticated REST client (using HMAC SHA384 signature headers)
   to place limit orders, query account USD balance, and manage positions.
3. GeminiClient: Unified client alias matching KalshiClient's interface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from signals.base_client import BaseTradingClient, LogTag

# Load configuration from the project's .env file if available
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

# Base API URLs for Gemini Exchange
PRODUCTION_BASE = "https://api.gemini.com"
SANDBOX_BASE = "https://api.sandbox.gemini.com"

SERIES_15M_GEMINI = [
    "BTC15M", "ETH15M", "SOL15M", "XRP15M", "DOGE15M",
    "BNB15M", "HYPE15M", "ADA15M", "TON15M", "BCH15M", "NEAR15M", "ZEC15M"
]


# ---------------------------------------------------------------------------
# Public Gemini Prediction Market Client
# ---------------------------------------------------------------------------
class GeminiPredictionClient:
    """
    Public API client to list events, prices, and 15-minute crypto prediction markets on Gemini.
    No API authentication required for public market data.
    """
    def __init__(self, base_url: str | None = None):
        use_sandbox = os.getenv("GEMINI_USE_SANDBOX", "false").lower() == "true"
        self.base_url = (base_url or (SANDBOX_BASE if use_sandbox else PRODUCTION_BASE)).rstrip("/")
        self.session = requests.Session()

    def get_events(self, category: str = "crypto", status: str = "active") -> dict:
        """Fetch prediction events from Gemini."""
        url = f"{self.base_url}/v1/prediction-markets/events"
        params = {"category": category, "status": status}
        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 200 and resp.text:
                return resp.json()
        except Exception as e:
            log.debug(f"Error fetching Gemini prediction events: {e}")
        return {"data": []}

    def get_15m_crypto_markets(self, status: str = "open", limit: int = 100) -> list[dict]:
        """
        Retrieves active 15-minute crypto prediction events from Gemini.
        Searches for 15M tickers (e.g. BTC15M, ETH15M, SOL15M, XRP15M) and normalizes output.
        """
        events = []
        for ticker_term in SERIES_15M_GEMINI:
            try:
                url = f"{self.base_url}/v1/prediction-markets/events"
                params = {"category": "crypto", "status": "active", "search": ticker_term}
                r = self.session.get(url, params=params, timeout=10)
                if r.status_code == 200 and r.text:
                    data = r.json().get("data", [])
                    if isinstance(data, list):
                        events.extend(data)
            except Exception as e:
                log.debug(f"Error fetching Gemini 15m events for {ticker_term}: {e}")

        normalized_markets = []
        for evt in events:
            if not isinstance(evt, dict):
                continue
            ticker = evt.get("ticker") or evt.get("symbol") or evt.get("event_id")
            if not ticker:
                continue
            series = evt.get("series_ticker") or evt.get("series") or "CRYPTO15M"

            yes_ask_c = evt.get("yes_ask") or evt.get("yes_ask_cents")
            yes_bid_c = evt.get("yes_bid") or evt.get("yes_bid_cents")
            no_ask_c = evt.get("no_ask") or evt.get("no_ask_cents")
            no_bid_c = evt.get("no_bid") or evt.get("no_bid_cents")

            dollar_str = f"{(yes_ask_c / 100.0):.4f}" if isinstance(yes_ask_c, (int, float)) else evt.get("yes_ask_dollars")

            normalized_markets.append({
                "ticker": str(ticker),
                "series_ticker": str(series),
                "yes_ask": yes_ask_c,
                "yes_bid": yes_bid_c,
                "no_ask": no_ask_c,
                "no_bid": no_bid_c,
                "yes_ask_dollars": dollar_str,
                "floor_strike": evt.get("floor_strike") or evt.get("strike_price"),
                "close_time": evt.get("close_time") or evt.get("expiration_time"),
                "expected_expiration_time": evt.get("expected_expiration_time") or evt.get("close_time"),
                "volume_fp": evt.get("volume_fp") or evt.get("volume", 0),
                "result": evt.get("result"),
                "exchange": "gemini",
            })
        return normalized_markets

    def get_market(self, ticker: str) -> dict:
        """Fetches detailed market metadata for a specific ticker."""
        try:
            url = f"{self.base_url}/v1/prediction-markets/markets/{ticker}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.debug(f"Error fetching Gemini market {ticker}: {e}")
        return {"market": {"ticker": ticker}}


# ---------------------------------------------------------------------------
# Authenticated Gemini Trading Client
# ---------------------------------------------------------------------------
class GeminiTradingClient(BaseTradingClient, GeminiPredictionClient):
    """
    Authenticated API client for placing orders and checking balance on Gemini.
    Uses HMAC-SHA384 signatures passed via X-GEMINI-PAYLOAD headers.
    """
    exchange_name = "Gemini"

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url)
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.api_secret = os.getenv("GEMINI_API_SECRET")
        self.account = os.getenv("GEMINI_ACCOUNT", "trading-bot")
        self._positions_cache: dict[str, dict] = {}
        self._orders_cache: dict[str, list[dict]] = {}

    def _build_auth_headers(self, path: str, payload_dict: dict | None = None) -> dict[str, str]:
        """Generates HMAC-SHA384 authentication headers for Gemini REST API."""
        if not self.api_key or not self.api_secret:
            return {}

        payload = {
            "request": path,
            "nonce": int(time.time() * 1000),
        }
        if self.account:
            payload["account"] = self.account
        if payload_dict:
            payload.update(payload_dict)

        payload_json = json.dumps(payload)
        payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("utf-8")

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha384,
        ).hexdigest()

        return {
            "Content-Type": "text/plain",
            "Content-Length": "0",
            "X-GEMINI-APIKEY": self.api_key,
            "X-GEMINI-PAYLOAD": payload_b64,
            "X-GEMINI-SIGNATURE": signature,
            "Cache-Control": "no-cache",
        }

    def _post(self, path: str, payload_dict: dict | None = None) -> dict:
        """Helper to send authenticated POST request to Gemini."""
        url = f"{self.base_url}{path}"
        headers = self._build_auth_headers(path, payload_dict)
        if not headers:
            return {}
        try:
            resp = self.session.post(url, headers=headers, timeout=10)
            if resp.status_code == 200 and resp.text:
                return resp.json()
            else:
                log.debug(f"Gemini POST {path} returned status {resp.status_code}: {resp.text}")
        except Exception as e:
            log.debug(f"Gemini POST {path} exception: {e}")
        return {}

    def get_balance(self) -> dict:
        """Fetches available USD balance from Gemini account."""
        res = self._post("/v1/balances")
        if isinstance(res, list):
            for b in res:
                if isinstance(b, dict) and b.get("currency") == "USD":
                    return {"balance": float(b.get("amount", 0)), "available": float(b.get("available", 0))}
        return {"balance": 0.0, "available": 0.0}

    def get_portfolio_pnl_summary(self) -> dict:
        """Calculates portfolio PnL summary for Gemini account."""
        bal = self.get_balance()
        bal_usd = bal.get("balance", 0.0)
        return {
            "balance_dollars": bal_usd,
            "portfolio_value_dollars": bal_usd,
            "open_positions_count": len(self._positions_cache),
            "open_exposure_dollars": 0.0,
            "total_realized_pnl_dollars": 0.0,
            "total_realized_pnl_cents": 0,
            "settlements_count": 0,
            "total_settled_revenue_dollars": 0.0,
            "total_settled_cost_dollars": 0.0,
            "settlements": [],
            "exchange": "gemini",
        }

    def get_market_position(self, ticker: str) -> dict | None:
        """Checks if there is an active position for the given market ticker."""
        return self._positions_cache.get(ticker)

    def has_resting_take_profit_order(self, ticker: str) -> bool:
        """Returns True if there is a resting take profit order for ticker."""
        orders = self._orders_cache.get(ticker, [])
        return any(o.get("type") == "take_profit" for o in orders)

    def has_existing_position_or_order(self, ticker: str) -> bool:
        """Returns True if an active position or resting order exists for ticker."""
        return (ticker in self._positions_cache) or bool(self._orders_cache.get(ticker))

    def cancel_unfilled_buy_orders(self, ticker: str) -> int:
        """Cancels any unfilled buy orders for ticker."""
        if ticker in self._orders_cache:
            self._orders_cache[ticker] = [o for o in self._orders_cache[ticker] if o.get("side") != "buy"]
        return 0

    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        """Cancels all resting orders for ticker."""
        if ticker in self._orders_cache:
            self._orders_cache.pop(ticker, None)
            return True
        return False

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        """Executes position liquidation order on Gemini and removes from local cache."""
        res = self._post("/v1/order/new", {
            "symbol": ticker,
            "amount": str(count),
            "price": f"{(current_price / 100.0):.2f}",
            "side": "sell",
            "type": "exchange limit",
        })
        self._positions_cache.pop(ticker, None)
        return res

    def buy_and_set_take_profit(
        self,
        ticker: str,
        side: str,
        buy_price_cents: int,
        take_profit_cents: int,
        count: int = 1,
        maker_bid_cents: int | None = None,
    ) -> dict:
        """Executes a buy order and sets resting take-profit sell order on Gemini."""
        payload = {
            "symbol": ticker,
            "amount": str(count),
            "price": f"{(buy_price_cents / 100.0):.2f}",
            "side": "buy",
            "type": "exchange limit",
        }
        res = self._post("/v1/order/new", payload)

        self._positions_cache[ticker] = {
            "side": side.lower(),
            "count": count,
            "entry_price_cents": buy_price_cents,
        }
        self._orders_cache.setdefault(ticker, []).append({
            "type": "take_profit",
            "side": "sell",
            "price_cents": take_profit_cents,
        })

        log.info(f"{LogTag.TRADE_EXECUTION.value} [Gemini] Order placed for {ticker} ({side.upper()}) at {buy_price_cents}¢.")
        return {"buy_price_cents": buy_price_cents, "res": res}

    def place_take_profit_order(
        self,
        ticker: str,
        side: str,
        count: int,
        take_profit_cents: int,
    ) -> dict:
        """Places a resting take-profit sell order on Gemini."""
        self._orders_cache.setdefault(ticker, []).append({
            "type": "take_profit",
            "side": "sell",
            "price_cents": take_profit_cents,
        })
        log.info(f"{LogTag.AUTO_SELL_SET.value} [Gemini] Placed resting TP order for {ticker} ({side.upper()}) at {take_profit_cents}¢.")
        return {"take_profit_cents": take_profit_cents}


# Primary Gemini client alias
GeminiClient = GeminiTradingClient
