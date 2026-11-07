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
from risk_engine.engine import (
    Trader,
    choose_sell_if_2n,
    choose_sell_if_2n_5m,
    choose_sell_if_3n,
    choose_side_last_3,
    choose_side_last_5,
    choose_side_random,
    get_strategy_resting_multiplier,
    is_checkin_time,
    is_entry_time,
    is_entry_time_5m,
    is_entry_time_10m,
    should_sell,
)
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


def test_choose_side_last_5():
    tests = [
        (["yes", "yes", "yes", "no", "no"], PositionSide.YES),
        (["yes", "no", "no", "no", "yes"], PositionSide.NO),
        (["yes", "yes", "yes", "yes", "yes", "no", "no", "no"], PositionSide.YES),
        (["no", "no", "no", "yes", "yes"], PositionSide.NO),
    ]
    for past_results, expected in tests:
        assert choose_side_last_5(past_results) == expected


def test_trader_uses_last_5_markets_for_direction():
    db = Database(":memory:")
    client = KalshiClient()
    trader = Trader(client=client, db=db, strategy="last_5")

    # 3 ups, 2 downs -> should bid YES
    past_markets = [
        {"ticker": "KXBTC15M-00", "status": "finalized", "result": "yes", "close_time": "2026-11-01T10:00:00Z"},
        {"ticker": "KXBTC15M-15", "status": "finalized", "result": "no", "close_time": "2026-11-01T10:15:00Z"},
        {"ticker": "KXBTC15M-30", "status": "finalized", "result": "yes", "close_time": "2026-11-01T10:30:00Z"},
        {"ticker": "KXBTC15M-45", "status": "finalized", "result": "no", "close_time": "2026-11-01T10:45:00Z"},
        {"ticker": "KXBTC15M-60", "status": "finalized", "result": "yes", "close_time": "2026-11-01T11:00:00Z"},
    ]
    db.upsert_markets(past_markets)

    order = trader.process_market({"ticker": "KXBTC15M-75", "status": "open", "market_type": "binary"})
    assert order["side"] == PositionSide.YES
    db.close()


def test_strategy_sell_3n_contract_selection():
    # Pick whichever side is under 33c; if none, return None
    assert choose_sell_if_3n({"yes_ask": 25, "no_ask": 75}) == PositionSide.YES
    assert choose_sell_if_3n({"yes_ask": 80, "no_ask": 20}) == PositionSide.NO
    assert choose_sell_if_3n({"yes_ask": 50, "no_ask": 50}) is None
    assert choose_sell_if_3n({"yes_ask": 33, "no_ask": 67}) is None
    assert choose_sell_if_3n({"yes_ask": 32, "no_ask": 68}) == PositionSide.YES
    assert choose_sell_if_3n({"yes_ask": 20, "no_ask": 30}) == PositionSide.YES


def test_strategy_sell_3n_timing_with_close_time():
    market = {"ticker": "KXBTC15M-15", "close_time": "2026-11-01T10:15:00Z"}
    # 10:00 start -> 10:02 is minute 2, 10:05 is minute 5, 10:10 is minute 10
    assert not is_entry_time(market, now="2026-11-01T10:02:00Z")
    assert is_entry_time(market, now="2026-11-01T10:05:00Z")
    assert not is_checkin_time(market, now="2026-11-01T10:05:00Z")
    assert is_checkin_time(market, now="2026-11-01T10:10:00Z")

    # Sell if current value >= 3x entry price, else hold
    assert not should_sell(entry_price_cents=25, current_price_cents=74)
    assert should_sell(entry_price_cents=25, current_price_cents=75)
    assert should_sell(entry_price_cents=25, current_price_cents=80)


def test_strategy_sell_3n_trader_flow():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_3n")

    # 1. Reject if before 5 minutes past (e.g. at minute 2)
    m1 = {"ticker": "M-EARLY", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 2}
    assert trader.process_market(m1) is None

    # 2. Reject if at 5 min past but none < 33c
    m2 = {"ticker": "M-NOPICK", "status": "open", "yes_ask": 50, "no_ask": 50, "minute": 5}
    assert trader.process_market(m2) is None

    # 3. Enter at 5 min past when contract < 33c (e.g. YES @ 25c)
    m3 = {"ticker": "M-WIN", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 5}
    order = trader.process_market(m3)
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price"] == 25
    # Immediately places resting sell order for 3x (3 * 25c = 75c)
    assert order["resting_price_cents"] == 75
    assert order["resting_order"]["action"] == "sell"
    assert order["resting_order"]["price"] == 75

    positions = db.get_all_positions()
    assert len(positions) == 1
    assert positions[0]["status"] == PositionStatus.OPEN
    assert positions[0]["resting_order_id"] != ""

    # 4. Check-in at 10 min past: current price 50c (< 3x 25c) -> hold
    m3_hold = {"ticker": "M-WIN", "status": "open", "yes_ask": 50, "no_ask": 50, "minute": 10}
    sold = trader.check_open_positions(markets=[m3_hold], now=None)
    assert len(sold) == 0
    assert db.get_all_positions()[0]["status"] == PositionStatus.OPEN

    # 5. Check-in: current price 75c (resting 3x reached) -> fills resting sell
    m3_sell = {"ticker": "M-WIN", "status": "open", "yes_ask": 75, "no_ask": 25, "minute": 10}
    sold = trader.check_open_positions(markets=[m3_sell], now=None)
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == 50  # 75c - 25c = +50c
    assert db.get_all_positions()[0]["status"] == PositionStatus.SETTLED

    db.close()


def test_strategy_sell_2n_trader_flow():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_2n")

    # 1. Reject if before 10 minutes past (e.g. at minute 5)
    m1 = {"ticker": "M-2N-5M", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 5}
    assert trader.process_market(m1) is None

    # 2. Reject if at 10 min past but none < 33c
    m2 = {"ticker": "M-2N-NOPICK", "status": "open", "yes_ask": 50, "no_ask": 50, "minute": 10}
    assert trader.process_market(m2) is None

    # 3. Enter at 10 min past when contract < 33c (e.g. NO @ 20c)
    m3 = {"ticker": "M-2N-WIN", "status": "open", "yes_ask": 80, "no_ask": 20, "minute": 10}
    order = trader.process_market(m3)
    assert order is not None
    assert order["side"] == PositionSide.NO
    assert order["price"] == 20
    # Immediately places resting sell order for 2x (2 * 20c = 40c)
    assert order["resting_price_cents"] == 40
    assert order["resting_order"]["action"] == "sell"
    assert order["resting_order"]["price"] == 40

    positions = db.get_all_positions(strategy="sell_2n")
    assert len(positions) == 1
    assert positions[0]["status"] == PositionStatus.OPEN
    assert positions[0]["resting_order_id"] != ""

    # 4. Check if resting sell order fills when price reaches 40c (2x)
    m3_fill = {"ticker": "M-2N-WIN", "status": "open", "yes_ask": 60, "no_ask": 40, "minute": 12}
    sold = trader.check_open_positions(markets=[m3_fill])
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == 20  # 40c - 20c = +20c
    assert db.get_all_positions(strategy="sell_2n")[0]["status"] == PositionStatus.SETTLED

    db.close()


def test_sell_2n_timing_helpers():
    # 5m window is for sell_3n; 10m window is for sell_2n
    market = {"ticker": "M-TEST", "close_time": "2026-11-01T10:15:00Z"}
    assert is_entry_time_5m(market, now="2026-11-01T10:05:00Z")
    assert not is_entry_time_10m(market, now="2026-11-01T10:05:00Z")

    assert not is_entry_time_5m(market, now="2026-11-01T10:10:00Z")
    assert is_entry_time_10m(market, now="2026-11-01T10:10:00Z")

    assert get_strategy_resting_multiplier("sell_3n") == 3
    assert get_strategy_resting_multiplier("sell_2n") == 2
    assert get_strategy_resting_multiplier("sell_2n_5m") == 2
    assert get_strategy_resting_multiplier("random") is None


def test_strategy_sell_2n_5m_trader_flow():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_2n_5m")

    # 1. Reject if before 5 minutes past (e.g. at minute 2)
    m1 = {"ticker": "M-5M-EARLY", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 2}
    assert trader.process_market(m1) is None

    # 2. Enter at 5 min past (same as sell_3n) when contract < 33c (e.g. YES @ 25c)
    m2 = {"ticker": "M-5M-WIN", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 5}
    order = trader.process_market(m2)
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price"] == 25
    # Places resting sell order for 2x (2 * 25c = 50c) instead of 3x
    assert order["resting_price_cents"] == 50
    assert order["resting_order"]["action"] == "sell"
    assert order["resting_order"]["price"] == 50

    positions = db.get_all_positions(strategy="sell_2n_5m")
    assert len(positions) == 1
    assert positions[0]["status"] == PositionStatus.OPEN
    assert positions[0]["resting_order_id"] != ""

    # 3. Check if resting sell order fills when price reaches 50c (2x)
    m2_fill = {"ticker": "M-5M-WIN", "status": "open", "yes_ask": 50, "no_ask": 50, "minute": 8}
    sold = trader.check_open_positions(markets=[m2_fill])
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == 25  # 50c - 25c = +25c
    assert db.get_all_positions(strategy="sell_2n_5m")[0]["status"] == PositionStatus.SETTLED

    db.close()
