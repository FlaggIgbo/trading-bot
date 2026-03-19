"""
Gemini Prediction Markets API client (crypto only).
Docs: https://docs.gemini.com/prediction-markets/markets

Two clients:
- GeminiPredictionClient: public API (list events, get prices)
- GeminiTradingClient: authenticated (place/cancel orders, positions)
"""
from __future__ import annotations

from typing import Any

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

# Gemini 4xx error reasons (from API: {"reason":"...","message":"...","result":"error"})
# See: https://docs.gemini.com/error-codes
GEMINI_REASON_INSUFFICIENT_FUNDS = "InsufficientFunds"
GEMINI_REASON_INVALID_PRICE = "InvalidPrice"
GEMINI_REASON_INVALID_QUANTITY = "InvalidQuantity"
GEMINI_REASON_INVALID_SYMBOL = "InvalidSymbol"
GEMINI_REASON_MARKET_NOT_OPEN = "MarketNotOpen"
# Master API keys require account in payload; see docs.gemini.com/authentication/api-key
GEMINI_REASON_MISSING_ACCOUNTS = "MissingAccounts"
# From Prediction Markets: accept terms at exchange.gemini.com
GEMINI_REASON_TERMS_NOT_ACCEPTED = "TermsNotAccepted"

PRODUCTION_BASE = "https://api.gemini.com"
SANDBOX_BASE = "https://api.sandbox.gemini.com"


def parse_gemini_error(response) -> tuple[str | None, str]:
    """
    Parse Gemini 4xx/406 error response. Returns (reason, message).
    Response has: {"reason":"...","message":"...","result":"error"}
    See: https://docs.gemini.com/error-codes
    """
    text = ""
    if hasattr(response, "text") and response.text:
        text = response.text
    try:
        data = json.loads(text) if text else {}
    except Exception:
        return None, (text or "")[:200]
    reason = data.get("reason")
    msg = data.get("message", "")
    return reason, msg


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
    "xrp": "XRP-USD",
    "ripple": "XRP-USD",
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
        """Fetch active crypto prediction events (daily, weekly, hourly). Returns list of events with contracts."""
        seen: set[str] = set()
        out: list[dict] = []

        def _fetch_category(cat: str) -> None:
            nonlocal out
            offset = 0
            while True:
                r = self.get_events(category=cat, status="active", limit=limit, offset=offset)
                data = r.get("data") or []
                for evt in data:
                    eid = evt.get("id") or evt.get("ticker") or ""
                    if eid and eid not in seen:
                        seen.add(eid)
                        out.append(evt)
                pag = r.get("pagination") or {}
                if len(data) < (r.get("limit") or limit):
                    break
                offset = pag.get("offset", offset) + len(data)
                if offset >= pag.get("total", offset + 1):
                    break

        _fetch_category("crypto")  # daily, weekly
        try:
            _fetch_category("hourly")  # hourly (BTC, ETH, SOL, XRP at 1am EDT etc.)
        except Exception as e:
            log.debug("Gemini hourly category fetch failed (API may have deprecated it): %s", e)
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


def _get_no_contract(event: dict) -> dict | None:
    """Return the No contract from event.contracts, or None."""
    for c in event.get("contracts") or []:
        if (c.get("label") or "").lower() == "no":
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


def get_no_price(event: dict) -> float:
    """Extract implied P(NO) from Gemini event. For binary markets, P(NO) = 1 - P(YES)."""
    yes_c = _get_yes_contract(event)
    if yes_c:
        yes_p = get_yes_price(yes_c)
        if 0 < yes_p < 1:
            return 1.0 - yes_p
    return 0.0


def extract_strike_from_event(event: dict) -> tuple[float | None, str | None]:
    """Extract (threshold, direction) from Gemini event for historical comparison."""
    return _extract_threshold_from_title(event.get("title") or "")


def expand_gemini_events_to_markets(events: list[dict]) -> list[dict]:
    """
    Expand Gemini events into individual tradeable markets.

    Binary events: one market per event (threshold from title).
    Categorical events (e.g. "Bitcoin tomorrow at 12 AM" with strikes like BTC > $74,250):
    one market per contract that has a parseable strike in its label/abbreviatedName.

    Returns list of market dicts: {event, contract, threshold, direction, ticker, title, instrument_symbol}.
    contract is None for binary; instrument_symbol is contract's instrumentSymbol for categorical.
    """
    markets = []
    for evt in events:
        evt_type = (evt.get("type") or "").lower()
        title = (evt.get("title") or evt.get("ticker") or "")[:55]
        evt_ticker = evt.get("ticker", "?")
        # Treat as categorical if type says so, or if no Yes/No contract but contracts have strikes
        has_binary_contracts = _get_yes_contract(evt) is not None
        is_categorical = evt_type == "categorical" or (
            not has_binary_contracts
            and any(
                _extract_threshold_from_title(c.get("label") or c.get("abbreviatedName") or "")[0] is not None
                for c in evt.get("contracts") or []
            )
        )

        if is_categorical:
            for c in evt.get("contracts") or []:
                label = c.get("label") or c.get("abbreviatedName") or ""
                threshold, direction = _extract_threshold_from_title(label)
                if threshold is not None and direction is not None:
                    inst_sym = c.get("instrumentSymbol")
                    if inst_sym:
                        contract_label = (c.get("label") or c.get("abbreviatedName") or "")[:40]
                        markets.append({
                            "event": evt,
                            "contract": c,
                            "threshold": threshold,
                            "direction": direction,
                            "ticker": evt_ticker,
                            "title": f"{title} ({contract_label})",
                            "instrument_symbol": inst_sym,
                        })
        else:
            threshold, direction = _extract_threshold_from_title(evt.get("title") or "")
            if threshold is not None and direction is not None:
                markets.append({
                    "event": evt,
                    "contract": None,
                    "threshold": threshold,
                    "direction": direction,
                    "ticker": evt_ticker,
                    "title": title,
                    "instrument_symbol": None,
                })
    return markets


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
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def _next_nonce(self) -> int:
        """Return a nonce strictly greater than the last. Gemini requires monotonic increase per API key."""
        with self._nonce_lock:
            now_ms = int(time.time() * 1000)
            self._last_nonce = max(self._last_nonce + 1, now_ms)
            return self._last_nonce

    def _auth_headers(self, path: str, payload_obj: dict) -> dict:
        """Build X-GEMINI-* headers per Gemini REST auth."""
        payload_obj["request"] = path
        payload_obj["nonce"] = self._next_nonce()
        # Master API keys (master-*) require accounts array; account-scoped keys (account-*) do not
        account = os.getenv("GEMINI_ACCOUNT", "").strip()
        if account:
            # Prediction Markets API expects "accounts" as JSON array, not "account" as string
            payload_obj["accounts"] = [account]
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
        payload = body.copy()
        payload["request"] = path
        # nonce set in _auth_headers (monotonically increasing per Gemini docs)
        account = os.getenv("GEMINI_ACCOUNT", "").strip()
        if account:
            payload["accounts"] = [account]  # Must be JSON array for Master keys
        headers = self._auth_headers(path, payload)
        # Prediction Markets API expects JSON body with accounts array (not just header)
        data = json.dumps(payload)
        resp = requests.post(url, headers=headers, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def get_balance(self) -> dict:
        """
        Fetch USD balance in cents (matches Kalshi format for consistency).
        Uses Gemini /v1/balances; finds USD entry and returns available for trading.
        """
        body: dict = {}
        account = os.getenv("GEMINI_ACCOUNT", "").strip()
        if account:
            body["account"] = account  # /v1/balances uses "account" (singular) per Gemini docs
        try:
            resp = self._post_auth("/v1/balances", body)
        except Exception as e:
            log.warning("get_balance: could not fetch Gemini balance: %s", e)
            return {"balance": 0}
        balances = resp if isinstance(resp, list) else []
        for b in balances:
            if (b.get("currency") or "").upper() == "USD":
                avail = float(b.get("available") or b.get("amount") or 0)
                return {"balance": int(avail * 100)}
        return {"balance": 0}

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

    @staticmethod
    def parse_fills_response(resp: Any) -> list[dict]:
        """
        Normalize Gemini fill/trade list from various JSON shapes.
        Public API responses may use fills, trades, data, or a bare list.
        """
        if resp is None:
            return []
        if isinstance(resp, list):
            return [x for x in resp if isinstance(x, dict)]
        if not isinstance(resp, dict):
            return []
        for key in ("fills", "trades", "data", "executions", "results", "orders"):
            v = resp.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        inner = resp.get("result") or resp.get("response")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            for key in ("fills", "trades", "data"):
                v = inner.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
        return []

    def get_fills(self, limit: int = 200, offset: int = 0, symbol: str | None = None) -> dict:
        """
        Fetch confirmed fills/trades when available.
        Gemini endpoint naming can vary; try known variants and return first success.
        """
        body = {"limit": min(max(1, int(limit)), 500), "offset": max(0, int(offset))}
        if symbol:
            body["symbol"] = symbol
        paths = [
            "/v1/prediction-markets/fills",
            "/v1/prediction-markets/trades",
            "/v1/prediction-markets/orders/fills",
        ]
        for path in paths:
            try:
                return self._post_auth(path, body)
            except requests.HTTPError as e:
                if e.response is not None and 400 <= e.response.status_code < 500:
                    continue
                raise
        return {"fills": []}

    def get_fills_paginated(
        self,
        *,
        page_size: int = 200,
        max_pages: int = 50,
        symbol: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """
        Fetch fills across multiple pages using the first working fills endpoint.

        Returns (list of fill dicts, path_used or None if no endpoint worked).
        Stops when a page is empty or shorter than page_size.
        """
        page_size = min(max(1, int(page_size)), 500)
        max_pages = max(1, int(max_pages))
        paths = [
            "/v1/prediction-markets/fills",
            "/v1/prediction-markets/trades",
            "/v1/prediction-markets/orders/fills",
        ]
        for path in paths:
            all_rows: list[dict] = []
            try:
                for page_idx in range(max_pages):
                    offset = page_idx * page_size
                    body = {"limit": page_size, "offset": offset}
                    if symbol:
                        body["symbol"] = symbol
                    raw = self._post_auth(path, body)
                    rows = self.parse_fills_response(raw)
                    if not rows:
                        break
                    all_rows.extend(rows)
                    if len(rows) < page_size:
                        break
                return all_rows, path
            except requests.HTTPError as e:
                if e.response is not None and 400 <= e.response.status_code < 500:
                    continue
                log.warning("get_fills_paginated: path %s failed: %s", path, e)
                continue
            except Exception as e:
                log.warning("get_fills_paginated: path %s failed: %s", path, e)
                continue
        return [], None

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

    def place_order_safe(
        self,
        symbol: str,
        side: str,
        outcome: str,
        quantity: int | str,
        price: float | str,
        ticker_label: str = "",
        order_type: str = "limit",
        time_in_force: str = "good-til-cancel",
        max_pct_of_balance: float = 0.90,
    ) -> dict | None:
        """
        Place limit order; never raises on 4xx/406. Logs error and returns None on failure.
        Caps quantity by Gemini balance (like Kalshi create_order_safe).
        ticker_label: optional label for log messages (e.g. Kalshi ticker).
        Rounds price to 0.01 to avoid InvalidPrice (Gemini uses 1¢ ticks).
        """
        p = float(price)
        if 0 < p < 1:
            price = round(p * 100) / 100
        price_cents = int(p * 100)
        qty = int(quantity) if quantity else 0
        if qty < 1:
            log.warning("place_order_safe: quantity must be >= 1")
            return None
        # Cap by balance (buy only; sell uses position)
        if side.lower() == "buy" and outcome.lower() in ("yes", "no"):
            try:
                bal = self.get_balance()
                balance_cents = int(bal.get("balance", 0) or 0)
            except Exception as e:
                log.warning("place_order_safe: could not fetch balance: %s", e)
                return None
            max_spend = int(balance_cents * max_pct_of_balance)
            max_count = max_spend // price_cents if price_cents else 0
            if max_count <= 0:
                log.info(
                    "place_order_safe: skip - Gemini balance $%.2f insufficient for %s @ %d¢",
                    balance_cents / 100,
                    ticker_label or symbol,
                    price_cents,
                )
                return None
            qty = min(qty, max_count)
        try:
            return self.place_order(
                symbol=symbol,
                side=side,
                outcome=outcome,
                quantity=qty,
                price=price,
                order_type=order_type,
                time_in_force=time_in_force,
            )
        except requests.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500:
                reason, msg = parse_gemini_error(e.response)
                hint = ""
                if reason == GEMINI_REASON_INSUFFICIENT_FUNDS:
                    hint = " (check balance)"
                elif reason == GEMINI_REASON_INVALID_PRICE:
                    hint = " (round price to valid tick, e.g. 0.01 steps)"
                elif reason == GEMINI_REASON_INVALID_QUANTITY:
                    hint = " (quantity must be positive integer)"
                elif reason == GEMINI_REASON_MARKET_NOT_OPEN:
                    hint = " (market closed or paused)"
                elif reason == GEMINI_REASON_INVALID_SYMBOL:
                    hint = " (invalid symbol)"
                elif reason == GEMINI_REASON_TERMS_NOT_ACCEPTED:
                    hint = " (accept Prediction Markets terms at exchange.gemini.com)"
                elif reason == GEMINI_REASON_MISSING_ACCOUNTS:
                    hint = " (Master API key: set GEMINI_ACCOUNT in .env, or use account-scoped key at exchange.gemini.com/settings/api)"
                log.warning(
                    "place_order_safe: order rejected %s %s %s - %s%s",
                    symbol,
                    side,
                    outcome,
                    reason or str(e.response.status_code),
                    hint,
                )
                if msg and msg != (reason or ""):
                    log.warning("place_order_safe: %s", msg[:200])
            else:
                raise
            return None

    def cancel_order(self, order_id: int | str) -> dict:
        """Cancel order by orderId."""
        return self._post_auth("/v1/prediction-markets/order/cancel", {"orderId": int(order_id)})

    def cancel_order_safe(self, order_id: int | str, ticker_label: str = "") -> bool:
        """
        Cancel order; never raises on 4xx/406 (e.g. order already filled/canceled).
        Returns True if canceled, False on failure.
        """
        try:
            self.cancel_order(order_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500:
                reason, msg = parse_gemini_error(e.response)
                log.warning(
                    "cancel_order_safe: order %s not canceled - %s: %s",
                    order_id,
                    reason or e.response.status_code,
                    (msg[:100] if msg else ""),
                )
                return False
            raise
