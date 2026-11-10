"""
Unit tests for SpotClient, spot trend comparison, and last_9 spot-enhanced strategy.
"""

from unittest.mock import MagicMock
import pytest

from db import Database, PositionSide
from ingestion.spot import SpotClient, extract_asset_symbol, extract_market_strike
from risk_engine.engine import Trader, choose_side_last_9, last_9_spot


def test_extract_asset_symbol():
    assert extract_asset_symbol("KXBTC15M-26NOV100015-15") == "BTC"
    assert extract_asset_symbol("KXETH15M") == "ETH"
    assert extract_asset_symbol("KXSOL15M-26NOV091730-30") == "SOL"
    assert extract_asset_symbol("KXXRP15M") == "XRP"
    assert extract_asset_symbol("KXDOGE15M") == "DOGE"
    assert extract_asset_symbol("KXBNB15M") == "BNB"
    assert extract_asset_symbol("UNKNOWN-SERIES") is None
    assert extract_asset_symbol("") is None


def test_extract_market_strike():
    # 1. From floor_strike directly
    assert extract_market_strike({"floor_strike": 74697.25}) == 74697.25
    assert extract_market_strike({"floor_strike": "2119.94"}) == 2119.94

    # 2. From yes_sub_title fallback
    assert extract_market_strike({"yes_sub_title": "Target Price: $85.9772"}) == 85.9772
    assert extract_market_strike({"yes_sub_title": "Target Price: $1,234.56"}) == 1234.56

    # 3. Missing or invalid
    assert extract_market_strike({}) is None
    assert extract_market_strike({"floor_strike": None}) is None


def test_spot_client_caching_and_mock():
    mock_session = MagicMock()
    # Mock Coinbase response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"amount": "74850.50"}}
    mock_session.get.return_value = mock_resp

    client = SpotClient(session=mock_session, cache_ttl_seconds=10.0)
    price1 = client.get_spot_price("BTC")
    assert price1 == 74850.50
    assert mock_session.get.call_count == 1

    # Second call within TTL should hit cache, not session.get
    price2 = client.get_spot_price("KXBTC15M-26NOV100015-15")
    assert price2 == 74850.50
    assert mock_session.get.call_count == 1


def test_spot_client_kraken_fallback():
    mock_session = MagicMock()
    # Coinbase fails (500)
    cb_resp = MagicMock()
    cb_resp.status_code = 500

    # Kraken succeeds (200)
    kraken_resp = MagicMock()
    kraken_resp.status_code = 200
    kraken_resp.json.return_value = {
        "result": {
            "XXBTZUSD": {"c": ["74650.00"]}
        }
    }
    mock_session.get.side_effect = [cb_resp, kraken_resp]

    client = SpotClient(session=mock_session)
    price = client.get_spot_price("BTC")
    assert price == 74650.00
    assert mock_session.get.call_count == 2


def test_spot_client_get_spot_trend():
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"amount": "75000.00"}}
    mock_session.get.return_value = mock_resp

    client = SpotClient(session=mock_session)
    # Market strike is 74500, spot is 75000 -> spot > strike -> YES
    market_bullish = {"ticker": "KXBTC15M-1", "floor_strike": 74500.0}
    side, spot, strike = client.get_spot_trend(market_bullish)
    assert side == PositionSide.YES
    assert spot == 75000.00
    assert strike == 74500.0

    # Market strike is 76000, spot is 75000 -> spot < strike -> NO
    market_bearish = {"ticker": "KXBTC15M-1", "floor_strike": 76000.0}
    side, spot, strike = client.get_spot_trend(market_bearish)
    assert side == PositionSide.NO


def test_choose_side_last_9_with_spot_agreement():
    # 5 ups, 4 downs -> history says YES
    past_results = ["yes", "yes", "yes", "yes", "yes", "no", "no", "no", "no"]
    market = {"ticker": "KXBTC15M-1", "floor_strike": 74000.0}

    mock_spot_client = MagicMock()
    # Spot price is 74500 > strike 74000 -> spot trend is YES
    mock_spot_client.get_spot_trend.return_value = (PositionSide.YES, 74500.0, 74000.0)

    # Both agree on YES
    chosen = choose_side_last_9(
        past_results=past_results,
        market=market,
        spot_client=mock_spot_client,
        check_spot=True,
    )
    assert chosen == PositionSide.YES


def test_choose_side_last_9_with_spot_conflict_skips():
    # 5 ups, 4 downs -> history says YES
    past_results = ["yes", "yes", "yes", "yes", "yes", "no", "no", "no", "no"]
    market = {"ticker": "KXBTC15M-1", "floor_strike": 75000.0}

    mock_spot_client = MagicMock()
    # Spot price is 74500 < strike 75000 -> spot trend is NO!
    mock_spot_client.get_spot_trend.return_value = (PositionSide.NO, 74500.0, 75000.0)

    # History says YES, but real-time spot says NO -> conflict! Should return None (skip trade)
    chosen = choose_side_last_9(
        past_results=past_results,
        market=market,
        spot_client=mock_spot_client,
        check_spot=True,
        require_spot_agreement=True,
    )
    assert chosen is None


def test_choose_side_last_9_value_filter():
    # History says YES, spot says YES
    past_results = ["yes"] * 9
    mock_spot_client = MagicMock()
    mock_spot_client.get_spot_trend.return_value = (PositionSide.YES, 75000.0, 74000.0)

    # 1. Ask is 90c, max_price is 65c -> should skip (overpriced)
    overpriced_market = {"ticker": "KXBTC15M-1", "floor_strike": 74000.0, "yes_ask": 90}
    chosen = choose_side_last_9(
        past_results=past_results,
        market=overpriced_market,
        spot_client=mock_spot_client,
        check_spot=True,
        min_entry_price=20,
        max_entry_price=65,
    )
    assert chosen is None

    # 2. Ask is 15c, min_price is 20c -> should skip (low probability longshot)
    underpriced_market = {"ticker": "KXBTC15M-1", "floor_strike": 74000.0, "yes_ask": 15}
    chosen = choose_side_last_9(
        past_results=past_results,
        market=underpriced_market,
        spot_client=mock_spot_client,
        check_spot=True,
        min_entry_price=20,
        max_entry_price=65,
    )
    assert chosen is None

    # 3. Ask is 45c, within [20, 65] -> should enter YES
    valid_market = {"ticker": "KXBTC15M-1", "floor_strike": 74000.0, "yes_ask": 45}
    chosen = choose_side_last_9(
        past_results=past_results,
        market=valid_market,
        spot_client=mock_spot_client,
        check_spot=True,
        min_entry_price=20,
        max_entry_price=65,
    )
    assert chosen == PositionSide.YES


def test_trader_last_9_spot_strategy_integration():
    db = Database(":memory:")
    # Seed 5 yes, 4 no in history
    for i in range(5):
        db.upsert_markets([{"ticker": f"KXBTC15M-{i}", "status": "finalized", "result": "yes", "close_time": "2026-11-01T10:00:00Z"}])
    for i in range(5, 9):
        db.upsert_markets([{"ticker": f"KXBTC15M-{i}", "status": "finalized", "result": "no", "close_time": "2026-11-01T10:00:00Z"}])

    mock_client = MagicMock()
    mock_client.place_order.return_value = {"order_id": "test_order", "side": PositionSide.YES, "price_cents": 50}
    mock_client.live_orders = False

    mock_spot_client = MagicMock()
    mock_spot_client.get_spot_trend.return_value = (PositionSide.YES, 75000.0, 74000.0)

    trader = Trader(
        client=mock_client,
        db=db,
        strategy="last_9_spot",
        spot_client=mock_spot_client,
        min_entry_price=20,
        max_entry_price=65,
    )

    # 1. Market with valid price 50c -> should trade YES
    market_valid = {"ticker": "KXBTC15M-OPEN1", "status": "open", "market_type": "binary", "yes_ask": 50, "floor_strike": 74000.0}
    order = trader.process_market(market_valid)
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price_cents"] == 50

    # 2. Market with ask 85c -> value filter skips
    market_too_expensive = {"ticker": "KXBTC15M-OPEN2", "status": "open", "market_type": "binary", "yes_ask": 85, "floor_strike": 74000.0}
    order2 = trader.process_market(market_too_expensive)
    assert order2 is None
