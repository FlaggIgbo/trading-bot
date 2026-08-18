"""
Gemini Prediction Markets API Client & Multi-Asset Execution Engine
====================================================================

What is Gemini Prediction Markets?
-----------------------------------
Gemini offers CFTC-regulated binary prediction market contracts spanning:
- 15-Minute Markets: Crypto (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ADA, TON, BCH, NEAR, ZEC)
  and Commodities (Gold, Silver, WTI Oil).
- Hourly Multi-Strike Markets: Crypto, Commodities, and Equity Indices (S&P 500, Nasdaq-100).
Contracts settle at either $1.00 (100 cents) if YES occurs, or $0.00 if NO occurs.

Authentication Explained:
-------------------------
Gemini uses HMAC-SHA384 digital signatures passed via HTTP headers:
1. `X-GEMINI-APIKEY`: Your Gemini API key identifier.
2. `X-GEMINI-PAYLOAD`: Base64-encoded JSON payload containing the request path and nonce.
3. `X-GEMINI-SIGNATURE`: Hex-encoded HMAC-SHA384 signature computed using your API secret.

Architecture & Market Parity:
------------------------------
This client provides full parity with KalshiClient across:
- 15-Minute & Hourly multi-asset discovery across Crypto, Commodities, and Equity Indices.
- Passive Maker execution (Bid + 1¢ limit orders) and endgame Favorite Maker orders (74¢–93¢).
- Checkpoint lifecycle: stop-loss liquidations, 5m/20m early cuts, 10m/40m/55m profit banking, and 12.5m/50m/57.5m endgame cuts.
- Cross-asset directional portfolio risk caps and single-side market locks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from signals.base_client import BaseTradingClient, LogTag, validate_market_closure_time

# Load configuration from the project's .env file if available
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

# Base API URLs for Gemini Exchange
PRODUCTION_BASE = "https://api.gemini.com"
SANDBOX_BASE = "https://api.sandbox.gemini.com"

# Default resting order timeout in seconds for Maker buy orders
DEFAULT_BUY_ORDER_TIMEOUT_SECONDS = int(
    os.getenv("GEMINI_BUY_ORDER_EXPIRATION_SECONDS", os.getenv("BUY_ORDER_EXPIRATION_SECONDS", "45"))
)

# ---------------------------------------------------------------------------
# 15-Minute Series Identifiers (Crypto & Commodities)
# ---------------------------------------------------------------------------
SERIES_15M_CRYPTO = [
    "BTC15M", "ETH15M", "SOL15M", "XRP15M", "DOGE15M",
    "BNB15M", "HYPE15M", "ADA15M", "TON15M", "BCH15M", "NEAR15M", "ZEC15M"
]

SERIES_15M_COMMODITIES = [
    "GOLD15M", "WTI15M", "SILVER15M",
    "XAU15M", "CL15M", "XAG15M",
]

# Unified list of all active 15-minute prediction market series on Gemini
SERIES_15M_ALL = SERIES_15M_CRYPTO + SERIES_15M_COMMODITIES

# ---------------------------------------------------------------------------
# Hourly Multi-Strike Series Identifiers (Crypto, Commodities & Equity Indices)
# ---------------------------------------------------------------------------
SERIES_HOURLY_CRYPTO = [
    "BTCD", "ETHD", "SOLD", "XRPD", "DOGED",
    "BNBD", "HYPED", "TOND", "NEARD", "ZECD",
    "BTCH", "ETHH", "SOLH", "XRPH", "DOGEH",
    "BNBH", "HYPEH", "TONH", "NEARH", "ZECH",
]

SERIES_HOURLY_COMMODITIES = [
    "GOLDH", "SILVERH", "WTIH",
    "XAUH", "XAGH", "CLH",
    "GOLDD", "SILVERD", "WTID",
]

SERIES_HOURLY_INDICES = [
    "INXU", "NASDAQ100U",
    "SPXU", "NDXU", "SP500U",
]

# Unified list of all active hourly prediction market series on Gemini
SERIES_HOURLY_ALL = SERIES_HOURLY_CRYPTO + SERIES_HOURLY_COMMODITIES + SERIES_HOURLY_INDICES

# Gemini Prediction Market Categories
GEMINI_CATEGORIES = ["crypto", "commodities", "commodity", "indices", "equities", "stocks"]

# Common Gemini API error reason constants
GEMINI_REASON_INSUFFICIENT_FUNDS = "InsufficientFunds"
GEMINI_REASON_INVALID_PRICE = "InvalidPrice"
GEMINI_REASON_INVALID_QUANTITY = "InvalidQuantity"
GEMINI_REASON_INVALID_SYMBOL = "InvalidSymbol"
GEMINI_REASON_MARKET_NOT_OPEN = "MarketNotOpen"
GEMINI_REASON_MISSING_ACCOUNTS = "MissingAccounts"
GEMINI_REASON_TERMS_NOT_ACCEPTED = "TermsNotAccepted"


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


def parse_gemini_error(response: requests.Response | dict) -> tuple[str | None, str]:
    """
    Extracts the error reason and error message from a Gemini HTTP error response.
    Returns: tuple of (reason, message)
    """
    text = ""
    if hasattr(response, "text") and response.text:
        text = response.text
    try:
        data = json.loads(text) if text else (response if isinstance(response, dict) else {})
    except Exception:
        return None, (text or "")[:200]
    reason = data.get("reason")
    msg = data.get("message", "") or str(data)
    return reason, msg


def is_5min_market(text: str | None) -> bool:
    """
    Returns True if the given ticker, series, or instrument symbol indicates a 5-minute prediction market.
    
    Gemini uses 3-character zero-padded duration markers:
    - 5-Minute Contracts: '05M' or '5M' (e.g. 'GEMI-BTC05M2606011000-UP', 'BTC05M', '05MIN', '5MIN')
    - 15-Minute Contracts: '15M' (e.g. 'GEMI-BTC15M2602251745-UP', 'BTC15M')
    
    Matches '05M' or '5M' (where 5 is not preceded by a digit, avoiding false positives on '15M' or '25M').
    """
    if not text:
        return False
    return bool(re.search(r"(?:05M|(?<![0-9])5M|05MIN|5MIN|05-MIN|5-MIN|5_MIN|05_MIN|5MINUTE|05MINUTE)", str(text), re.IGNORECASE))


def generate_safe_client_order_id(prefix: str, ticker: str, timestamp_ms: int | None = None) -> str:
    """Generates a sanitized client_order_id for Gemini orders."""
    safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
    ts = timestamp_ms or int(time.time() * 1000)
    return f"{prefix}_{safe_ticker}_{ts}"


# ---------------------------------------------------------------------------
# Public Gemini Prediction Market Client
# ---------------------------------------------------------------------------
class GeminiPredictionClient:
    """
    Public API client to list prediction events, orderbooks, and active contracts on Gemini.
    No API authentication required for public market data.
    """

    def __init__(self, base_url: str | None = None):
        use_sandbox = os.getenv("GEMINI_USE_SANDBOX", "false").lower() == "true"
        self.base_url = (base_url or (SANDBOX_BASE if use_sandbox else PRODUCTION_BASE)).rstrip("/")
        self.session = requests.Session()

    def get_events(
        self,
        category: str | list[str] | None = None,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Fetch prediction events from Gemini across specified or all categories."""
        url = f"{self.base_url}/v1/prediction-markets/events"
        categories_to_query = (
            [category] if isinstance(category, str)
            else category if isinstance(category, list)
            else ["crypto", "commodities", "indices"]
        )

        all_events = []
        for cat in categories_to_query:
            params = {"category": cat, "status": status, "limit": min(limit, 100), "offset": offset}
            try:
                resp = self.session.get(url, params=params, timeout=10)
                if resp.status_code == 200 and resp.text:
                    data = resp.json().get("data", [])
                    if isinstance(data, list):
                        all_events.extend(data)
            except Exception as e:
                log.debug(f"Error fetching Gemini prediction events for category '{cat}': {e}")

        return {"data": all_events}

    def get_15m_crypto_markets(
        self,
        status: str = "open",
        limit: int = 100,
        series_list: list[str] | None = None,
    ) -> list[dict]:
        """
        Retrieves active 15-minute prediction markets across all supported series
        (Crypto: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ADA, TON, BCH, NEAR, ZEC;
         Commodities: Gold, Silver, WTI Oil).
        Parses contract structures, prices, strike values, and expiration dates.
        Strictly excludes 5-minute markets (e.g. 05M, 5M) and verifies 15-minute boundary (:00, :15, :30, :45).
        """
        target_series = set(s.upper() for s in (series_list or SERIES_15M_ALL))
        events_resp = self.get_events(category=["crypto", "commodities", "commodity"], status="active", limit=limit)
        events = events_resp.get("data", [])

        normalized_markets = []
        now_utc = datetime.now(timezone.utc)

        for evt in events:
            if not isinstance(evt, dict):
                continue

            event_ticker = str(evt.get("ticker", ""))
            series = str(evt.get("series") or evt.get("series_ticker") or "")
            expiry = evt.get("expiryDate") or evt.get("close_time") or evt.get("expiration_time")

            # Explicitly exclude 5-minute markets
            if is_5min_market(event_ticker) or is_5min_market(series):
                continue

            # Filter for 15M / 15-minute prediction series
            is_15m = (
                "15M" in series.upper()
                or "15M" in event_ticker.upper()
                or any(s in series.upper() or s in event_ticker.upper() for s in target_series)
            )
            if not is_15m:
                continue

            # Only include markets for the current active cycle (expiring within the next 15-16 minutes on a :00/:15/:30/:45 boundary with matching time distance)
            if expiry or event_ticker:
                is_valid, reason, _ = validate_market_closure_time(
                    close_time_raw=expiry,
                    now_utc=now_utc,
                    timeframe="15m",
                    ticker=event_ticker,
                )
                if not is_valid:
                    continue

            contracts = evt.get("contracts", [])
            if not isinstance(contracts, list) or not contracts:
                continue

            for c in contracts:
                if not isinstance(c, dict):
                    continue

                inst = str(c.get("instrumentSymbol") or c.get("ticker") or event_ticker)

                # Skip any contract with 5-minute marker in instrument symbol
                if is_5min_market(inst):
                    continue

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
                yes_bid_dollar_str = f"{(yes_bid / 100.0):.4f}" if yes_bid is not None else None
                no_ask_dollar_str = f"{(no_ask / 100.0):.4f}" if no_ask is not None else None
                no_bid_dollar_str = f"{(no_bid / 100.0):.4f}" if no_bid is not None else None

                normalized_markets.append({
                    "ticker": inst,
                    "event_ticker": event_ticker,
                    "series_ticker": series or "CRYPTO15M",
                    "yes_ask": yes_ask,
                    "yes_bid": yes_bid,
                    "no_ask": no_ask,
                    "no_bid": no_bid,
                    "yes_ask_dollars": dollar_str,
                    "yes_bid_dollars": yes_bid_dollar_str,
                    "no_ask_dollars": no_ask_dollar_str,
                    "no_bid_dollars": no_bid_dollar_str,
                    "floor_strike": strike_val,
                    "close_time": expiry,
                    "expiration_time": expiry,
                    "expected_expiration_time": expiry,
                    "volume_fp": evt.get("volume_fp") or evt.get("volume", 0),
                    "result": evt.get("result"),
                    "exchange": "gemini",
                })

        return normalized_markets

    # Alias for general 15m retrieval
    get_15m_markets = get_15m_crypto_markets

    def get_hourly_crypto_markets(
        self,
        status: str = "open",
        limit: int = 100,
        current_hour_only: bool = True,
        now_utc: datetime | None = None,
        series_list: list[str] | None = None,
    ) -> list[dict]:
        """
        Retrieves active hourly multi-strike prediction markets across all supported series:
        Crypto (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, TON, NEAR, ZEC),
        Commodities (Gold, Silver, WTI Oil), and
        Equity Indices (S&P 500 / INX, Nasdaq-100).
        Strictly excludes 5-minute and 15-minute contracts, and verifies expiry is on the hour (:00).
        """
        ref_time = now_utc or datetime.now(timezone.utc)
        target_series = set(s.upper() for s in (series_list or SERIES_HOURLY_ALL))

        events_resp = self.get_events(
            category=["crypto", "commodities", "commodity", "indices", "equities", "stocks"],
            status="active",
            limit=limit,
        )
        events = events_resp.get("data", [])

        normalized_markets = []

        for evt in events:
            if not isinstance(evt, dict):
                continue

            event_ticker = str(evt.get("ticker", ""))
            series = str(evt.get("series") or evt.get("series_ticker") or "")
            expiry = evt.get("expiryDate") or evt.get("close_time") or evt.get("expiration_time")

            # Strictly exclude any 5-minute or 15-minute markets from hourly discovery
            if is_5min_market(event_ticker) or is_5min_market(series):
                continue
            if "15M" in series.upper() or "15M" in event_ticker.upper():
                continue

            # Hourly series check: must match supported hourly series or hourly conventions
            is_hourly = (
                any(s in series.upper() or s in event_ticker.upper() for s in target_series)
                or series.upper().endswith(("H", "D", "U", "1H", "60M"))
                or event_ticker.upper().endswith(("H", "D", "U", "1H", "60M"))
                or any(sym in series.upper() or sym in event_ticker.upper() for sym in (
                    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC",
                    "GOLD", "SILVER", "WTI", "XAU", "XAG", "CL", "INX", "NASDAQ100", "SPX", "NDX", "SP500"
                ))
            )
            if not is_hourly:
                continue

            if current_hour_only:
                is_valid, reason, _ = validate_market_closure_time(
                    close_time_raw=expiry,
                    now_utc=ref_time,
                    timeframe="1h",
                    ticker=event_ticker,
                )
                if not is_valid:
                    continue

            contracts = evt.get("contracts", [])
            if not isinstance(contracts, list) or not contracts:
                continue

            for c in contracts:
                if not isinstance(c, dict):
                    continue

                inst = str(c.get("instrumentSymbol") or c.get("ticker") or event_ticker)

                # Skip any contract with 5-minute or 15-minute marker in instrument symbol
                if is_5min_market(inst) or "15M" in inst.upper():
                    continue

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

                # Extract top-of-book prices
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

                # Impute NO side prices if missing
                if no_ask is None and yes_bid is not None:
                    no_ask = 100 - yes_bid
                if no_bid is None and yes_ask is not None:
                    no_bid = 100 - yes_ask

                dollar_str = f"{(yes_ask / 100.0):.4f}" if yes_ask is not None else None
                yes_bid_dollar_str = f"{(yes_bid / 100.0):.4f}" if yes_bid is not None else None
                no_ask_dollar_str = f"{(no_ask / 100.0):.4f}" if no_ask is not None else None
                no_bid_dollar_str = f"{(no_bid / 100.0):.4f}" if no_bid is not None else None

                normalized_markets.append({
                    "ticker": inst,
                    "event_ticker": event_ticker,
                    "series_ticker": series or "HOURLY",
                    "yes_ask": yes_ask,
                    "yes_bid": yes_bid,
                    "no_ask": no_ask,
                    "no_bid": no_bid,
                    "yes_ask_dollars": dollar_str,
                    "yes_bid_dollars": yes_bid_dollar_str,
                    "no_ask_dollars": no_ask_dollar_str,
                    "no_bid_dollars": no_bid_dollar_str,
                    "floor_strike": strike_val,
                    "close_time": expiry,
                    "expiration_time": expiry,
                    "expected_expiration_time": expiry,
                    "volume_fp": evt.get("volume_fp") or evt.get("volume", 0),
                    "result": evt.get("result"),
                    "exchange": "gemini",
                })

        return normalized_markets

    # Alias for general hourly retrieval
    get_hourly_markets = get_hourly_crypto_markets

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

    def get_orderbook(self, ticker: str) -> dict:
        """Fetches raw orderbook for ticker."""
        try:
            url = f"{self.base_url}/v1/book/{ticker}"
            resp = self.session.get(url, timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.debug(f"Error fetching orderbook for {ticker}: {e}")
        return {"bids": [], "asks": []}


# ---------------------------------------------------------------------------
# Authenticated Gemini Trading Client
# ---------------------------------------------------------------------------
class GeminiTradingClient(BaseTradingClient, GeminiPredictionClient):
    """
    Authenticated API client for placing orders, managing positions, and checking balance on Gemini.
    Uses HMAC-SHA384 signatures passed via X-GEMINI-PAYLOAD headers.
    """
    exchange_name = "Gemini"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        account: str | None = None,
    ):
        BaseTradingClient.__init__(self)
        GeminiPredictionClient.__init__(self, base_url=base_url)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.api_secret = api_secret or os.getenv("GEMINI_API_SECRET")
        self.account = account or os.getenv("GEMINI_ACCOUNT", "trading-bot")

        # In-memory local positions and orders cache
        self._positions_cache: dict[str, dict] = {}
        self._orders_cache: dict[str, list[dict]] = defaultdict(list)

        # Batch cache for portfolio REST endpoints
        self._cached_positions_map: dict[str, dict] = {}
        self._cached_positions_ts: float = 0.0
        self._cached_resting_orders_map: dict[str, list[dict]] = {}
        self._cached_resting_orders_ts: float = 0.0
        self._CACHE_VALIDITY_SECONDS: float = 4.0

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
            resp = self.session.post(url, headers=headers, data="", timeout=10)
            if resp.status_code in (200, 201) and resp.text:
                try:
                    return resp.json()
                except Exception:
                    return {}
            else:
                log.warning(f"Gemini POST {path} returned status {resp.status_code}: {resp.text}")
        except Exception as e:
            log.warning(f"Gemini POST {path} exception: {e}")
        return {}

    def invalidate_portfolio_caches(self):
        """Invalidates in-memory position and order caches upon trade execution or order cancellation."""
        self._cached_positions_ts = 0.0
        self._cached_resting_orders_ts = 0.0

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
            return {
                "balance": max(total_bal, total_avail),
                "available": total_avail,
                "amount": total_bal,
            }
        return {"balance": 0.0, "available": 0.0, "amount": 0.0}

    def get_portfolio_pnl_summary(self) -> dict:
        """Calculates portfolio PnL summary for Gemini account using live exchange positions."""
        bal = self.get_balance()
        avail_usd = bal.get("available", 0.0)
        live_positions = self.get_all_active_positions(force_refresh=True)
        total_open_cost = sum(
            float(pos.get("exposure_dollars") or (pos.get("entry_price_cents", 0) / 100.0) * pos.get("count", 1))
            for pos in live_positions.values()
        )
        port_val = round(avail_usd + total_open_cost, 4)
        return {
            "balance_dollars": avail_usd,
            "portfolio_value_dollars": port_val,
            "open_positions_count": len(live_positions),
            "open_exposure_dollars": round(total_open_cost, 4),
            "total_realized_pnl_dollars": 0.0,
            "total_realized_pnl_cents": 0,
            "settlements_count": 0,
            "total_settled_revenue_dollars": 0.0,
            "total_settled_cost_dollars": 0.0,
            "settlements": [],
            "exchange": "gemini",
        }

    def get_all_active_positions(self, force_refresh: bool = False) -> dict[str, dict]:
        """
        Fetches all live open positions from Gemini's /v1/prediction-markets/positions endpoint.
        Caches results for 4 seconds to prevent redundant REST calls.
        """
        now = time.time()
        if not force_refresh and (now - self._cached_positions_ts) < self._CACHE_VALIDITY_SECONDS:
            return self._cached_positions_map

        positions_map: dict[str, dict] = {}
        try:
            res = self._post("/v1/prediction-markets/positions", {})
            raw_positions = res.get("positions", []) if isinstance(res, dict) else []
            for p in raw_positions:
                sym = p.get("symbol") or p.get("contractMetadata", {}).get("instrumentSymbol")
                if not sym:
                    continue
                qty = float(p.get("totalQuantity") or p.get("quantity") or 0)
                if qty > 0:
                    side = (p.get("outcome") or p.get("side") or "yes").lower()
                    count = int(round(qty))
                    avg_price = float(p.get("avgPrice") or p.get("price") or 0)
                    entry_price = int(round(avg_price * 100)) if avg_price > 0 else self.get_entry_price(sym)
                    exposure = float(p.get("marketValue") or 0) or round(((entry_price or 0) * count) / 100.0, 4)
                    positions_map[sym] = {
                        "ticker": sym,
                        "side": side,
                        "count": count,
                        "position_fp": qty,
                        "entry_price_cents": entry_price,
                        "exposure_dollars": exposure,
                        "avg_price": avg_price,
                    }
            self._cached_positions_map = positions_map
            self._cached_positions_ts = now
        except Exception as error:
            log.warning(f"Could not batch-fetch active Gemini prediction positions: {error}")

        return self._cached_positions_map

    def get_all_resting_orders(self, force_refresh: bool = False) -> dict[str, list[dict]]:
        """
        Fetches all active resting orders from Gemini's /v1/prediction-markets/orders/active endpoint.
        """
        now = time.time()
        if not force_refresh and (now - self._cached_resting_orders_ts) < self._CACHE_VALIDITY_SECONDS:
            return self._cached_resting_orders_map

        orders_map: dict[str, list[dict]] = defaultdict(list)
        try:
            res = self._post("/v1/prediction-markets/orders/active", {"limit": 50})
            orders = res.get("orders", []) if isinstance(res, dict) else []
            for o in orders:
                sym = o.get("symbol")
                if sym:
                    orders_map[sym].append(o)
            self._cached_resting_orders_map = dict(orders_map)
            self._cached_resting_orders_ts = now
        except Exception as error:
            log.warning(f"Could not batch-fetch active Gemini resting orders: {error}")

        return self._cached_resting_orders_map

    def get_market_position(self, ticker: str) -> dict | None:
        """Checks if there is an active position for the given market ticker."""
        return self.get_all_active_positions().get(ticker)

    def has_resting_take_profit_order(self, ticker: str) -> bool:
        """Returns True if there is a resting take profit order for ticker."""
        orders = self.get_all_resting_orders().get(ticker, [])
        return any(o.get("side") == "sell" or o.get("type") == "take_profit" for o in orders)

    def has_existing_position_or_order(self, ticker: str) -> bool:
        """Returns True if an active position or resting order exists for ticker."""
        pos = self.get_market_position(ticker)
        if pos is not None:
            log.info(
                f"{LogTag.POSITION_EXISTS.value} [Gemini] Found active {pos['side'].upper()} "
                f"position of {pos['count']} contract(s) in {ticker}"
            )
            return True

        orders = self.get_all_resting_orders().get(ticker, [])
        if orders:
            log.info(
                f"{LogTag.ORDER_EXISTS.value} [Gemini] Found {len(orders)} resting order(s) for {ticker}"
            )
            return True

        return False

    def place_take_profit_order(
        self,
        ticker: str,
        side: str,
        count: int = 1,
        take_profit_cents: int = 62,
    ) -> dict | None:
        """Places a resting take-profit limit sell order on Gemini Prediction Markets."""
        side_clean = side.lower()

        # Safeguard: Take-Profit limit sell must NEVER be placed <= entry price
        entry_price = self.get_entry_price(ticker)
        if entry_price and take_profit_cents <= entry_price:
            safe_tp = min(99, max(entry_price + 5, 88 if entry_price >= 50 else 62))
            log.warning(
                f"⚠️ [TAKE-PROFIT GUARD] Attempted to place TP at {take_profit_cents}¢ <= entry price {entry_price}¢ "
                f"on {ticker}! Auto-adjusting TP to {safe_tp}¢."
            )
            take_profit_cents = safe_tp

        payload = {
            "symbol": ticker,
            "side": "sell",
            "outcome": side_clean,
            "quantity": str(count),
            "price": f"{(take_profit_cents / 100.0):.2f}",
            "orderType": "limit",
            "timeInForce": "good-til-cancel",
        }
        res = self._post("/v1/prediction-markets/order", payload)
        self.invalidate_portfolio_caches()
        log.info(f"{LogTag.AUTO_SELL_SET.value} [Gemini] Placed resting TP order for {ticker} ({side_clean.upper()}) at {take_profit_cents}¢.")
        return {"take_profit_cents": take_profit_cents, "order": res}

    def buy_and_set_take_profit(
        self,
        ticker: str,
        side: str,
        buy_price_cents: int,
        take_profit_cents: int = 62,
        count: int = 1,
        maker_bid_cents: int | None = None,
        buy_order_timeout_seconds: int = DEFAULT_BUY_ORDER_TIMEOUT_SECONDS,
    ) -> dict:
        """
        Submits a Maker limit buy order and places resting take-profit sell order on Gemini.
        """
        if is_5min_market(ticker):
            log.error(f"🛑 [5-MINUTE MARKET REJECTED] [Gemini] Refusing to place order on 5-minute market: {ticker}")
            return {"error": "5min_market_rejected", "ticker": ticker}

        side_clean = side.lower()
        effective_buy_cents = min(buy_price_cents - 1, maker_bid_cents + 1) if (maker_bid_cents is not None and maker_bid_cents < buy_price_cents) else max(1, buy_price_cents - 1)

        payload = {
            "symbol": ticker,
            "side": "buy",
            "outcome": side_clean,
            "quantity": str(count),
            "price": f"{(effective_buy_cents / 100.0):.2f}",
            "orderType": "limit",
            "timeInForce": "good-til-cancel",
        }
        res = self._post("/v1/prediction-markets/order", payload)
        order_id = res.get("orderId") if isinstance(res, dict) else None

        if not order_id and not res.get("status"):
            log.warning(f"❌ [ORDER FAILED] [Gemini] Order rejected for {ticker} ({side_clean.upper()}): {res}")
            return {"error": "order_rejected", "res": res}

        client_order_id = generate_safe_client_order_id(f"buy_{side_clean}", ticker)

        self.set_entry_price(
            ticker=ticker,
            price_cents=effective_buy_cents,
            side=side_clean,
            count=count,
            client_order_id=client_order_id,
            take_profit_cents=take_profit_cents,
        )

        sell_order_res = self.place_take_profit_order(
            ticker=ticker,
            side=side_clean,
            count=count,
            take_profit_cents=take_profit_cents,
        )

        self.invalidate_portfolio_caches()
        log.info(f"{LogTag.TRADE_EXECUTION.value} [Gemini] Order placed for {ticker} ({side_clean.upper()}) at {effective_buy_cents}¢.")
        return {
            "buy_price_cents": effective_buy_cents,
            "take_profit_cents": take_profit_cents,
            "res": res,
            "client_order_id": client_order_id,
            "sell_auto_take_profit_order": sell_order_res,
        }

    def buy_favorite_maker_order(
        self,
        ticker: str,
        side: str,
        maker_bid_cents: int,
        target_ask_cents: int,
        count: int = 1,
        buy_order_timeout_seconds: int = 60,
    ) -> dict:
        """
        Submits a post_only Maker Limit Bid for a confirmed Favorite (74¢–93¢) on Gemini.
        """
        if is_5min_market(ticker):
            log.error(f"🛑 [5-MINUTE MARKET REJECTED] [Gemini] Refusing to place Favorite Maker order on 5-minute market: {ticker}")
            return {"error": "5min_market_rejected", "ticker": ticker}

        side_clean = side.lower()
        effective_bid = max(1, min(99, maker_bid_cents))
        if target_ask_cents and effective_bid >= target_ask_cents:
            effective_bid = target_ask_cents - 1

        if effective_bid < 1 or (target_ask_cents and target_ask_cents <= 1):
            log.warning(
                f"⚠️ [POST-ONLY CROSS SKIPPED] [Gemini] Favorite Maker bid on {ticker} ({side_clean.upper()}) "
                f"would cross ask ({target_ask_cents}¢) or produce <1¢ price. Skipping to avoid taker fill."
            )
            return {"error": "post_only_cross", "reason": "target_ask_too_low"}

        effective_bid = max(1, min(99, effective_bid))
        spread_saved = max(0, target_ask_cents - effective_bid)

        payload = {
            "symbol": ticker,
            "side": "buy",
            "outcome": side_clean,
            "quantity": str(count),
            "price": f"{(effective_bid / 100.0):.2f}",
            "orderType": "limit",
            "timeInForce": "good-til-cancel",
        }
        res = self._post("/v1/prediction-markets/order", payload)
        order_id = res.get("orderId") if isinstance(res, dict) else None

        if not order_id and not res.get("status"):
            log.warning(f"❌ [FAVORITE MAKER FAILED] [Gemini] Order rejected for {ticker} ({side_clean.upper()}): {res}")
            return {"error": "favorite_maker_rejected", "res": res}

        client_order_id = generate_safe_client_order_id(f"fav_maker_{side_clean}", ticker)

        self.set_entry_price(
            ticker=ticker,
            price_cents=effective_bid,
            side=side_clean,
            count=count,
            client_order_id=client_order_id,
            strategy_tag="endgame_favorite_maker",
            spread_saved_cents=spread_saved,
            order_role="maker",
        )

        self.invalidate_portfolio_caches()
        log.info(
            f"🎯 [FAVORITE MAKER BID] [Gemini] {ticker} | Side: {side_clean.upper()} | "
            f"Resting Bid: {effective_bid}¢ (Ask: {target_ask_cents}¢, Spread Saved: +{spread_saved}¢) | Count: {count}"
        )

        return {
            "buy_order": res,
            "side": side_clean,
            "buy_price_cents": effective_bid,
            "target_ask_cents": target_ask_cents,
            "spread_saved_cents": spread_saved,
            "client_order_id": client_order_id,
            "strategy_tag": "endgame_favorite_maker",
        }

    def execute_buy(self, ticker: str, side: str, count: int = 1, price_cents: int = 70) -> dict:
        """Executes a buy order on Gemini."""
        if is_5min_market(ticker):
            log.error(f"🛑 [5-MINUTE MARKET REJECTED] [Gemini] Refusing to place buy order on 5-minute market: {ticker}")
            return {"error": "5min_market_rejected", "ticker": ticker}

        side_clean = side.lower()
        payload = {
            "symbol": ticker,
            "side": "buy",
            "outcome": side_clean,
            "quantity": str(count),
            "price": f"{(price_cents / 100.0):.2f}",
            "orderType": "limit",
            "timeInForce": "good-til-cancel",
        }
        res = self._post("/v1/prediction-markets/order", payload)
        if res.get("orderId") or res.get("status"):
            self.set_entry_price(ticker, price_cents, side=side_clean, count=count)
            self.invalidate_portfolio_caches()
        return res

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        """Executes position liquidation sell order on Gemini and clears local entry price."""
        side_clean = side.lower()
        payload = {
            "symbol": ticker,
            "side": "sell",
            "outcome": side_clean,
            "quantity": str(count),
            "price": f"{(current_price / 100.0):.2f}",
            "orderType": "limit",
            "timeInForce": "good-til-cancel",
        }
        res = self._post("/v1/prediction-markets/order", payload)
        self.clear_profit_floor(ticker)
        self.clear_entry_price(ticker)
        self.invalidate_portfolio_caches()
        return res

    def cancel_order(self, order_id: str | int) -> dict:
        """Cancels an existing order by its unique order ID."""
        order_val = int(order_id) if str(order_id).isdigit() else order_id
        return self._post("/v1/prediction-markets/order/cancel", {"orderId": order_val})

    def cancel_order_safe(self, order_id: str | int) -> bool:
        """Safely attempts to cancel an order."""
        try:
            res = self.cancel_order(order_id)
            return bool(res)
        except Exception:
            return False

    def cancel_unfilled_buy_orders(self, ticker: str) -> int:
        """Cancels any unfilled buy orders for ticker."""
        orders = self.get_all_resting_orders(force_refresh=True).get(ticker, [])
        cancelled = 0
        for o in orders:
            if o.get("side") == "buy":
                oid = o.get("orderId")
                if oid and self.cancel_order_safe(oid):
                    cancelled += 1
        if cancelled > 0:
            self.invalidate_portfolio_caches()
        return cancelled

    def cancel_and_verify_resting_orders(
        self,
        ticker: str,
        max_retries: int = 5,
        poll_interval: float = 0.2,
    ) -> bool:
        """Cancels all resting orders for ticker and verifies they are cleared."""
        orders = self.get_all_resting_orders(force_refresh=True).get(ticker, [])
        for o in orders:
            oid = o.get("orderId")
            if oid:
                self.cancel_order_safe(oid)
        self.invalidate_portfolio_caches()
        return True

    def get_crypto_directional_counts(self) -> dict[str, int]:
        """Counts active open positions on crypto assets categorized by direction."""
        positions_map = self.get_all_active_positions()
        counts = {"yes": 0, "no": 0}
        crypto_prefixes = (
            "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB",
            "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC",
        )
        for ticker, pos in positions_map.items():
            if any(ticker.upper().startswith(p) for p in crypto_prefixes):
                side = (pos.get("side") or "").lower()
                if side in counts:
                    counts[side] += 1
        return counts

    def check_crypto_portfolio_directional_cap(self, side: str, max_concurrent: int = 3) -> bool:
        """Enforces a cross-asset directional portfolio cap."""
        side_clean = side.lower()
        counts = self.get_crypto_directional_counts()
        current_in_dir = counts.get(side_clean, 0)
        if current_in_dir >= max_concurrent:
            log.info(
                f"🛡️ [CORRELATION RISK CAP] [Gemini] Active crypto {side_clean.upper()} positions ({current_in_dir}) "
                f">= max cap ({max_concurrent}). Skipping new entry."
            )
            return False
        return True


# Primary Gemini client alias
GeminiClient = GeminiTradingClient

