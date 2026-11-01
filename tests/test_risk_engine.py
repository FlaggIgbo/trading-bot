"""
Tests for the Trader (RiskEngine) and Settler with SQLite storage.
Rule:
If market is open (and when it opens), randomly bet 1 contract on one side ("YES", "NO").
Never sell, just hold it until resolution and compute PnL.
"""

from db import Database
from ingestion.api.client import KalshiClient
from risk_engine.engine import RiskEngine, Trader
from risk_engine.settler import Settler


def test_is_market_open():
    engine = RiskEngine(client=None)

    assert engine.is_market_open({"status": "open"}) is True
    assert engine.is_market_open({"status": "active"}) is True
    assert engine.is_market_open({"status": "closed"}) is False
    assert engine.is_market_open({"status": "settled"}) is False
    assert engine.is_market_open({}) is False


def test_pick_random_side():
    engine = RiskEngine(client=None)
    for _ in range(20):
        side = engine.pick_random_side()
        assert side in ["YES", "NO"]


def test_bets_1_contract_on_open_market_and_writes_to_db():
    db = Database(":memory:")
    client = KalshiClient()
    trader = Trader(client=client, db=db)

    market = {
        "ticker": "KXBTC15M-26OCT312200-00",
        "status": "open",
        "yes_ask": 40,
        "no_ask": 60,
        "close_time": "2026-10-31T22:00:00Z",
    }
    db.upsert_markets([market])

    orders = trader.trade()
    assert len(orders) == 1
    assert "KXBTC15M-26OCT312200-00" in trader.bet_markets

    positions = db.get_all_positions()
    assert len(positions) == 1
    assert positions[0]["ticker"] == "KXBTC15M-26OCT312200-00"
    assert positions[0]["status"] == "OPEN"
    assert positions[0]["count"] == 1
    assert positions[0]["side"] in ["YES", "NO"]
    db.close()


def test_does_not_bet_on_closed_market():
    client = KalshiClient()
    engine = RiskEngine(client=client)
    closed_market = {"ticker": "KXBTC15M-CLOSED", "status": "closed"}

    result = engine.process_market(closed_market)
    assert result is None
    assert "KXBTC15M-CLOSED" not in engine.bet_markets


def test_does_not_bet_twice_on_same_market():
    db = Database(":memory:")
    client = KalshiClient()
    engine = RiskEngine(client=client, db=db)
    market = {"ticker": "KXBTC15M-26OCT312200-00", "status": "open"}

    # First time market is seen, bet should be placed
    result1 = engine.process_market(market)
    assert result1 is not None
    assert len(engine.bet_markets) == 1

    # Second time market is seen while still open, do not bet again
    result2 = engine.process_market(market)
    assert result2 is None
    assert len(engine.bet_markets) == 1
    db.close()


def test_never_sells():
    # Verify that the engine has no sell methods
    engine = RiskEngine(client=None)
    assert not hasattr(engine, "sell")
    assert not hasattr(engine, "liquidate")
    assert not hasattr(engine, "stop_loss")
    assert not hasattr(engine, "take_profit")


def test_only_bets_on_binary_markets():
    client = KalshiClient()
    engine = RiskEngine(client=client)

    # Multi-strike ladder market (e.g. S&P 500 or Nasdaq above/below strike) must be skipped
    multi_strike = {"ticker": "KXINXU-26NOV02H1600-T7499.9999", "event_ticker": "KXINXU-26NOV02H1600", "status": "open"}
    assert engine.process_market(multi_strike) is None

    # Non-binary market_type must be skipped
    non_binary = {"ticker": "KXBTC15M-26OCT312200-00", "market_type": "scalar", "status": "open"}
    assert engine.process_market(non_binary) is None

    # Multiple markets sharing the same event_ticker must be skipped
    multi_event_markets = [
        {"ticker": "KXEVENT-1", "event_ticker": "KXEVENT", "status": "open"},
        {"ticker": "KXEVENT-2", "event_ticker": "KXEVENT", "status": "open"},
    ]
    assert engine.evaluate_all(multi_event_markets) == []

    # True standalone binary market (e.g. BTC 15-min) is bet on
    binary_market = {"ticker": "KXBTC15M-26OCT312200-00", "event_ticker": "KXBTC15M-26OCT312200", "market_type": "binary", "status": "open"}
    res = engine.evaluate_all([binary_market])
    assert len(res) == 1
    assert "KXBTC15M-26OCT312200-00" in engine.bet_markets


def test_settler_checks_resolution_and_computes_pnl():
    db = Database(":memory:")

    # Insert two expired OPEN positions (one winning YES bet at 40c, one losing NO bet at 60c)
    db.insert_open_position(
        client_order_id="order_win_1",
        ticker="KXBTC15M-WIN",
        side="YES",
        count=1,
        price_cents=40,
        close_time="2026-10-31T10:00:00Z",
    )
    db.insert_open_position(
        client_order_id="order_loss_2",
        ticker="KXBTC15M-LOSS",
        side="NO",
        count=1,
        price_cents=60,
        close_time="2026-10-31T10:00:00Z",
    )
    # And one future position that shouldn't be checked yet
    db.insert_open_position(
        client_order_id="order_future_3",
        ticker="KXBTC15M-FUTURE",
        side="YES",
        count=1,
        price_cents=50,
        close_time="2099-12-31T23:59:59Z",
    )

    class StubResolutionClient:
        def get_market(self, ticker):
            # Return Kalshi v2 finalized market with result="yes"
            return {"ticker": ticker, "status": "finalized", "result": "yes"}

    settler = Settler(client=StubResolutionClient(), db=db)
    settled = settler.settle(now_iso="2026-11-01T00:00:00Z")

    assert len(settled) == 2

    positions = {p["client_order_id"]: p for p in db.get_all_positions()}
    # Winning YES at 40c -> +60c PnL
    assert positions["order_win_1"]["status"] == "SETTLED"
    assert positions["order_win_1"]["pnl_cents"] == 60
    # Losing NO at 60c when result is YES -> -60c PnL
    assert positions["order_loss_2"]["status"] == "SETTLED"
    assert positions["order_loss_2"]["pnl_cents"] == -60
    # Future position remains OPEN
    assert positions["order_future_3"]["status"] == "OPEN"
    assert positions["order_future_3"]["pnl_cents"] is None
    db.close()


def test_sqlite_differentiates_production_and_demo_entries(tmp_path):
    db_file = str(tmp_path / "shared_state.db")
    demo_db = Database(db_file, environment="demo.kalshi.co")
    prod_db = Database(db_file, environment="production")

    # Upsert same ticker with different prices in demo vs production
    demo_db.upsert_markets([{"ticker": "KXBTC15M-SHARED", "status": "open", "yes_ask": 42, "no_ask": 58}])
    prod_db.upsert_markets([{"ticker": "KXBTC15M-SHARED", "status": "open", "yes_ask": 75, "no_ask": 25}])

    demo_markets = demo_db.get_open_markets()
    prod_markets = prod_db.get_open_markets()
    assert len(demo_markets) == 1
    assert demo_markets[0]["environment"] == "demo.kalshi.co"
    assert demo_markets[0]["yes_ask"] == 42
    assert len(prod_markets) == 1
    assert prod_markets[0]["environment"] == "production"
    assert prod_markets[0]["yes_ask"] == 75

    # Insert open position in demo.kalshi.co only
    demo_db.insert_open_position(
        client_order_id="demo_order_1",
        ticker="KXBTC15M-SHARED",
        side="YES",
        count=1,
        price_cents=42,
        close_time="2026-10-31T10:00:00Z",
    )

    # Demo sees the position; production does not collide with demo positions
    assert "KXBTC15M-SHARED" in demo_db.get_positioned_tickers()
    assert "KXBTC15M-SHARED" not in prod_db.get_positioned_tickers()
    assert len(demo_db.get_all_positions()) == 1
    assert demo_db.get_all_positions()[0]["environment"] == "demo.kalshi.co"
    assert len(prod_db.get_all_positions()) == 0
    assert len(prod_db.get_all_positions(environment="*")) == 1

    demo_db.close()
    prod_db.close()
