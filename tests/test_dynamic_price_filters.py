from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from unittest.mock import MagicMock, patch
import pytest

from database import (
    DEFAULT_DYNAMIC_PRICE_MAP,
    calibrate_dynamic_price_map_from_db,
    get_dynamic_filter_for_market,
    is_default_exploration_window,
    should_refresh_dynamic_map,
)
from main import process_single_market


@pytest.fixture(autouse=True)
def reset_db_cache():
    import database
    database._DYNAMIC_PRICE_MAP_CACHE = dict(database.DEFAULT_DYNAMIC_PRICE_MAP)
    database._LAST_MAP_REFRESH_TIME = time.time()
    yield
    database._DYNAMIC_PRICE_MAP_CACHE = None
    database._LAST_MAP_REFRESH_TIME = 0.0


def test_default_price_map_and_sl_defaults():
    for sym in ("DEFAULT", "BTC", "DOGE"):
        assert sym in DEFAULT_DYNAMIC_PRICE_MAP or sym in ("BTC", "DOGE")
        f = get_dynamic_filter_for_market(sym, is_5min_mark=False, now_utc=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc))
        assert f["sl_threshold"] == 25
        assert f["sl_floor"] == 15
        assert f["cut_5min"] == 15


@pytest.mark.parametrize("now, last_refresh_delta_hrs, cache_empty, expected", [
    (datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc), 0, True, True),
    (datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc), 1, False, False),
    (datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc), 7, False, True),
    (datetime(2026, 8, 14, 5, 16, tzinfo=timezone.utc), 0.5, False, True),  # 05:15 UTC post-exploration anchor
    (datetime(2026, 8, 14, 7, 2, tzinfo=timezone.utc), 0.5, False, True),  # 07:00 UTC clock anchor
])
def test_should_refresh_rules(now, last_refresh_delta_hrs, cache_empty, expected):
    last_ts = (now - timedelta(hours=last_refresh_delta_hrs)).timestamp()
    assert should_refresh_dynamic_map(now_utc=now, last_refresh_time=last_ts, cache_is_empty=cache_empty) is expected


@pytest.mark.parametrize("hour, minute, expected_exploring", [
    (5, 0, True), (5, 10, True), (5, 15, False), (5, 30, False), (6, 30, False), (7, 0, False), (14, 30, False)
])
def test_exploration_window_flag(hour, minute, expected_exploring):
    dt = datetime(2026, 8, 15, hour, minute, 0, tzinfo=timezone.utc)
    assert is_default_exploration_window(dt) is expected_exploring


@patch("database.get_mongo_collection")
def test_dynamic_calibration_from_mock_db(mock_get_coll):
    snaps = [{"sym": "BTC", "tick": "T1", "m": 0, "pct": 0.02, "trd": {"buy": 32, "side": "yes"}}]
    settles = [{"ticker": "T1", "market_result": "yes"}]

    mock_get_coll.side_effect = lambda n: MagicMock(find=MagicMock(return_value=snaps if n == "market_snapshots" else settles))
    calibrated = calibrate_dynamic_price_map_from_db()
    
    assert "BTC" in calibrated
    assert calibrated["BTC"]["00"]["sl_threshold"] == 25


@pytest.mark.parametrize("ask, spot_lead, should_buy, corridor", [
    (40, 0.0001, False, None),          # Expensive above max_buy
    (30, 0.0001, True, "discount"),     # Deep discount
    (62, 0.0015, True, "momentum"),     # Strong momentum lead
    (62, 0.0003, False, None),          # Weak momentum lead
    (50, 0.0001, False, None),          # Coin-flip mid-zone
])
def test_market_screening_corridors(ask, spot_lead, should_buy, corridor):
    client = MagicMock(exchange_name="Kalshi")
    client.get_market_position.return_value = None
    client.has_existing_position_or_order.return_value = False
    
    market = {
        "ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": f"{ask/100:.4f}", "yes_bid_dollars": f"{(ask-2)/100:.4f}",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }
    
    snap = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60000.0 * (1 + spot_lead)},
        dry_run=True,
    )

    if should_buy:
        assert "trd" in snap
        assert snap["trd"]["corridor"] == corridor
        assert snap["trd"]["action"] == "buy_opened"
    else:
        # Either no trade or a counterfactual dry-run sample
        assert snap.get("trd", {}).get("action") != "buy_opened"


@pytest.mark.parametrize("hour, minute, expected_avoid_mid, expected_is_exploration", [
    (5, 0, False, True),    # 05:00 UTC -> Mid-zone allowed, general exploration active
    (5, 10, False, True),   # 05:10 UTC -> Mid-zone allowed, general exploration active
    (5, 15, True, False),   # 05:15 UTC -> Mid-zone AVOIDED, exploration inactive
    (5, 30, True, False),   # 05:30 UTC -> Mid-zone AVOIDED, exploration inactive
    (6, 0, True, False),    # 06:00 UTC -> Mid-zone AVOIDED, exploration inactive
    (7, 0, True, False),    # 07:00 UTC -> Mid-zone avoided, exploration inactive
    (12, 0, True, False),   # 12:00 UTC -> Mid-zone avoided, exploration inactive
    (0, 0, True, False),    # 00:00 UTC -> Mid-zone avoided, exploration inactive
])
def test_mid_zone_flag_during_exploration(hour, minute, expected_avoid_mid, expected_is_exploration):
    dt = datetime(2026, 8, 15, hour, minute, 0, tzinfo=timezone.utc)
    filters = get_dynamic_filter_for_market("BTC", is_5min_mark=False, now_utc=dt)
    assert filters["avoid_mid_zone"] is expected_avoid_mid
    assert filters["is_exploration"] is expected_is_exploration


def test_market_screening_mid_zone_allowed_during_exploration():
    from main import reset_exploration_trades_count
    reset_exploration_trades_count()

    client = MagicMock(exchange_name="Kalshi")
    client.get_market_position.return_value = None
    client.has_existing_position_or_order.return_value = False

    market = {
        "ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.50", "yes_bid_dollars": "0.48",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }

    # At 05:00 UTC (15m discovery window 05:00-05:15 UTC), 50¢ coin-flip mid-zone should execute as LIVE exploration order
    snap_exp = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 5, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60000.0 * 1.0001},
        dry_run=True,
    )
    assert "trd" in snap_exp
    assert snap_exp["trd"]["corridor"] == "mid_zone"
    assert snap_exp["trd"]["action"] == "buy_opened"
    assert snap_exp["trd"]["buy"] == 50

    # At 05:20 UTC (after 15m window), 50¢ coin-flip mid-zone should be logged as COUNTERFACTUAL sample (no real money risked)
    snap_0520 = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 5, 20, 0, tzinfo=timezone.utc),
        minute_past_hour=20,
        spot_cache={"BTC": 60000.0 * 1.0001},
        dry_run=True,
    )
    assert "trd" in snap_0520
    assert snap_0520["trd"]["action"] == "counterfactual_mid_zone"
    assert snap_0520["trd"]["dry_run"] is True

    # At 12:00 UTC (normal window), 50¢ coin-flip mid-zone should also be logged as COUNTERFACTUAL sample
    snap_normal = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60000.0 * 1.0001},
        dry_run=True,
    )
    assert "trd" in snap_normal
    assert snap_normal["trd"]["action"] == "counterfactual_mid_zone"
    assert snap_normal["trd"]["dry_run"] is True


def test_exploration_trade_cap_enforcement():
    from main import (
        can_execute_exploration_trade,
        record_exploration_trade_executed,
        reset_exploration_trades_count,
        get_exploration_trades_count,
        MAX_EXPLORATION_TRADES_PER_SESSION,
    )
    reset_exploration_trades_count()
    dt = datetime(2026, 8, 15, 5, 5, 0, tzinfo=timezone.utc)

    assert can_execute_exploration_trade(dt) is True
    assert get_exploration_trades_count() == 0

    # Record 3 trades
    for i in range(MAX_EXPLORATION_TRADES_PER_SESSION):
        assert can_execute_exploration_trade(dt) is True
        record_exploration_trade_executed(dt)

    assert get_exploration_trades_count() == MAX_EXPLORATION_TRADES_PER_SESSION
    assert can_execute_exploration_trade(dt) is False

    # Reset
    reset_exploration_trades_count()
    assert can_execute_exploration_trade(dt) is True


def test_interim_data_collection_pass_non_trading_interval():
    """Verify that process_single_market succeeds without UnboundLocalError during data-collection-only marks."""
    client = MagicMock(exchange_name="Kalshi")
    client.get_market_position.return_value = None
    client.has_existing_position_or_order.return_value = False

    market = {
        "ticker": "KXBTC15M-26AUG170200-00",
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.50",
        "yes_bid_dollars": "0.48",
        "no_ask_dollars": "0.52",
        "no_bid_dollars": "0.50",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }

    # Calling during an off-clock minute (minute 51 past the hour, non-trading mark)
    snap = process_single_market(
        market=market,
        is_trading_interval=False,
        client=client,
        now_utc=datetime(2026, 8, 17, 5, 51, 41, tzinfo=timezone.utc),
        minute_past_hour=51,
        spot_cache={"BTC": 60100.0},
        dry_run=False,
    )
    assert snap is not None
    assert snap["tick"] == "KXBTC15M-26AUG170200-00"
    assert snap["m"] == 51

