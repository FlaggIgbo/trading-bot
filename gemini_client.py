"""
Gemini Prediction Markets API client (crypto only).
Docs: https://docs.gemini.com/prediction-markets/markets

Two clients:
- GeminiPredictionClient: public API (list events, get prices)
- GeminiTradingClient: authenticated (place/cancel orders, positions)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

PRODUCTION_BASE = "https://api.gemini.com"
SANDBOX_BASE = "https://api.sandbox.gemini.com"


# ---------------------------------------------------------------------------
# Event/asset mapping
# ---------------------------------------------------------------------------
# Map Gemini event keywords to our asset labels (yfinance tickers)
_CRYPTO_ASSET_KEYS = {
    "btc": "BTC-USD",
    "bitcoin": "BTC-USD",
    "eth": "ETH-USD",
    "ethereum": "ETH-USD",
    "sol": "SOL-USD",
    "solana": "SOL-USD",
}


def _event_to_asset(event: dict) -> str | None:
    """Map Gemini event to asset (BTC-USD, ETH-USD, SOL-USD) from title/ticker/tags."""
    title = (event.get("title") or "").lower()
    ticker = (event.get("ticker") or "").lower()
    tags = [t.lower() for t in event.get("tags") or []]
    combined = f"{title} {ticker} {' '.join(tags)}"

    for key, asset in _CRYPTO_ASSET_KEYS.items():
        if key in combined:
            return asset
    return None


def _extract_threshold_from_title(title: str) -> tuple[float | None, str | None]:
    """Parse threshold and direction from event title. Returns (threshold, direction)."""
    import re
    title_lower = (title or "").lower()
    for pattern, direction in [
        (r"(?:above|over|reach|hit)\s*\$?([\d,]+(?:\.\d+)?)[kK]?", "above"),
        (r">\s*\$?([\d,]+(?:\.\d+)?)", "above"),
        (r"(?:below|under)\s*\$?([\d,]+(?:\.\d+)?)[kK]?", "below"),
        (r"<\s*\$?([\d,]+(?:\.\d+)?)", "below"),
        (r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)\s*(?:by|before)", "above"),
    ]:
        m = re.search(pattern, title_lower, re.I)
        if m:
            raw = m.group(1).replace(",", "").replace("k", "").replace("K", "")
            try:
                val = float(raw)
                if "k" in m.group(0).lower() and val < 100:
                    val *= 1000
                return val, direction
            except ValueError:
                pass
    return None, None


class GeminiPredictionClient:
    """Client for Gemini Prediction Markets API. Public endpoints, no auth for listing."""

    def __init__(self, base_url: str | None = None):
        use_sandbox = os.getenv("GEMINI_USE_SANDBOX", "false").lower() == "true"
        self.base_url = (
            (base_url or os.getenv("GEMINI_PREDICTION_BASE_URL"))
            or (SANDBOX_BASE if use_sandbox else PRODUCTION_BASE)
        ).rstrip("/")

    def get_events(
        self,
        category: str | list[str] | None = "crypto",
        status: str | list[str] = "active",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List prediction market events. Public endpoint."""
        params = {"limit": min(limit, 500), "offset": offset}
        if category:
            params["category"] = category if isinstance(category, list) else [category]
        if status:
            params["status"] = status if isinstance(status, list) else [status]
        if search:
            params["search"] = search

        url = f"{self.base_url}/v1/prediction-markets/events"
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {"data": [], "pagination": {}}

    def get_crypto_events(self, limit: int = 100) -> list[dict]:
        """Fetch active crypto prediction events. Returns list of events with contracts."""
        out = []
        offset = 0
        while True:
            r = self.get_events(
                category="crypto",
                status="active",
                limit=limit,
                offset=offset,
            )
            data = r.get("data") or []
            out.extend(data)
            pag = r.get("pagination") or {}
            if len(data) < (r.get("limit") or limit):
                break
            offset = pag.get("offset", offset) + len(data)
            if offset >= pag.get("total", offset + 1):
                break
        return out

    def get_events_by_asset(
        self, asset: str, limit_per_asset: int = 10
    ) -> list[dict]:
        """
        Fetch crypto events grouped by asset (BTC-USD, ETH-USD, SOL-USD).
        Returns events that match the asset, up to limit_per_asset each.
        """
        events = self.get_crypto_events(limit=200)
        result = []
        for evt in events:
            mapped = _event_to_asset(evt)
            if mapped == asset:
                result.append(evt)
                if len(result) >= limit_per_asset:
                    break
        return result

    @staticmethod
    def event_to_asset(event: dict) -> str | None:
        """Map Gemini event to asset (BTC-USD, ETH-USD, SOL-USD)."""
        return _event_to_asset(event)


def _get_yes_contract(event: dict) -> dict | None:
    """Return the Yes contract from event.contracts, or None."""
    for c in event.get("contracts") or []:
        if (c.get("label") or "").lower() == "yes":
            return c
    return None


def get_yes_price(contract: dict) -> float:
    """Extract implied P(YES) from Gemini contract. Prefer bestBid, else lastTradePrice, else buy.yes."""
    prices = contract.get("prices") or {}
    bid = prices.get("bestBid")
    if bid is not None:
        try:
            return float(bid)
        except (ValueError, TypeError):
            pass
    last = prices.get("lastTradePrice")
    if last is not None:
        try:
            return float(last)
        except (ValueError, TypeError):
            pass
    buy = prices.get("buy") or {}
    yes = buy.get("yes")
    if yes is not None:
        try:
            return float(yes)
        except (ValueError, TypeError):
            pass
    return 0.0


def extract_strike_from_event(event: dict) -> tuple[float | None, str | None]:
    """Extract (threshold, direction) from Gemini event for historical comparison."""
    return _extract_threshold_from_title(event.get("title") or "")


def get_instrument_symbol(event: dict, outcome: str = "yes") -> str | None:
    """Get trading symbol from event for given outcome (yes/no). Contracts have instrumentSymbol."""
    outcome_lower = outcome.lower()
    for c in event.get("contracts") or []:
        label = (c.get("label") or "").lower()
        if label == outcome_lower:
            return c.get("instrumentSymbol")
    # Fallback: event ticker + first contract
    ticker = event.get("ticker")
    contracts = event.get("contracts") or []
    if ticker and contracts:
        c = contracts[0]
        sym = c.get("instrumentSymbol")
        if sym:
            return sym
        return f"GEMI-{c.get('ticker', ticker)}"
    return None


class GeminiTradingClient(GeminiPredictionClient):
    """Authenticated client for Gemini Prediction Markets: orders, positions."""

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url)
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.api_secret = os.getenv("GEMINI_API_SECRET")
        if not self.api_key or not self.api_secret:
            raise ValueError("GEMINI_API_KEY and GEMINI_API_SECRET must be set for authenticated API")

    def _auth_headers(self, path: str, payload_obj: dict) -> dict:
        """Build X-GEMINI-* headers per Gemini REST auth."""
        payload_obj["request"] = path
        payload_obj["nonce"] = int(time.time())
        encoded = json.dumps(payload_obj).encode("utf-8")
        b64 = base64.b64encode(encoded).decode("ascii")
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            b64.encode("utf-8"),
            hashlib.sha384,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-GEMINI-APIKEY": self.api_key,
            "X-GEMINI-PAYLOAD": b64,
            "X-GEMINI-SIGNATURE": sig,
            "Cache-Control": "no-cache",
        }

    def _post_auth(self, path: str, body: dict) -> dict:
        """POST to authenticated endpoint with signed payload."""
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(path, body.copy())
        resp = requests.post(url, headers=headers, data="", timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def get_active_orders(
        self, symbol: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict:
        """List active (open) orders. Requires auth."""
        body = {"limit": min(limit, 100), "offset": offset}
        if symbol:
            body["symbol"] = symbol
        return self._post_auth("/v1/prediction-markets/orders/active", body)

    def get_positions(self, symbol: str | None = None) -> dict:
        """List positions. Requires auth."""
        body = {}
        if symbol:
            body["symbol"] = symbol
        return self._post_auth("/v1/prediction-markets/positions", body)

    def place_order(
        self,
        symbol: str,
        side: str,
        outcome: str,
        quantity: int | str,
        price: float | str,
        order_type: str = "limit",
        time_in_force: str = "good-til-cancel",
    ) -> dict:
        """
        Place limit order. side: buy|sell, outcome: yes|no.
        quantity: contracts, price: 0-1 (e.g. 0.65).
        """
        body = {
            "symbol": symbol,
            "orderType": order_type,
            "side": side.lower(),
            "quantity": str(quantity),
            "price": str(price),
            "outcome": outcome.lower(),
            "timeInForce": time_in_force,
        }
        return self._post_auth("/v1/prediction-markets/order", body)

    def cancel_order(self, order_id: int | str) -> dict:
        """Cancel order by orderId."""
        return self._post_auth("/v1/prediction-markets/order/cancel", {"orderId": int(order_id)})
