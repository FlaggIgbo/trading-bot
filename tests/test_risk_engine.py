"""
Tests for the simplified risk engine.
Rule:
If market is open (and when it opens), randomly bet 1 contract on one side ("YES", "NO").
Never sell, just hold it and hope for the best.
"""

from unittest.mock import MagicMock
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
    mock_client = MagicMock()
    mock_client.place_order.return_value = {"status": "ok"}

    engine = RiskEngine(client=mock_client)
    market = {"ticker": "MARKET-A", "status": "open", "yes_ask": 40, "no_ask": 60}

    result = engine.process_market(market)
    assert result == {"status": "ok"}
    assert "MARKET-A" in engine.bet_markets

    # Verify order was placed for count=1
    call_args = mock_client.place_order.call_args[1]
    assert call_args["ticker"] == "MARKET-A"
    assert call_args["count"] == 1
    assert call_args["side"] in ["YES", "NO"]


def test_does_not_bet_on_closed_market():
    mock_client = MagicMock()
    engine = RiskEngine(client=mock_client)
    closed_market = {"ticker": "MARKET-CLOSED", "status": "closed"}

    result = engine.process_market(closed_market)
    assert result is None
    assert "MARKET-CLOSED" not in engine.bet_markets
    mock_client.place_order.assert_not_called()


def test_does_not_bet_twice_on_same_market():
    mock_client = MagicMock()
    mock_client.place_order.return_value = {"status": "ok"}

    engine = RiskEngine(client=mock_client)
    market = {"ticker": "MARKET-A", "status": "open"}

    # First time market is seen, bet should be placed
    result1 = engine.process_market(market)
    assert result1 is not None
    assert mock_client.place_order.call_count == 1

    # Second time market is seen while still open, do not bet again
    result2 = engine.process_market(market)
    assert result2 is None
    assert mock_client.place_order.call_count == 1


def test_never_sells():
    # Verify that the engine has no sell methods
    engine = RiskEngine(client=None)
    assert not hasattr(engine, "sell")
    assert not hasattr(engine, "liquidate")
    assert not hasattr(engine, "stop_loss")
    assert not hasattr(engine, "take_profit")
