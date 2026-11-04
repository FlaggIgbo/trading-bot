"""
Smoke test for the core bot pipeline (Scanner -> Trader -> Settler).
"""

from db import Database, PositionStatus
from ingestion.api.client import KalshiClient
from risk_engine.engine import Trader
from risk_engine.settler import Settler


def test_smoke_pipeline():
    db = Database(":memory:")
    client = KalshiClient(live_orders=False)
    trader = Trader(client=client, db=db)

    db.upsert_markets([{"ticker": "TEST-1", "status": "open", "yes_ask": 40, "no_ask": 60, "close_time": "2026-01-01T00:00:00Z"}])
    assert len(trader.trade()) == 1
    assert db.get_all_positions()[0]["status"] == PositionStatus.OPEN

    client.get_market = lambda ticker: {"ticker": ticker, "status": "finalized", "result": "yes"}
    settled = Settler(client=client, db=db).settle(now_iso="2026-01-02T00:00:00Z")
    assert len(settled) == 1
    assert db.get_all_positions()[0]["status"] == PositionStatus.SETTLED
    db.close()
