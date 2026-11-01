"""
Smoke tests for basic bot initialization and flow (Scanner -> Trader -> Settler).
"""

from db import Database
from ingestion.api.client import KalshiClient
from ingestion.scanner import Scanner
from risk_engine.engine import RiskEngine
from risk_engine.settler import Settler


def test_smoke_initialization():
    # Test that database, client, scanner, trader, and settler can be initialized
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    scanner = Scanner(client=client, db=db)
    engine = RiskEngine(client=client, db=db)
    settler = Settler(client=client, db=db)

    assert client is not None
    assert scanner is not None
    assert engine is not None
    assert settler is not None
    assert len(engine.bet_markets) == 0
    db.close()


def test_smoke_cycle_with_mock_market():
    # Test a basic simulated cycle through SQLite
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    engine = RiskEngine(client=client, db=db)

    sample_market = {
        "ticker": "TEST-TICKER-1",
        "status": "open",
        "yes_ask": 50,
        "no_ask": 50,
        "close_time": "2026-11-01T15:00:00Z",
    }

    db.upsert_markets([sample_market])
    results = engine.trade()
    assert len(results) == 1
    assert "TEST-TICKER-1" in engine.bet_markets
    assert results[0]["status"] == "simulated"
    assert len(db.get_all_positions()) == 1
    db.close()
