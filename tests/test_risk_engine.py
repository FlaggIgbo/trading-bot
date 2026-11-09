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
    get_strategy_entry_minute,
    get_strategy_max_entry_price,
    get_strategy_resting_multiplier,
    is_checkin_time,
    is_entry_time,
    is_entry_time_10m,
    is_entry_time_5m,
    is_entry_time_ym,
    is_sell_strategy,
    sell_1_5n,
    sell_1_5n_5m,
    sell_2_5n,
    sell_2n,
    sell_2n_5m,
    sell_3n,
    sell_Xn_Ym,
    sell_xn_ym,
    sell_Xn_Ym_Zp,
    sell_xn_ym_zp,
    should_sell,
    should_stop_loss,
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


def test_sell_xn_ym_parameterized():
    # Direct calling with market and parameters
    m_yes = {"yes_ask": 25, "no_ask": 75}
    m_no = {"yes_ask": 80, "no_ask": 20}
    m_none_33 = {"yes_ask": 50, "no_ask": 50}
    m_none_66 = {"yes_ask": 70, "no_ask": 70}

    # For n=2 (z=50): 25 is < 50
    assert sell_Xn_Ym(m_yes, n=2, y=5) == PositionSide.YES
    # For n=3 (z=33): 20 is < 33
    assert sell_Xn_Ym(m_no, n=3, y=5) == PositionSide.NO
    # For n=3 (z=33): 50 is >= 33 -> None
    assert sell_Xn_Ym(m_none_33, n=3, y=5) is None
    # For n=1.5 (default z=66): 50 is < 66 -> picks YES (50 <= 50)
    assert sell_Xn_Ym(m_none_33, n=1.5, y=5) == PositionSide.YES
    # For n=1.5 (default z=66): 70 is >= 66 -> None
    assert sell_Xn_Ym(m_none_66, n=1.5, y=5) is None
    # If z is explicitly passed (e.g. z=33), overrides default 100/n
    assert sell_Xn_Ym(m_none_33, n=1.5, y=5, z=33) is None
    assert sell_Xn_Ym(m_none_33, n=2, y=5, zp=33) is None

    # Convenience aliases call sell_Xn_Ym with the configured params
    assert sell_3n(m_yes) == PositionSide.YES
    assert sell_2n(m_yes) == PositionSide.YES
    assert sell_2n_5m(m_yes) == PositionSide.YES
    assert sell_1_5n(m_yes) == PositionSide.YES
    assert sell_2_5n(m_no) == PositionSide.NO

    # Positional factory usage: sell_Xn_Ym(n, y, z) returns a bound strategy callable
    bound_strat = sell_Xn_Ym(1.5, 5)
    assert callable(bound_strat)
    assert bound_strat.n == 1.5
    assert bound_strat.y == 5
    assert bound_strat.z == 66  # default 100/1.5
    assert bound_strat(m_yes) == PositionSide.YES
    assert bound_strat(m_no) == PositionSide.NO

    # Positional factory with custom z
    bound_strat_z = sell_Xn_Ym(2, 5, 30)
    assert bound_strat_z.z == 30
    assert bound_strat_z(m_yes) == PositionSide.YES  # 25 < 30
    assert bound_strat_z({"yes_ask": 35, "no_ask": 65}) is None  # 35 >= 30


def test_strategy_sell_1_5n_trader_flow():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_1.5n")

    # 1. Reject if before 5 minutes past (e.g. at minute 2)
    m1 = {"ticker": "M-1.5N-EARLY", "status": "open", "yes_ask": 20, "no_ask": 80, "minute": 2}
    assert trader.process_market(m1) is None

    # 2. Enter at 5 min past when contract < 33c (e.g. YES @ 20c)
    m2 = {"ticker": "M-1.5N-WIN", "status": "open", "yes_ask": 20, "no_ask": 80, "minute": 5}
    order = trader.process_market(m2)
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price"] == 20
    # Places resting sell order for 1.5x (1.5 * 20c = 30c)
    assert order["resting_price_cents"] == 30
    assert order["resting_order"]["action"] == "sell"
    assert order["resting_order"]["price"] == 30

    positions = db.get_all_positions(strategy="sell_1.5n")
    assert len(positions) == 1
    assert positions[0]["status"] == PositionStatus.OPEN
    assert positions[0]["resting_order_id"] != ""

    # 3. Check if resting sell order fills when price reaches 30c (1.5x)
    m2_fill = {"ticker": "M-1.5N-WIN", "status": "open", "yes_ask": 30, "no_ask": 70, "minute": 7}
    sold = trader.check_open_positions(markets=[m2_fill])
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == 10  # 30c - 20c = +10c
    assert db.get_all_positions(strategy="sell_1.5n")[0]["status"] == PositionStatus.SETTLED

    db.close()


def test_strategy_sell_2_5n_trader_flow():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_2.5n")

    # 1. Reject if before 5 minutes past (e.g. at minute 3)
    m1 = {"ticker": "M-2.5N-EARLY", "status": "open", "yes_ask": 80, "no_ask": 20, "minute": 3}
    assert trader.process_market(m1) is None

    # 2. Enter at 5 min past when contract < 33c (e.g. NO @ 20c)
    m2 = {"ticker": "M-2.5N-WIN", "status": "open", "yes_ask": 80, "no_ask": 20, "minute": 6}
    order = trader.process_market(m2)
    assert order is not None
    assert order["side"] == PositionSide.NO
    assert order["price"] == 20
    # Places resting sell order for 2.5x (2.5 * 20c = 50c)
    assert order["resting_price_cents"] == 50
    assert order["resting_order"]["action"] == "sell"
    assert order["resting_order"]["price"] == 50

    positions = db.get_all_positions(strategy="sell_2.5n")
    assert len(positions) == 1
    assert positions[0]["status"] == PositionStatus.OPEN
    assert positions[0]["resting_order_id"] != ""

    # 3. Check if resting sell order fills when price reaches 50c (2.5x)
    m2_fill = {"ticker": "M-2.5N-WIN", "status": "open", "yes_ask": 50, "no_ask": 50, "minute": 9}
    sold = trader.check_open_positions(markets=[m2_fill])
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == 30  # 50c - 20c = +30c
    assert db.get_all_positions(strategy="sell_2.5n")[0]["status"] == PositionStatus.SETTLED

    db.close()


def test_strategy_resting_multiplier_and_entry_minute():
    # Resting multipliers
    assert get_strategy_resting_multiplier("sell_3n") == 3
    assert get_strategy_resting_multiplier("sell_2n") == 2
    assert get_strategy_resting_multiplier("sell_2n_5m") == 2
    assert get_strategy_resting_multiplier("sell_1.5n") == 1.5
    assert get_strategy_resting_multiplier("sell_1_5n") == 1.5
    assert get_strategy_resting_multiplier("sell_1.5n_35p") == 1.5
    assert get_strategy_resting_multiplier("sell_1_5n_35p") == 1.5
    assert get_strategy_resting_multiplier("sell_2.5n") == 2.5
    assert get_strategy_resting_multiplier("sell_2_5n") == 2.5
    assert get_strategy_resting_multiplier("random") is None
    assert get_strategy_resting_multiplier("last_5") is None

    # Entry minute
    assert get_strategy_entry_minute("sell_3n") == 5
    assert get_strategy_entry_minute("sell_2n") == 10
    assert get_strategy_entry_minute("sell_2n_5m") == 5
    assert get_strategy_entry_minute("sell_1.5n") == 5
    assert get_strategy_entry_minute("sell_1.5n_35p") == 5
    assert get_strategy_entry_minute("sell_2.5n") == 5

    # Max entry price (Zp default 100/n, or explicit override)
    assert get_strategy_max_entry_price("sell_3n") == 33
    assert get_strategy_max_entry_price("sell_2n") == 50
    assert get_strategy_max_entry_price("sell_2n_5m") == 50
    assert get_strategy_max_entry_price("sell_1.5n") == 66
    assert get_strategy_max_entry_price("sell_1.5n_5m") == 35
    assert get_strategy_max_entry_price("sell_1.5n_5m_35p") == 35
    assert get_strategy_max_entry_price("sell_1.5n_35p") == 35
    assert get_strategy_max_entry_price("sell_2.5n") == 40
    assert get_strategy_max_entry_price("sell_2n_5m_33p") == 33
    assert get_strategy_max_entry_price("sell_1.5n_45p") == 45
    assert get_strategy_max_entry_price("random") == 33

    # is_sell_strategy helper
    assert is_sell_strategy("sell_3n")
    assert is_sell_strategy("sell_2n")
    assert is_sell_strategy("sell_2n_5m")
    assert is_sell_strategy("sell_1.5n")
    assert is_sell_strategy("sell_1.5n_35p")
    assert is_sell_strategy("sell_2.5n")
    assert not is_sell_strategy("random")
    assert not is_sell_strategy("last_5")


def test_should_stop_loss():
    # Minute < 10: never triggers stop loss regardless of price
    assert not should_stop_loss(entry_price=35, current_price=10, minute=5)
    assert not should_stop_loss(entry_price=35, current_price=15, minute=9)
    assert not should_stop_loss(entry_price=50, current_price=10, minute=None)

    # Minute >= 10: triggers if current_price <= 25c
    assert should_stop_loss(entry_price=35, current_price=25, minute=10)
    assert should_stop_loss(entry_price=35, current_price=20, minute=10)
    assert should_stop_loss(entry_price=35, current_price=10, minute=12)

    # Minute >= 10: triggers if current_price < 50% of entry_price
    assert should_stop_loss(entry_price=60, current_price=28, minute=10)  # 28c < 30c (50% of 60c)
    assert should_stop_loss(entry_price=54, current_price=26, minute=11)  # 26c < 27c (50% of 54c)

    # Minute >= 10: does NOT trigger if > 25c and >= 50% entry price
    assert not should_stop_loss(entry_price=35, current_price=26, minute=10)  # 26 > 25 and 26 >= 17.5
    assert not should_stop_loss(entry_price=40, current_price=28, minute=10)  # 28 > 25 and 28 >= 20
    assert not should_stop_loss(entry_price=60, current_price=35, minute=10)  # 35 > 25 and 35 >= 30

    # Missing / None values should safely return False
    assert not should_stop_loss(entry_price=None, current_price=10, minute=10)
    assert not should_stop_loss(entry_price=35, current_price=None, minute=10)


def test_trader_stop_loss_execution():
    db = Database(":memory:")
    canceled_orders = []

    class MockClient(KalshiClient):
        def __init__(self):
            super().__init__(live_orders=False)

        def cancel_order(self, order_id):
            canceled_orders.append(order_id)
            return {"order_id": order_id, "status": "canceled"}

    client = MockClient()
    trader_sl = Trader(client=client, db=db, strategy="sell_1.5n_5m", stop_loss=True)

    # 1. Enter position at minute 5
    market_entry = {
        "ticker": "M-SL-TEST",
        "status": "open",
        "yes_ask": 30,
        "no_ask": 70,
        "minute": 5,
        "open_time": "2026-11-01T10:00:00Z",
    }
    order = trader_sl.process_market(market_entry)
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price"] == 30
    assert order["resting_price_cents"] == 45
    resting_id = order["resting_order_id"]
    assert resting_id != ""

    # 2. Minute 8: price drops to 20c, but minute < 10 -> hold
    market_m8 = {"ticker": "M-SL-TEST", "status": "open", "yes_ask": 20, "no_ask": 80, "minute": 8}
    sold = trader_sl.check_open_positions(markets=[market_m8])
    assert len(sold) == 0
    assert len(canceled_orders) == 0

    # 3. Minute 10: price drops to 20c (<= 25c) -> stop loss triggers!
    market_m10 = {"ticker": "M-SL-TEST", "status": "open", "yes_ask": 20, "no_ask": 80, "minute": 10}
    sold = trader_sl.check_open_positions(markets=[market_m10])
    assert len(sold) == 1
    assert sold[0]["reason"] == "stop_loss"
    assert sold[0]["sell_price"] == 20
    assert sold[0]["pnl_cents"] == -10  # 20c - 30c = -10c
    assert resting_id in canceled_orders

    # Verify db status is settled
    positions = db.get_all_positions(strategy="sell_1.5n_5m")
    assert len(positions) == 1
    assert positions[0]["status"] == PositionStatus.SETTLED
    assert positions[0]["pnl_cents"] == -10

    # 4. Now test with stop_loss=False -> should NOT trigger stop loss at minute 10
    db2 = Database(":memory:")
    canceled_orders.clear()
    trader_no_sl = Trader(client=client, db=db2, strategy="sell_1.5n_5m", stop_loss=False)

    order2 = trader_no_sl.process_market(market_entry)
    assert order2 is not None

    sold_no_sl = trader_no_sl.check_open_positions(markets=[market_m10])
    assert len(sold_no_sl) == 0
    assert len(canceled_orders) == 0
    pos_no_sl = db2.get_all_positions(strategy="sell_1.5n_5m")
    assert pos_no_sl[0]["status"] == PositionStatus.OPEN

    db.close()
    db2.close()


def test_sell_1_5n_5m_strategy():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_1.5n_5m")

    # Reject at minute 3
    assert trader.process_market({"ticker": "M-1.5N-5M", "status": "open", "yes_ask": 30, "no_ask": 70, "minute": 3}) is None

    # Reject at minute 5 if ask > 35c (e.g. 40c)
    assert trader.process_market({"ticker": "M-1.5N-5M-EXPENSIVE", "status": "open", "yes_ask": 40, "no_ask": 60, "minute": 5}) is None

    # Accept at minute 5 with ask <= 35c (e.g. 30c)
    order = trader.process_market({"ticker": "M-1.5N-5M", "status": "open", "yes_ask": 30, "no_ask": 70, "minute": 5})
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price"] == 30
    assert order["resting_price_cents"] == 45  # 1.5 * 30c = 45c
    db.close()


def test_sell_2n_5m_35p_strategy():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db, strategy="sell_2n_5m_35p", stop_loss=True)

    # 1. Reject at minute 3 (before minute 5)
    assert trader.process_market({"ticker": "M-2N-5M-EARLY", "status": "open", "yes_ask": 30, "no_ask": 70, "minute": 3}) is None

    # 2. Reject at minute 5 if ask > 35c (e.g. 40c)
    assert trader.process_market({"ticker": "M-2N-5M-EXPENSIVE", "status": "open", "yes_ask": 40, "no_ask": 60, "minute": 5}) is None

    # 3. Accept at minute 5 with ask <= 35c (e.g. 28c)
    order = trader.process_market({"ticker": "M-2N-5M-WIN", "status": "open", "yes_ask": 28, "no_ask": 72, "minute": 5})
    assert order is not None
    assert order["side"] == PositionSide.YES
    assert order["price"] == 28
    assert order["resting_price_cents"] == 56  # 2.0 * 28c = 56c

    # 4. Minute 10 stop loss trigger when price drops <= 25c or < 50%
    market_sl = {"ticker": "M-2N-5M-WIN", "status": "open", "yes_ask": 12, "no_ask": 88, "minute": 10}
    sold = trader.check_open_positions(markets=[market_sl])
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == -16  # 12c - 28c = -16c
    db.close()


def test_live_order_reconciliation_and_error_handling():
    db = Database(":memory:")

    class MockLiveKalshiClient:
        def __init__(self):
            self.live_orders = True
            self.orders = {}
            self.placed_orders = []

        def place_order(self, ticker, side, count=1, price_cents=50, client_order_id=None, action="buy", time_in_force="good_till_canceled", reduce_only=False):
            order_id = f"order_{len(self.placed_orders) + 1}"
            payload = {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "ticker": ticker,
                "side": side,
                "count": count,
                "price": price_cents,
                "action": action,
                "time_in_force": time_in_force,
                "reduce_only": reduce_only,
                "status": "executed",
            }
            self.placed_orders.append(payload)
            self.orders[order_id] = payload
            return {"order": payload, "order_id": order_id, "client_order_id": client_order_id}

        def get_order(self, order_id):
            return self.orders.get(order_id)

        def cancel_order(self, order_id):
            if order_id in self.orders:
                self.orders[order_id]["status"] = "canceled"
            return {"order_id": order_id, "status": "canceled"}

    client = MockLiveKalshiClient()
    trader = Trader(client=client, db=db, strategy="sell_2n_5m_35p", stop_loss=True)

    # 1. Enter position with live_orders=True
    market_entry = {"ticker": "KXBTC-LIVE", "status": "open", "yes_ask": 30, "no_ask": 70, "minute": 5}
    order = trader.process_market(market_entry)
    assert order is not None
    assert order["resting_order_id"] != ""
    resting_id = order["resting_order_id"]

    # In mock, resting order was placed with GTC
    resting_call = client.placed_orders[1]
    assert resting_call["action"] == "sell"
    assert resting_call["price"] == 60
    assert resting_call["time_in_force"] == "good_till_canceled"

    # Make the resting order "resting" (unfilled) on Kalshi
    client.orders[resting_id]["status"] = "resting"

    # 2. Check open positions when market fluctuates: order is still "resting" on Kalshi -> DO NOT SETTLE
    market_fluct = {"ticker": "KXBTC-LIVE", "status": "open", "yes_bid": 65, "no_ask": 35, "minute": 7}
    sold = trader.check_open_positions(markets=[market_fluct])
    assert len(sold) == 0
    assert db.get_all_positions(strategy="sell_2n_5m_35p")[0]["status"] == PositionStatus.OPEN

    # 3. Simulate resting order filling on Kalshi: status becomes "executed"
    client.orders[resting_id]["status"] = "executed"
    sold = trader.check_open_positions(markets=[market_fluct])
    assert len(sold) == 1
    assert sold[0]["pnl_cents"] == 30  # 60c - 30c = 30c
    assert db.get_all_positions(strategy="sell_2n_5m_35p")[0]["status"] == PositionStatus.SETTLED

    # 4. Verify that failed sell_position does NOT settle position in SQLite
    client.orders[resting_id]["status"] = "resting"
    db.conn.execute("UPDATE positions SET status = 'OPEN'")
    db.conn.commit()

    # Make client.place_order return error on next call
    client.place_order = lambda *args, **kwargs: {"error": "insufficient_balance"}
    pos = db.get_all_positions(strategy="sell_2n_5m_35p")[0]
    result = trader.sell_position(pos, current_price_cents=10, reason="stop_loss")
    assert result is None  # Sell rejected
    # Position must STILL be OPEN in SQLite!
    assert db.get_all_positions(strategy="sell_2n_5m_35p")[0]["status"] == PositionStatus.OPEN

    db.close()


def test_entry_fill_verification_and_stop_loss_ioc_check():
    db = Database(":memory:")

    class MockLiveExchange:
        def __init__(self):
            self.live_orders = True
            self.orders = {}
            self.canceled_order_ids = []
            self.entry_status = "resting"
            self.entry_fill_count = "0.00"
            self.actual_taker_cost = "0.000000"
            self.sl_status = "canceled"
            self.sl_fill_count = "0.00"

        def place_order(self, ticker, side, count=1, price_cents=50, client_order_id=None, action="buy", time_in_force="good_till_canceled", reduce_only=False):
            order_id = f"ord_{len(self.orders) + 1}"
            if action == "buy":
                status = self.entry_status
                fill_count = self.entry_fill_count
                taker_cost = self.actual_taker_cost
            else:
                status = self.sl_status
                fill_count = self.sl_fill_count
                taker_cost = "0.000000"

            payload = {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "ticker": ticker,
                "side": side,
                "count": count,
                "price": price_cents,
                "action": action,
                "time_in_force": time_in_force,
                "reduce_only": reduce_only,
                "status": status,
                "fill_count_fp": fill_count,
                "taker_fill_cost_dollars": taker_cost,
            }
            self.orders[order_id] = payload
            return {"order": payload, "order_id": order_id, "client_order_id": client_order_id}

        def get_order(self, order_id):
            return self.orders.get(order_id)

        def cancel_order(self, order_id):
            self.canceled_order_ids.append(order_id)
            if order_id in self.orders:
                self.orders[order_id]["status"] = "canceled"
            return {"order_id": order_id, "status": "canceled"}

    mock_ex = MockLiveExchange()
    trader = Trader(client=mock_ex, db=db, strategy="sell_2n_5m_35p", stop_loss=True)

    # 1. Entry order that stays resting and never fills
    market = {"ticker": "KXTEST-1", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 5}
    order_res = trader.process_market(market)
    assert order_res is not None
    assert order_res.get("status") == "unfilled"
    # Unfilled resting entry order was canceled on the exchange
    assert len(mock_ex.canceled_order_ids) == 1
    # NO position was opened in SQLite
    assert len(db.get_all_positions()) == 0

    # 2. Entry order that executes immediately with real Kalshi fill cost (e.g. 18c instead of 25c)
    mock_ex.entry_status = "executed"
    mock_ex.entry_fill_count = "1.00"
    mock_ex.actual_taker_cost = "0.180000"
    market_2 = {"ticker": "KXTEST-2", "status": "open", "yes_ask": 25, "no_ask": 75, "minute": 5}
    order_res_2 = trader.process_market(market_2)
    assert order_res_2 is not None
    # Position was opened in SQLite with the actual fill price (18c)
    positions = db.get_all_positions()
    assert len(positions) == 1
    assert positions[0]["ticker"] == "KXTEST-2"
    assert positions[0]["price_cents"] == 18
    assert positions[0]["status"] == PositionStatus.OPEN

    # 3. Stop-loss IOC order is canceled by Kalshi (no bid liquidity)
    mock_ex.sl_status = "canceled"
    mock_ex.sl_fill_count = "0.00"
    pos = positions[0]
    sl_res = trader.sell_position(pos, current_price_cents=9, reason="stop_loss")
    # Must return None and keep position OPEN in SQLite!
    assert sl_res is None
    assert db.get_all_positions()[0]["status"] == PositionStatus.OPEN

    # 4. Stop-loss IOC order executes when liquidity appears
    mock_ex.sl_status = "executed"
    mock_ex.sl_fill_count = "1.00"
    mock_ex.orders.clear()
    sl_res_2 = trader.sell_position(pos, current_price_cents=9, reason="stop_loss")
    assert sl_res_2 is not None
    assert sl_res_2["status"] == PositionStatus.SETTLED
    assert db.get_all_positions()[0]["status"] == PositionStatus.SETTLED
    assert db.get_all_positions()[0]["pnl_cents"] == -9  # 9c - 18c = -9c

    db.close()




