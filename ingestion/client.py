"""
Ingestion Client for Kalshi
============================
Fetches open markets from Kalshi and places orders.
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

# Load environment variables
load_dotenv()

# Default series to look for tradeable prediction markets
DEFAULT_SERIES = ["KXBTCD", "KXETHD", "KXSOLD", "KXINXU", "KXNASDAQ100U"]


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
        # API Key ID
        self.api_key_id = api_key_id or os.getenv("KALSHI_API_KEY_ID", "")

        # Base URL
        default_base = "https://demo-api.kalshi.co/trade-api/v2"
        self.base_url = (base_url or os.getenv("KALSHI_BASE_URL", default_base)).rstrip("/")

        # Check if live orders are enabled
        if live_orders is None:
            live_env = os.getenv("ENABLE_LIVE_ORDERS", "false").lower()
            self.live_orders = live_env in ["true", "1", "yes"]
        else:
            self.live_orders = live_orders

        # Load RSA private key
        key_path = private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH")
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
        # List of series to query
        series_to_query = series_list or DEFAULT_SERIES
        open_markets = []

        for series in series_to_query:
            url = f"{self.base_url}/markets?series_ticker={series}&status=open&limit={limit_per_series}"
            headers = self._build_auth_headers("GET", url)

            try:
                response = self.session.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    markets = response.json().get("markets", [])
                    open_markets.extend(markets)
                else:
                    print(f"Could not fetch series {series}: {response.status_code}")
            except Exception as error:
                print(f"Error fetching markets for {series}: {error}")

        return open_markets

    def place_order(self, ticker, side, count=1, price_cents=50):
        # Clean side string ('yes' or 'no')
        side_clean = side.lower()

        # In Kalshi event model, buying YES is 'bid', buying NO is 'ask'
        if side_clean == "yes":
            order_side = "bid"
        else:
            order_side = "ask"

        # Price in cents between 1 and 99
        safe_price = max(1, min(99, int(price_cents)))
        price_string = f"0.{safe_price:02d}00"

        # Generate safe order ID with only alphanumeric and underscore
        safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
        order_id = f"buy_{side_clean}_{safe_ticker}_{int(time.time() * 1000)}"

        # If live orders are off, return simulated order
        if not self.live_orders:
            print(f"[SIMULATION] Bet 1 contract on {side.upper()} for {ticker} at {safe_price} cents.")
            return {
                "order_id": order_id,
                "ticker": ticker,
                "side": side.upper(),
                "count": count,
                "price": safe_price,
                "status": "simulated",
            }

        # Payload for live order
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
                print(f"[LIVE] Placed order: 1 contract on {side.upper()} for {ticker}!")
                return response.json()
            else:
                print(f"Order failed ({response.status_code}): {response.text[:100]}")
                return {"error": response.text}
        except Exception as error:
            print(f"Error placing order: {error}")
            return {"error": str(error)}
