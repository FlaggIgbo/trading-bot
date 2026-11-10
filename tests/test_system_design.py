"""
Unit tests for System Design Improvements:
- Pure Risk Rules (unit testable without I/O)
- Strategy Protocol & functions (e.g. strategy_last_9 requiring 9 resolutions)
- Broker implementations (KalshiBroker & SimulatedBroker)
- Decision dataclass
- TradeEngine orchestration & run_cycle()
- Order state machine & Reconciler (PENDING -> OPEN / FAILED)
- Bankroll guardrails (max open positions, daily loss cap, kill switch)
- Shadow mode execution
- SQLite events table audit logging
"""

import os
from datetime import datetime, timezone, timedelta
import pytest

from db import Database, Environment, PositionSide, PositionStatus, Strategy
from risk_engine.broker import KalshiBroker, SimulatedBroker
from risk_engine.decision import Decision
from risk_engine.engine import (
    Config,
    TradeEngine,
    strategy_last_9,
    strategy_random,
)
from risk_engine.reconciler import Reconciler
from risk_engine.rules import (
    DEFAULT_RISK_RULES,
    RiskContext,
    evaluate_risk_rules,
    rule_is_binary_market,
    rule_kill_switch_not_active,
    rule_market_is_open,
    rule_no_existing_position,
    rule_not_past_close,
    rule_under_daily_loss_cap,
    rule_under_max_open,
)


def test_strategy_protocol_and_functions():
    # strategy_last_9 returns None if fewer than 9 results (no opinion)
    assert strategy_last_9(["yes"] * 8) is None
    assert strategy_last_9(["yes"] * 9) == PositionSide.YES
    assert strategy_last_9(["no"] * 9) == PositionSide.NO
    assert strategy_last_9(["yes"] * 5 + ["no"] * 4) == PositionSide.YES
    assert strategy_last_9(["no"] * 5 + ["yes"] * 4) == PositionSide.NO

    # strategy_random returns either YES or NO
    assert strategy_random() in (PositionSide.YES, PositionSide.NO)


def test_pure_risk_rules_in_isolation(tmp_path):
    db = Database(":memory:")
    now_utc = datetime.now(timezone.utc)

    # 1. Market is open rule
    ctx_open = RiskContext(market={"status": "open"}, strategy_name="last_9", db=db)
    ctx_closed = RiskContext(market={"status": "closed"}, strategy_name="last_9", db=db)
    assert rule_market_is_open(ctx_open) is None
    assert rule_market_is_open(ctx_closed) == "market_not_open_closed"

    # 2. Binary market rule
    assert rule_is_binary_market(RiskContext(market={"market_type": "binary"}, strategy_name="last_9")) is None
    assert rule_is_binary_market(RiskContext(market={"market_type": "scalar"}, strategy_name="last_9")) == "not_standalone_binary_market"

    # 3. Not past close rule
    future_iso = (now_utc + timedelta(minutes=10)).isoformat()
    past_iso = (now_utc - timedelta(minutes=5)).isoformat()
    assert rule_not_past_close(RiskContext(market={"close_time": future_iso}, strategy_name="last_9", now=now_utc)) is None
    assert rule_not_past_close(RiskContext(market={"close_time": past_iso}, strategy_name="last_9", now=now_utc)) == "market_already_past_close_time"

    # 4. No existing position rule
    db.insert_open_position("ord_1", "T-1", PositionSide.YES, 1, 50, strategy=Strategy.LAST_9)
    assert rule_no_existing_position(RiskContext(market={"ticker": "T-1"}, strategy_name="last_9", db=db)) == "position_already_exists"
    assert rule_no_existing_position(RiskContext(market={"ticker": "T-2"}, strategy_name="last_9", db=db)) is None

    # 5. Under max open positions rule
    assert rule_under_max_open(RiskContext(market={}, strategy_name="last_9", db=db, max_open_positions=1)) == "max_open_positions_reached_1_of_1"
    assert rule_under_max_open(RiskContext(market={}, strategy_name="last_9", db=db, max_open_positions=5)) is None

    # 6. Daily loss cap rule
    db.settle_position("ord_1", pnl_cents=-6000)
    assert rule_under_daily_loss_cap(RiskContext(market={}, strategy_name="last_9", db=db, daily_loss_cap_cents=5000)) is not None
    assert rule_under_daily_loss_cap(RiskContext(market={}, strategy_name="last_9", db=db, daily_loss_cap_cents=10000)) is None

    # 7. Kill switch rule
    ks_file = str(tmp_path / "KILL_SWITCH")
    assert rule_kill_switch_not_active(RiskContext(market={}, strategy_name="last_9", kill_switch_path=ks_file)) is None
    with open(ks_file, "w") as f:
        f.write("STOP")
    assert rule_kill_switch_not_active(RiskContext(market={}, strategy_name="last_9", kill_switch_path=ks_file)) == "kill_switch_file_detected"

    db.close()


def test_simulated_broker():
    broker = SimulatedBroker()
    assert broker.is_live is False
    res = broker.place_order(
        ticker="KXTEST-1",
        side=PositionSide.YES,
        count=2,
        price_cents=45,
        client_order_id="sim_1",
    )
    assert res["order"]["status"] == "executed"
    assert res["order"]["count"] == 2
    assert broker.get_order("sim_1") is not None
    assert broker.cancel_order("sim_1")["status"] == "canceled"


def test_decision_dataclass():
    market = {"ticker": "KXBTC15M", "status": "open"}
    dec_accepted = Decision(
        ticker="KXBTC15M",
        market=market,
        strategy="last_9",
        side=PositionSide.YES,
        price_cents=50,
    )
    assert dec_accepted.is_accepted is True
    assert dec_accepted.reason is None

    dec_rejected = Decision(
        ticker="KXBTC15M",
        market=market,
        strategy="last_9",
        side=None,
        reason="max_open_positions_reached",
    )
    assert dec_rejected.is_accepted is False
    assert dec_rejected.reason == "max_open_positions_reached"


def test_trade_engine_run_cycle_and_order_state_machine():
    db = Database(":memory:")
    broker = SimulatedBroker()

    # Seed candidate markets
    db.upsert_markets([
        {"ticker": "KXBTC-OPEN", "status": "open", "market_type": "binary", "yes_ask": 40, "no_ask": 60},
        {"ticker": "KXBTC-CLOSED", "status": "closed", "market_type": "binary", "yes_ask": 40, "no_ask": 60},
    ])

    config = Config(env="demo", max_open_positions=10)
    engine = TradeEngine(
        repo=db,
        broker=broker,
        strategy_fn=lambda **kwargs: PositionSide.YES,
        strategy_name="last_9",
        config=config,
    )

    # Run cycle
    decisions = engine.run_cycle()
    assert len(decisions) >= 1

    # Verify positions in database
    positions = db.get_all_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["ticker"] == "KXBTC-OPEN"
    assert pos["side"] == PositionSide.YES
    assert pos["status"] == PositionStatus.OPEN  # Transitioned PENDING -> OPEN

    # Verify audit events table
    events = db.get_events()
    assert len(events) >= 1
    assert any("PENDING" in ev["message"] or "OPEN" in ev["message"] for ev in events)

    db.close()


def test_shadow_mode():
    db = Database(":memory:")
    broker = SimulatedBroker()

    db.upsert_markets([
        {"ticker": "KXSHADOW-1", "status": "open", "market_type": "binary", "yes_ask": 45, "no_ask": 55},
    ])

    config = Config(env="demo", shadow_mode=True)
    engine = TradeEngine(
        repo=db,
        broker=broker,
        strategy_fn=lambda **kwargs: PositionSide.YES,
        strategy_name="last_9",
        config=config,
    )

    decisions = engine.run_cycle()
    assert len(decisions) == 1
    assert decisions[0].shadow is True
    # In shadow mode, no position is opened in SQLite
    assert len(db.get_all_positions()) == 0
    # But a shadow event is recorded
    events = db.get_events()
    assert any("[SHADOW]" in ev["message"] for ev in events)

    db.close()


def test_reconciler_resolves_pending_orders():
    db = Database(":memory:")

    class MockBroker:
        is_live = True
        def __init__(self):
            self.orders = {
                "ord_found": {"status": "executed", "order_id": "ord_found"},
            }
        def get_order(self, order_id):
            return self.orders.get(order_id)
        def place_order(self, **kwargs):
            return {}
        def cancel_order(self, order_id):
            return {}

    broker = MockBroker()
    reconciler = Reconciler(broker=broker, db=db)

    # Insert two PENDING positions (one found on Kalshi, one lost/crashed)
    db.insert_pending_position("ord_found", "T-FOUND", PositionSide.YES, count=1, price_cents=50)
    db.insert_pending_position("ord_lost", "T-LOST", PositionSide.NO, count=1, price_cents=50)

    assert len(db.get_pending_positions()) == 2

    # Reconcile
    reconciled = reconciler.reconcile()
    assert len(reconciled) == 2

    all_positions = {p["client_order_id"]: p for p in db.get_all_positions()}
    assert all_positions["ord_found"]["status"] == PositionStatus.OPEN
    assert all_positions["ord_lost"]["status"] == PositionStatus.FAILED

    # No pending positions remain
    assert len(db.get_pending_positions()) == 0

    db.close()
