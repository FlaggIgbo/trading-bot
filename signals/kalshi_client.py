"""
Kalshi API Client & Multi-Timeframe Crypto Execution Engine
============================================================

What is Kalshi?
---------------
Kalshi is a CFTC-regulated prediction market exchange offering binary event contracts
across 15-Minute and Hourly directional markets spanning Crypto, Commodities, and Equity Indices.
Contracts settle at either $1.00 (100 cents) if YES occurs, or $0.00 if NO occurs.

Authentication Explained:
-------------------------
Kalshi uses RSA-PSS digital signatures for API security:
1. Every request header includes a millisecond timestamp, API key ID, and an RSA signature.
2. The signature signs `timestamp + HTTP_METHOD + path` using an RSA private key file (`.key`).
3. Kalshi verifies this signature using your registered public key.

Execution Safety & Liquidation Rules:
--------------------------------------
- Maker Limit Buys (`Bid + 1¢`) to capture spread discounts and optimize edge.
- IOC-Only Liquidation (Immediate-Or-Cancel) with dynamic re-pricing.
- Zero Resting 99¢ GTC Bids: Prevents asymmetric downside risk during market shocks.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from signals.base_client import (
    BaseTradingClient,
    LogTag,
    is_5min_market,
    validate_market_closure_time,
)

# Load configuration from the project's .env file if available
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)





# Default resting order timeout in seconds for Maker buy orders (mitigates adverse selection)
DEFAULT_BUY_ORDER_TIMEOUT_SECONDS = int(
    os.getenv("KALSHI_BUY_ORDER_EXPIRATION_SECONDS", os.getenv("BUY_ORDER_EXPIRATION_SECONDS", "45"))
)


def generate_safe_client_order_id(prefix: str, ticker: str, timestamp_ms: int | None = None) -> str:
    """
    Generates a sanitized client_order_id for Kalshi orders.
    Kalshi strictly requires alphanumeric, hyphen, and underscore characters ([a-zA-Z0-9_-]).
    Strike tickers containing decimal points (e.g. DOGE 'T0.0699999', XRP 'T0.9999', or SOL 'T99.9999')
    will cause Kalshi to reject the order with 400 invalid_parameters if dots '.' are included.
    """
    safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
    ts = timestamp_ms or int(time.time() * 1000)
    return f"{prefix}_{safe_ticker}_{ts}"

# ---------------------------------------------------------------------------
# API Base Endpoints
# ---------------------------------------------------------------------------
# Demo/Sandbox URL (for safe paper trading & testing)
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Production URL (for live real-money trading)
PRODUCTION_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ---------------------------------------------------------------------------
# 15-Minute Series Tickers (Crypto & Commodities)
# ---------------------------------------------------------------------------
# Kalshi groups 15-minute crypto markets under these series identifiers:
SERIES_15M_CRYPTO = [
    "KXBTC15M",   # Bitcoin 15-minute up/down
    "KXETH15M",   # Ethereum 15-minute up/down
    "KXSOL15M",   # Solana 15-minute up/down
    "KXXRP15M",   # Ripple / XRP 15-minute up/down
    "KXDOGE15M",  # Dogecoin 15-minute up/down
    "KXBNB15M",   # Binance Coin (BNB) 15-minute up/down
    "KXHYPE15M",  # Hyperliquid (HYPE) 15-minute up/down
    "KXADA15M",   # Cardano (ADA) 15-minute up/down
    "KXTON15M",   # Toncoin (TON) 15-minute up/down
    "KXBCH15M",   # Bitcoin Cash (BCH) 15-minute up/down
    "KXNEAR15M",  # NEAR Protocol 15-minute up/down
    "KXZEC15M",   # Zcash (ZEC) 15-minute up/down
]

# Kalshi groups 15-minute commodity markets under these series identifiers:
SERIES_15M_COMMODITIES = [
    "KXGOLD15M",    # Gold 15-minute up/down
    "KXWTI15M",     # WTI Crude Oil 15-minute up/down
    "KXSILVER15M",  # Silver 15-minute up/down
]

# Unified list of all active 15-minute prediction market series
SERIES_15M_ALL = SERIES_15M_CRYPTO + SERIES_15M_COMMODITIES

# ---------------------------------------------------------------------------
# Hourly Multi-Strike Series Tickers (Crypto, Commodities & Equity Indices)
# ---------------------------------------------------------------------------
# Kalshi groups hourly multi-strike crypto markets under these series identifiers:
SERIES_HOURLY_CRYPTO = [
    "KXBTCD",   # Bitcoin Hourly Above/Below
    "KXETHD",   # Ethereum Hourly Above/Below
    "KXSOLD",   # Solana Hourly Directional
    "KXXRPD",   # Ripple / XRP Hourly Above/Below
    "KXDOGED",  # Dogecoin Hourly Above/Below
    "KXBNBD",   # Binance Coin (BNB) Hourly Directional
    "KXHYPED",  # Hyperliquid (HYPE) Hourly Directional
    "KXTOND",   # Toncoin (TON) Hourly Directional
    "KXNEARD",  # NEAR Protocol Hourly Directional
    "KXZECD",   # Zcash (ZEC) Hourly Directional
    "KXTONH",   # Toncoin (TON) Alternate Hourly Directional
    "KXNEARH",  # NEAR Alternate Hourly Directional
    "KXZECH",   # Zcash Alternate Hourly Directional
]

# Kalshi groups hourly commodity markets under these series identifiers:
SERIES_HOURLY_COMMODITIES = [
    "KXGOLDH",    # Gold Hourly Above/Below
    "KXSILVERH",  # Silver Hourly Above/Below
    "KXWTIH",     # WTI Crude Oil Hourly Above/Below
]

# Kalshi groups hourly equity index markets under these series identifiers:
SERIES_HOURLY_INDICES = [
    "KXINXU",        # S&P 500 Hourly Above/Below
    "KXNASDAQ100U",  # Nasdaq-100 Hourly Above/Below
]

# Unified list of all active hourly prediction market series
SERIES_HOURLY_ALL = SERIES_HOURLY_CRYPTO + SERIES_HOURLY_COMMODITIES + SERIES_HOURLY_INDICES

# ---------------------------------------------------------------------------
# CF Benchmarks Real-Time Index (RTI) Identifiers for Crypto Series
# ---------------------------------------------------------------------------
# Kalshi settles all crypto prediction markets against CF Benchmarks RTIs
# (60-second TWAP average during the final minute before contract expiration).
CFBENCHMARKS_CRYPTO_INDEX_MAP: dict[str, str] = {
    "BTC": "BRTI",
    "ETH": "ETHUSD_RTI",
    "SOL": "SOLUSD_RTI",
    "DOGE": "DOGEUSD_RTI",
    "XRP": "XRPUSD_RTI",
    "HYPE": "HYPEUSD_RTI",
    "NEAR": "NEARUSD_RTI",
    "ADA": "ADAUSD_RTI",
    "BNB": "BNBUSD_RTI",
    "BCH": "BCHUSD_RTI",
    "TON": "TONUSD_RTI",
    "ZEC": "ZECUSD_RTI",
}

# Common Kalshi API 400 error codes
KALSHI_ERROR_INSUFFICIENT_BALANCE = "insufficient_balance"
KALSHI_ERROR_INVALID_PRICE = "invalid_price"
KALSHI_ERROR_UNKNOWN_SYMBOL = "unknown_symbol"
KALSHI_ERROR_MARKET_CLOSED = "market_closed"
KALSHI_ERROR_DUPLICATE_ORDER = "duplicate_order"


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def parse_kalshi_error(response: requests.Response | dict) -> tuple[str | None, str]:
    """
    Extracts the error code and error message from a Kalshi HTTP error response.

    Returns:
        tuple of (error_code, error_message)
    """
    error_data = None
    if hasattr(response, "text") and response.text:
        try:
            data = json.loads(response.text)
            error_data = data.get("error") or data
        except Exception:
            return None, (response.text or "")[:200]
    elif isinstance(response, dict):
        error_data = response.get("error") or response

    if not error_data or not isinstance(error_data, dict):
        return None, str(error_data or "")

    code = error_data.get("code")
    message = error_data.get("message") or str(error_data)
    return code, message


def round_price_to_tick(price_dollars: float | str) -> str:
    """
    Rounds a dollar price float (e.g. 0.523) to a valid Kalshi tick ($0.01 step).
    Prices must remain within the open interval ($0.01 to $0.99).
    """
    price_val = float(price_dollars)
    clamped_price = max(0.01, min(0.99, price_val))
    rounded_cents = round(clamped_price * 100) / 100
    return f"{rounded_cents:.2f}"


def parse_cents(val_dollars: str | float | None, val_cents: int | str | None = None) -> int | None:
    """
    Safely converts string dollars (e.g. '0.3500') or cent integers (e.g. 35) to an integer cent value (1-99).
    Returns None if the price is missing, 0, or invalid.
    """
    for val, multiplier in ((val_dollars, 100), (val_cents, 1)):
        if val is not None:
            try:
                res = int(round(float(val) * multiplier))
                if 1 <= res <= 99:
                    return res
            except (ValueError, TypeError):
                continue
    return None


def _load_private_key():
    """
    Loads an RSA private key from either a local file path (KALSHI_PRIVATE_KEY_PATH)
    or directly from an environment variable string (KALSHI_PRIVATE_KEY).
    """
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    key_content = os.getenv("KALSHI_PRIVATE_KEY")

    if key_path:
        path = Path(key_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"RSA Private Key file not found at path: {path}")
        pem_bytes = path.read_bytes()
    elif key_content:
        pem_bytes = key_content.replace("\\n", "\n").encode("utf-8")
    else:
        raise ValueError(
            "Missing RSA key. Set KALSHI_PRIVATE_KEY_PATH (path to .key file) "
            "or KALSHI_PRIVATE_KEY (PEM string) in your .env file."
        )

    # Parse and return the RSA private key object
    return serialization.load_pem_private_key(
        pem_bytes, password=None, backend=default_backend()
    )


def _sign_request(private_key, timestamp_ms: str, http_method: str, request_path: str) -> str:
    """
    Generates an RSA-PSS SHA-256 digital signature for API request authentication.

    The message signed is: `timestamp_ms + HTTP_METHOD + path_without_query_params`
    """
    # Strip any query parameters (e.g., '?limit=100') before signing
    clean_path = request_path.split("?")[0]
    message_to_sign = f"{timestamp_ms}{http_method}{clean_path}".encode("utf-8")

    # Generate RSA-PSS signature
    raw_signature = private_key.sign(
        message_to_sign,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    # Convert binary signature to base64 string
    return base64.b64encode(raw_signature).decode("utf-8")


# ---------------------------------------------------------------------------
# Main Kalshi API Client Class
# ---------------------------------------------------------------------------

class KalshiClient(BaseTradingClient):
    """
    Authenticated Client for interacting with the Kalshi Exchange REST API.
    Provides methods to query balances, fetch 15-minute crypto markets, and place orders.
    """
    exchange_name = "Kalshi"

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key=None,
        base_url: str | None = None,
        allow_live_orders: bool | None = None,
    ):
        """
        Initializes the Kalshi API client.

        Args:
            api_key_id: Your Kalshi API key ID (UUID format).
            private_key: Loaded RSA private key object (optional; loaded automatically if None).
            base_url: Overriding API base URL (optional; defaults to Demo or Production).
            allow_live_orders: Whether to allow live order placement (defaults to False for safety unless ENABLE_LIVE_ORDERS=true).
        """
        super().__init__(allow_live_orders=allow_live_orders)
        self.api_key_id = api_key_id or os.getenv("KALSHI_API_KEY_ID")
        self.private_key = private_key or _load_private_key()

        # Determine base URL (Demo vs Production)
        use_demo = os.getenv("KALSHI_USE_DEMO", "true").lower() == "true"
        default_base = DEMO_BASE_URL if use_demo else PRODUCTION_BASE_URL
        self.base_url = (base_url or os.getenv("KALSHI_BASE_URL") or default_base).rstrip("/")

        if not self.api_key_id:
            raise ValueError(
                "KALSHI_API_KEY_ID is missing! Please set it in your .env file."
            )

        # Persistent HTTP session for connection pooling across 24/7 execution
        self.session = requests.Session()

        # In-memory batch caches for positions and resting orders to eliminate 429 rate limit errors
        self._cached_positions_map: dict[str, dict] = {}
        self._cached_positions_ts: float = 0.0
        self._cached_resting_orders_map: dict[str, list[dict]] = {}
        self._cached_resting_orders_ts: float = 0.0
        self._CACHE_VALIDITY_SECONDS: float = 4.0

    def _build_auth_headers(self, http_method: str, path: str) -> dict[str, str]:
        """
        Generates required authentication HTTP headers for Kalshi requests.
        Headers include Access Key, Timestamp in ms, and RSA-PSS Signature.
        """
        timestamp_ms = str(int(time.time() * 1000))
        signature = _sign_request(self.private_key, timestamp_ms, http_method, path)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _get_signing_path(self, endpoint: str) -> str:
        """Extracts URL path without domain name (e.g. '/trade-api/v2/portfolio/balance')."""
        clean_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        full_url = f"{self.base_url}{clean_endpoint}"
        return urlparse(full_url).path

    def _build_full_url(self, endpoint: str, params: dict | None = None) -> str:
        """Constructs full URL with optional query parameters."""
        clean_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        full_url = f"{self.base_url}{clean_endpoint}"
        if params:
            # Filter out None values and encode query parameters
            filtered_params = {k: v for k, v in params.items() if v is not None}
            if filtered_params:
                full_url = f"{full_url}?{urlencode(filtered_params)}"
        return full_url

    # -----------------------------------------------------------------------
    # Core HTTP Request Methods (GET, POST, DELETE)
    # -----------------------------------------------------------------------

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """Sends an authenticated HTTP GET request to Kalshi using connection pooling."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint, params)
        headers = self._build_auth_headers("GET", signing_path)

        response = self.session.get(full_url, headers=headers, timeout=30)
        response.raise_for_status()
        if response.text:
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                log.debug(f"Non-JSON response from Kalshi GET {endpoint}: {response.text[:200]}")
        return {}

    def post(self, endpoint: str, payload: dict | None = None) -> dict:
        """Sends an authenticated HTTP POST request to Kalshi using connection pooling."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint)
        headers = self._build_auth_headers("POST", signing_path)

        response = self.session.post(full_url, headers=headers, json=payload or {}, timeout=30)
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            err_details = f"{e} | Response body: {response.text}"
            if "post only cross" in (response.text or "").lower():
                log.debug(f"Kalshi POST {endpoint} post-only cross (book moved): {err_details}")
            else:
                log.error(f"Kalshi POST {endpoint} failed with {err_details}")
            raise requests.HTTPError(err_details, response=response) from e

        if response.text:
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                log.debug(f"Non-JSON response from Kalshi POST {endpoint}: {response.text[:200]}")
        return {}

    def delete(self, endpoint: str, allow_404: bool = False) -> dict:
        """Sends an authenticated HTTP DELETE request to Kalshi using connection pooling."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint)
        headers = self._build_auth_headers("DELETE", signing_path)

        response = self.session.delete(full_url, headers=headers, timeout=30)
        if allow_404 and response.status_code == 404:
            return {"status": "not_found"}

        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            err_details = f"{e} | Response body: {response.text}"
            resp_lower = (response.text or "").lower()
            if allow_404 and (response.status_code == 404 or "not_found" in resp_lower or "order not found" in resp_lower):
                log.debug(f"Kalshi DELETE {endpoint} order not found or already closed: {err_details}")
                return {"status": "not_found"}
            log.error(f"Kalshi DELETE {endpoint} failed with {err_details}")
            raise requests.HTTPError(err_details, response=response) from e

        if response.text:
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                log.debug(f"Non-JSON response from Kalshi DELETE {endpoint}: {response.text[:200]}")
        return {}

    # -----------------------------------------------------------------------
    # Account & Portfolio Endpoints
    # -----------------------------------------------------------------------

    def get_balance(self) -> dict:
        """
        Fetches portfolio balance in cents.
        Example return: `{"balance": 1298}` -> representing $12.98 USD available.
        """
        return self.get("/portfolio/balance")

    def get_positions(self, **params) -> dict:
        """Fetches current open positions in your portfolio."""
        return self.get("/portfolio/positions", params)

    def get_orders(self, **params) -> dict:
        """Fetches active resting orders in your portfolio."""
        return self.get("/portfolio/orders", params)

    def get_fills(self, **params) -> dict:
        """Fetches recent order execution fills."""
        return self.get("/portfolio/fills", params)

    def get_settlements(self, **params) -> dict:
        """Fetches settled market positions and historical payouts."""
        return self.get("/portfolio/settlements", params)

    def get_portfolio_pnl_summary(self) -> dict:
        """
        Queries native Kalshi REST endpoints (/portfolio/balance, /portfolio/positions, /portfolio/settlements)
        to compute a real-time portfolio balance, active open exposure, and realized PnL summary.
        """
        # 1. Fetch Account Balance & Total Portfolio Value
        balance_resp = self.get_balance()
        bal_dollars = balance_resp.get("balance_dollars")
        bal_cents = balance_resp.get("balance")
        val_dollars = balance_resp.get("portfolio_value_dollars")
        val_cents = balance_resp.get("portfolio_value")

        balance = (
            float(bal_dollars)
            if bal_dollars is not None
            else ((bal_cents / 100.0) if bal_cents is not None else 0.0)
        )
        portfolio_value = (
            float(val_dollars)
            if val_dollars is not None
            else ((val_cents / 100.0) if val_cents is not None else balance)
        )

        # 2. Fetch Open Positions & Active Exposure
        try:
            positions_resp = self.get_positions()
            raw_positions = positions_resp.get("market_positions") or []
        except Exception as err:
            log.debug(f"Could not fetch open positions for PnL summary: {err}")
            raw_positions = []

        active_positions = []
        total_open_cost = 0.0
        for p in raw_positions:
            fp = float(p.get("position_fp") or p.get("position") or 0)
            if abs(fp) > 0:
                side = "yes" if fp > 0 else "no"
                count = int(round(abs(fp)))
                exp = float(p.get("market_exposure_dollars") or 0.0)
                total_open_cost += exp
                active_positions.append({
                    "ticker": p.get("ticker"),
                    "side": side,
                    "count": count,
                    "exposure_dollars": exp,
                })

        # 3. Fetch Settlements & Calculate Realized PnL
        try:
            settlements_resp = self.get_settlements(limit=100)
            raw_settlements = settlements_resp.get("settlements") or []
        except Exception as err:
            log.debug(f"Could not fetch settlements for PnL summary: {err}")
            raw_settlements = []

        settlement_records = []
        total_realized_pnl = 0.0
        total_settled_revenue = 0.0
        total_settled_cost = 0.0

        for s in raw_settlements:
            ticker = s.get("ticker", "UNKNOWN")
            result = str(s.get("market_result") or "").lower()
            settled_time = s.get("settled_time")

            # Parse counts from either string float (_fp) or int
            y_count_raw = s.get("yes_count_fp") or s.get("yes_count") or 0
            n_count_raw = s.get("no_count_fp") or s.get("no_count") or 0
            try:
                y_count = float(y_count_raw)
            except (ValueError, TypeError):
                y_count = 0.0
            try:
                n_count = float(n_count_raw)
            except (ValueError, TypeError):
                n_count = 0.0

            # Revenue / payout:
            # On Kalshi V2, s.get("revenue") is 0 for expired events.
            # True payout is $1.00 per winning contract held at expiration.
            rev_dollars = s.get("revenue_dollars")
            rev_cents = s.get("revenue")
            if rev_dollars is not None and float(rev_dollars) > 0:
                revenue = float(rev_dollars)
            elif rev_cents is not None and float(rev_cents) > 0:
                revenue = float(rev_cents) / 100.0
            else:
                if result == "yes":
                    revenue = round(y_count * 1.00, 4)
                elif result == "no":
                    revenue = round(n_count * 1.00, 4)
                else:
                    revenue = 0.0

            # Costs
            y_cost_d = s.get("yes_total_cost_dollars")
            y_cost_c = s.get("yes_total_cost")
            y_cost = (
                float(y_cost_d)
                if y_cost_d is not None
                else ((y_cost_c / 100.0) if y_cost_c is not None else 0.0)
            )

            n_cost_d = s.get("no_total_cost_dollars")
            n_cost_c = s.get("no_total_cost")
            n_cost = (
                float(n_cost_d)
                if n_cost_d is not None
                else ((n_cost_c / 100.0) if n_cost_c is not None else 0.0)
            )

            total_cost = round(y_cost + n_cost, 4)
            pnl = round(revenue - total_cost, 4)

            total_settled_revenue += revenue
            total_settled_cost += total_cost
            total_realized_pnl += pnl

            settlement_records.append({
                "ticker": ticker,
                "market_result": result,
                "yes_count": int(round(y_count)),
                "no_count": int(round(n_count)),
                "revenue_dollars": round(revenue, 2),
                "cost_dollars": round(total_cost, 2),
                "pnl_dollars": round(pnl, 2),
                "pnl_cents": int(round(pnl * 100)),
                "settled_time": settled_time,
            })

        return {
            "balance_dollars": round(balance, 2),
            "portfolio_value_dollars": round(portfolio_value, 2),
            "open_positions_count": len(active_positions),
            "open_exposure_dollars": round(total_open_cost, 2),
            "active_positions": active_positions,
            "settlements_count": len(settlement_records),
            "total_settled_cost_dollars": round(total_settled_cost, 2),
            "total_settled_revenue_dollars": round(total_settled_revenue, 2),
            "total_realized_pnl_dollars": round(total_realized_pnl, 2),
            "total_realized_pnl_cents": int(round(total_realized_pnl * 100)),
            "settlements": settlement_records,
        }


    # -----------------------------------------------------------------------
    # Market Data & Multi-Asset 15-Minute / Hourly Retrieval
    # -----------------------------------------------------------------------

    def get_markets(self, **params) -> dict:
        """Queries markets from Kalshi (supports filtering by series_ticker, status, limit)."""
        return self.get("/markets", params)

    def get_15m_crypto_markets(
        self,
        status: str = "open",
        limit: int = 100,
        series_list: list[str] | None = None,
        now_utc: datetime | None = None,
    ) -> list[dict]:
        """
        Retrieves active 15-minute prediction markets across all supported series
        (Crypto: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ADA, TON, BCH, NEAR, ZEC;
         Commodities: GOLD, WTI Oil, SILVER).
        Strictly excludes 5-minute markets and verifies expiry is within active 15m window (:00/:15/:30/:45).

        Returns:
            list[dict]: A list of market dictionaries matching active 15m contracts.
        """
        all_15m_markets = []
        ref_time = now_utc or datetime.now(timezone.utc)
        target_series = series_list or SERIES_15M_ALL

        for series_ticker in target_series:
            # Skip any 5-minute series
            if is_5min_market(series_ticker):
                continue
            try:
                response = self.get_markets(
                    series_ticker=series_ticker, status=status, limit=limit
                )
                markets = response.get("markets", [])
                for m in markets:
                    ticker = m.get("ticker", "")
                    if is_5min_market(ticker):
                        continue

                    # Validate 15m expiration boundary and active cycle time distance
                    close_iso = (
                        m.get("close_time")
                        or m.get("expiration_time")
                        or m.get("expected_expiration_time")
                        or m.get("latest_expiration_time")
                    )
                    is_valid, reason, _ = validate_market_closure_time(
                        close_time_raw=close_iso,
                        now_utc=ref_time,
                        timeframe="15m",
                        ticker=ticker,
                    )
                    if not is_valid:
                        continue

                    all_15m_markets.append(m)
            except Exception as error:
                log.warning(
                    f"Could not fetch 15m markets for series '{series_ticker}': {error}"
                )

        return all_15m_markets

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
        Strictly excludes 5-minute and 15-minute contracts, and validates hourly boundary (:00).

        Args:
            status: Market status ('open', 'closed', 'settled')
            limit: Pagination limit per series
            current_hour_only: If True, filters only for contracts closing in the current hour (<= 65 mins remaining)
            now_utc: Reference UTC time for expiration filtering
            series_list: Optional override of series tickers to query (defaults to SERIES_HOURLY_ALL)

        Returns:
            list[dict]: List of market dictionaries matching active hourly contracts.
        """
        all_hourly_markets = []
        ref_time = now_utc or datetime.now(timezone.utc)
        target_series = series_list or SERIES_HOURLY_ALL

        for series_ticker in target_series:
            # Skip any 5-minute or 15-minute series from hourly discovery
            if is_5min_market(series_ticker) or "15M" in series_ticker.upper():
                continue
            try:
                response = self.get_markets(
                    series_ticker=series_ticker, status=status, limit=limit
                )
                markets = response.get("markets", [])
                for m in markets:
                    ticker = m.get("ticker", "")
                    if is_5min_market(ticker) or "15M" in ticker.upper():
                        continue

                    if current_hour_only:
                        close_iso = (
                            m.get("close_time")
                            or m.get("expiration_time")
                            or m.get("expected_expiration_time")
                            or m.get("latest_expiration_time")
                        )
                        is_valid, reason, _ = validate_market_closure_time(
                            close_time_raw=close_iso,
                            now_utc=ref_time,
                            timeframe="1h",
                            ticker=ticker,
                        )
                        if not is_valid:
                            continue

                    all_hourly_markets.append(m)
            except Exception as error:
                log.warning(
                    f"Could not fetch hourly markets for series '{series_ticker}': {error}"
                )

        return all_hourly_markets

    # Alias for general hourly retrieval
    get_hourly_markets = get_hourly_crypto_markets

    def get_market(self, ticker: str) -> dict:
        """Fetches detailed information for a single market ticker."""
        return self.get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        """Fetches current orderbook (bids and asks) for a given market ticker."""
        return self.get(f"/markets/{ticker}/orderbook")

    def get_cfbenchmarks_values(self, index_id: str) -> dict:
        """
        Retrieves real-time index tick data from Kalshi's CF Benchmarks feed endpoint.
        Returns:
            dict containing {"data": {"serverTime": "...", "payload": [{"value": "...", "time": ...}, ...]}}
        """
        return self.get("/cfbenchmarks/values", params={"id": index_id})

    def get_cfbenchmarks_price(self, symbol: str, calc_twap_seconds: int = 0) -> float | None:
        """
        Retrieves the latest CF Benchmarks index price for a crypto symbol.
        If calc_twap_seconds > 0 (e.g. 60s for Kalshi expiration), computes the time-weighted
        average price (TWAP) across the payload ticks in that time window matching Kalshi settlement rules.
        """
        idx = CFBENCHMARKS_CRYPTO_INDEX_MAP.get(symbol.upper())
        if not idx:
            return None
        try:
            res = self.get_cfbenchmarks_values(idx)
            payload = res.get("data", {}).get("payload", [])
            if not payload:
                return None
            if calc_twap_seconds > 0 and len(payload) > 1:
                latest_time = payload[-1].get("time", 0)
                cutoff_time = latest_time - (calc_twap_seconds * 1000)
                window_values = [
                    float(p["value"]) for p in payload
                    if p.get("time", 0) >= cutoff_time and p.get("value") is not None
                ]
                if window_values:
                    return round(sum(window_values) / len(window_values), 4)
            latest_val = payload[-1].get("value")
            return float(latest_val) if latest_val is not None else None
        except Exception as err:
            log.debug(f"Could not fetch CF Benchmarks price for {symbol} ({idx}): {err}")
            return None

    def get_series_settlement_sources(self, series_ticker: str) -> list[dict]:
        """
        Fetches official settlement sources for a given series ticker (e.g. 'KXBTC15M')
        from Kalshi /series/{series_ticker}. Caches in-memory to prevent duplicate queries.
        """
        if not hasattr(self, "_series_settlement_sources_cache"):
            self._series_settlement_sources_cache = {}
        if series_ticker in self._series_settlement_sources_cache:
            return self._series_settlement_sources_cache[series_ticker]
        try:
            res = self.get(f"/series/{series_ticker}")
            series = res.get("series", {})
            sources = series.get("settlement_sources", [])
            self._series_settlement_sources_cache[series_ticker] = sources
            return sources
        except Exception as err:
            log.debug(f"Could not fetch settlement sources for series {series_ticker}: {err}")
            return []

    # -----------------------------------------------------------------------
    # Order Placement & Execution
    # -----------------------------------------------------------------------

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        yes_price: int | None = None,
        yes_price_dollars: str | None = None,
        no_price: int | None = None,
        no_price_dollars: str | None = None,
        time_in_force: str = "good_till_canceled",
        expiration_ts: int | None = None,
        **kwargs,
    ) -> dict:
        """
        Submits an order to Kalshi using the standardized V2 event model (`/portfolio/events/orders`).

        Args:
            ticker: Market identifier (e.g. 'KXBTC15M-26AUG132245-45')
            side: 'yes' / 'no' or 'bid' / 'ask'
            action: 'buy' or 'sell'
            count: Number of contracts to buy/sell
            yes_price / no_price: Price in cents (1-99)
            yes_price_dollars / no_price_dollars: Price as float string (e.g. '0.50')
            time_in_force: Order expiration rule (defaults to 'good_till_canceled')
            expiration_ts: Unix timestamp (in seconds) when resting order automatically expires on exchange.

        Returns:
            dict: API response payload from `/portfolio/events/orders`.
        """
        # Clean inputs
        side_clean = side.lower()
        action_clean = action.lower()
        now_ms = int(time.time() * 1000)

        # Parse target cent price from passed arguments
        price_cents = None
        if yes_price is not None:
            price_cents = yes_price
        elif yes_price_dollars is not None:
            price_cents = int(round(float(yes_price_dollars) * 100))
        elif no_price is not None:
            price_cents = no_price
        elif no_price_dollars is not None:
            price_cents = int(round(float(no_price_dollars) * 100))

        # Map to V2 Event Order parameters (side: 'bid' / 'ask', price: '0.XX00')
        if side_clean in ("bid", "ask"):
            event_side = side_clean
            price_val = price_cents if price_cents is not None else 50
        elif side_clean == "yes":
            event_side = "bid" if action_clean == "buy" else "ask"
            price_val = price_cents if price_cents is not None else 50
        else:  # side == "no"
            event_side = "ask" if action_clean == "buy" else "bid"
            price_val = (100 - price_cents) if price_cents is not None else 50

        # Clamp price between 1c and 99c
        clamped_cents = max(1, min(99, price_val))

        order_payload = {
            "ticker": ticker,
            "client_order_id": generate_safe_client_order_id(f"{action_clean}_{side_clean}", ticker, now_ms),
            "side": event_side,
            "count": str(count),
            "price": f"0.{clamped_cents:02d}00",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if expiration_ts is not None and time_in_force != "immediate_or_cancel":
            order_payload["expiration_ts"] = int(expiration_ts)
        order_payload.update(kwargs)

        if not self.allow_live_orders:
            log.info(
                f"🛡️ [SAFETY MODE] [Kalshi] Live orders disabled. Simulated {action_clean.upper()} order for {ticker} ({count} contracts @ {clamped_cents}¢)."
            )
            return {
                "order_id": f"sim_k_{now_ms}",
                "client_order_id": order_payload.get("client_order_id"),
                "status": "simulated",
                "ticker": ticker,
                "side": event_side,
                "price": order_payload.get("price"),
                "count": count,
            }

        return self.post("/portfolio/events/orders", payload=order_payload)

    def invalidate_portfolio_caches(self):
        """Invalidates in-memory position and order caches upon trade execution or order cancellation."""
        self._cached_positions_ts = 0.0
        self._cached_resting_orders_ts = 0.0

    def get_all_active_positions(self, force_refresh: bool = False) -> dict[str, dict]:
        """
        Fetches all active open positions in the portfolio in a single batch REST call.
        Caches results for 4.0 seconds to eliminate hundreds of redundant per-ticker calls
        and prevent 429 Too Many Requests rate limit errors on multi-strike scans.
        """
        now = time.time()
        if not force_refresh and (now - self._cached_positions_ts < self._CACHE_VALIDITY_SECONDS):
            return self._cached_positions_map

        try:
            raw_positions = self.get_positions(limit=200).get("market_positions", [])
            positions_map = {}
            for p in raw_positions:
                ticker = p.get("ticker")
                if not ticker:
                    continue
                pos_count = float(p.get("position_fp") or p.get("position") or 0)
                if abs(pos_count) > 0:
                    side = "yes" if pos_count > 0 else "no"
                    count = int(round(abs(pos_count)))
                    entry_p = parse_cents(p.get("market_exposure_dollars"), p.get("market_exposure"))
                    positions_map[ticker] = {
                        "ticker": ticker,
                        "side": side,
                        "count": count,
                        "position_fp": pos_count,
                        "entry_price_cents": entry_p,
                    }
            self._cached_positions_map = positions_map
            self._cached_positions_ts = now
        except Exception as error:
            log.warning(f"Could not batch-fetch open positions: {error}")

        return self._cached_positions_map

    def get_all_resting_orders(self, force_refresh: bool = False) -> dict[str, list[dict]]:
        """
        Fetches all resting orders across all markets in a single batch REST call.
        Caches results for 4.0 seconds to eliminate hundreds of per-market order queries.
        """
        now = time.time()
        if not force_refresh and (now - self._cached_resting_orders_ts < self._CACHE_VALIDITY_SECONDS):
            return self._cached_resting_orders_map

        try:
            raw_orders = self.get_orders(status="resting", limit=200).get("orders", [])
            orders_map = defaultdict(list)
            for o in raw_orders:
                ticker = o.get("ticker")
                if ticker:
                    orders_map[ticker].append(o)
            self._cached_resting_orders_map = dict(orders_map)
            self._cached_resting_orders_ts = now
        except Exception as error:
            log.warning(f"Could not batch-fetch resting orders: {error}")

        return self._cached_resting_orders_map

    def get_market_position(self, ticker: str) -> dict | None:
        """
        Retrieves active position details for `ticker`.
        Checks in-memory paper positions first, then batch-cached positions map.
        """
        pos = self.get_paper_position(ticker)
        if pos is not None:
            return pos
        return self.get_all_active_positions().get(ticker)

    def has_resting_take_profit_order(self, ticker: str) -> bool:
        """Checks if a resting take-profit order already exists for this ticker."""
        orders = self.get_all_resting_orders().get(ticker, [])
        for o in orders:
            client_order_id = o.get("client_order_id") or ""
            if "sell_tp_" in client_order_id:
                return True
        return False

    def place_take_profit_order(
        self,
        ticker: str,
        side: str,
        count: int = 1,
        take_profit_cents: int = 62,
    ) -> dict | None:
        """
        Places a resting take-profit limit order for an existing position:
        - If holding YES: Sell YES by placing Ask at `take_profit_cents` (e.g. 62¢).
        - If holding NO: Sell NO by placing Bid at (100 - `take_profit_cents`) (e.g. 38¢).
        """
        side_clean = side.lower()
        now_ms = int(time.time() * 1000)

        # Safeguard: Take-Profit limit sell must NEVER be placed <= entry price (which would cross the spread / cause instant loss)
        entry_price = self.get_entry_price(ticker)
        if entry_price and take_profit_cents <= entry_price:
            safe_tp = min(99, max(entry_price + 5, 88 if entry_price >= 50 else 62))
            log.warning(
                f"⚠️ [TAKE-PROFIT GUARD] Attempted to place TP at {take_profit_cents}¢ <= entry price {entry_price}¢ "
                f"on {ticker}! Auto-adjusting TP to {safe_tp}¢."
            )
            take_profit_cents = safe_tp

        if side_clean == "yes":
            sell_side = "ask"
            sell_price_str = f"0.{take_profit_cents:02d}00"
        else:
            sell_side = "bid"
            sell_price_str = f"0.{(100 - take_profit_cents):02d}00"

        sell_payload = {
            "ticker": ticker,
            "client_order_id": generate_safe_client_order_id(f"sell_tp_{side_clean}", ticker, now_ms),
            "side": sell_side,
            "count": str(count),
            "price": sell_price_str,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }

        if not self.allow_live_orders:
            log.info(
                f"{LogTag.AUTO_SELL_SET.value} [SAFETY MODE] [Kalshi] Simulated resting Take-Profit order for {side_clean.upper()} "
                f"(exit at {take_profit_cents}¢) on {ticker}"
            )
            return {
                "order_id": f"sim_tp_{now_ms}",
                "client_order_id": sell_payload["client_order_id"],
                "status": "simulated",
                "ticker": ticker,
                "take_profit_cents": take_profit_cents,
            }

        try:
            res = self.post("/portfolio/events/orders", payload=sell_payload)
            self.invalidate_portfolio_caches()
            log.info(
                f"{LogTag.AUTO_SELL_SET.value} Placed resting Take-Profit order for {side_clean.upper()} "
                f"(exit at {take_profit_cents}¢) on {ticker}"
            )
            return res
        except Exception as error:
            log.warning(f"⚠️ Could not place resting auto-sell limit order on {ticker}: {error}")
            return None

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
        Submits a Maker limit Buy order at (Bid + 1¢) with an automatic exchange-side expiration (e.g. 45s).
        If filled immediately, places the resting Take-Profit order right away.
        If resting as an unfilled Maker order, Kalshi automatically cancels the buy order upon timeout.
        Take-Profit is placed upon fill confirmation.
        """
        if is_5min_market(ticker):
            log.error(f"🛑 [5-MINUTE MARKET REJECTED] [Kalshi] Refusing to place order on 5-minute market: {ticker}")
            return {"error": "5min_market_rejected", "ticker": ticker}

        side_clean = side.lower()

        # Passive Maker Buy Pricing (strictly <= ask - 1¢):
        # We place our limit bid at existing Bid + 1¢ (or 1¢ below Ask if no Bid exists).
        # This guarantees the order rests on the order book as a Maker order, earning maker rebates
        # and avoiding aggressive taker fee friction.
        if maker_bid_cents is not None and maker_bid_cents < buy_price_cents:
            effective_buy_cents = min(buy_price_cents - 1, maker_bid_cents + 1)
        elif maker_bid_cents is not None:
            effective_buy_cents = max(1, min(buy_price_cents - 1, maker_bid_cents))
        else:
            effective_buy_cents = max(1, buy_price_cents - 1)

        timeout_tag = f" | Timeout: {buy_order_timeout_seconds}s" if buy_order_timeout_seconds > 0 else ""
        log.info(
            f"{LogTag.TRADE_EXECUTION.value} Market: {ticker} | Side: {side_clean.upper()} | "
            f"Ask: {buy_price_cents}¢ | Maker Limit Bid: {effective_buy_cents}¢{timeout_tag} | Target TP: {take_profit_cents}¢"
        )

        now_sec = int(time.time())
        now_ms = int(time.time() * 1000)

        if side_clean == "yes":
            # Buying YES at effective_buy_cents
            buy_side = "bid"
            buy_price_str = f"0.{effective_buy_cents:02d}00"
        else:
            # Buying NO at effective_buy_cents (asking YES at 100 - effective_buy_cents)
            buy_side = "ask"
            buy_price_str = f"0.{(100 - effective_buy_cents):02d}00"

        # Submit Buy Order via Kalshi V2 Endpoint (/portfolio/events/orders)
        buy_payload = {
            "ticker": ticker,
            "client_order_id": generate_safe_client_order_id(f"buy_{side_clean}", ticker, now_ms),
            "side": buy_side,
            "count": str(count),
            "price": buy_price_str,
            "time_in_force": "good_till_canceled",
            "post_only": True,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if buy_order_timeout_seconds > 0:
            buy_payload["expiration_ts"] = now_sec + buy_order_timeout_seconds

        if not self.allow_live_orders:
            log.info(
                f"🛡️ [SAFETY MODE] [Kalshi] Live orders disabled. Simulated Maker Buy for {ticker} "
                f"({side_clean.upper()} x{count} @ {effective_buy_cents}¢, TP: {take_profit_cents}¢)."
            )
            self.record_paper_position(
                ticker=ticker,
                side=side_clean,
                count=count,
                entry_price_cents=effective_buy_cents,
                target_tp_cents=take_profit_cents,
                strategy_tag="maker_buy",
                entry_ts=now_sec,
            )
            return {
                "buy_order": {"status": "simulated", "client_order_id": buy_payload["client_order_id"]},
                "buy_price_cents": effective_buy_cents,
                "client_order_id": buy_payload["client_order_id"],
                "order_id": f"sim_k_buy_{now_ms}",
                "tp_order_id": f"sim_k_tp_{now_ms}",
                "take_profit_order": {"status": "simulated", "take_profit_cents": take_profit_cents},
            }

        try:
            buy_order_res = self.post("/portfolio/events/orders", payload=buy_payload)
        except requests.HTTPError as err:
            err_text = str(err).lower()
            if "post only cross" in err_text:
                # If post_only crossed, attempt retry 1 tick deeper inside the spread
                if effective_buy_cents - 1 >= 1:
                    retry_price = effective_buy_cents - 1
                    if side_clean == "yes":
                        buy_payload["price"] = f"0.{retry_price:02d}00"
                    else:
                        buy_payload["price"] = f"0.{(100 - retry_price):02d}00"
                    buy_payload["client_order_id"] = generate_safe_client_order_id(f"buy_{side_clean}", ticker, int(time.time() * 1000))
                    try:
                        buy_order_res = self.post("/portfolio/events/orders", payload=buy_payload)
                        effective_buy_cents = retry_price
                        log.info(f"🔄 [POST-ONLY RETRY] Successfully rested buy order 1¢ deeper at {effective_buy_cents}¢ on {ticker}.")
                    except Exception:
                        log.warning(f"⚠️ [POST-ONLY CROSS] {ticker} buy order crossed book (spread moved/tightened). Skipping to prevent taker execution.")
                        return {"error": "post_only_cross", "ticker": ticker}
                else:
                    log.warning(f"⚠️ [POST-ONLY CROSS] {ticker} buy order crossed book (spread moved/tightened). Skipping to prevent taker execution.")
                    return {"error": "post_only_cross", "ticker": ticker}
            else:
                raise

        self.invalidate_portfolio_caches()

        order_id = (
            buy_order_res.get("order", {}).get("order_id")
            or buy_order_res.get("order_id")
            if isinstance(buy_order_res, dict)
            else None
        )
        client_order_id = buy_payload["client_order_id"]

        # Check if filled immediately
        fill_count = float(
            (buy_order_res.get("fill_count") if isinstance(buy_order_res, dict) else 0)
            or (buy_order_res.get("order", {}).get("fill_count_fp") if isinstance(buy_order_res, dict) else 0)
            or (buy_order_res.get("order", {}).get("fill_count") if isinstance(buy_order_res, dict) else 0)
            or 0
        )
        sell_order_res = None

        if fill_count > 0:
            log.info(f"✅ Maker Buy filled immediately for {ticker} ({fill_count} contracts). Placing Take-Profit...")
            sell_order_res = self.place_take_profit_order(
                ticker=ticker,
                side=side_clean,
                count=int(round(fill_count)),
                take_profit_cents=take_profit_cents,
            )
        else:
            log.info(
                f"{LogTag.MAKER_BUY_RESTING.value} Buy order resting at {effective_buy_cents}¢ for {ticker}. "
                f"Take-Profit ({take_profit_cents}¢) will be placed once filled."
            )

        tp_order_id = (
            sell_order_res.get("order", {}).get("order_id")
            or sell_order_res.get("order_id")
            if isinstance(sell_order_res, dict)
            else None
        )

        self.set_entry_price(
            ticker=ticker,
            price_cents=effective_buy_cents,
            side=side_clean,
            count=count,
            client_order_id=client_order_id,
            tp_order_id=tp_order_id,
            take_profit_cents=take_profit_cents,
        )

        return {
            "buy_order": buy_order_res,
            "sell_auto_take_profit_order": sell_order_res,
            "side": side_clean,
            "buy_price_cents": effective_buy_cents,
            "take_profit_cents": take_profit_cents,
            "client_order_id": client_order_id,
            "order_id": order_id,
            "tp_order_id": tp_order_id,
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
        Submits a post_only Maker Limit Bid for a confirmed Favorite (74¢–93¢).
        - Places resting limit bid inside the spread (1¢–2¢ below ask, or 1¢ above bid).
        - Captures the spread discount and avoids taker fee friction (free maker trades on Kalshi crypto).
        - Holds position through to 100¢ ($1.00) settlement payout without premature exit.
        """
        if is_5min_market(ticker):
            log.error(f"🛑 [5-MINUTE MARKET REJECTED] [Kalshi] Refusing to place Favorite Maker order on 5-minute market: {ticker}")
            return {"error": "5min_market_rejected", "ticker": ticker}

        side_clean = side.lower()
        effective_bid = max(1, min(99, maker_bid_cents))
        if target_ask_cents and effective_bid >= target_ask_cents:
            effective_bid = max(1, target_ask_cents - 1)
        spread_saved = max(0, target_ask_cents - effective_bid)

        now_sec = int(time.time())
        now_ms = int(time.time() * 1000)

        if side_clean == "yes":
            buy_side = "bid"
            buy_price_str = f"0.{effective_bid:02d}00"
        else:
            buy_side = "ask"
            buy_price_str = f"0.{(100 - effective_bid):02d}00"

        buy_payload = {
            "ticker": ticker,
            "client_order_id": generate_safe_client_order_id(f"fav_maker_{side_clean}", ticker, now_ms),
            "side": buy_side,
            "count": str(count),
            "price": buy_price_str,
            "time_in_force": "good_till_canceled",
            "post_only": True,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if buy_order_timeout_seconds > 0:
            buy_payload["expiration_ts"] = now_sec + buy_order_timeout_seconds

        log.info(
            f"🎯 [FAVORITE MAKER BID] {ticker} | Side: {side_clean.upper()} | "
            f"Resting Bid: {effective_bid}¢ (Ask: {target_ask_cents}¢, Spread Saved: +{spread_saved}¢) | Count: {count}"
        )

        if not self.allow_live_orders:
            log.info(
                f"🛡️ [SAFETY MODE] [Kalshi] Live orders disabled. Simulated Favorite Maker Buy for {ticker} "
                f"({side_clean.upper()} x{count} @ {effective_bid}¢)."
            )
            self.record_paper_position(
                ticker=ticker,
                side=side_clean,
                count=count,
                entry_price_cents=effective_bid,
                target_tp_cents=100,
                strategy_tag="endgame_favorite_maker",
                entry_ts=now_sec,
            )
            return {
                "buy_order": {"status": "simulated", "client_order_id": buy_payload["client_order_id"]},
                "buy_price_cents": effective_bid,
                "client_order_id": buy_payload["client_order_id"],
                "order_id": f"sim_fav_k_{now_ms}",
            }

        try:
            buy_order_res = self.post("/portfolio/events/orders", payload=buy_payload)
        except requests.HTTPError as err:
            err_text = str(err).lower()
            if "post only cross" in err_text:
                # If post_only crossed because spread narrowed, retry 1 tick deeper
                if effective_bid - 1 >= 1:
                    retry_bid = effective_bid - 1
                    if side_clean == "yes":
                        buy_payload["price"] = f"0.{retry_bid:02d}00"
                    else:
                        buy_payload["price"] = f"0.{(100 - retry_bid):02d}00"
                    buy_payload["client_order_id"] = generate_safe_client_order_id(f"fav_maker_{side_clean}", ticker, int(time.time() * 1000))
                    try:
                        buy_order_res = self.post("/portfolio/events/orders", payload=buy_payload)
                        effective_bid = retry_bid
                        spread_saved = max(0, target_ask_cents - effective_bid)
                        log.info(f"🔄 [POST-ONLY RETRY] Successfully rested maker bid 1¢ deeper at {effective_bid}¢ on {ticker}.")
                    except Exception:
                        log.warning(f"⚠️ [POST-ONLY CROSS] {ticker} resting maker order crossed book (spread moved/tightened). Skipping to prevent taker execution.")
                        return {"error": "post_only_cross", "ticker": ticker}
                else:
                    log.warning(f"⚠️ [POST-ONLY CROSS] {ticker} resting maker order crossed book (spread moved/tightened). Skipping to prevent taker execution.")
                    return {"error": "post_only_cross", "ticker": ticker}
            else:
                raise

        self.invalidate_portfolio_caches()

        order_id = (
            buy_order_res.get("order", {}).get("order_id")
            or buy_order_res.get("order_id")
            if isinstance(buy_order_res, dict)
            else None
        )
        client_order_id = buy_payload["client_order_id"]

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

        return {
            "buy_order": buy_order_res,
            "side": side_clean,
            "buy_price_cents": effective_bid,
            "target_ask_cents": target_ask_cents,
            "spread_saved_cents": spread_saved,
            "client_order_id": client_order_id,
            "order_id": order_id,
            "strategy_tag": "endgame_favorite_maker",
        }

    def get_crypto_directional_counts(self) -> dict[str, int]:
        """
        Counts active open positions on crypto assets categorized by direction ('yes' vs 'no').
        Used to enforce cross-asset portfolio correlation exposure caps (Reddit Comment p3ok6ft).
        """
        positions_map = self.get_all_active_positions()
        counts = {"yes": 0, "no": 0}
        crypto_prefixes = (
            "KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB",
            "KXHYPE", "KXADA", "KXTON", "KXBCH", "KXNEAR", "KXZEC",
        )
        for ticker, pos in positions_map.items():
            if any(ticker.startswith(p) for p in crypto_prefixes):
                side = (pos.get("side") or "").lower()
                if side in counts:
                    counts[side] += 1
        return counts

    def check_crypto_portfolio_directional_cap(self, side: str, max_concurrent: int = 3) -> bool:
        """
        Enforces a cross-asset directional portfolio cap (Comment p3ok6ft).
        Since top crypto assets settle co-directionally 74%–84% of the time, entering more than
        max_concurrent positions in the same direction compounds downside risk.
        Returns True if allowed to enter, False if cap is reached.
        """
        side_clean = side.lower()
        counts = self.get_crypto_directional_counts()
        current_in_dir = counts.get(side_clean, 0)
        if current_in_dir >= max_concurrent:
            log.info(
                f"🛡️ [CORRELATION RISK CAP] Active crypto {side_clean.upper()} positions ({current_in_dir}) "
                f">= max cap ({max_concurrent}). Skipping new {side_clean.upper()} entry to prevent co-directional drawdown."
            )
            return False
        return True

    def cancel_order(self, order_id: str) -> dict:
        """Cancels an existing order by its unique order ID using Kalshi V2 endpoint."""
        if not self.allow_live_orders:
            return {"status": "simulated_canceled", "order_id": str(order_id)}
        return self.delete(f"/portfolio/events/orders/{order_id}", allow_404=True)

    def cancel_order_safe(self, order_id: str) -> bool:
        """
        Safely attempts to cancel an order. Returns True if canceled or already filled/closed (404),
        or False if unexpected error occurs.
        """
        if not self.allow_live_orders:
            return True
        try:
            res = self.cancel_order(order_id)
            if isinstance(res, dict) and res.get("status") == "not_found":
                log.debug(f"Order {order_id} already closed/filled on Kalshi (not found).")
                return True
            return True
        except requests.HTTPError as error:
            if error.response is not None and 400 <= error.response.status_code < 500:
                code, msg = parse_kalshi_error(error.response)
                if code == "not_found" or error.response.status_code == 404:
                    log.debug(f"Order {order_id} already closed/filled (not_found).")
                    return True
                log.warning(f"Could not cancel order {order_id}: {code or msg}")
                return False
            raise

    def cancel_and_verify_resting_orders(
        self,
        ticker: str,
        max_retries: int = 5,
        poll_interval: float = 0.2,
    ) -> bool:
        """
        Cancels ALL resting limit orders (including resting Take-Profit orders) for a given market ticker,
        and polls the API to verify that all resting orders are confirmed cleared.

        Why this is critical:
        On Kalshi, active resting limit orders (such as Take-Profit orders) lock contract positions.
        Attempting to liquidate a position while a resting order is still active causes Kalshi
        to reject the sell order with an `insufficient_position` / `position_locked` error.

        Args:
            ticker: Market identifier
            max_retries: Number of verification polling attempts
            poll_interval: Delay in seconds between polling attempts

        Returns:
            bool: True if all resting orders were successfully canceled and confirmed cleared,
                  False if resting orders remain after retries.
        """
        if not self.allow_live_orders:
            return True
        for attempt in range(1, max_retries + 1):
            try:
                orders_resp = self.get_orders(ticker=ticker, status="resting")
                resting_orders = orders_resp.get("orders") or []
            except Exception as error:
                log.warning(f"⚠️ Attempt {attempt}/{max_retries}: Failed to fetch resting orders for {ticker}: {error}")
                resting_orders = []

            if not resting_orders:
                return True

            log.info(
                f"🧹 Attempt {attempt}/{max_retries}: Canceling {len(resting_orders)} resting order(s) for {ticker}..."
            )
            for order in resting_orders:
                order_id = order.get("order_id")
                if order_id:
                    self.cancel_order_safe(order_id)

            # Invalidate caches so next query gets fresh data from Kalshi
            self.invalidate_portfolio_caches()

            # Pause briefly to allow Kalshi orderbook state engine to process cancellations
            time.sleep(poll_interval)

        # Final check after retry loop
        try:
            final_check = self.get_orders(ticker=ticker, status="resting")
            remaining = final_check.get("orders") or []
            if not remaining:
                return True
            log.error(f"❌ Could not clear all resting orders for {ticker}; {len(remaining)} order(s) still resting.")
            return False
        except Exception as error:
            log.warning(f"⚠️ Final resting order verification failed for {ticker}: {error}")
            return False

    def cancel_unfilled_buy_orders(self, ticker: str) -> int:
        """
        Cancels any resting unfilled BUY orders for `ticker` from prior intervals.
        Keeps resting Take-Profit SELL orders untouched!
        """
        canceled_count = 0
        try:
            orders = self.get_all_resting_orders(force_refresh=True).get(ticker, [])
            for o in orders:
                client_order_id = o.get("client_order_id") or ""
                action = (o.get("action") or "").lower()
                order_id = o.get("order_id")

                # Identify buy orders (client_order_id starts with 'buy_', 'fav_maker_', 'rebuy_' or action is 'buy' without 'sell_tp_')
                is_buy_order = (
                    client_order_id.startswith(("buy_", "fav_maker_", "rebuy_"))
                    or (action == "buy" and not client_order_id.startswith("sell_tp_"))
                )
                if is_buy_order and order_id:
                    if self.cancel_order_safe(order_id):
                        canceled_count += 1
                        log.info(f"{LogTag.PRUNE_STALE_BUY.value} Canceled stale unfilled buy order {order_id} for {ticker}")
            if canceled_count > 0:
                self.invalidate_portfolio_caches()
        except Exception as error:
            log.warning(f"⚠️ Could not check/prune resting buy orders for {ticker}: {error}")

        return canceled_count

    def cancel_all_stale_resting_buy_orders(self, max_age_seconds: int = 45) -> int:
        """
        Sweeps all resting orders across all markets in the account and cancels any stale unfilled BUY orders.
        Preserves resting Take-Profit SELL orders.
        """
        if not self.allow_live_orders:
            return 0
        total_canceled = 0
        try:
            all_resting = self.get_all_resting_orders(force_refresh=True)
            for ticker, orders in all_resting.items():
                for o in orders:
                    client_order_id = o.get("client_order_id") or ""
                    action = (o.get("action") or "").lower()
                    order_id = o.get("order_id")

                    is_buy_order = (
                        client_order_id.startswith(("buy_", "fav_maker_", "rebuy_"))
                        or (action == "buy" and not client_order_id.startswith("sell_tp_"))
                    )
                    if is_buy_order and order_id:
                        if self.cancel_order_safe(order_id):
                            total_canceled += 1
                            log.info(f"{LogTag.PRUNE_STALE_BUY.value} [Kalshi] Swept stale unfilled buy order {order_id} for {ticker}")
            if total_canceled > 0:
                self.invalidate_portfolio_caches()
        except Exception as error:
            log.warning(f"⚠️ Could not sweep resting buy orders on Kalshi: {error}")
        return total_canceled

    def has_existing_position_or_order(self, ticker: str) -> bool:
        """
        Checks if we ALREADY hold an active position or resting order for the given market ticker.
        Uses batch-cached position and order maps to eliminate 429 rate limits.
        """
        # Step 1: Check active market positions
        pos = self.get_market_position(ticker)
        if pos is not None:
            log.info(
                f"{LogTag.POSITION_EXISTS.value} Found active {pos['side'].upper()} "
                f"position of {pos['count']} contract(s) in {ticker}"
            )
            return True

        # Step 2: Check resting orders
        orders = self.get_all_resting_orders().get(ticker, [])
        if len(orders) > 0:
            log.info(f"{LogTag.ORDER_EXISTS.value} Found {len(orders)} resting order(s) for {ticker}")
            return True

        # Step 3: Check memory-tracked traded markets in this lifecycle
        traded_side = self.get_traded_market_side(ticker)
        if traded_side is not None:
            log.info(f"{LogTag.POSITION_EXISTS.value} Ticker {ticker} already traded ({traded_side.upper()}) in current lifecycle.")
            return True

        # Step 4: Check persistent database active position store
        try:
            from database import get_active_position_doc
            db_pos = get_active_position_doc(ticker, "kalshi")
            if db_pos is not None and (db_pos.get("entry_price_cents") or 0) > 0:
                log.info(f"{LogTag.POSITION_EXISTS.value} Found persisted active position in DB for {ticker} (side: {db_pos.get('side', '').upper()})")
                return True
        except Exception:
            pass

        return False

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        """
        Executes an immediate True Taker market liquidation sell order on Kalshi using IOC (Immediate-or-Cancel).

        High School Explanation:
        -------------------------
        When our stop-loss or profit-floor triggers, we want to SELL RIGHT NOW so we don't
        risk losing 100% of our money when the 15-minute / hourly contract ends.

        To guarantee an immediate fill against the best available buyer (taker execution):
        1. When selling YES contracts:
           - We offer to sell at 1¢ ($0.01) with immediate_or_cancel.
           - Kalshi automatically matches our order with the highest existing buyer (price improvement).
        2. When selling NO contracts:
           - In Kalshi's event order model, selling NO is done by placing a bid on YES up to 99¢ ($0.99) IOC.
           - Kalshi matches this against the best available NO buyer immediately!

        CRITICAL RISK CONTROL:
        We NEVER fall back to 'good_till_canceled' (GTC) at 99¢/1¢. A resting 99¢ GTC bid to close NO
        leaves the account exposed to buying YES at maximum price if liquidity vanishes.
        Instead, any retry dynamically queries top-of-book and retries with fresh IOC orders.
        """
        now_ms = int(time.time() * 1000)
        side_clean = side.lower()

        if not self.allow_live_orders:
            log.info(f"🛡️ [SAFETY MODE] [Kalshi] Simulated liquidation for {ticker} ({side_clean.upper()} x{count} @ {current_price}¢).")
            self.remove_paper_position(ticker)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            self.clear_strategy_tag(ticker)
            return {"status": "simulated_liquidated", "ticker": ticker, "price": current_price, "count": count}

        if side_clean == "yes":
            # Selling YES -> Event Ask on YES at current_price (or 1¢ floor)
            sell_price_cents = max(1, min(99, current_price))
            sell_payload = {
                "ticker": ticker,
                "client_order_id": generate_safe_client_order_id("liq_yes", ticker, now_ms),
                "side": "ask",
                "count": str(count),
                "price": f"0.{sell_price_cents:02d}00",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
            }
        else:
            # Selling NO -> Event Bid on YES at (100 - current_no_price).
            # For example: if NO is at 40¢, we Bid on YES at 60¢ (100 - 40).
            # This guarantees we sell NO for at least 40¢ and prevents buying YES at 99¢!
            no_sell_cents = max(1, min(99, current_price))
            yes_equiv_price = 100 - no_sell_cents
            sell_payload = {
                "ticker": ticker,
                "client_order_id": generate_safe_client_order_id("liq_no", ticker, now_ms),
                "side": "bid",
                "count": str(count),
                "price": f"0.{yes_equiv_price:02d}00",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
            }

        try:
            res = self.post("/portfolio/events/orders", payload=sell_payload)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            self.invalidate_portfolio_caches()
            try:
                from database import remove_active_position_cost
                remove_active_position_cost(ticker, "kalshi")
            except Exception:
                pass
            log.info(f"✅ Executed immediate taker liquidation for {ticker} ({side_clean.upper()} x{count}): {res}")
            return res
        except Exception as error:
            log.warning(f"⚠️ Primary IOC taker liquidation attempt for {ticker} had response/error: {error}. Re-attempting with fresh top-of-book quote...")
            try:
                mkt_resp = self.get_market(ticker)
                mkt_detail = mkt_resp.get("market", {}) if isinstance(mkt_resp, dict) else {}
                retry_ms = int(time.time() * 1000)

                if side_clean == "yes":
                    top_bid = parse_cents(mkt_detail.get("yes_bid_dollars"), mkt_detail.get("yes_bid")) or 1
                    sell_payload["price"] = f"0.{max(1, min(99, top_bid)):02d}00"
                    sell_payload["client_order_id"] = generate_safe_client_order_id("liq_retry_yes", ticker, retry_ms)
                else:
                    top_no_bid = parse_cents(mkt_detail.get("no_bid_dollars"), mkt_detail.get("no_bid")) or 1
                    yes_equiv = 100 - max(1, min(99, top_no_bid))
                    sell_payload["price"] = f"0.{yes_equiv:02d}00"
                    sell_payload["client_order_id"] = generate_safe_client_order_id("liq_retry_no", ticker, retry_ms)

                sell_payload["time_in_force"] = "immediate_or_cancel"
                res = self.post("/portfolio/events/orders", payload=sell_payload)
                self.clear_profit_floor(ticker)
                self.clear_entry_price(ticker)
                self.invalidate_portfolio_caches()
                try:
                    from database import remove_active_position_cost
                    remove_active_position_cost(ticker, "kalshi")
                except Exception:
                    pass
                log.info(f"✅ Executed secondary IOC taker liquidation for {ticker}: {res}")
                return res
            except Exception as err2:
                log.error(f"❌ Failed secondary IOC liquidation exit order for {ticker}: {err2}. Zero resting GTC orders posted.")
                return {}

    def execute_buy(
        self,
        ticker: str,
        side: str,
        count: int = 1,
        price_cents: int = 70,
        buy_order_timeout_seconds: int = DEFAULT_BUY_ORDER_TIMEOUT_SECONDS,
    ) -> dict:
        """
        Submits an immediate limit/taker buy order to Kalshi with automatic expiration timeout:
        - When buying YES: Places a bid on YES at `price_cents` (e.g. 73¢)
        - When buying NO: Places an ask on YES at `100 - price_cents` (e.g. 27¢)
        """
        if is_5min_market(ticker):
            log.error(f"🛑 [5-MINUTE MARKET REJECTED] [Kalshi] Refusing to place buy order on 5-minute market: {ticker}")
            return {"error": "5min_market_rejected", "ticker": ticker}

        side_clean = side.lower()
        now_sec = int(time.time())
        now_ms = int(time.time() * 1000)
        clamped_cents = max(1, min(99, price_cents))

        if not self.allow_live_orders:
            log.info(f"🛡️ [SAFETY MODE] [Kalshi] Simulated buy for {ticker} ({side_clean.upper()} x{count} @ {clamped_cents}¢).")
            self.record_paper_position(ticker, side_clean, count, clamped_cents)
            return {"status": "simulated_bought", "ticker": ticker, "price": clamped_cents, "count": count}

        if side_clean == "yes":
            event_side = "bid"
            price_str = f"0.{clamped_cents:02d}00"
        else:
            event_side = "ask"
            price_str = f"0.{(100 - clamped_cents):02d}00"

        buy_payload = {
            "ticker": ticker,
            "client_order_id": generate_safe_client_order_id(f"rebuy_{side_clean}", ticker, now_ms),
            "side": event_side,
            "count": str(count),
            "price": price_str,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }
        if buy_order_timeout_seconds > 0:
            buy_payload["expiration_ts"] = now_sec + buy_order_timeout_seconds

        res = self.post("/portfolio/events/orders", payload=buy_payload)
        self.invalidate_portfolio_caches()
        self.set_entry_price(ticker, clamped_cents)
        return res
