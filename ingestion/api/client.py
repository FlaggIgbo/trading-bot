"""
Kalshi REST API Client
======================
Handles authentication, fetching open markets, placing orders, and checking market resolution.
"""

import base64
import os
import random
import re
import time
from urllib.parse import urlparse
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
import requests

from db import Environment, MarketType, PositionSide
from logging.analytics import analytics

# Load environment variables (fall back to .env.example defaults if not set in .env)
load_dotenv()
load_dotenv(".env.example", override=False)

# Default series to look for tradeable binary (single-market Yes/No) prediction markets
DEFAULT_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXBNB15M"]
DEMO_BASE_URL = os.getenv("KALSHI_DEMO_BASE_URL", "")
DEFAULT_DEMO_API_KEY_ID = os.getenv("KALSHI_DEMO_API_KEY_ID", "")

# Regex matching multi-strike ladder ticker suffixes (e.g., -T7499.9999, -B7500, -A100)
MULTI_STRIKE_SUFFIX_RE = re.compile(r"-[TBA]\d+(?:\.\d+)?$", re.IGNORECASE)


def is_binary_market(market, event_market_count=None):
    """
    Checks if a Kalshi market is a true standalone binary market (only Buy YES / Buy NO,
    not a multi-strike ladder or range bracket market).
    """
    if not isinstance(market, dict):
        return False

    # Must have market_type == 'binary' (if specified)
    market_type = str(market.get("market_type", MarketType.BINARY)).lower()
    if market_type != MarketType.BINARY.value:
        return False

    # If event market count is known, event must have exactly 1 market
    if event_market_count is not None and event_market_count != 1:
        return False

    # Reject multi-strike ladder tickers (e.g. KXINXU-26NOV02H1600-T7499.9999)
    ticker = market.get("ticker", "")
    if MULTI_STRIKE_SUFFIX_RE.search(ticker):
        return False

    return True


def load_private_key(key_path=None, key_content=None):
    # Load RSA private key from file or environment string
    if key_path and os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    if key_content:
        pem_bytes = key_content.replace("\\n", "\n").encode("utf-8")
        return serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )

    return None


def sign_request(private_key, timestamp_ms, http_method, request_path):
    # Kalshi signs: timestamp + method + path without query parameters
    clean_path = request_path.split("?")[0]
    message_to_sign = f"{timestamp_ms}{http_method}{clean_path}".encode("utf-8")

    # Support modern Ed25519 keys as well as legacy RSA keys
    from cryptography.hazmat.primitives.asymmetric import ed25519
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message_to_sign)
        return base64.b64encode(signature).decode("utf-8")

    # Generate RSA-PSS signature
    signature = private_key.sign(
        message_to_sign,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    # Return base64 string
    return base64.b64encode(signature).decode("utf-8")


class KalshiClient:
    def __init__(self, api_key_id=None, private_key_path=None, base_url=None, live_orders=None):
        # Base URL (if KALSHI_USE_DEMO=true, always use demo environment)
        demo_base_url = os.getenv("KALSHI_DEMO_BASE_URL", DEMO_BASE_URL).rstrip("/")
        use_demo = os.getenv("KALSHI_USE_DEMO", "").lower() in ["true", "1", "yes"]
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif use_demo:
            self.base_url = demo_base_url
        else:
            self.base_url = os.getenv("KALSHI_BASE_URL", demo_base_url).rstrip("/")

        is_demo_url = "demo-api.kalshi.co" in self.base_url or "demo.kalshi.co" in self.base_url
        self.is_demo = is_demo_url
        self.environment = Environment.DEMO if is_demo_url else Environment.PRODUCTION

        # API Key ID (use KALSHI_DEMO_API_KEY_ID when targeting demo environment)
        if api_key_id:
            self.api_key_id = api_key_id
        elif is_demo_url:
            self.api_key_id = os.getenv(
                "KALSHI_DEMO_API_KEY_ID",
                os.getenv("KALSHI_API_KEY_ID", DEFAULT_DEMO_API_KEY_ID),
            )
        else:
            self.api_key_id = os.getenv("KALSHI_API_KEY_ID", "")

        # Check if live orders are enabled
        if live_orders is None:
            live_env = os.getenv("ENABLE_LIVE_ORDERS", "false").lower()
            self.live_orders = live_env in ["true", "1", "yes"]
        else:
            self.live_orders = live_orders

        # Load RSA private key
        if private_key_path:
            key_path = private_key_path
        elif is_demo_url:
            key_path = os.getenv("KALSHI_DEMO_PRIVATE_KEY_PATH", os.getenv("KALSHI_PRIVATE_KEY_PATH"))
        else:
            key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        key_content = os.getenv("KALSHI_DEMO_PRIVATE_KEY_CONTENT") if is_demo_url else (os.getenv("KALSHI_PRIVATE_KEY_CONTENT") or os.getenv("KALSHI_PRIVATE_KEY"))
        self.private_key = load_private_key(key_path=key_path, key_content=key_content)

        # HTTP session and resilience settings
        self.session = requests.Session()
        self.min_request_interval: float = 0.1  # 10 req/s rate limit
        self._last_request_time: float = 0.0
        self.max_retries: int = 3
        self.consecutive_failures: int = 0
        self.circuit_breaker_threshold: int = 5
        self.circuit_breaker_cooldown: float = 60.0  # seconds
        self._circuit_open_until: float = 0.0

    def _build_auth_headers(self, method, full_url):
        # Extract path without domain name
        path = urlparse(full_url).path

        # Timestamp in milliseconds
        timestamp_ms = str(int(time.time() * 1000))

        # RSA signature
        signature = ""
        if self.private_key:
            signature = sign_request(self.private_key, timestamp_ms, method, path)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

    def _log_api_call(self, method: str, url: str):
        """Log time remaining in parallel run whenever a Kalshi API call is made."""
        end_time_str = os.getenv("PARALLEL_RUN_END_TIME")
        if end_time_str:
            try:
                end_time = float(end_time_str)
                remaining_sec = max(0, int(end_time - time.time()))
                hours = remaining_sec // 3600
                mins = (remaining_sec % 3600) // 60
                secs = remaining_sec % 60
                path = urlparse(url).path
                analytics.info(
                    f"Kalshi API {method.upper()} {path} | Time remaining: {hours:02d}h {mins:02d}m {secs:02d}s ({remaining_sec}s left)",
                    event="api_call",
                    method=method.upper(),
                    path=path,
                    time_remaining_seconds=remaining_sec,
                    time_remaining=f"{hours:02d}h {mins:02d}m {secs:02d}s",
                )
            except Exception:
                pass

    def _request(self, method: str, url: str, **kwargs):
        """
        Execute HTTP request with:
        - Circuit breaker protection
        - Client-side rate limiting
        - Exponential backoff and jitter on 5xx and network errors
        - Default 10s timeout
        """
        now = time.time()
        # 1. Check Circuit Breaker
        if now < self._circuit_open_until:
            wait_time = int(self._circuit_open_until - now)
            analytics.warning(
                f"[CIRCUIT BREAKER] Open due to {self.consecutive_failures} failures. Calls paused for {wait_time}s.",
                event="circuit_breaker_active",
                wait_seconds=wait_time,
            )
            raise RuntimeError(f"Circuit breaker is OPEN. Kalshi API calls paused for {wait_time}s.")

        # 2. Client-side rate limit
        elapsed = now - self._last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self._last_request_time = time.time()

        # 3. Timeout default
        kwargs.setdefault("timeout", 10.0)

        # 4. Exponential backoff and retry loop
        last_exception = None
        for attempt in range(self.max_retries + 1):
            try:
                self._log_api_call(method, url)
                response = self.session.request(method, url, **kwargs)

                # Retry on 5xx server errors
                if response.status_code >= 500:
                    if attempt < self.max_retries:
                        backoff = (0.5 * (2 ** attempt)) + random.uniform(0.05, 0.15)
                        time.sleep(backoff)
                        continue

                # Successful request resets consecutive failure count
                self.consecutive_failures = 0
                return response

            except (requests.exceptions.RequestException, TimeoutError) as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff = (0.5 * (2 ** attempt)) + random.uniform(0.05, 0.15)
                    time.sleep(backoff)
                else:
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= self.circuit_breaker_threshold:
                        self._circuit_open_until = time.time() + self.circuit_breaker_cooldown
                        analytics.error(
                            f"[CIRCUIT BREAKER] Tripped after {self.consecutive_failures} consecutive failures. Pausing API for {self.circuit_breaker_cooldown}s.",
                            event="circuit_breaker_tripped",
                            failures=self.consecutive_failures,
                            cooldown_seconds=self.circuit_breaker_cooldown,
                        )
                    raise last_exception

    def get_open_markets(self, series_list=None, limit_per_series=20):
        # GET /markets?series_ticker=...&status=open
        series_to_query = series_list or DEFAULT_SERIES
        open_markets = []

        for series in series_to_query:
            url = f"{self.base_url}/markets?series_ticker={series}&status=open&limit={limit_per_series}"
            headers = self._build_auth_headers("GET", url)

            try:
                response = self._request("GET", url, headers=headers, timeout=10)
                if response.status_code == 200:
                    markets = response.json().get("markets", [])
                    # Group by event_ticker to ensure we only keep standalone binary events (1 market per event)
                    event_counts = {}
                    for m in markets:
                        ev = m.get("event_ticker")
                        if ev:
                            event_counts[ev] = event_counts.get(ev, 0) + 1

                    for m in markets:
                        ev = m.get("event_ticker")
                        ev_count = event_counts.get(ev, 1) if ev else 1
                        if is_binary_market(m, event_market_count=ev_count):
                            open_markets.append(m)
                else:
                    analytics.warning(
                        f"Could not fetch series {series}: {response.status_code}",
                        series=series,
                        status_code=response.status_code,
                    )
            except Exception as error:
                analytics.error(
                    f"Error fetching markets for {series}: {error}",
                    series=series,
                    error=str(error),
                )

        return open_markets

    def get_market(self, ticker):
        # GET /markets/{ticker} to check resolution status and result ('yes' or 'no')
        url = f"{self.base_url}/markets/{ticker}"
        headers = self._build_auth_headers("GET", url)

        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get("market", data)
            else:
                analytics.warning(
                    f"Could not fetch market {ticker}: {response.status_code}",
                    ticker=ticker,
                    status_code=response.status_code,
                )
                return None
        except Exception as error:
            analytics.error(
                f"Error fetching market {ticker}: {error}",
                ticker=ticker,
                error=str(error),
            )
            return None

    def get_settled_markets(self, series_ticker, limit=3):
        # GET /markets?series_ticker=...&status=settled&limit=... for recent resolved markets
        url = f"{self.base_url}/markets?series_ticker={series_ticker}&status=settled&limit={limit}"
        headers = self._build_auth_headers("GET", url)

        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json().get("markets", [])
        except Exception as error:
            analytics.error(
                f"Error fetching settled markets for {series_ticker}: {error}",
                series_ticker=series_ticker,
                error=str(error),
            )
        return []

    def get_candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_interval=1):
        """Query Kalshi API for market candlesticks (OHLCV and bid/ask history)."""
        url = f"{self.base_url}/series/{series_ticker}/markets/{ticker}/candlesticks?start_ts={start_ts}&end_ts={end_ts}&period_interval={period_interval}"
        headers = self._build_auth_headers("GET", url)
        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json().get("candlesticks", [])
            return []
        except Exception as error:
            analytics.error(
                f"Error fetching candlesticks for {ticker}: {error}",
                ticker=ticker,
                error=str(error),
            )
            return []

    def get_order(self, order_id):
        """Query Kalshi API for order execution status."""
        if not order_id:
            return None

        if not self.live_orders:
            return {"order_id": order_id, "status": "simulated"}

        url = f"{self.base_url}/portfolio/orders/{order_id}"
        headers = self._build_auth_headers("GET", url)
        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get("order", data)
            return None
        except Exception as error:
            analytics.error(
                f"Error fetching order {order_id}: {error}",
                order_id=order_id,
                error=str(error),
            )
            return None

    def get_settlements(self, limit=100):
        """Query Kalshi API for settled portfolio contracts."""
        if not self.live_orders:
            return []
        url = f"{self.base_url}/portfolio/settlements?limit={limit}"
        headers = self._build_auth_headers("GET", url)
        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json().get("settlements", [])
            return []
        except Exception as error:
            analytics.error(
                f"Error fetching settlements: {error}",
                error=str(error),
            )
            return []

    def get_fills(self, order_id=None, ticker=None, limit=100):
        """Query Kalshi API for portfolio fills."""
        if not self.live_orders:
            return []
        url = f"{self.base_url}/portfolio/fills?limit={limit}"
        if order_id:
            url += f"&order_id={order_id}"
        if ticker:
            url += f"&ticker={ticker}"
        headers = self._build_auth_headers("GET", url)
        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json().get("fills", [])
            return []
        except Exception as error:
            analytics.error(
                f"Error fetching fills: {error}",
                error=str(error),
            )
            return []

    def place_order(
        self,
        ticker,
        side,
        count=1,
        price_cents=50,
        client_order_id=None,
        action="buy",
        time_in_force="good_till_canceled",
        reduce_only=False,
    ):
        # Reject multi-strike ladder tickers (only binary markets allowed)
        if not is_binary_market({"ticker": ticker, "market_type": MarketType.BINARY}):
            raise ValueError(f"Non-binary multi-strike market '{ticker}' is not allowed.")

        # Normalize side ('yes'/'buy_yes' or 'no'/'buy_no')
        side_clean = str(side).strip().lower()

        # In Kalshi event model, buying YES is 'bid', buying NO is 'ask'
        if side_clean in ("yes", "buy_yes"):
            normalized_side = PositionSide.YES
            order_side = "bid" if action == "buy" else "ask"
        elif side_clean in ("no", "buy_no"):
            normalized_side = PositionSide.NO
            order_side = "ask" if action == "buy" else "bid"
        else:
            raise ValueError(f"Invalid order side '{side}'. Expected 'YES'/'BUY_YES' or 'NO'/'BUY_NO'.")

        # Price in cents between 1 and 99 (passed as the target side's cost)
        safe_price = max(1, min(99, int(price_cents)))

        # Kalshi /portfolio/events/orders prices the book on the YES leg:
        # Buying YES ('bid') uses safe_price; Buying NO ('ask') uses (100 - safe_price)
        book_price_cents = safe_price if normalized_side == PositionSide.YES else (100 - safe_price)
        price_string = f"0.{book_price_cents:02d}00"

        # Generate safe client_order_id with only alphanumeric and underscore (max 64 chars)
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        order_id = (client_order_id or f"{action}_{normalized_side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}")[:64]

        # If live orders are off, return simulated order
        if not self.live_orders:
            action_label = "Bet" if action == "buy" else "Sold"
            analytics.track(
                "order_simulated",
                message=f"[SIMULATION] {action_label} {count} contract on {normalized_side.value} for {ticker} at {safe_price} cents.",
                ticker=ticker,
                side=normalized_side.value,
                count=count,
                price_cents=safe_price,
                action=action,
            )
            return {
                "order_id": order_id,
                "client_order_id": order_id,
                "ticker": ticker,
                "side": normalized_side,
                "count": count,
                "price": safe_price,
                "status": "simulated",
                "action": action,
            }

        # Payload for live order (POST /portfolio/events/orders)
        payload = {
            "ticker": ticker,
            "client_order_id": order_id,
            "side": order_side,
            "count": str(count),
            "price": price_string,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if reduce_only:
            payload["reduce_only"] = True

        url = f"{self.base_url}/portfolio/events/orders"
        headers = self._build_auth_headers("POST", url)

        try:
            response = self._request("POST", url, headers=headers, json=payload, timeout=10)
            if response.status_code in [200, 201]:
                analytics.track(
                    "order_live",
                    message=f"[LIVE] Placed order: 1 contract on {normalized_side.value} for {ticker}!",
                    ticker=ticker,
                    side=normalized_side.value,
                    count=count,
                    order_id=order_id,
                )
                data = response.json()
                data["client_order_id"] = order_id
                return data
            else:
                analytics.error(
                    f"Order failed ({response.status_code}): {response.text[:100]}",
                    status_code=response.status_code,
                    ticker=ticker,
                )
                return {"error": response.text}
        except Exception as error:
            analytics.error(
                f"Error placing order: {error}",
                ticker=ticker,
                error=str(error),
            )
            return {"error": str(error)}

    def cancel_order(self, order_id, ticker=None):
        if not order_id:
            return None

        if not self.live_orders:
            analytics.track(
                "order_canceled",
                message=f"[SIMULATION] Canceled order {order_id}.",
                order_id=order_id,
            )
            return {"order_id": order_id, "status": "canceled"}

        url = f"{self.base_url}/portfolio/events/orders/{order_id}"
        if ticker:
            url += f"?market_ticker={ticker}"
        headers = self._build_auth_headers("DELETE", url)
        try:
            response = self._request("DELETE", url, headers=headers, timeout=10)
            if response.status_code in (200, 204):
                analytics.track(
                    "order_canceled",
                    message=f"[LIVE] Canceled resting order {order_id}.",
                    order_id=order_id,
                )
                return response.json() if response.text else {"order_id": order_id, "status": "canceled"}
            elif response.status_code == 404:
                analytics.info(
                    f"Resting order {order_id} no longer active on book (status 404, already filled or canceled).",
                    order_id=order_id,
                )
                return {"order_id": order_id, "status": "not_found"}
            else:
                analytics.warning(
                    f"Could not cancel order {order_id}: {response.status_code} {response.text[:100]}",
                    order_id=order_id,
                    status_code=response.status_code,
                )
                return {"error": response.text}
        except Exception as error:
            analytics.error(
                f"Error canceling order {order_id}: {error}",
                order_id=order_id,
                error=str(error),
            )
            return {"error": str(error)}

    def get_balance(self):
        """Fetch portfolio balance and breakdown by exchange shard."""
        url = f"{self.base_url}/portfolio/balance"
        headers = self._build_auth_headers("GET", url)
        try:
            response = self._request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as error:
            analytics.error(f"Error fetching balance: {error}", error=str(error))
            return None

    def transfer_between_shards(self, source_shard: int, dest_shard: int, amount_dollars: float):
        """Transfer cash between exchange shards (e.g. 0=elections/general, 2=crypto contracts)."""
        import uuid
        url = f"{self.base_url}/portfolio/intra_exchange_instance_transfer"
        headers = self._build_auth_headers("POST", url)
        # 1 dollar = 10,000 centicents
        centicents = int(round(amount_dollars * 10000))
        payload = {
            "source": "event_contract",
            "destination": "event_contract",
            "source_exchange_shard": source_shard,
            "destination_exchange_shard": dest_shard,
            "amount": centicents,
            "client_transfer_id": str(uuid.uuid4()),
        }
        try:
            response = self._request("POST", url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                analytics.info(
                    f"Transferred ${amount_dollars:.2f} from shard {source_shard} to shard {dest_shard}.",
                    source_shard=source_shard,
                    dest_shard=dest_shard,
                    amount=amount_dollars,
                )
                return response.json()
            else:
                analytics.error(
                    f"Shard transfer failed ({response.status_code}): {response.text}",
                    status_code=response.status_code,
                )
                return {"error": response.text}
        except Exception as error:
            analytics.error(f"Error during shard transfer: {error}", error=str(error))
            return {"error": str(error)}

