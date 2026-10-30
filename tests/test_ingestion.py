"""
Tests for the ingestion layer.
"""

from unittest.mock import MagicMock
from ingestion.client import KalshiClient, load_private_key, sign_request
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


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
    # Mock HTTP session response for get_open_markets
    client = KalshiClient(live_orders=False)
    client.session = MagicMock()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "markets": [
            {"ticker": "KXBTCD-MOCK-1", "status": "open"},
            {"ticker": "KXBTCD-MOCK-2", "status": "open"},
        ]
    }
    client.session.get.return_value = mock_response

    markets = client.get_open_markets(series_list=["KXBTCD"])
    assert len(markets) == 2
    assert markets[0]["ticker"] == "KXBTCD-MOCK-1"


def test_place_order_simulation():
    # In simulation mode, order should not send HTTP POST
    client = KalshiClient(live_orders=False)
    client.session = MagicMock()

    order = client.place_order(ticker="KXBTCD-100", side="YES", count=1, price_cents=45)
    assert order["status"] == "simulated"
    assert order["ticker"] == "KXBTCD-100"
    assert order["side"] == "YES"
    assert order["count"] == 1
    assert order["price"] == 45
    # Ensure no network call was made
    client.session.post.assert_not_called()
