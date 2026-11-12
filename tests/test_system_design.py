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
    RiskRejectReason,
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
    assert rule_market_is_open(ctx_closed) == RiskRejectReason.MARKET_NOT_OPEN
    assert isinstance(rule_market_is_open(ctx_closed), RiskRejectReason)

    # 2. Binary market rule
    assert rule_is_binary_market(RiskContext(market={"market_type": "binary"}, strategy_name="last_9")) is None
    res_not_bin = rule_is_binary_market(RiskContext(market={"market_type": "scalar"}, strategy_name="last_9"))
    assert res_not_bin == RiskRejectReason.NOT_STANDALONE_BINARY_MARKET
    assert isinstance(res_not_bin, RiskRejectReason)

    # 3. Not past close rule
    future_iso = (now_utc + timedelta(minutes=10)).isoformat()
    past_iso = (now_utc - timedelta(minutes=5)).isoformat()
    assert rule_not_past_close(RiskContext(market={"close_time": future_iso}, strategy_name="last_9", now=now_utc)) is None
    res_past = rule_not_past_close(RiskContext(market={"close_time": past_iso}, strategy_name="last_9", now=now_utc))
    assert res_past == RiskRejectReason.MARKET_ALREADY_PAST_CLOSE_TIME
    assert isinstance(res_past, RiskRejectReason)

    # 4. No existing position rule (returns POSITION_ALREADY_HELD for last_N when open, or POSITION_ALREADY_EXISTS)
    db.insert_open_position("ord_1", "T-1", PositionSide.YES, 1, 50, strategy=Strategy.LAST_9)
    res_held = rule_no_existing_position(RiskContext(market={"ticker": "T-1"}, strategy_name="last_9", db=db))
    assert res_held == RiskRejectReason.POSITION_ALREADY_HELD
    assert isinstance(res_held, RiskRejectReason)
    assert rule_no_existing_position(RiskContext(market={"ticker": "T-2"}, strategy_name="last_9", db=db)) is None

    # Test POSITION_ALREADY_EXISTS when already traded for non-last_N strategy
    db.insert_open_position("ord_2", "T-3", PositionSide.YES, 1, 50, strategy=Strategy.RANDOM)
    db.settle_position("ord_2", pnl_cents=50)
    res_exists = rule_no_existing_position(RiskContext(market={"ticker": "T-3"}, strategy_name="random", db=db))
    assert res_exists == RiskRejectReason.POSITION_ALREADY_EXISTS
    assert isinstance(res_exists, RiskRejectReason)

    # 5. Under max open positions rule
    res_max = rule_under_max_open(RiskContext(market={}, strategy_name="last_9", db=db, max_open_positions=1))
    assert res_max == RiskRejectReason.MAX_OPEN_POSITIONS_REACHED
    assert isinstance(res_max, RiskRejectReason)
    assert rule_under_max_open(RiskContext(market={}, strategy_name="last_9", db=db, max_open_positions=5)) is None

    # 6. Daily loss cap rule
    db.settle_position("ord_1", pnl_cents=-6000)
    res_loss = rule_under_daily_loss_cap(RiskContext(market={}, strategy_name="last_9", db=db, daily_loss_cap_cents=5000))
    assert res_loss == RiskRejectReason.DAILY_LOSS_CAP_EXCEEDED
    assert isinstance(res_loss, RiskRejectReason)
    assert rule_under_daily_loss_cap(RiskContext(market={}, strategy_name="last_9", db=db, daily_loss_cap_cents=10000)) is None

    # 7. Kill switch rule
    ks_file = str(tmp_path / "KILL_SWITCH")
    assert rule_kill_switch_not_active(RiskContext(market={}, strategy_name="last_9", kill_switch_path=ks_file)) is None
    with open(ks_file, "w") as f:
        f.write("STOP")
    res_ks = rule_kill_switch_not_active(RiskContext(market={}, strategy_name="last_9", kill_switch_path=ks_file))
    assert res_ks == RiskRejectReason.KILL_SWITCH_FILE_DETECTED
    assert isinstance(res_ks, RiskRejectReason)

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
        reason=RiskRejectReason.MAX_OPEN_POSITIONS_REACHED,
    )
    assert dec_rejected.is_accepted is False
    assert dec_rejected.reason == RiskRejectReason.MAX_OPEN_POSITIONS_REACHED
    assert dec_rejected.reason == "max_open_positions_reached"
    assert dec_rejected.to_dict()["reason"] == "max_open_positions_reached"


def test_risk_reject_reasons_enumeration():
    # Verify all members are StrEnum instances
    for reason in RiskRejectReason:
        assert isinstance(reason, str)
        assert isinstance(reason, RiskRejectReason)
        assert reason.value == str(reason)

    # Test key members
    assert RiskRejectReason.POSITION_ALREADY_HELD == "position_already_held"
    assert RiskRejectReason.POSITION_ALREADY_EXISTS == "position_already_exists"
    assert RiskRejectReason.MARKET_NOT_OPEN == "market_not_open"
    assert RiskRejectReason.NOT_STANDALONE_BINARY_MARKET == "not_standalone_binary_market"
    assert RiskRejectReason.MARKET_ALREADY_PAST_CLOSE_TIME == "market_already_past_close_time"
    assert RiskRejectReason.KILL_SWITCH_FILE_DETECTED == "kill_switch_file_detected"
    assert RiskRejectReason.KILL_SWITCH_ENV_DETECTED == "kill_switch_env_detected"
    assert RiskRejectReason.MAX_OPEN_POSITIONS_REACHED == "max_open_positions_reached"
    assert RiskRejectReason.DAILY_LOSS_CAP_EXCEEDED == "daily_loss_cap_exceeded"
    assert RiskRejectReason.MISSING_TICKER == "missing_ticker"

    # Missing ticker check
    assert rule_no_existing_position(RiskContext(market={}, strategy_name="last_9")) == RiskRejectReason.MISSING_TICKER

    # evaluate_risk_rules returns Enum directly
    ctx_blocked = RiskContext(market={"status": "closed"}, strategy_name="last_9")
    eval_res = evaluate_risk_rules(ctx_blocked)
    assert eval_res == RiskRejectReason.MARKET_NOT_OPEN
    assert isinstance(eval_res, RiskRejectReason)


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


def test_simplified_strategy_dataclass_and_registry():
    from risk_engine.engine import (
        Strategy as StratData,
        STRATEGIES,
        get_strategy,
        trend_signal,
        regime_signal,
        cheapest_side,
        random_signal,
        in_band,
    )

    # Core simplified strategies
    assert "trend" in STRATEGIES
    assert "fade" in STRATEGIES
    assert "cheap2x" in STRATEGIES
    assert "cheap1.5x" in STRATEGIES
    assert "random" in STRATEGIES

    strat_trend = get_strategy("trend")
    assert isinstance(strat_trend, StratData)
    assert strat_trend.price_band == (20, 65)

    strat_cheap = get_strategy("cheap2x")
    assert strat_cheap.take_profit == 2.0
    assert strat_cheap.entry_window == (5.0, 10.0)
    assert strat_cheap.price_band == (1, 35)

    # Runtime overrides
    custom = get_strategy("trend", price_band=(25, 75), stop_loss=True, take_profit=85)
    assert custom.price_band == (25, 75)
    assert custom.stop_loss is True
    assert custom.take_profit == 85

    # Pure signals
    assert in_band(45, (20, 65))
    assert not in_band(15, (20, 65))
    assert not in_band(70, (20, 65))

    # cheapest_side pure signal
    assert cheapest_side(market={"yes_ask": 25, "no_ask": 75}) == PositionSide.YES
    assert cheapest_side(market={"yes_ask": 80, "no_ask": 20}) == PositionSide.NO
    assert cheapest_side(market={"yes_ask": 40, "no_ask": 40}) == PositionSide.YES


def test_reject_single_home_for_risk_rules():
    from risk_engine.engine import get_strategy
    from risk_engine.rules import reject, RiskContext, RiskRejectReason

    db = Database(":memory:")
    strat_trend = get_strategy("trend")

    # 1. Closed market rejected
    ctx_closed = RiskContext(market={"status": "closed"}, strategy_name="trend", db=db)
    assert reject({"status": "closed"}, strat_trend, ctx_closed) == RiskRejectReason.MARKET_NOT_OPEN

    # 2. Timing outside entry window rejected
    strat_cheap = get_strategy("cheap2x")  # entry window (5.0, 10.0)
    ctx_m2 = RiskContext(market={"status": "open", "market_type": "binary", "ticker": "T-1", "minute": 2.0}, strategy_name="cheap2x", db=db)
    assert reject({"status": "open", "market_type": "binary", "ticker": "T-1", "minute": 2.0}, strat_cheap, ctx_m2) == "outside_entry_window"

    # 3. Valid in window accepted
    ctx_m6 = RiskContext(market={"status": "open", "market_type": "binary", "ticker": "T-1", "minute": 6.0}, strategy_name="cheap2x", db=db)
    assert reject({"status": "open", "market_type": "binary", "ticker": "T-1", "minute": 6.0}, strat_cheap, ctx_m6) is None

    db.close()


def test_linear_run_cycle_pipeline():
    from risk_engine.engine import get_strategy, run_cycle, TradeEngine

    db = Database(":memory:")
    broker = SimulatedBroker(db=db)
    strat = get_strategy("trend", price_band=(20, 65))

    db.upsert_markets([
        {"ticker": "KXBTC-OPEN1", "status": "open", "market_type": "binary", "yes_ask": 45, "no_ask": 55},
        {"ticker": "KXBTC-HIGHASK", "status": "open", "market_type": "binary", "yes_ask": 85, "no_ask": 15},
    ])

    engine = TradeEngine(repo=db, broker=broker, strategy_name="trend")
    decisions = run_cycle(strat, engine)

    assert len(decisions) >= 1
    # Check that position was opened
    positions = db.get_all_positions()
    assert len(positions) == 1
    assert positions[0]["ticker"] == "KXBTC-OPEN1"
    assert positions[0]["status"] == PositionStatus.OPEN

    db.close()

