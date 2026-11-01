"""
Tests for the simplified risk engine.
Rule:
If market is open (and when it opens), randomly bet 1 contract on one side ("YES", "NO").
Never sell, just hold it and hope for the best.
"""

from ingestion.client import KalshiClient
from risk_engine.engine import RiskEngine


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


def test_bets_1_contract_on_open_market():
    client = KalshiClient()
    engine = RiskEngine(client=client)
    market = {"ticker": "KXBTC15M-26OCT312200-00", "status": "open", "yes_ask": 40, "no_ask": 60}

    result = engine.process_market(market)
    assert result is not None
    assert "KXBTC15M-26OCT312200-00" in engine.bet_markets
    assert result["ticker"] == "KXBTC15M-26OCT312200-00"
    assert result["count"] == 1
    assert result["side"] in ["YES", "NO"]


def test_does_not_bet_on_closed_market():
    client = KalshiClient()
    engine = RiskEngine(client=client)
    closed_market = {"ticker": "KXBTC15M-CLOSED", "status": "closed"}

    result = engine.process_market(closed_market)
    assert result is None
    assert "KXBTC15M-CLOSED" not in engine.bet_markets


def test_does_not_bet_twice_on_same_market():
    client = KalshiClient()
    engine = RiskEngine(client=client)
    market = {"ticker": "KXBTC15M-26OCT312200-00", "status": "open"}

    # First time market is seen, bet should be placed
    result1 = engine.process_market(market)
    assert result1 is not None
    assert len(engine.bet_markets) == 1

    # Second time market is seen while still open, do not bet again
    result2 = engine.process_market(market)
    assert result2 is None
    assert len(engine.bet_markets) == 1


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
