"""
Kalshi API Client & Multi-Timeframe Crypto Execution Engine
============================================================

What is Kalshi?
---------------
Kalshi is a CFTC-regulated prediction market exchange offering binary event contracts
across 15-Minute and Hourly directional crypto markets.
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

from signals.base_client import BaseTradingClient, LogTag

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
# 15-Minute Crypto Series Tickers
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

# ---------------------------------------------------------------------------
# Hourly Crypto Series Tickers (Directional / Above-Below)
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
    ):
        """
        Initializes the Kalshi API client.

        Args:
            api_key_id: Your Kalshi API key ID (UUID format).
            private_key: Loaded RSA private key object (optional; loaded automatically if None).
            base_url: Overriding API base URL (optional; defaults to Demo or Production).
        """
        super().__init__()
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
            log.error(f"Kalshi POST {endpoint} failed with {err_details}")
            raise requests.HTTPError(err_details, response=response) from e

        if response.text:
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                log.debug(f"Non-JSON response from Kalshi POST {endpoint}: {response.text[:200]}")
        return {}

    def delete(self, endpoint: str) -> dict:
        """Sends an authenticated HTTP DELETE request to Kalshi using connection pooling."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint)
        headers = self._build_auth_headers("DELETE", signing_path)

        response = self.session.delete(full_url, headers=headers, timeout=30)
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            err_details = f"{e} | Response body: {response.text}"
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
            result = s.get("market_result")
            settled_time = s.get("settled_time")

            # Revenue / payout
            rev_dollars = s.get("revenue_dollars")
            rev_cents = s.get("revenue")
            revenue = (
                float(rev_dollars)
                if rev_dollars is not None
                else ((rev_cents / 100.0) if rev_cents is not None else 0.0)
            )

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
                "yes_count": s.get("yes_count", 0),
                "no_count": s.get("no_count", 0),
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
    # Market Data & 15-Minute Crypto Retrieval
    # -----------------------------------------------------------------------

    def get_markets(self, **params) -> dict:
        """Queries markets from Kalshi (supports filtering by series_ticker, status, limit)."""
        return self.get("/markets", params)

    def get_15m_crypto_markets(self, status: str = "open", limit: int = 100) -> list[dict]:
        """
        Retrieves active 15-minute crypto prediction markets across all 12 crypto series:
        BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ADA, TON, BCH, NEAR, and ZEC.

        Returns:
            list[dict]: A list of market dictionaries matching active 15m crypto contracts.
        """
        all_15m_markets = []

        for series_ticker in SERIES_15M_CRYPTO:
            try:
                response = self.get_markets(
                    series_ticker=series_ticker, status=status, limit=limit
                )
                markets = response.get("markets", [])
                all_15m_markets.extend(markets)
            except Exception as error:
                log.warning(
                    f"Could not fetch 15m markets for series '{series_ticker}': {error}"
                )

        return all_15m_markets

    def get_hourly_crypto_markets(
        self,
        status: str = "open",
        limit: int = 100,
        current_hour_only: bool = True,
        now_utc: datetime | None = None,
    ) -> list[dict]:
        """
        Retrieves active hourly multi-strike crypto prediction markets across all supported crypto series:
        BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, TON, NEAR, and ZEC.

        Args:
            status: Market status ('open', 'closed', 'settled')
            limit: Pagination limit per series
            current_hour_only: If True, filters only for contracts closing in the current hour (<= 75 mins remaining)
            now_utc: Reference UTC time for expiration filtering

        Returns:
            list[dict]: List of market dictionaries matching active hourly crypto contracts.
        """
        all_hourly_markets = []
        ref_time = now_utc or datetime.now(timezone.utc)

        for series_ticker in SERIES_HOURLY_CRYPTO:
            try:
                response = self.get_markets(
                    series_ticker=series_ticker, status=status, limit=limit
                )
                markets = response.get("markets", [])
                for m in markets:
                    if current_hour_only:
                        # Ensure market expires within the active current hourly window (0 to 65 minutes ahead)
                        close_iso = (
                            m.get("close_time")
                            or m.get("expiration_time")
                            or m.get("expected_expiration_time")
                            or m.get("latest_expiration_time")
                        )
                        close_dt = None
                        if close_iso:
                            try:
                                close_dt = datetime.fromisoformat(str(close_iso).replace("Z", "+00:00"))
                            except Exception:
                                pass

                        # Fallback: Parse expiration from ticker (e.g. KXBTCD-26AUG1616 -> 2026-08-16 16:00 UTC)
                        if close_dt is None and (tick := m.get("ticker")):
                            m_match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})", tick)
                            if m_match:
                                yy, mmm, dd, hh = m_match.groups()
                                try:
                                    dt_str = f"20{yy}-{mmm}-{dd} {hh}:00:00"
                                    close_dt = datetime.strptime(dt_str, "%Y-%b-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                                except Exception:
                                    pass

                        if close_dt is not None:
                            remaining_minutes = (close_dt - ref_time).total_seconds() / 60.0
                            # Current hour contract must be active and closing between 0 and 65 minutes in the future
                            if remaining_minutes <= 0.0 or remaining_minutes > 65.0:
                                continue
                        else:
                            # Skip if expiration is unverified
                            continue

                    all_hourly_markets.append(m)
            except Exception as error:
                log.warning(
                    f"Could not fetch hourly markets for series '{series_ticker}': {error}"
                )

        return all_hourly_markets

    def get_market(self, ticker: str) -> dict:
        """Fetches detailed information for a single market ticker."""
        return self.get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        """Fetches current orderbook (bids and asks) for a given market ticker."""
        return self.get(f"/markets/{ticker}/orderbook")

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
        if not force_refresh and (now - self._cached_positions_ts) < self._CACHE_VALIDITY_SECONDS:
            return self._cached_positions_map

        positions_map: dict[str, dict] = {}
        try:
            pos_resp = self.get_positions()
            positions = pos_resp.get("market_positions") or []
            for p in positions:
                ticker = p.get("ticker")
                if not ticker:
                    continue
                fp = float(p.get("position_fp") or p.get("position") or 0)
                if abs(fp) > 0:
                    side = "yes" if fp > 0 else "no"
                    count = int(round(abs(fp)))
                    exposure = float(p.get("market_exposure_dollars") or 0)
                    entry_price = int(round((exposure / count) * 100)) if (count > 0 and exposure > 0) else None
                    positions_map[ticker] = {
                        "ticker": ticker,
                        "side": side,
                        "count": count,
                        "position_fp": fp,
                        "entry_price_cents": entry_price,
                        "exposure_dollars": exposure,
                    }
            self._cached_positions_map = positions_map
            self._cached_positions_ts = now
        except Exception as error:
            log.warning(f"Could not batch-fetch active market positions: {error}")

        return self._cached_positions_map

    def get_all_resting_orders(self, force_refresh: bool = False) -> dict[str, list[dict]]:
        """
        Fetches all resting orders across all markets in the portfolio in a single batch REST call.
        Caches results for 4.0 seconds to prevent 429 Too Many Requests rate limit errors.
        """
        now = time.time()
        if not force_refresh and (now - self._cached_resting_orders_ts) < self._CACHE_VALIDITY_SECONDS:
            return self._cached_resting_orders_map

        orders_map: dict[str, list[dict]] = defaultdict(list)
        try:
            orders_resp = self.get_orders(status="resting")
            orders = orders_resp.get("orders") or []
            for o in orders:
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
        Uses batch-cached positions map to prevent 429 rate limit errors on multi-strike scans.
        """
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
        side_clean = side.lower()

        # Taker Buy Execution Buffer (ask + 1¢):
        # Adding +1¢ buffer to current ask ensures immediate taker fill even if ask ticks up by 1¢ during network latency.
        if maker_bid_cents is not None:
            effective_buy_cents = min(buy_price_cents, maker_bid_cents + 1)
        else:
            effective_buy_cents = min(99, buy_price_cents + 1)

        timeout_tag = f" | Timeout: {buy_order_timeout_seconds}s" if buy_order_timeout_seconds > 0 else ""
        log.info(
            f"{LogTag.TRADE_EXECUTION.value} Market: {ticker} | Side: {side_clean.upper()} | "
            f"Ask: {buy_price_cents}¢ | Order Limit (Ask + 1¢): {effective_buy_cents}¢{timeout_tag} | Target TP: {take_profit_cents}¢"
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
            "self_trade_prevention_type": "taker_at_cross",
        }
        if buy_order_timeout_seconds > 0:
            buy_payload["expiration_ts"] = now_sec + buy_order_timeout_seconds

        buy_order_res = self.post("/portfolio/events/orders", payload=buy_payload)
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

        # Record the exact price we paid into our bot's memory and MongoDB
        self.set_entry_price(
            ticker=ticker,
            price_cents=effective_buy_cents,
            side=side_clean,
            count=count,
            client_order_id=client_order_id,
            tp_order_id=tp_order_id,
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

    def cancel_order(self, order_id: str) -> dict:
        """Cancels an existing order by its unique order ID using Kalshi V2 endpoint."""
        return self.delete(f"/portfolio/events/orders/{order_id}")

    def cancel_order_safe(self, order_id: str) -> bool:
        """
        Safely attempts to cancel an order. Returns True if canceled,
        or False if order is already filled/closed (without throwing an error).
        """
        try:
            self.cancel_order(order_id)
            return True
        except requests.HTTPError as error:
            if error.response is not None and 400 <= error.response.status_code < 500:
                code, msg = parse_kalshi_error(error.response)
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
        for attempt in range(1, max_retries + 1):
            try:
                orders_resp = self.get_orders(ticker=ticker, status="resting")
                resting_orders = orders_resp.get("orders") or []
            except Exception as error:
                log.warning(f"⚠️ Attempt {attempt}/{max_retries}: Failed to fetch resting orders for {ticker}: {error}")
                resting_orders = []

            if not resting_orders:
                log.info(f"✅ Confirmed 0 resting orders for {ticker}.")
                return True

            log.info(
                f"🧹 Attempt {attempt}/{max_retries}: Canceling {len(resting_orders)} resting order(s) for {ticker}..."
            )
            for order in resting_orders:
                order_id = order.get("order_id")
                if order_id:
                    self.cancel_order_safe(order_id)

            # Pause briefly to allow Kalshi orderbook state engine to process cancellations
            time.sleep(poll_interval)

        # Final check after retry loop
        try:
            final_check = self.get_orders(ticker=ticker, status="resting")
            remaining = final_check.get("orders") or []
            if not remaining:
                log.info(f"✅ Confirmed 0 resting orders for {ticker} after final check.")
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
            orders = self.get_all_resting_orders().get(ticker, [])
            for o in orders:
                client_order_id = o.get("client_order_id") or ""
                action = (o.get("action") or "").lower()
                order_id = o.get("order_id")

                # Identify buy orders (client_order_id starts with 'buy_' or action is 'buy' without 'sell_tp_')
                is_buy_order = (
                    client_order_id.startswith("buy_")
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

        if side_clean == "yes":
            sell_payload = {
                "ticker": ticker,
                "client_order_id": generate_safe_client_order_id("liq_yes", ticker, now_ms),
                "side": "ask",
                "count": str(count),
                "price": "0.0100",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
            }
        else:
            sell_payload = {
                "ticker": ticker,
                "client_order_id": generate_safe_client_order_id("liq_no", ticker, now_ms),
                "side": "bid",
                "count": str(count),
                "price": "0.9900",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
            }

        try:
            res = self.post("/portfolio/events/orders", payload=sell_payload)
            self.invalidate_portfolio_caches()
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
                    sell_payload["price"] = f"0.{max(1, top_bid):02d}00"
                    sell_payload["client_order_id"] = generate_safe_client_order_id("liq_retry_yes", ticker, retry_ms)
                else:
                    top_no_bid = parse_cents(mkt_detail.get("no_bid_dollars"), mkt_detail.get("no_bid")) or 1
                    yes_equiv = min(99, max(1, 100 - top_no_bid))
                    sell_payload["price"] = f"0.{yes_equiv:02d}00"
                    sell_payload["client_order_id"] = generate_safe_client_order_id("liq_retry_no", ticker, retry_ms)

                sell_payload["time_in_force"] = "immediate_or_cancel"
                res = self.post("/portfolio/events/orders", payload=sell_payload)
                self.invalidate_portfolio_caches()
                log.info(f"✅ Executed secondary IOC taker liquidation for {ticker}: {res}")
                return res
            except Exception as err2:
                log.error(f"❌ Failed secondary IOC liquidation exit order for {ticker}: {err2}. Zero resting GTC orders posted.")
                return {}

    def execute_buy(self, ticker: str, side: str, count: int = 1, price_cents: int = 70) -> dict:
        """
        Submits an immediate limit/taker buy order to Kalshi:
        - When buying YES: Places a bid on YES at `price_cents` (e.g. 73¢)
        - When buying NO: Places an ask on YES at `100 - price_cents` (e.g. 27¢)
        """
        side_clean = side.lower()
        now_ms = int(time.time() * 1000)
        clamped_cents = max(1, min(99, price_cents))

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
        res = self.post("/portfolio/events/orders", payload=buy_payload)
        self.invalidate_portfolio_caches()
        self.set_entry_price(ticker, clamped_cents)
        return res
