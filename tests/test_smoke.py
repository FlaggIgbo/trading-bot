"""
Smoke tests for basic bot initialization and flow.
"""

from ingestion.client import KalshiClient
from risk_engine.engine import RiskEngine


def test_smoke_initialization():
    # Test that client and engine can be initialized
    client = KalshiClient(live_orders=False)
    engine = RiskEngine(client=client)

    assert client is not None
    assert engine is not None
    assert len(engine.bet_markets) == 0


def test_smoke_cycle_with_mock_market():
    # Test a basic simulated cycle
    client = KalshiClient(live_orders=False)
    engine = RiskEngine(client=client)

    sample_market = {
        "ticker": "TEST-TICKER-1",
        "status": "open",
        "yes_ask": 50,
        "no_ask": 50,
    }

    results = engine.evaluate_all([sample_market])
    assert len(results) == 1
    assert "TEST-TICKER-1" in engine.bet_markets
    assert results[0]["status"] == "simulated"
