"""
Tests for the ingestion layer.
"""

from unittest.mock import MagicMock
import pytest
from ingestion.client import (
    DEFAULT_DEMO_API_KEY_ID,
    DEMO_BASE_URL,
    KalshiClient,
    is_binary_market,
    load_private_key,
    sign_request,
)
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
    headers = client._build_auth_headers("GET", "https://api.kalshi.com/trade-api/v2/markets")
    assert headers["KALSHI-ACCESS-KEY"] == "test_key"
    assert headers["KALSHI-ACCESS-SIGNATURE"] != ""


def test_get_open_markets_mock():
    # Mock HTTP session response for get_open_markets (filters out multi-strike markets)
    client = KalshiClient(live_orders=False)
    client.session = MagicMock()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "markets": [
            {"ticker": "KXBTC15M-26OCT312200-00", "event_ticker": "KXBTC15M-26OCT312200", "market_type": "binary", "status": "open"},
            {"ticker": "KXINXU-26NOV02H1600-T7499.9999", "event_ticker": "KXINXU-26NOV02H1600", "market_type": "binary", "status": "open"},
        ]
    }
    client.session.get.return_value = mock_response

    markets = client.get_open_markets(series_list=["KXBTC15M"])
    assert len(markets) == 1
    assert markets[0]["ticker"] == "KXBTC15M-26OCT312200-00"


def test_place_order_simulation():
    # In simulation mode, order should not send HTTP POST
    client = KalshiClient(live_orders=False)
    client.session = MagicMock()

    for side_input in ("YES", "BUY_YES"):
        order = client.place_order(ticker="KXBTCD-100", side=side_input, count=1, price_cents=45)
        assert order["status"] == "simulated"
        assert order["ticker"] == "KXBTCD-100"
        assert order["side"] == "YES"
        assert order["count"] == 1
        assert order["price"] == 45
        assert order["order_id"].startswith("buy_yes_KXBTCD-100_")

    for side_input in ("NO", "BUY_NO"):
        order = client.place_order(ticker="KXBTCD-100", side=side_input, count=1, price_cents=65)
        assert order["status"] == "simulated"
        assert order["ticker"] == "KXBTCD-100"
        assert order["side"] == "NO"
        assert order["count"] == 1
        assert order["price"] == 65
        assert order["order_id"].startswith("buy_no_KXBTCD-100_")

    # Ensure no network call was made
    client.session.post.assert_not_called()


def test_place_order_live_buy_yes_and_buy_no():
    # Verify live order payload maps BUY_YES -> bid (price) and BUY_NO -> ask (100 - price)
    client = KalshiClient(live_orders=True)
    client.session = MagicMock()

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"order": {"status": "resting"}}
    client.session.post.return_value = mock_response

    # 1. Test BUY_YES at 45 cents -> side='bid', price='0.4500'
    client.place_order(ticker="KXBTCD-100", side="BUY_YES", count=1, price_cents=45)
    yes_payload = client.session.post.call_args[1]["json"]
    assert yes_payload["ticker"] == "KXBTCD-100"
    assert yes_payload["side"] == "bid"
    assert yes_payload["price"] == "0.4500"
    assert yes_payload["count"] == "1"

    # 2. Test BUY_NO at 65 cents -> side='ask', price='0.3500' (100 - 65)
    client.place_order(ticker="KXBTCD-100", side="BUY_NO", count=1, price_cents=65)
    no_payload = client.session.post.call_args[1]["json"]
    assert no_payload["ticker"] == "KXBTCD-100"
    assert no_payload["side"] == "ask"
    assert no_payload["price"] == "0.3500"
    assert no_payload["count"] == "1"


def test_place_order_invalid_side():
    client = KalshiClient(live_orders=False)
    with pytest.raises(ValueError, match="Invalid order side"):
        client.place_order(ticker="KXBTC15M-26OCT312200-00", side="INVALID_SIDE", count=1, price_cents=50)


def test_place_order_rejects_multi_strike_market():
    client = KalshiClient(live_orders=False)
    with pytest.raises(ValueError, match="Non-binary multi-strike market"):
        client.place_order(ticker="KXINXU-26NOV02H1600-T7499.9999", side="YES", count=1, price_cents=50)

