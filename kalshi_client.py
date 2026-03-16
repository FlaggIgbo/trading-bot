"""
Kalshi API client with RSA-PSS signature authentication.
Based on: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

# Load .env from project root so it's used regardless of cwd
load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

# API endpoints per Kalshi docs (https://docs.kalshi.com)
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PRODUCTION_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ---------------------------------------------------------------------------
# Auth helpers (RSA-PSS signature)
# ---------------------------------------------------------------------------


def _load_private_key():
    """Load RSA private key from file path or env var (PEM content)."""
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    key_content = os.getenv("KALSHI_PRIVATE_KEY")

    if key_path:
        path = Path(key_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Private key file not found: {path}")
        pem = path.read_bytes()
    elif key_content:
        # Support multiline PEM in env (e.g. \n for newlines)
        pem = key_content.replace("\\n", "\n").encode("utf-8")
    else:
        raise ValueError(
            "Set KALSHI_PRIVATE_KEY_PATH (path to .key file) or KALSHI_PRIVATE_KEY (PEM content)"
        )

    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())


def _sign_request(private_key, timestamp: str, method: str, path: str) -> str:
    """Sign timestamp + method + path (without query params) using RSA-PSS SHA256."""
    path_without_query = path.split("?")[0]
    message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class KalshiClient:
    """Client for Kalshi Trade API with authenticated requests."""

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key=None,
        base_url: str | None = None,
    ):
        self.api_key_id = api_key_id or os.getenv("KALSHI_API_KEY_ID")
        self.private_key = private_key or _load_private_key()
        use_demo = os.getenv("KALSHI_USE_DEMO", "true").lower() == "true"
        default_base = DEMO_BASE if use_demo else PRODUCTION_BASE
        self.base_url = (
            base_url or os.getenv("KALSHI_BASE_URL") or default_base
        ).rstrip("/")

        if not self.api_key_id:
            raise ValueError("KALSHI_API_KEY_ID must be set")

    def _auth_headers(self, method: str, path: str) -> dict:
        import time

        timestamp = str(int(time.time() * 1000))
        signature = _sign_request(self.private_key, timestamp, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _path(self, endpoint: str) -> str:
        return endpoint if endpoint.startswith("/") else f"/{endpoint}"

    def _url(self, endpoint: str, params: dict | None = None) -> str:
        path = self._path(endpoint)
        url = f"{self.base_url}{path}"
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        return url

    def _sign_path(self, endpoint: str, params: dict | None = None) -> str:
        """Full path for signing (includes /trade-api/v2, no query string)."""
        full = f"{self.base_url}{self._path(endpoint)}"
        return urlparse(full).path

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        path = self._sign_path(endpoint, params)
        url = self._url(endpoint, params)
        headers = self._auth_headers("GET", path)
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def post(self, endpoint: str, json: dict | None = None) -> dict:
        path = self._sign_path(endpoint)
        url = self._url(endpoint)
        headers = self._auth_headers("POST", path)
        resp = requests.post(url, headers=headers, json=json or {}, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def delete(self, endpoint: str) -> dict:
        path = self._sign_path(endpoint)
        url = self._url(endpoint)
        headers = self._auth_headers("DELETE", path)
        resp = requests.delete(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    # --- Portfolio ---
    # Balance in cents; positions and orders are per-ticker
    def get_balance(self) -> dict:
        return self.get("/portfolio/balance")

    def get_positions(self, **params) -> dict:
        return self.get("/portfolio/positions", params)

    def get_orders(self, **params) -> dict:
        return self.get("/portfolio/orders", params)

    # --- Markets (public data also available unauthenticated) ---
    def get_markets(self, **params) -> dict:
        return self.get("/markets", params)

    def get_market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}/orderbook")

    def get_market_candlesticks(
        self,
        ticker: str,
        series_ticker: str,
        period_minutes: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> dict:
        """Historical candlesticks. period_minutes: 1, 60, or 1440 (day). Requires series_ticker."""
        params = {"period_interval": period_minutes}
        if start_ts is not None:
            params["start_ts"] = start_ts
        if end_ts is not None:
            params["end_ts"] = end_ts
        return self.get(f"/series/{series_ticker}/markets/{ticker}/candlesticks", params)

    # --- Orders ---
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
        """Create order. side: yes|no, action: buy|sell. Price as cents (1-99) or dollars string."""
        payload = {"ticker": ticker, "side": side, "action": action, "count": count}
        if yes_price is not None:
            payload["yes_price"] = yes_price
        elif yes_price_dollars is not None:
            payload["yes_price_dollars"] = yes_price_dollars
        if no_price is not None:
            payload["no_price"] = no_price
        elif no_price_dollars is not None:
            payload["no_price_dollars"] = no_price_dollars
        payload["time_in_force"] = time_in_force
        payload.update(kwargs)
        return self.post("/portfolio/orders", json=payload)

    def cancel_order(self, order_id: str) -> dict:
        return self.delete(f"/portfolio/orders/{order_id}")

    def create_order_safe(
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
        max_pct_of_balance: float = 0.90,
        **kwargs,
    ) -> dict | None:
        """
        Create order, capping size by balance (buy) or position (sell).
        Never raises on 4xx (e.g. insufficient funds); logs and returns None so scripts continue.
        """
        # Resolve price in cents (Kalshi: 1-99 cents per contract)
        price_cents: int | None = None
        if action == "buy":
            if side == "yes":
                if yes_price is not None:
                    price_cents = int(yes_price)
                elif yes_price_dollars:
                    price_cents = int(float(yes_price_dollars) * 100)
            elif side == "no":
                if no_price is not None:
                    price_cents = int(no_price)
                elif no_price_dollars:
                    price_cents = int(float(no_price_dollars) * 100)
        else:
            # sell: use bid price (yes_bid for sell YES, no_bid for sell NO)
            if side == "yes":
                if yes_price is not None:
                    price_cents = int(yes_price)
                elif yes_price_dollars:
                    price_cents = int(float(yes_price_dollars) * 100)
            elif side == "no":
                if no_price is not None:
                    price_cents = int(no_price)
                elif no_price_dollars:
                    price_cents = int(float(no_price_dollars) * 100)

        if not price_cents or price_cents < 1:
            log.warning("create_order_safe: invalid price (need yes_price/no_price in cents or dollars)")
            return None

        max_count: int
        if action == "sell":
            # Cap by position size. Kalshi API: position_fp is signed (positive=YES, negative=NO)
            try:
                r = self.get_positions(ticker=ticker)
                positions = r.get("market_positions") or []
                side_lower = side.lower()
                pos = None
                for p in positions:
                    if p.get("ticker") != ticker:
                        continue
                    fp_val = float(p.get("position_fp") or p.get("position") or 0)
                    if side_lower == "yes" and fp_val > 0:
                        pos = p
                        break
                    if side_lower == "no" and fp_val < 0:
                        pos = p
                        break
                fp_raw = pos.get("position_fp") or pos.get("position") or "0" if pos else "0"
                position_count = int(abs(float(fp_raw))) if pos else 0
                max_count = position_count
            except Exception as e:
                log.warning("create_order_safe: could not fetch positions: %s", e)
                return None
        else:
            # buy: cap by balance
            try:
                bal = self.get_balance()
                balance_cents = int(bal.get("balance", 0) or 0)
            except Exception as e:
                log.warning("create_order_safe: could not fetch balance: %s", e)
                return None
            max_spend = int(balance_cents * max_pct_of_balance)
            max_count = max_spend // price_cents if price_cents else 0

        count = min(count, max_count) if max_count else 0

        if count <= 0:
            if action == "sell":
                log.info(
                    "create_order_safe: skip - no position in %s %s (max %d contracts)",
                    ticker,
                    side,
                    max_count,
                )
            else:
                log.info(
                    "create_order_safe: skip - balance $%.2f insufficient for %s @ %d¢ (max %d contracts)",
                    (self.get_balance().get("balance", 0) or 0) / 100,
                    ticker,
                    price_cents,
                    max_count,
                )
            return None

        try:
            return self.create_order(
                ticker=ticker,
                side=side,
                action=action,
                count=count,
                yes_price=yes_price,
                yes_price_dollars=yes_price_dollars,
                no_price=no_price,
                no_price_dollars=no_price_dollars,
                time_in_force=time_in_force,
                **kwargs,
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500:
                try:
                    err_text = (e.response.text or "")[:200]
                except Exception:
                    err_text = str(e)
                log.warning(
                    "create_order_safe: order rejected (4xx) %s %s - %s",
                    getattr(e.response, "status_code", "?"),
                    ticker,
                    err_text,
                )
                return None
            raise
