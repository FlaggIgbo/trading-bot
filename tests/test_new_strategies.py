"""
Unit and Integration Tests for the Three New 15-Minute Crypto Trading Strategies:
1. spot_lag: Cross-venue spot vs order-book latency edge
2. panic_fade: Mean reversion fading temporary order book volatility panic
3. late_window_scalp: Late-window high-probability scalping with asymmetric tail risk management
"""

import pytest
from unittest.mock import MagicMock

from db import Database, PositionSide, PositionStatus, Strategy
from ingestion.spot import SpotClient
from risk_engine.engine import (
    STRATEGIES,
    Trader,
    TradeEngine,
    get_strategy,
    spot_lag_signal,
    panic_fade_signal,
    late_window_scalp_signal,
    choose_side_spot_lag,
    choose_side_panic_fade,
    choose_side_late_window_scalp,
    should_stop_loss,
    is_entry_time,
)


# ---------------------------------------------------------------------------
# 1. spot_lag Strategy Tests
# ---------------------------------------------------------------------------

def test_spot_lag_signal_yes_edge():
    # Strike is $90,000, Spot is $90,100 (+0.11% > 0.05% buffer)
    # Market maker order book is lagging: YES ask is only 52c (<= 68c max ask)
    market = {
        "ticker": "KXBTC15M-TEST-1",
        "floor_strike": 90000.0,
        "yes_ask": 52,
        "no_ask": 50,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90100.0

    signal = spot_lag_signal(market=market, spot_client=mock_spot)
    assert signal == PositionSide.YES


def test_spot_lag_signal_no_edge():
    # Strike is $90,000, Spot is $89,900 (-0.11% < -0.05% buffer)
    # Market maker order book is lagging: NO ask is only 48c
    market = {
        "ticker": "KXBTC15M-TEST-2",
        "floor_strike": 90000.0,
        "yes_ask": 54,
        "no_ask": 48,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 89900.0

    signal = spot_lag_signal(market=market, spot_client=mock_spot)
    assert signal == PositionSide.NO


def test_spot_lag_no_latency_edge_when_book_already_updated():
    # Spot moved up to $90,100, but order book already adjusted YES ask to 75c (> 68c max)
    market = {
        "ticker": "KXBTC15M-TEST-3",
        "floor_strike": 90000.0,
        "yes_ask": 75,
        "no_ask": 27,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90100.0

    signal = spot_lag_signal(market=market, spot_client=mock_spot)
    assert signal is None


def test_spot_lag_no_signal_within_noise_buffer():
    # Spot is $90,010 (+0.011% buffer, less than min_buffer_pct 0.05%)
    market = {
        "ticker": "KXBTC15M-TEST-4",
        "floor_strike": 90000.0,
        "yes_ask": 50,
        "no_ask": 52,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90010.0

    signal = spot_lag_signal(market=market, spot_client=mock_spot)
    assert signal is None


def test_spot_lag_missing_data():
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = None
    assert spot_lag_signal(market={"floor_strike": 90000.0}, spot_client=mock_spot) is None
    assert spot_lag_signal(market={}, spot_client=mock_spot) is None


# ---------------------------------------------------------------------------
# 2. panic_fade Strategy Tests
# ---------------------------------------------------------------------------

def test_panic_fade_fades_yes_panic():
    # Order book panicked into YES: yes_ask = 82c, making NO ask cheap at 22c
    # Underlying spot is $90,050 vs strike $90,000 (deviation 0.05% << 0.60%)
    market = {
        "ticker": "KXBTC15M-TEST-5",
        "floor_strike": 90000.0,
        "yes_ask": 82,
        "no_ask": 22,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90050.0

    signal = panic_fade_signal(market=market, spot_client=mock_spot)
    assert signal == PositionSide.NO


def test_panic_fade_fades_no_panic():
    # Order book panicked into NO: no_ask = 85c, making YES ask cheap at 18c
    # Underlying spot is $89,950 vs strike $90,000 (deviation 0.05% << 0.60%)
    market = {
        "ticker": "KXBTC15M-TEST-6",
        "floor_strike": 90000.0,
        "yes_ask": 18,
        "no_ask": 85,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 89950.0

    signal = panic_fade_signal(market=market, spot_client=mock_spot)
    assert signal == PositionSide.YES


def test_panic_fade_does_not_fade_runaway_breakout():
    # NO spiked to 85c, YES is 18c, BUT spot has crashed by 1.2% ($88,900 vs strike $90,000)
    # This is a genuine macro trend move, NOT temporary order book panic
    market = {
        "ticker": "KXBTC15M-TEST-7",
        "floor_strike": 90000.0,
        "yes_ask": 18,
        "no_ask": 85,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 88900.0

    signal = panic_fade_signal(market=market, spot_client=mock_spot)
    assert signal is None


def test_panic_fade_no_panic_condition():
    # Balanced market
    market = {
        "ticker": "KXBTC15M-TEST-8",
        "floor_strike": 90000.0,
        "yes_ask": 52,
        "no_ask": 50,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90000.0

    signal = panic_fade_signal(market=market, spot_client=mock_spot)
    assert signal is None


# ---------------------------------------------------------------------------
# 3. late_window_scalp Strategy Tests
# ---------------------------------------------------------------------------

def test_late_window_scalp_yes():
    # Minute 10.0 (inside 7.5 to 13.5 window)
    # Spot has +0.20% buffer above strike ($90,180 vs $90,000)
    # YES ask is 86c (within 78c to 96c high-probability band)
    market = {
        "ticker": "KXBTC15M-TEST-9",
        "floor_strike": 90000.0,
        "yes_ask": 86,
        "no_ask": 16,
        "minute": 10.0,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90180.0

    signal = late_window_scalp_signal(market=market, spot_client=mock_spot)
    assert signal == PositionSide.YES


def test_late_window_scalp_no():
    # Minute 11.5 (inside 7.5 to 13.5 window)
    # Spot has -0.22% buffer below strike ($89,800 vs $90,000)
    # NO ask is 88c (within 78c to 96c band)
    market = {
        "ticker": "KXBTC15M-TEST-10",
        "floor_strike": 90000.0,
        "yes_ask": 14,
        "no_ask": 88,
        "minute": 11.5,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 89800.0

    signal = late_window_scalp_signal(market=market, spot_client=mock_spot)
    assert signal == PositionSide.NO


def test_late_window_scalp_rejects_early_window():
    # Minute 3.0 (< 7.5 minutes) -> Too early for late-window scalping
    market = {
        "ticker": "KXBTC15M-TEST-11",
        "floor_strike": 90000.0,
        "yes_ask": 85,
        "no_ask": 18,
        "minute": 3.0,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90200.0

    signal = late_window_scalp_signal(market=market, spot_client=mock_spot)
    assert signal is None


def test_late_window_scalp_rejects_final_settlement_window():
    # Minute 14.2 (>= 13.8 minutes) -> Inside final 60s benchmark average window
    market = {
        "ticker": "KXBTC15M-TEST-12",
        "floor_strike": 90000.0,
        "yes_ask": 92,
        "no_ask": 10,
        "minute": 14.2,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90200.0

    signal = late_window_scalp_signal(market=market, spot_client=mock_spot)
    assert signal is None


def test_late_window_scalp_rejects_insufficient_buffer():
    # Spot is barely above strike ($90,020 vs $90,000, only +0.022% < 0.08% buffer)
    # Asymmetric risk would be too high
    market = {
        "ticker": "KXBTC15M-TEST-13",
        "floor_strike": 90000.0,
        "yes_ask": 84,
        "no_ask": 18,
        "minute": 9.0,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90020.0

    signal = late_window_scalp_signal(market=market, spot_client=mock_spot)
    assert signal is None


# ---------------------------------------------------------------------------
# 4. Stop-Loss & Risk Rule Tests
# ---------------------------------------------------------------------------

def test_late_window_scalp_stop_loss():
    # Entry at 88c, drops to 64c (<= 65c threshold) -> Stop loss triggered
    assert should_stop_loss(
        entry_price_cents=88,
        current_price_cents=64,
        minute=9.0,
        strategy="late_window_scalp",
    ) is True

    # Entry at 90c, drops to 73c (losing 17c >= 15c tail risk drop) -> Stop loss triggered
    assert should_stop_loss(
        entry_price_cents=90,
        current_price_cents=73,
        minute=9.0,
        strategy="late_window_scalp",
    ) is True

    # Benchmark protection: Minute >= 14.0 and current price not secured at >= 98c -> Exit triggered
    assert should_stop_loss(
        entry_price_cents=85,
        current_price_cents=92,
        minute=14.1,
        strategy="late_window_scalp",
    ) is True

    # Winning position holding well at minute 10.0 -> Do NOT stop loss
    assert should_stop_loss(
        entry_price_cents=85,
        current_price_cents=89,
        minute=10.0,
        strategy="late_window_scalp",
    ) is False


def test_panic_fade_stop_loss():
    # Entry at 30c, drops to 9c (<= 10c threshold) -> Stop loss triggered
    assert should_stop_loss(
        entry_price_cents=30,
        current_price_cents=9,
        minute=5.0,
        strategy="panic_fade",
    ) is True

    # Entry at 30c, holding at 25c -> Do not stop loss
    assert should_stop_loss(
        entry_price_cents=30,
        current_price_cents=25,
        minute=5.0,
        strategy="panic_fade",
    ) is False


# ---------------------------------------------------------------------------
# 5. Registry & Integration Tests
# ---------------------------------------------------------------------------

def test_strategy_registry_resolutions():
    for name in ("spot_lag", "spot-lag", "spotlag"):
        strat = get_strategy(name)
        assert strat.name == "spot_lag"
        assert strat.stop_loss is True
        assert strat.check_spot is True
        assert strat.taker_ioc is True
        assert strat.price_band == (20, 70)

    for name in ("panic_fade", "panic-fade", "panicfade"):
        strat = get_strategy(name)
        assert strat.name == "panic_fade"
        assert strat.stop_loss is True
        assert strat.take_profit == 2.0
        assert strat.price_band == (15, 45)

    for name in ("late_window_scalp", "late_scalp", "late-window-scalp"):
        strat = get_strategy(name)
        assert strat.name == "late_window_scalp"
        assert strat.stop_loss is True
        assert strat.taker_ioc is True
        assert strat.price_band == (78, 96)
        assert strat.entry_window == (7.5, 13.5)


def test_is_entry_time_new_strategies():
    mkt_early = {"minute": 3.0}
    mkt_mid = {"minute": 5.0}
    mkt_late = {"minute": 10.0}
    mkt_final = {"minute": 14.5}

    # late_window_scalp: only 7.5 to 13.5
    strat_late = get_strategy("late_window_scalp")
    assert is_entry_time(mkt_early, strat_late) is False
    assert is_entry_time(mkt_mid, strat_late) is False
    assert is_entry_time(mkt_late, strat_late) is True
    assert is_entry_time(mkt_final, strat_late) is False

    # spot_lag: 1.0 to 12.0
    strat_spot = get_strategy("spot_lag")
    assert is_entry_time(mkt_early, strat_spot) is True
    assert is_entry_time(mkt_late, strat_spot) is True
    assert is_entry_time(mkt_final, strat_spot) is False


def test_database_strategy_enum_and_persistence(tmp_path):
    db_file = str(tmp_path / "test_strategies.db")
    db = Database(db_path=db_file)

    for strat in (Strategy.SPOT_LAG, Strategy.PANIC_FADE, Strategy.LATE_WINDOW_SCALP):
        db.upsert_markets([
            {
                "ticker": f"KXBTC15M-TEST-{strat.value}",
                "title": "Test",
                "subtitle": "Test",
                "status": "open",
                "close_time": "2026-11-20T00:00:00Z",
            }
        ])
        db.insert_open_position(
            client_order_id=f"order_{strat.value}",
            ticker=f"KXBTC15M-TEST-{strat.value}",
            side=PositionSide.YES,
            count=1,
            price_cents=50,
            strategy=strat,
        )

    all_pos = db.get_all_positions()
    assert len(all_pos) == 3
    persisted_strats = {p["strategy"] for p in all_pos}
    assert persisted_strats == {"spot_lag", "panic_fade", "late_window_scalp"}
    db.close()


def test_demo_vs_prod_tuning_late_window_scalp():
    # Contract with ask = 64c (typical demo maker quote), buffer = +0.05%
    market = {
        "ticker": "KXBTC15M-TEST-DEMO",
        "floor_strike": 90000.0,
        "yes_ask": 64,
        "no_ask": 40,
        "minute": 10.0,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90045.0  # +0.05% buffer

    # In demo: should trigger YES (64c >= 58c, buffer 0.05% >= 0.04%)
    sig_demo = late_window_scalp_signal(market=market, spot_client=mock_spot, env="demo")
    assert sig_demo == PositionSide.YES

    # In prod: should reject (64c < 78c floor, buffer 0.05% < 0.08% floor)
    sig_prod = late_window_scalp_signal(market=market, spot_client=mock_spot, env="production")
    assert sig_prod is None


def test_demo_vs_prod_tuning_panic_fade():
    # Contract with YES ask = 64c (demo spike), NO ask = 40c
    market = {
        "ticker": "KXBTC15M-TEST-DEMO-2",
        "floor_strike": 90000.0,
        "yes_ask": 64,
        "no_ask": 40,
    }
    mock_spot = MagicMock(spec=SpotClient)
    mock_spot.get_spot_price.return_value = 90020.0

    # In demo: should trigger NO (64c >= 62c demo panic threshold, NO ask <= 48c)
    sig_demo = panic_fade_signal(market=market, spot_client=mock_spot, env="demo")
    assert sig_demo == PositionSide.NO

    # In prod: should reject (64c < 75c prod panic threshold)
    sig_prod = panic_fade_signal(market=market, spot_client=mock_spot, env="production")
    assert sig_prod is None
