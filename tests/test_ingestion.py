"""
Core tests for Kalshi client and market scanner.
"""

import pytest
from db import Database, PositionSide
from ingestion.api.client import DEMO_BASE_URL, KalshiClient, is_binary_market
from ingestion.scanner import Scanner


def test_client_uses_demo_by_default():
    client = KalshiClient()
    assert client.base_url == DEMO_BASE_URL
    assert client.live_orders is False


def test_is_binary_market():
    tests = [
        ({"ticker": "KXBTC15M-26OCT312200-00", "market_type": "binary"}, 1, True),
        ({"ticker": "KXINXU-26NOV02H1600-T7499.99", "market_type": "binary"}, 1, False),
        ({"ticker": "KXBTCD-26OCT3123-B75000", "market_type": "binary"}, 1, False),
        ({"ticker": "KXEVENT-1", "market_type": "binary"}, 5, False),
        ({"ticker": "KXBTC15M-26OCT312200-00", "market_type": "scalar"}, 1, False),
    ]
    for market, event_count, expected in tests:
        assert is_binary_market(market, event_market_count=event_count) == expected


def test_scanner_upserts_only_binary_markets():
    db = Database(":memory:")
    scanner = Scanner(client=None, db=db)

    raw = [
        {"ticker": "KXBTC15M-1", "event_ticker": "E1", "market_type": "binary", "status": "open", "yes_ask": 45},
        {"ticker": "KXINXU-T7500", "event_ticker": "E2", "market_type": "binary", "status": "open"},
    ]
    db.upsert_markets(scanner.filter_markets(raw))

    saved = db.get_open_markets()
    assert len(saved) == 1
    assert saved[0]["ticker"] == "KXBTC15M-1"
    db.close()


def test_place_order_simulation_and_validation():
    client = KalshiClient(live_orders=False)

    tests = [
        ("YES", PositionSide.YES, 45),
        ("BUY_YES", PositionSide.YES, 45),
        ("NO", PositionSide.NO, 65),
        ("BUY_NO", PositionSide.NO, 65),
    ]
    for side_in, expected_side, price in tests:
        order = client.place_order(ticker="KXBTCD-100", side=side_in, count=1, price_cents=price)
        assert order["status"] == "simulated"
        assert order["side"] == expected_side
        assert order["price"] == price

    invalid_tests = [
        ("KXBTCD-100", "BAD_SIDE"),
        ("KXINXU-26NOV02H1600-T7499.99", "YES"),
    ]
    for ticker, side in invalid_tests:
        with pytest.raises(ValueError):
            client.place_order(ticker=ticker, side=side, count=1, price_cents=50)
