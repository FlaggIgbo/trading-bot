"""
Kalshi API Client & 15-Minute Crypto Market Data Retriever
===========================================================

What is Kalshi?
---------------
Kalshi is a regulated prediction market exchange where users buy and sell contracts 
based on the outcome of future events (e.g., "Will Bitcoin price increase in the next 15 minutes?").
Contracts settle at either $1.00 (100 cents) if YES occurs, or $0.00 if NO occurs.

Authentication Explained:
-------------------------
Kalshi uses RSA-PSS digital signatures for API security rather than traditional password tokens:
1. Every request header includes a millisecond timestamp, API key ID, and an RSA signature.
2. The signature is created by signing the concatenated string `timestamp + HTTP_METHOD + path`
   using an RSA private key file (`.key`).
3. Kalshi verifies this signature using your public key on file.

Price Structure:
----------------
- Kalshi prices are expressed as cents (1¢ to 99¢) or dollar floats ($0.01 to $0.99).
- 1 contract cost = contract price (e.g., 50¢).
- Max payout = $1.00 per contract.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from enum import Enum

# Load configuration from the project's .env file if available
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Log Tag Enum Definitions (Human-Readable Info Brackets)
# ---------------------------------------------------------------------------
class LogTag(str, Enum):
    """
    Log Tag Enums representing info log event categories with code comments:
    """
    CLOCK_ALIGNMENT = "[CLOCK ALIGNMENT] ⏱️"          # Sleep timer waiting for 30s past next 5-min mark
    BUY_MATCH = "⚡ [STRATEGY MATCH]"                 # Dynamic buy strategy criteria met (price range + spot momentum)
    DRY_RUN_MATCH = "🎯 [DRY-RUN MATCH]"              # Preview buy match in dry-run mode
    OPEN_POSITION_GATE = "🛡️ [OPEN POSITION GATE]"     # Skipping buy order because open position or order exists
    STOP_LOSS_EVALUATION = "📊 [STOP-LOSS EVALUATION]"    # Evaluating 10m stop-loss condition
    STOP_LOSS_TRIGGERED = "🛑 [STOP-LOSS TRIGGERED]"      # Liquidating position immediately (loss >= 10¢ or price <= 30¢)
    DRY_RUN_STOP_LOSS = "🛑 [DRY-RUN STOP-LOSS]"        # Preview stop-loss liquidation in dry-run mode
    LET_IT_RUN = "🚀 [LET IT RUN TO 100¢]"             # Canceling 82¢ TP at 10m for strong winners (>= 70¢) to get 100¢
    TRADE_EXECUTION = "🎯 [TRADE EXECUTION]"          # Submitting Maker buy order to Kalshi V2 API
    AUTO_SELL_SET = "💰 [AUTO-SELL SET]"              # Placing resting 82¢ limit sell order for take-profit
    POSITION_EXISTS = "🛡️ [POSITION EXISTS]"          # Active market position found in Kalshi portfolio
    ORDER_EXISTS = "🛡️ [ORDER EXISTS]"                # Resting order found in Kalshi portfolio
    PRUNE_STALE_BUY = "🧹 [PRUNED STALE BUY]"         # Canceled unfilled resting buy order from prior pass

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
    # Clamp price between 1 cent ($0.01) and 99 cents ($0.99)
    clamped_price = max(0.01, min(0.99, price_val))
    # Round to 2 decimal places (1 cent ticks)
    rounded_cents = round(clamped_price * 100) / 100
    return f"{rounded_cents:.2f}"


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

class KalshiClient:
    """
    Authenticated Client for interacting with the Kalshi Exchange REST API.
    Provides methods to query balances, fetch 15-minute crypto markets, and place orders.
    """

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
        """Sends an authenticated HTTP GET request to Kalshi."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint, params)
        headers = self._build_auth_headers("GET", signing_path)

        response = requests.get(full_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json() if response.text else {}

    def post(self, endpoint: str, payload: dict | None = None) -> dict:
        """Sends an authenticated HTTP POST request to Kalshi."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint)
        headers = self._build_auth_headers("POST", signing_path)

        response = requests.post(full_url, headers=headers, json=payload or {}, timeout=30)
        response.raise_for_status()
        return response.json() if response.text else {}

    def delete(self, endpoint: str) -> dict:
        """Sends an authenticated HTTP DELETE request to Kalshi."""
        signing_path = self._get_signing_path(endpoint)
        full_url = self._build_full_url(endpoint)
        headers = self._build_auth_headers("DELETE", signing_path)

        response = requests.delete(full_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json() if response.text else {}

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

    # -----------------------------------------------------------------------
    # Market Data & 15-Minute Crypto Retrieval
    # -----------------------------------------------------------------------

    def get_markets(self, **params) -> dict:
        """Queries markets from Kalshi (supports filtering by series_ticker, status, limit)."""
        return self.get("/markets", params)

    def get_15m_crypto_markets(self, status: str = "open", limit: int = 100) -> list[dict]:
        """
        Retrieves active 15-minute crypto prediction markets across all 7 crypto series:
        BTC, ETH, SOL, XRP, DOGE, BNB, and HYPE.
        
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
        **kwargs,
    ) -> dict:
        """
        Submits an order to Kalshi.
        
        Args:
            ticker: Market identifier (e.g. 'KXBTC15M-26AUG132245-45')
            side: 'yes' or 'no'
            action: 'buy' or 'sell'
            count: Number of contracts to buy/sell
            yes_price / no_price: Price in cents (1-99)
            yes_price_dollars / no_price_dollars: Price as float string (e.g. '0.50')
        """
        order_payload = {
            "ticker": ticker,
            "side": side.lower(),
            "action": action.lower(),
            "count": count,
            "time_in_force": time_in_force,
        }

        if yes_price is not None:
            order_payload["yes_price"] = yes_price
        elif yes_price_dollars is not None:
            order_payload["yes_price_dollars"] = yes_price_dollars

        if no_price is not None:
            order_payload["no_price"] = no_price
        elif no_price_dollars is not None:
            order_payload["no_price_dollars"] = no_price_dollars

        order_payload.update(kwargs)
        return self.post("/portfolio/orders", payload=order_payload)

    def buy_and_set_take_profit(
        self,
        ticker: str,
        side: str,
        buy_price_cents: int,
        take_profit_cents: int = 82,
        count: int = 1,
        maker_bid_cents: int | None = None,
    ) -> dict:
        """
        Executes a 2-step trade strategy for auto-selling using Kalshi V2 endpoints:
        1. Submits a Maker limit Buy order at (Bid + 1¢) to save 1¢-3¢ entry cost.
        2. Immediately places a resting Sell limit order at `take_profit_cents` (82¢)
           so Kalshi automatically takes profit on price spikes.
        """
        side_clean = side.lower()

        # EV Rule 1: Target Maker limit price at (Bid + 1c) to save 1c-3c on entry cost
        if maker_bid_cents is not None:
            effective_buy_cents = min(buy_price_cents, maker_bid_cents + 1)
        else:
            effective_buy_cents = buy_price_cents

        log.info(
            f"{LogTag.TRADE_EXECUTION.value} Market: {ticker} | Side: {side_clean.upper()} | "
            f"Maker Buy Price: {effective_buy_cents}¢ (Saved vs Ask!) | Target TP: {take_profit_cents}¢"
        )

        now_ms = int(time.time() * 1000)

        if side_clean == "yes":
            # Buying YES at effective_buy_cents
            buy_side = "bid"
            buy_price_str = f"0.{effective_buy_cents:02d}00"

            # Auto-selling YES at take_profit_cents (82¢/76¢)
            sell_side = "ask"
            sell_price_str = f"0.{take_profit_cents:02d}00"
        else:
            # Buying NO at effective_buy_cents (asking YES at 100 - effective_buy_cents)
            buy_side = "ask"
            buy_price_str = f"0.{(100 - effective_buy_cents):02d}00"

            # Auto-selling NO at take_profit_cents (bidding YES at 100 - take_profit_cents)
            sell_side = "bid"
            sell_price_str = f"0.{(100 - take_profit_cents):02d}00"

        # Step 1: Submit Buy Order via Kalshi V2 Endpoint (/portfolio/events/orders)
        buy_payload = {
            "ticker": ticker,
            "client_order_id": f"buy_{side_clean}_{ticker}_{now_ms}",
            "side": buy_side,
            "count": str(count),
            "price": buy_price_str,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }
        buy_order_res = self.post("/portfolio/events/orders", payload=buy_payload)

        # Step 2: Submit Auto-Sell Limit Order (Take Profit target at 82¢/76¢)
        sell_payload = {
            "ticker": ticker,
            "client_order_id": f"sell_tp_{side_clean}_{ticker}_{now_ms}",
            "side": sell_side,
            "count": str(count),
            "price": sell_price_str,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }

        sell_order_res = None
        try:
            sell_order_res = self.post("/portfolio/events/orders", payload=sell_payload)
            log.info(
                f"{LogTag.AUTO_SELL_SET.value} Placed resting Sell limit order for {side_clean.upper()} at {take_profit_cents}¢"
            )
        except Exception as error:
            log.warning(f"⚠️ Could not place resting auto-sell limit order: {error}")

        return {
            "buy_order": buy_order_res,
            "sell_auto_take_profit_order": sell_order_res,
            "side": side_clean,
            "buy_price_cents": effective_buy_cents,
            "take_profit_cents": take_profit_cents,
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

    def cancel_unfilled_buy_orders(self, ticker: str) -> int:
        """
        Cancels any resting unfilled BUY orders for `ticker` from prior intervals.
        Keeps resting Take-Profit SELL orders untouched!
        """
        canceled_count = 0
        try:
            orders_resp = self.get_orders(ticker=ticker, status="resting")
            orders = orders_resp.get("orders") or []
            for o in orders:
                client_order_id = o.get("client_order_id") or ""
                action = (o.get("action") or "").lower()
                order_id = o.get("order_id")

                # Identify buy orders (client_order_id starts with 'buy_' or action is 'buy')
                is_buy_order = client_order_id.startswith("buy_") or action == "buy"
                if is_buy_order and order_id:
                    if self.cancel_order_safe(order_id):
                        canceled_count += 1
                        log.info(f"{LogTag.PRUNE_STALE_BUY.value} Canceled stale unfilled buy order {order_id} for {ticker}")
        except Exception as error:
            log.warning(f"⚠️ Could not check/prune resting buy orders for {ticker}: {error}")

        return canceled_count

    def has_existing_position_or_order(self, ticker: str) -> bool:
        """
        Checks if we ALREADY hold an active position or resting order for the given market ticker.
        Returns True if an open contract or resting order exists, False otherwise.
        """
        # Step 1: Check active market positions
        try:
            pos_resp = self.get_positions(ticker=ticker)
            positions = pos_resp.get("market_positions") or []
            for p in positions:
                if p.get("ticker") == ticker:
                    count_fp = float(p.get("position_fp") or p.get("position") or 0)
                    if abs(count_fp) > 0:
                        log.info(f"{LogTag.POSITION_EXISTS.value} Found active position of {count_fp} contracts in {ticker}")
                        return True
        except Exception as error:
            log.warning(f"Could not check market positions for {ticker}: {error}")

        # Step 2: Check resting orders
        try:
            orders_resp = self.get_orders(ticker=ticker, status="resting")
            orders = orders_resp.get("orders") or []
            if len(orders) > 0:
                log.info(f"{LogTag.ORDER_EXISTS.value} Found {len(orders)} resting order(s) for {ticker}")
                return True
        except Exception as error:
            log.warning(f"Could not check resting orders for {ticker}: {error}")

        return False

    def evaluate_and_execute_stop_loss(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        dry_run: bool = False,
        stop_loss_threshold_cents: int = 10,
    ) -> dict:
        """
        Evaluates active positions for `ticker` at the 10-minute mark (:10, :25, :40, :55):
        1. Tightened Stop-Loss: If loss >= 10¢ OR price <= 30¢, sells position immediately.
        2. Let Winners Run: If position price >= 70¢, cancels resting TP limit order for 100¢ settlement.
        """
        # Check resting orders and position status
        try:
            orders_resp = self.get_orders(ticker=ticker, status="resting")
            resting_orders = orders_resp.get("orders") or []
        except Exception as error:
            log.warning(f"⚠️ Could not fetch resting orders for stop-loss check on {ticker}: {error}")
            resting_orders = []

        try:
            pos_resp = self.get_positions(ticker=ticker)
            positions = pos_resp.get("market_positions") or []
        except Exception as error:
            log.warning(f"⚠️ Could not fetch market positions for stop-loss check on {ticker}: {error}")
            positions = []

        # Find active position for this ticker
        active_pos = None
        for p in positions:
            if p.get("ticker") == ticker:
                count_fp = float(p.get("position_fp") or p.get("position") or 0)
                if abs(count_fp) > 0:
                    active_pos = p
                    break

        # If no active position and no resting orders, nothing to evaluate
        if not active_pos and not resting_orders:
            return {"triggered": False}

        # Determine side and benchmark entry price
        side = "yes"
        entry_price = 38  # Estimated benchmark maker entry buy price (cents)

        for o in resting_orders:
            order_side = (o.get("side") or "").lower()
            if order_side == "ask":
                side = "yes"
            elif order_side == "bid":
                side = "no"

        current_price = current_yes_price if side == "yes" else current_no_price

        if current_price is None:
            log.warning(f"⚠️ Cannot evaluate stop-loss for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        # Calculate current PnL in cents (loss is positive, gain is negative)
        loss_cents = entry_price - current_price
        pnl_label = f"Gain: +{abs(loss_cents)}¢ (Profit)" if loss_cents < 0 else f"Loss: -{loss_cents}¢"

        log.info(
            f"{LogTag.STOP_LOSS_EVALUATION.value} Market: {ticker} | Side: {side.upper()} | "
            f"Est. Entry: ~{entry_price}¢ | Current: {current_price}¢ | Status: {pnl_label}"
        )

        # Optimization Rule 3B: Let Winners Run to 100¢ if price >= 70¢ at minute 10
        if current_price >= 70 and resting_orders:
            log.info(
                f"{LogTag.LET_IT_RUN.value} {ticker} ({side.upper()}) is performing strongly at {current_price}¢ (>= 70¢)! "
                f"Canceling resting TP order to capture 100¢ settlement."
            )
            for o in resting_orders:
                order_id = o.get("order_id")
                if order_id:
                    self.cancel_order_safe(order_id)
            return {"triggered": False, "action": "canceled_tp_let_run"}

        # Optimization Rule 4: Trigger Stop-Loss if loss >= 10¢ OR price <= 30¢
        is_loss_exceeded = loss_cents >= stop_loss_threshold_cents
        is_below_floor = current_price <= 30

        if is_loss_exceeded or is_below_floor:
            reason = f"loss is {loss_cents}¢ (>= {stop_loss_threshold_cents}¢ limit)" if is_loss_exceeded else f"price is {current_price}¢ (<= 30¢ floor)"

            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_STOP_LOSS.value} {ticker} ({side.upper()}) {reason}. "
                    f"WOULD CANCEL RESTING ORDERS & SELL POSITION IMMEDIATELY."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                    "dry_run": True,
                }

            log.info(
                f"{LogTag.STOP_LOSS_TRIGGERED.value} {ticker} ({side.upper()}) {reason}. "
                f"Canceling resting orders and closing position at market!"
            )

            # Step 1: Cancel resting limit orders
            for o in resting_orders:
                order_id = o.get("order_id")
                if order_id:
                    self.cancel_order_safe(order_id)

            # Step 2: Submit Sell order to close position immediately
            now_ms = int(time.time() * 1000)
            if side == "yes":
                sell_payload = {
                    "ticker": ticker,
                    "client_order_id": f"stoploss_yes_{ticker}_{now_ms}",
                    "side": "ask",
                    "count": "1",
                    "price": f"0.{max(1, current_price):02d}00",
                    "time_in_force": "good_till_canceled",
                    "self_trade_prevention_type": "taker_at_cross",
                }
            else:
                sell_payload = {
                    "ticker": ticker,
                    "client_order_id": f"stoploss_no_{ticker}_{now_ms}",
                    "side": "bid",
                    "count": "1",
                    "price": f"0.{(100 - max(1, current_price)):02d}00",
                    "time_in_force": "good_till_canceled",
                    "self_trade_prevention_type": "taker_at_cross",
                }

            try:
                res = self.post("/portfolio/events/orders", payload=sell_payload)
                log.info(f"✅ Successfully executed stop-loss exit order for {ticker}: {res}")
                return {
                    "triggered": True,
                    "side": side,
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                    "order": res,
                }
            except Exception as error:
                log.error(f"❌ Failed to execute stop-loss exit order for {ticker}: {error}")
                return {"triggered": False, "error": str(error)}

        return {"triggered": False}
