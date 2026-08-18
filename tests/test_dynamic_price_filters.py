from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from unittest.mock import MagicMock, patch
import pytest

from database import (
    DEFAULT_DYNAMIC_PRICE_MAP,
    DEFAULT_DYNAMIC_PRICE_MAP_1H,
    calculate_dynamic_contracts_count,
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
        f_15m = get_dynamic_filter_for_market(sym, is_5min_mark=False, timeframe="15m", now_utc=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc))
        assert f_15m["sl_threshold"] == 20
        assert f_15m["sl_floor"] == 15
        assert f_15m["cut_5min"] == 15
        assert f_15m["max_buy"] == 30
        assert f_15m["contracts_per_trade"] == 1
        assert f_15m["momentum_min_buy"] == 62
        assert f_15m["momentum_min_pct_dist"] == 0.15

        f_1h = get_dynamic_filter_for_market(sym, is_5min_mark=False, timeframe="1h", now_utc=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc))
        assert f_1h["sl_threshold"] == 25
        assert f_1h["sl_floor"] == 15
        assert f_1h["cut_5min"] == 15
        assert f_1h["max_buy"] == 44
        assert f_1h["contracts_per_trade"] == 3
        assert f_1h["momentum_min_buy"] == 56
        assert f_1h["momentum_min_pct_dist"] == 0.10


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

    def mock_coll(name):
        data = snaps if name == "market_snapshots" else settles
        mock = MagicMock()
        mock.find.return_value.sort.return_value.limit.return_value = data
        return mock

    mock_get_coll.side_effect = mock_coll
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


def test_15m_tighter_corridors_and_sizing():
    """Verify that 15m markets enforce tighter discount corridors (<=30¢), require >=0.15% spot lead for momentum, and size at 1 contract."""
    client = MagicMock(exchange_name="Kalshi")
    client.get_market_position.return_value = None
    client.has_existing_position_or_order.return_value = False

    # 1. 28¢ YES (Deep Discount) -> Should BUY with count=1
    market_discount = {
        "ticker": "KXBTC15M-TEST-28", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.28", "yes_bid_dollars": "0.26",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }
    snap_discount = process_single_market(
        market=market_discount,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60010.0},
        dry_run=True,
    )
    assert "trd" in snap_discount
    assert snap_discount["trd"]["corridor"] == "discount"
    assert snap_discount["trd"]["count"] == 1

    # 2. 38¢ YES (Noise / Chop Band) -> Rejected on 15m because 38¢ falls in dead zone (31¢-61¢)
    market_chop = {
        "ticker": "KXBTC15M-TEST-38", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.38", "yes_bid_dollars": "0.36",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }
    snap_chop = process_single_market(
        market=market_chop,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60050.0},
        dry_run=True,
    )
    assert snap_chop.get("trd", {}).get("action") != "buy_opened"

    # 3. 64¢ YES with +0.20% spot lead -> Enters momentum with count=1
    market_mom = {
        "ticker": "KXBTC15M-TEST-64", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.64", "yes_bid_dollars": "0.62",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }
    snap_mom_strong = process_single_market(
        market=market_mom,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60120.0},  # +0.20% spot lead
        dry_run=True,
    )
    assert "trd" in snap_mom_strong
    assert snap_mom_strong["trd"]["corridor"] == "momentum"
    assert snap_mom_strong["trd"]["count"] == 1

    # 4. 64¢ YES with +0.10% spot lead -> Rejected on 15m (requires >=0.15% lead)
    snap_mom_weak = process_single_market(
        market=market_mom,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60060.0},  # +0.10% spot lead (below 0.15%)
        dry_run=True,
    )
    assert snap_mom_weak.get("trd", {}).get("action") != "buy_opened"


def test_wti_restricted_profile_defaults():
    """Verify that WTI has restricted profiles by default: 1 contract, tighter corridors, and higher spot lead."""
    f_wti_1h = get_dynamic_filter_for_market("WTI", is_5min_mark=False, timeframe="1h", now_utc=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc))
    assert f_wti_1h["is_restricted"] is True
    assert f_wti_1h["contracts_per_trade"] == 1
    assert f_wti_1h["max_buy"] == 30  # Tighter than standard 44¢
    assert f_wti_1h["momentum_min_pct_dist"] == 0.20  # Stricter than standard 0.10%
    assert f_wti_1h["sl_threshold"] == 20

    f_wti_15m = get_dynamic_filter_for_market("WTI", is_5min_mark=False, timeframe="15m", now_utc=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc))
    assert f_wti_15m["is_restricted"] is True
    assert f_wti_15m["contracts_per_trade"] == 1
    assert f_wti_15m["max_buy"] == 28
    assert f_wti_15m["momentum_min_pct_dist"] == 0.20


def test_calculate_dynamic_contracts_count_rules():
    """Verify dynamic bankroll sizing rules across balances, prices, and restriction states."""
    # 1. Restricted market -> strictly 1 contract even if balance is large
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=30, base_count=3, is_restricted=True) == 1

    # 2. Small bankroll (<$10.00 / 1000¢) -> strictly 1 contract
    assert calculate_dynamic_contracts_count(balance_cents=375, buy_price_cents=30, base_count=3, is_restricted=False) == 1
    assert calculate_dynamic_contracts_count(balance_cents=800, buy_price_cents=25, base_count=3, is_restricted=False) == 1

    # 3. Unaffordable trade (balance < price) -> returns 0
    assert calculate_dynamic_contracts_count(balance_cents=18, buy_price_cents=30, base_count=1, is_restricted=False) == 0

    # 4. Large bankroll ($50.00 / 5000¢) with 15% risk ($7.50 / 750¢ max risk per trade) -> 3 contracts @ 30¢ (cost 90¢ <= 750¢)
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=30, base_count=3, is_restricted=False) == 3

    # 5. Base count cap respected (e.g. 15m with base_count=1 on large bankroll returns 1)
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=30, base_count=1, is_restricted=False) == 1

    # 6. Mock / None balance -> returns base_count (or 1 if restricted)
    assert calculate_dynamic_contracts_count(balance_cents=None, buy_price_cents=30, base_count=3, is_restricted=False) == 3
    assert calculate_dynamic_contracts_count(balance_cents=None, buy_price_cents=30, base_count=3, is_restricted=True) == 1


@patch("database.get_mongo_collection")
def test_dynamic_loss_restriction_calibration_from_db(mock_get_coll):
    """Verify that assets with recent heavy losses are dynamically restricted."""
    snaps = [
        {"sym": "BNB", "tick": "KXBNB15M-T1", "m": 0, "pct": 0.02, "trd": {"buy": 32, "side": "yes"}},
        {"sym": "SOL", "tick": "KXSOL15M-T2", "m": 0, "pct": 0.02, "trd": {"buy": 28, "side": "yes"}},
    ]
    # BNB: 1 win, 4 losses (20% WR, negative PnL) -> Restricted
    # SOL: 3 wins, 0 losses (100% WR, positive PnL) -> Healthy
    settles = [
        {"ticker": "KXBNB15M-T1", "market_result": "no", "pnl_cents": -35},
        {"ticker": "KXBNB15M-T2", "market_result": "no", "pnl_cents": -30},
        {"ticker": "KXBNB15M-T3", "market_result": "no", "pnl_cents": -30},
        {"ticker": "KXBNB15M-T4", "market_result": "yes", "pnl_cents": 65},
        {"ticker": "KXBNB15M-T5", "market_result": "no", "pnl_cents": -35},
        {"ticker": "KXSOL15M-T1", "market_result": "yes", "pnl_cents": 70},
        {"ticker": "KXSOL15M-T2", "market_result": "yes", "pnl_cents": 72},
        {"ticker": "KXSOL15M-T3", "market_result": "yes", "pnl_cents": 68},
    ]

    def mock_coll(name):
        data = snaps if name == "market_snapshots" else settles
        mock = MagicMock()
        mock.find.return_value.sort.return_value.limit.return_value = data
        return mock

    mock_get_coll.side_effect = mock_coll
    calibrated = calibrate_dynamic_price_map_from_db()

    # BNB should be dynamically restricted
    assert "BNB" in calibrated
    assert calibrated["BNB"]["00"]["is_restricted"] is True
    assert calibrated["BNB"]["00"]["contracts_per_trade"] == 1
    assert calibrated["BNB"]["00"]["max_buy"] <= 28
    assert calibrated["BNB"]["00"]["momentum_min_pct_dist"] == 0.20

    # SOL should be healthy
    assert "SOL" in calibrated
    assert calibrated["SOL"]["00"]["is_restricted"] is False
    assert calibrated["SOL"]["00"]["contracts_per_trade"] == 3


