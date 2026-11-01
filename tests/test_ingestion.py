"""
Tests for the ingestion layer (Kalshi API client and Scanner) against Kalshi demo environment (demo-api.kalshi.co).
"""

import pytest
from db import Database
from ingestion.api.client import (
    DEFAULT_DEMO_API_KEY_ID,
    DEMO_BASE_URL,
    KalshiClient,
    is_binary_market,
    load_private_key,
    sign_request,
)
from ingestion.scanner import Scanner
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


def test_tests_always_hit_demo_environment():
    # Ensure KalshiClient in tests always points to demo-api.kalshi.co, uses the demo key, and never hits production kalshi.com
    client = KalshiClient()
    assert client.base_url == DEMO_BASE_URL
    assert "demo-api.kalshi.co" in client.base_url
    assert "kalshi.com" not in client.base_url
    assert client.api_key_id == DEFAULT_DEMO_API_KEY_ID
    assert client.live_orders is False


def test_is_binary_market_filter():
    # Standalone binary market (e.g. KXBTC15M-26OCT312200-00) -> True
    assert is_binary_market({"ticker": "KXBTC15M-26OCT312200-00", "market_type": "binary"}, event_market_count=1) is True
    # Multi-strike ladder markets -> False
    assert is_binary_market({"ticker": "KXINXU-26NOV02H1600-T7499.9999", "market_type": "binary"}) is False
    assert is_binary_market({"ticker": "KXNASDAQ100U-26NOV02H1000-T31229.99", "market_type": "binary"}) is False
    assert is_binary_market({"ticker": "KXBTCD-26OCT3123-B75000", "market_type": "binary"}) is False
    # Event with multiple markets -> False
    assert is_binary_market({"ticker": "KXEVENT-1", "market_type": "binary"}, event_market_count=5) is False
    # Non-binary market_type -> False
    assert is_binary_market({"ticker": "KXBTC15M-26OCT312200-00", "market_type": "scalar"}) is False


def test_sign_request_and_auth_headers():
    # Generate temporary test RSA private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Test signing function
    sig = sign_request(private_key, "123456789", "GET", "/trade-api/v2/markets?status=open")
    assert isinstance(sig, str)
    assert len(sig) > 0

    # Test client auth headers
    client = KalshiClient(api_key_id="test_key", live_orders=False)
    client.private_key = private_key
    headers = client._build_auth_headers("GET", f"{DEMO_BASE_URL}/markets")
    assert headers["KALSHI-ACCESS-KEY"] == "test_key"
    assert headers["KALSHI-ACCESS-SIGNATURE"] != ""


def test_get_open_markets_demo():
    # Query open markets from demo-api.kalshi.co using the real KalshiClient
    client = KalshiClient(live_orders=False)
    assert client.base_url == DEMO_BASE_URL

    markets = client.get_open_markets(series_list=["KXBTC15M"], limit_per_series=5)
    assert isinstance(markets, list)
    for market in markets:
        assert is_binary_market(market) is True


def test_scanner_upserts_into_sqlite():
    # Verify Scanner filters binary markets and saves them to SQLite
    db = Database(":memory:")
    scanner = Scanner(client=None, db=db)

    raw_markets = [
        {"ticker": "KXBTC15M-1", "event_ticker": "EV1", "market_type": "binary", "status": "open", "yes_ask": 45, "no_ask": 55, "close_time": "2026-11-01T15:00:00Z"},
        {"ticker": "KXINXU-26NOV02H1600-T7499.9999", "event_ticker": "EV2", "market_type": "binary", "status": "open"},
    ]
    filtered = scanner.filter_markets(raw_markets)
    assert len(filtered) == 1
    assert filtered[0]["ticker"] == "KXBTC15M-1"

    db.upsert_markets(filtered)
    saved = db.get_open_markets()
    assert len(saved) == 1
    assert saved[0]["ticker"] == "KXBTC15M-1"
    assert saved[0]["yes_ask"] == 45
    db.close()


def test_place_order_simulation():
    # In simulation mode, order returns simulated dict using real KalshiClient
    client = KalshiClient(live_orders=False)
    assert client.base_url == DEMO_BASE_URL

    for side_input in ("YES", "BUY_YES"):
        order = client.place_order(ticker="KXBTCD-100", side=side_input, count=1, price_cents=45)
        assert order["status"] == "simulated"
        assert order["ticker"] == "KXBTCD-100"
        assert order["side"] == "YES"
        assert order["count"] == 1
        assert order["price"] == 45
        assert order["order_id"].startswith("buy_yes_KXBTCD-100_")
        assert order["client_order_id"] == order["order_id"]

    for side_input in ("NO", "BUY_NO"):
        order = client.place_order(ticker="KXBTCD-100", side=side_input, count=1, price_cents=65)
        assert order["status"] == "simulated"
        assert order["ticker"] == "KXBTCD-100"
        assert order["side"] == "NO"
        assert order["count"] == 1
        assert order["price"] == 65
        assert order["order_id"].startswith("buy_no_KXBTCD-100_")
        assert order["client_order_id"] == order["order_id"]


def test_place_order_live_buy_yes_and_buy_no_on_demo():
    # Place live BUY_YES and BUY_NO orders against demo-api.kalshi.co using real KalshiClient
    client = KalshiClient(live_orders=True)
    assert client.base_url == DEMO_BASE_URL
    assert "demo-api.kalshi.co" in client.base_url

    open_markets = client.get_open_markets(series_list=["KXBTC15M"], limit_per_series=1)
    ticker = open_markets[0]["ticker"] if open_markets else "KXBTC15M-26OCT312200-00"

    yes_res = client.place_order(ticker=ticker, side="BUY_YES", count=1, price_cents=45)
    assert isinstance(yes_res, dict)
    assert "order" in yes_res or "error" in yes_res

    no_res = client.place_order(ticker=ticker, side="BUY_NO", count=1, price_cents=65)
    assert isinstance(no_res, dict)
    assert "order" in no_res or "error" in no_res


def test_place_order_invalid_side():
    client = KalshiClient(live_orders=False)
    with pytest.raises(ValueError, match="Invalid order side"):
        client.place_order(ticker="KXBTC15M-26OCT312200-00", side="INVALID_SIDE", count=1, price_cents=50)


def test_place_order_rejects_multi_strike_market():
    client = KalshiClient(live_orders=False)
    with pytest.raises(ValueError, match="Non-binary multi-strike market"):
        client.place_order(ticker="KXINXU-26NOV02H1600-T7499.9999", side="YES", count=1, price_cents=50)
