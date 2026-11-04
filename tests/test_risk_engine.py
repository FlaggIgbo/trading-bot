"""
Core tests for Trader (RiskEngine), Settler, and SQLite state.
"""

import pytest
from db import (
    Database,
    Environment,
    MarketResult,
    MarketStatus,
    MarketType,
    PositionSide,
    PositionStatus,
)
from ingestion.api.client import KalshiClient
from risk_engine.engine import Trader, choose_side_last_3, choose_side_random
from risk_engine.settler import Settler


def test_is_market_open():
    trader = Trader(client=None)
    tests = [
        ("open", True),
        ("active", True),
        ("closed", False),
        ("settled", False),
        ("", False),
    ]
    for status, expected in tests:
        assert trader.is_market_open({"status": status}) == expected


def test_direction_strategies():
    assert choose_side_random() in (PositionSide.YES, PositionSide.NO)

    tests = [
        (["yes", "yes", "no"], PositionSide.YES),
        (["yes", "yes", "yes"], PositionSide.YES),
        (["yes", "no", "no"], PositionSide.NO),
        (["no", "no", "no"], PositionSide.NO),
        ([], PositionSide.NO),
    ]
    for past_results, expected_side in tests:
        assert choose_side_last_3(past_results) == expected_side


def test_trader_uses_last_3_markets_for_direction():
    db = Database(":memory:")
    client = KalshiClient()
    client.get_settled_markets = lambda series, limit=3: []
    trader = Trader(client=client, db=db, strategy="last_3")

    # Seed :00, :15, :30 resolved markets for KXBTC15M (2 ups, 1 down -> should bid YES at :45)
    past_markets = [
        {"ticker": "KXBTC15M-00", "status": "finalized", "result": "yes", "close_time": "2026-11-01T10:00:00Z"},
        {"ticker": "KXBTC15M-15", "status": "finalized", "result": "no", "close_time": "2026-11-01T10:15:00Z"},
        {"ticker": "KXBTC15M-30", "status": "finalized", "result": "yes", "close_time": "2026-11-01T10:30:00Z"},
    ]
    db.upsert_markets(past_markets)

    order = trader.process_market({"ticker": "KXBTC15M-45", "status": "open", "market_type": "binary"})
    assert order["side"] == PositionSide.YES
    db.close()


def test_trader_filtering_and_deduplication():
    db = Database(":memory:")
    trader = Trader(client=KalshiClient(), db=db, strategy="random")

    tests = [
        # (market_dict, should_place_order)
        ({"ticker": "KXBTC15M-1", "status": "open", "market_type": "binary"}, True),
        ({"ticker": "KXBTC15M-1", "status": "open", "market_type": "binary"}, False),  # duplicate
        ({"ticker": "KXBTC15M-2", "status": "closed", "market_type": "binary"}, False),  # closed
        ({"ticker": "KXBTC15M-3", "status": "open", "market_type": "scalar"}, False),  # non-binary
        ({"ticker": "KXINXU-T7500", "status": "open", "market_type": "binary"}, False),  # ladder
    ]
    for market, should_bet in tests:
        order = trader.process_market(market)
        assert (order is not None) == should_bet

    assert len(db.get_all_positions()) == 1
    db.close()


def test_settler_computes_pnl():
    db = Database(":memory:")

    # Seed positions: (id, ticker, side, price_cents, close_time, expected_status, expected_pnl)
    tests = [
        ("win_yes", "T-WIN", "YES", 40, "2026-10-31T10:00:00Z", PositionStatus.SETTLED, 60),
        ("lose_no", "T-LOSS", "NO", 60, "2026-10-31T10:00:00Z", PositionStatus.SETTLED, -60),
        ("future", "T-FUT", "YES", 50, "2099-12-31T23:59:59Z", PositionStatus.OPEN, None),
    ]
    for order_id, ticker, side, price, close_time, _, _ in tests:
        db.insert_open_position(order_id, ticker, side, 1, price, close_time)

    client = KalshiClient()
    client.get_market = lambda ticker: {"ticker": ticker, "status": "finalized", "result": "yes"}
    Settler(client=client, db=db).settle(now_iso="2026-11-01T00:00:00Z")

    saved = {p["client_order_id"]: p for p in db.get_all_positions()}
    for order_id, _, _, _, _, expected_status, expected_pnl in tests:
        assert saved[order_id]["status"] == expected_status
        assert saved[order_id]["pnl_cents"] == expected_pnl

    db.close()


def test_db_enums_and_environment_isolation(tmp_path):
    db_file = str(tmp_path / "state.db")
    demo_db = Database(db_file, environment=Environment.DEMO)
    prod_db = Database(db_file, environment=Environment.PRODUCTION)

    demo_db.upsert_markets([{"ticker": "M1", "status": "open", "market_type": "binary"}])
    demo_db.insert_open_position("ord_1", "M1", "YES", 1, 45)

    # Demo has data; production is isolated
    assert len(demo_db.get_open_markets()) == 1
    assert len(prod_db.get_open_markets()) == 0
    assert len(demo_db.get_all_positions()) == 1
    assert len(prod_db.get_all_positions()) == 0

    # Saved fields are hydrated as enums
    market = demo_db.get_open_markets()[0]
    pos = demo_db.get_all_positions()[0]
    enum_checks = [
        (market["environment"], Environment.DEMO, Environment),
        (market["market_type"], MarketType.BINARY, MarketType),
        (market["status"], MarketStatus.OPEN, MarketStatus),
        (market["result"], MarketResult.PENDING, MarketResult),
        (pos["side"], PositionSide.YES, PositionSide),
        (pos["status"], PositionStatus.OPEN, PositionStatus),
    ]
    for actual, expected_val, expected_type in enum_checks:
        assert actual == expected_val
        assert isinstance(actual, expected_type)

    # Invalid enum strings raise ValueError
    bad_markets = [
        {"ticker": "B1", "market_type": "bad_type", "status": "open"},
        {"ticker": "B2", "market_type": "binary", "status": "bad_status"},
        {"ticker": "B3", "market_type": "binary", "status": "open", "result": "bad_result"},
    ]
    for bad_market in bad_markets:
        with pytest.raises(ValueError):
            demo_db.upsert_markets([bad_market])

    demo_db.close()
    prod_db.close()
