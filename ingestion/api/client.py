"""
Kalshi REST API Client
======================
Handles authentication, fetching open markets, placing orders, and checking market resolution.
"""

import base64
import os
import re
import time
from urllib.parse import urlparse
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
import requests

from db import Environment, MarketType, PositionSide

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
        self.private_key = load_private_key(key_path=key_path)

        # HTTP session
        self.session = requests.Session()

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

    def get_open_markets(self, series_list=None, limit_per_series=20):
        # GET /markets?series_ticker=...&status=open
        series_to_query = series_list or DEFAULT_SERIES
        open_markets = []

        for series in series_to_query:
            url = f"{self.base_url}/markets?series_ticker={series}&status=open&limit={limit_per_series}"
            headers = self._build_auth_headers("GET", url)

            try:
                response = self.session.get(url, headers=headers, timeout=10)
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
                    print(f"Could not fetch series {series}: {response.status_code}")
            except Exception as error:
                print(f"Error fetching markets for {series}: {error}")

        return open_markets

    def get_market(self, ticker):
        # GET /markets/{ticker} to check resolution status and result ('yes' or 'no')
        url = f"{self.base_url}/markets/{ticker}"
        headers = self._build_auth_headers("GET", url)

        try:
            response = self.session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get("market", data)
            else:
                print(f"Could not fetch market {ticker}: {response.status_code}")
                return None
        except Exception as error:
            print(f"Error fetching market {ticker}: {error}")
            return None

    def get_settled_markets(self, series_ticker, limit=3):
        # GET /markets?series_ticker=...&status=settled&limit=... for recent resolved markets
        url = f"{self.base_url}/markets?series_ticker={series_ticker}&status=settled&limit={limit}"
        headers = self._build_auth_headers("GET", url)

        try:
            response = self.session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json().get("markets", [])
        except Exception as error:
            print(f"Error fetching settled markets for {series_ticker}: {error}")
        return []

    def place_order(self, ticker, side, count=1, price_cents=50, client_order_id=None):
        # Reject multi-strike ladder tickers (only binary markets allowed)
        if not is_binary_market({"ticker": ticker, "market_type": MarketType.BINARY}):
            raise ValueError(f"Non-binary multi-strike market '{ticker}' is not allowed.")

        # Normalize side ('yes'/'buy_yes' or 'no'/'buy_no')
        side_clean = str(side).strip().lower()

        # In Kalshi event model, buying YES is 'bid', buying NO is 'ask'
        if side_clean in ("yes", "buy_yes"):
            normalized_side = PositionSide.YES
            order_side = "bid"
        elif side_clean in ("no", "buy_no"):
            normalized_side = PositionSide.NO
            order_side = "ask"
        else:
            raise ValueError(f"Invalid order side '{side}'. Expected 'YES'/'BUY_YES' or 'NO'/'BUY_NO'.")

        # Price in cents between 1 and 99 (passed as the target side's cost)
        safe_price = max(1, min(99, int(price_cents)))

        # Kalshi /portfolio/events/orders prices the book on the YES leg:
        # Buying YES ('bid') uses safe_price; Buying NO ('ask') uses (100 - safe_price)
        book_price_cents = safe_price if normalized_side == PositionSide.YES else (100 - safe_price)
        price_string = f"0.{book_price_cents:02d}00"

        # Generate safe client_order_id with only alphanumeric and underscore
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        order_id = client_order_id or f"buy_{normalized_side.value.lower()}_{safe_ticker}_{int(time.time() * 1000)}"

        # If live orders are off, return simulated order
        if not self.live_orders:
            print(f"[SIMULATION] Bet 1 contract on {normalized_side.value} for {ticker} at {safe_price} cents.")
            return {
                "order_id": order_id,
                "client_order_id": order_id,
                "ticker": ticker,
                "side": normalized_side,
                "count": count,
                "price": safe_price,
                "status": "simulated",
            }

        # Payload for live order (POST /portfolio/events/orders)
        payload = {
            "ticker": ticker,
            "client_order_id": order_id,
            "side": order_side,
            "count": str(count),
            "price": price_string,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }

        url = f"{self.base_url}/portfolio/events/orders"
        headers = self._build_auth_headers("POST", url)

        try:
            response = self.session.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code in [200, 201]:
                print(f"[LIVE] Placed order: 1 contract on {normalized_side.value} for {ticker}!")
                data = response.json()
                data["client_order_id"] = order_id
                return data
            else:
                print(f"Order failed ({response.status_code}): {response.text[:100]}")
                return {"error": response.text}
        except Exception as error:
            print(f"Error placing order: {error}")
            return {"error": str(error)}
