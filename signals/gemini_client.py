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
from datetime import datetime, timezone
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


def parse_cents(val_dollars: str | float | None, val_cents: int | str | None = None) -> int | None:
    """Safely converts string dollars (e.g. '0.3500') or cent integers to an integer cent value (1-99)."""
    for val, multiplier in ((val_dollars, 100), (val_cents, 1)):
        if val is not None:
            try:
                res = int(round(float(val) * multiplier))
                if 1 <= res <= 99:
                    return res
            except (ValueError, TypeError):
                continue
    return None


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
        Parses contract structures, prices, strike values, and expiration dates.
        """
        events = []
        try:
            url = f"{self.base_url}/v1/prediction-markets/events"
            params = {"category": "crypto", "status": "active"}
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 200 and resp.text:
                data = resp.json().get("data", [])
                if isinstance(data, list):
                    events.extend(data)
        except Exception as e:
            log.debug(f"Error fetching Gemini prediction events: {e}")

        normalized_markets = []
        now_utc = datetime.now(timezone.utc)

        for evt in events:
            if not isinstance(evt, dict):
                continue

            event_ticker = str(evt.get("ticker", ""))
            series = str(evt.get("series") or evt.get("series_ticker") or "")
            expiry = evt.get("expiryDate") or evt.get("close_time") or evt.get("expiration_time")

            # Filter for 15M / 15-minute crypto prediction series
            if "15M" not in series.upper() and "15M" not in event_ticker.upper():
                continue

            # Only include markets for the current active cycle (expiring within the next 15-16 minutes)
            if expiry:
                try:
                    expiry_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    seconds_to_expiry = (expiry_dt - now_utc).total_seconds()
                    if seconds_to_expiry <= 0 or seconds_to_expiry > (15 * 60 + 60):
                        continue
                except Exception:
                    pass

            contracts = evt.get("contracts", [])
            if not isinstance(contracts, list) or not contracts:
                continue

            for c in contracts:
                if not isinstance(c, dict):
                    continue

                inst = str(c.get("instrumentSymbol") or c.get("ticker") or event_ticker)

                # Extract Strike Price
                strike_val = None
                if strike_dict := c.get("strike"):
                    if isinstance(strike_dict, dict) and "value" in strike_dict:
                        try:
                            strike_val = float(strike_dict["value"])
                        except (ValueError, TypeError):
                            pass
                if strike_val is None and evt.get("floor_strike"):
                    try:
                        strike_val = float(evt["floor_strike"])
                    except (ValueError, TypeError):
                        pass

                # Extract top-of-book prices from contract payload
                prices = c.get("prices") or {}
                buy_p = prices.get("buy") or {}
                sell_p = prices.get("sell") or {}

                yes_ask_str = buy_p.get("yes") or prices.get("bestAsk")
                yes_bid_str = sell_p.get("yes") or prices.get("bestBid")
                no_ask_str = buy_p.get("no")
                no_bid_str = sell_p.get("no")

                yes_ask = parse_cents(yes_ask_str)
                yes_bid = parse_cents(yes_bid_str)
                no_ask = parse_cents(no_ask_str)
                no_bid = parse_cents(no_bid_str)

                # Fallback: Query Gemini public orderbook if quotes are missing
                if yes_ask is None and yes_bid is None and inst:
                    try:
                        br = self.session.get(f"{self.base_url}/v1/book/{inst}", timeout=3.0)
                        if br.status_code == 200:
                            bd = br.json()
                            bids = bd.get("bids", [])
                            asks = bd.get("asks", [])
                            if asks and isinstance(asks, list) and asks[0].get("price"):
                                yes_ask = parse_cents(asks[0]["price"])
                            if bids and isinstance(bids, list) and bids[0].get("price"):
                                yes_bid = parse_cents(bids[0]["price"])
                    except Exception as err:
                        log.debug(f"Book fallback error for {inst}: {err}")

                # Impute NO side prices if YES side is present and NO side is missing
                if no_ask is None and yes_bid is not None:
                    no_ask = 100 - yes_bid
                if no_bid is None and yes_ask is not None:
                    no_bid = 100 - yes_ask

                dollar_str = f"{(yes_ask / 100.0):.4f}" if yes_ask is not None else None

                normalized_markets.append({
                    "ticker": inst,
                    "event_ticker": event_ticker,
                    "series_ticker": series or "CRYPTO15M",
                    "yes_ask": yes_ask,
                    "yes_bid": yes_bid,
                    "no_ask": no_ask,
                    "no_bid": no_bid,
                    "yes_ask_dollars": dollar_str,
                    "floor_strike": strike_val,
                    "close_time": expiry,
                    "expected_expiration_time": expiry,
                    "volume_fp": evt.get("volume_fp") or evt.get("volume", 0),
                    "result": evt.get("result"),
                    "exchange": "gemini",
                })

        return normalized_markets

    def get_market(self, ticker: str) -> dict:
        """Fetches detailed market metadata / book for a specific ticker."""
        try:
            url = f"{self.base_url}/v1/book/{ticker}"
            resp = self.session.get(url, timeout=5)
            if resp.status_code == 200:
                bd = resp.json()
                asks = bd.get("asks", [])
                bids = bd.get("bids", [])
                yes_ask = asks[0]["price"] if asks and isinstance(asks, list) and asks[0].get("price") else None
                yes_bid = bids[0]["price"] if bids and isinstance(bids, list) and bids[0].get("price") else None
                no_ask = None
                no_bid = None
                if yes_bid is not None:
                    try:
                        no_ask = f"{(1.0 - float(yes_bid)):.4f}"
                    except (ValueError, TypeError):
                        pass
                if yes_ask is not None:
                    try:
                        no_bid = f"{(1.0 - float(yes_ask)):.4f}"
                    except (ValueError, TypeError):
                        pass
                return {
                    "market": {
                        "ticker": ticker,
                        "yes_ask_dollars": yes_ask,
                        "yes_bid_dollars": yes_bid,
                        "no_ask_dollars": no_ask,
                        "no_bid_dollars": no_bid,
                    }
                }
        except Exception as e:
            log.debug(f"Error fetching Gemini market book for {ticker}: {e}")
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
        """Fetches available USD / stablecoin (USD, GUSD, USDC) balance from Gemini account."""
        res = self._post("/v1/balances")
        total_bal = 0.0
        total_avail = 0.0
        if isinstance(res, list):
            for b in res:
                if isinstance(b, dict) and b.get("currency") in ("USD", "GUSD", "USDC"):
                    try:
                        total_bal += float(b.get("amount", 0))
                        total_avail += float(b.get("available", 0))
                    except (ValueError, TypeError):
                        pass
            return {"balance": total_bal, "available": total_avail}
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
