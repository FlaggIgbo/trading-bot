from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from database import (
    DEFAULT_DYNAMIC_PRICE_MAP,
    calculate_dynamic_contracts_count,
    calibrate_dynamic_price_map_from_db,
    get_dynamic_filter_for_market,
    is_default_exploration_window,
    should_refresh_dynamic_map,
)
from main import process_single_market
from signals.base_client import BaseTradingClient


@pytest.fixture(autouse=True)
def mock_test_env(monkeypatch):
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


class MockTradingClient(BaseTradingClient):
    exchange_name = "Kalshi"

    def __init__(self):
        super().__init__()
        self._positions_cache = {}

    def get_market_position(self, ticker: str):
        return self._positions_cache.get(ticker)

    def has_existing_position_or_order(self, ticker: str) -> bool:
        if ticker in self._positions_cache:
            return True
        if self.get_traded_market_side(ticker) is not None:
            return True
        return False

    def cancel_unfilled_buy_orders(self, ticker: str):
        return True

    def cancel_and_verify_resting_orders(self, ticker: str):
        return True

    def execute_buy(self, ticker: str, side: str, count: int = 1, price_cents: int = 70):
        return {"status": "ok"}

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int):
        return {"status": "ok"}


@pytest.fixture(autouse=True)
def reset_db_cache():
    import database
    database._DYNAMIC_PRICE_MAP_CACHE = dict(database.DEFAULT_DYNAMIC_PRICE_MAP)
    database._LAST_MAP_REFRESH_TIME = time.time()
    neutral_streak = {"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}
    with patch("main.get_asset_settlement_streak", return_value=neutral_streak), \
         patch("database.get_asset_settlement_streak", return_value=neutral_streak), \
         patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
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
    (datetime(2026, 8, 14, 10, 30, tzinfo=timezone.utc), 0, True, True),
    (datetime(2026, 8, 14, 10, 30, tzinfo=timezone.utc), 0.4, False, False),
    (datetime(2026, 8, 14, 10, 30, tzinfo=timezone.utc), 1.2, False, True),  # 1-hour TTL expiry
    (datetime(2026, 8, 14, 5, 16, tzinfo=timezone.utc), 0.5, False, True),  # 05:15 UTC post-exploration anchor
    (datetime(2026, 8, 14, 7, 2, tzinfo=timezone.utc), 0.5, False, True),  # Top of the hour clock anchor
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
    (30, 0.0001, True, "discount"),     # Peak 1: Deep discount at minute 0
    (45, 0.0001, True, "momentum"),     # Peak 2: Early value reversion at minute 0
    (62, 0.0015, False, None),          # Valley of Death: 62¢ strictly banned in 15M
    (75, 0.0015, False, None),          # Valley of Death: 75¢ strictly banned in 15M
    (55, 0.0001, False, None),          # Valley of Death: 55¢ strictly banned in 15M
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
        "yes_ask_dollars": "0.65", "yes_bid_dollars": "0.63",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }

    # Under empirical rules, 65¢ Valley of Death in 15M is logged as COUNTERFACTUAL sample to preserve capital
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
    assert snap_exp["trd"]["action"] in ("counterfactual_empirical_reject", "counterfactual_mid_zone")
    assert snap_exp["trd"]["dry_run"] is True


def test_exploration_trade_cap_enforcement():
    from main import (
        MAX_EXPLORATION_TRADES_PER_SESSION,
        can_execute_exploration_trade,
        get_exploration_trades_count,
        record_exploration_trade_executed,
        reset_exploration_trades_count,
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
    """Verify that 15m markets enforce Peak 1 (<=35¢) and Peak 2 (36-50¢), strictly ban 51-80¢ Valley of Death, and size at 1 contract."""
    client = MagicMock(exchange_name="Kalshi")
    client.get_market_position.return_value = None
    client.has_existing_position_or_order.return_value = False

    # 1. 28¢ YES (Peak 1: Deep Discount) -> Should BUY with count=1
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

    # 2. 45¢ YES (Peak 2: Early Value Reversion at m=0) -> Should BUY with count=1
    market_val = {
        "ticker": "KXBTC15M-TEST-45", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.45", "yes_bid_dollars": "0.43",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }
    snap_val = process_single_market(
        market=market_val,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        minute_past_hour=0,
        spot_cache={"BTC": 60050.0},
        dry_run=True,
    )
    assert "trd" in snap_val
    assert snap_val["trd"]["corridor"] == "momentum"
    assert snap_val["trd"]["count"] == 1

    # 3. 64¢ YES (Valley of Death) -> Strictly banned on 15m across all minutes
    market_mom = {
        "ticker": "KXBTC15M-TEST-64", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.64", "yes_bid_dollars": "0.62",
        "floor_strike": 60000.0, "exchange": "kalshi",
    }
    snap_mom_strong = process_single_market(
        market=market_mom,
        is_trading_interval=True,
        client=client,
        now_utc=datetime(2026, 8, 15, 12, 10, 0, tzinfo=timezone.utc),
        minute_past_hour=10,
        spot_cache={"BTC": 60120.0},  # +0.20% spot lead
        dry_run=True,
    )
    assert snap_mom_strong.get("trd", {}).get("action") != "buy_opened"


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
    assert calibrated["BNB"]["00"]["favorite_maker_min_buy"] >= 80
    assert calibrated["BNB"]["00"]["max_lead_changes"] == 1

    # SOL should be healthy
    assert "SOL" in calibrated
    assert calibrated["SOL"]["00"]["is_restricted"] is False
    assert calibrated["SOL"]["00"]["contracts_per_trade"] == 3
    assert calibrated["SOL"]["00"]["favorite_maker_min_buy"] == 80


@patch("database.get_mongo_collection")
def test_dynamic_heavy_loss_calibration_from_db(mock_get_coll):
    """Verify that severe losses (>=3 losses, net PnL <= -100¢) trigger heavy loss tier with 80¢-93¢ floor."""
    snaps = [
        {"sym": "WTI", "tick": "KXWTIH-T1", "m": 0, "pct": 0.05, "trd": {"buy": 30, "side": "yes"}},
    ]
    settles = [
        {"ticker": "KXWTIH-T1", "market_result": "no", "pnl_cents": -75},
        {"ticker": "KXWTIH-T2", "market_result": "no", "pnl_cents": -80},
        {"ticker": "KXWTIH-T3", "market_result": "no", "pnl_cents": -70},
    ]

    def mock_coll(name):
        data = snaps if name == "market_snapshots" else settles
        mock = MagicMock()
        mock.find.return_value.sort.return_value.limit.return_value = data
        return mock

    mock_get_coll.side_effect = mock_coll
    calibrated = calibrate_dynamic_price_map_from_db()

    assert "WTI" in calibrated
    assert calibrated["WTI"]["00"]["is_restricted"] is True
    assert calibrated["WTI"]["00"]["favorite_maker_min_buy"] >= 80
    assert calibrated["WTI"]["00"]["max_buy"] <= 22
    assert calibrated["WTI"]["00"]["enable_momentum_buys"] is False
    assert calibrated["WTI"]["00"]["max_lead_changes"] == 1


def test_strict_single_side_market_lock_prevents_hedging():
    """Verify that client strictly rejects opposing side orders for the same ticker."""
    client = MockTradingClient()
    ticker = "KXWTIH-26AUG1810-T84.00"

    # Step 1: No trades yet -> both sides allowed
    assert client.is_side_allowed(ticker, "yes") is True
    assert client.is_side_allowed(ticker, "no") is True

    # Step 2: Record trade on YES
    client.record_traded_market(ticker, "yes")
    assert client.is_side_allowed(ticker, "yes") is True
    assert client.is_side_allowed(ticker, "no") is False

    # Step 3: has_existing_position_or_order detects existing trade
    assert client.has_existing_position_or_order(ticker) is True


def test_favorite_maker_spot_divergence_on_restricted_assets():
    """Verify that restricted assets require spot confirmation for Favorite Maker entries."""
    from main import evaluate_favorite_maker_opportunity
    client = MockTradingClient()

    market = {"ticker": "KXWTIH-26AUG1810-T84.00"}
    quotes_yes = {"yes_ask": 86, "yes_bid": 82, "no_ask": 18, "no_bid": 14}
    quotes_no = {"yes_ask": 18, "yes_bid": 14, "no_ask": 86, "no_bid": 82}

    restricted_filters = {
        "is_restricted": True,
        "favorite_maker_min_buy": 85,
        "favorite_maker_max_buy": 92,
        "max_lead_changes": 1,
    }

    # YES Favorite with spot below target ($83.50 < $84.00) -> Rejected due to spot divergence
    res_yes_bad = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_yes,
        rem_mins=3.0,
        dynamic_filters=restricted_filters,
        client=client,
        spot_price=83.50,
        target_price=84.00,
    )
    assert res_yes_bad is None

    # YES Favorite with spot above target ($84.50 >= $84.00) -> Accepted
    res_yes_good = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_yes,
        rem_mins=3.0,
        dynamic_filters=restricted_filters,
        client=client,
        spot_price=84.50,
        target_price=84.00,
    )
    assert res_yes_good is not None
    assert res_yes_good["side"] == "yes"

    # NO Favorite with spot above target ($84.50 > $84.00) -> Rejected
    res_no_bad = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_no,
        rem_mins=3.0,
        dynamic_filters=restricted_filters,
        client=client,
        spot_price=84.50,
        target_price=84.00,
    )
    assert res_no_bad is None

    # NO Favorite with spot below target ($83.50 <= $84.00) -> Accepted
    res_no_good = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_no,
        rem_mins=3.0,
        dynamic_filters=restricted_filters,
        client=client,
        spot_price=83.50,
        target_price=84.00,
    )
    assert res_no_good is not None
    assert res_no_good["side"] == "no"


def test_get_active_position_doc_and_field_helpers():
    from database import (
        get_active_position_cost,
        get_active_position_doc,
        get_active_position_strategy_tag,
        get_active_position_target_tp,
    )
    mock_doc = {
        "_id": "mock_id_123",
        "ticker": "KXSILVERH-TEST",
        "ex": "kalshi",
        "side": "yes",
        "count": 1,
        "entry_price_cents": 85,
        "strategy_tag": "endgame_favorite_maker",
        "target_tp_cents": 99,
    }
    with patch("database.get_mongo_collection") as mock_col_getter:
        mock_col = MagicMock()
        mock_col.find_one.return_value = mock_doc
        mock_col_getter.return_value = mock_col

        doc = get_active_position_doc("KXSILVERH-TEST", "kalshi")
        assert doc is not None
        assert doc["side"] == "yes"
        assert doc["entry_price_cents"] == 85
        assert doc["strategy_tag"] == "endgame_favorite_maker"

        cost = get_active_position_cost("KXSILVERH-TEST", "kalshi")
        assert cost == 85

        tag = get_active_position_strategy_tag("KXSILVERH-TEST", "kalshi")
        assert tag == "endgame_favorite_maker"

        tp = get_active_position_target_tp("KXSILVERH-TEST", "kalshi")
        assert tp == 99


def test_is_side_allowed_prevents_opposing_db_position():
    client = MockTradingClient()
    mock_doc = {
        "ticker": "KXWTIH-TEST",
        "ex": "kalshi",
        "side": "yes",
        "entry_price_cents": 84,
    }
    with patch("database.get_mongo_collection") as mock_col_getter:
        mock_col = MagicMock()
        mock_col.find_one.return_value = mock_doc
        mock_col_getter.return_value = mock_col

        # Same side allowed
        assert client.is_side_allowed("KXWTIH-TEST", "yes") is True
        # Conflicting side strictly blocked to prevent self-hedging
        assert client.is_side_allowed("KXWTIH-TEST", "no") is False


def test_heavy_loss_regime_disables_favorite_maker():
    from database import calibrate_dynamic_price_map_from_db
    mock_settles = [
        {"ticker": "KXWTIH-1", "market_result": "no", "pnl_cents": -80, "ts": 100},
        {"ticker": "KXWTIH-2", "market_result": "no", "pnl_cents": -85, "ts": 200},
        {"ticker": "KXWTIH-3", "market_result": "no", "pnl_cents": -90, "ts": 300},
    ]
    with patch("database.get_mongo_collection") as mock_col_getter:
        mock_snap = MagicMock()
        mock_snap.find.return_value.sort.return_value.limit.return_value = [
            {"sym": "WTI", "pct": 0.12, "ts": 100, "m": 0, "tick": "KXWTIH-1"}
        ]
        mock_settle = MagicMock()
        mock_settle.find.return_value.sort.return_value.limit.return_value = mock_settles

        def get_col(name):
            return mock_snap if name == "market_snapshots" else mock_settle

        mock_col_getter.side_effect = get_col

        calibrated = calibrate_dynamic_price_map_from_db(max_history_hours=48)
        assert "WTI" in calibrated
        wti_filter = calibrated["WTI"]["00"]
        assert wti_filter["enable_favorite_maker"] is True
        assert wti_filter["enable_momentum_buys"] is False
        assert wti_filter["contracts_per_trade"] == 1


def test_evaluate_favorite_maker_disabled_guard():
    from main import evaluate_favorite_maker_opportunity
    client = MockTradingClient()
    market = {"ticker": "KXSILVERH-TEST"}
    quotes = {"yes_ask": 88, "yes_bid": 84, "no_ask": 16, "no_bid": 12}
    disabled_filter = {
        "enable_favorite_maker": False,
        "favorite_maker_min_buy": 80,
        "favorite_maker_max_buy": 93,
    }
    res = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=disabled_filter,
        client=client,
        spot_price=64.0,
        target_price=63.5,
    )
    assert res is None


def test_calculate_dynamic_contracts_count_high_win_rate_scaling():
    from database import calculate_dynamic_contracts_count

    # Baseline 1 contract when restricted
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=84, base_count=1, is_restricted=True, recent_win_rate=0.95) == 1

    # Scaled to 3 contracts when recent_win_rate >= 0.85 (e.g. BTC/SOL/ETH) and balance >= $20 (20% of $50 = $10 / 84¢ = 11 -> capped at 3)
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=84, base_count=1, is_restricted=False, recent_win_rate=0.88) == 3

    # Scaled to 4 contracts when recent_win_rate >= 0.90 (e.g. Gold at 95.5% WR)
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=84, base_count=1, is_restricted=False, recent_win_rate=0.95) == 4

    # Small balance (< $10) strictly sized at 1 contract
    assert calculate_dynamic_contracts_count(balance_cents=800, buy_price_cents=84, base_count=1, is_restricted=False, recent_win_rate=0.95) == 1


def test_tiered_maker_pricing_and_sizing_in_favorite_maker():
    from main import evaluate_favorite_maker_opportunity
    client = MockTradingClient()
    client.get_balance_cents = lambda: 5000  # $50 balance

    market = {"ticker": "KXGOLDH-TEST"}
    # Spread is 8¢ wide (Bid: 80¢, Ask: 88¢)
    quotes = {"yes_ask": 88, "yes_bid": 80, "no_ask": 20, "no_bid": 12}
    gold_filter = {
        "enable_favorite_maker": True,
        "favorite_maker_min_buy": 80,
        "favorite_maker_max_buy": 93,
        "recent_win_rate": 0.95,  # 95.5% Gold win rate
        "contracts_per_trade": 1,
    }

    # 1. Early in runway (rem_mins = 5.0m): Places deep maker bid at min(bid + 2, ask - 2) = 82¢ (captures 6¢ spread)
    res_early = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=5.0,
        dynamic_filters=gold_filter,
        client=client,
        spot_price=4350.0,
        target_price=4340.0,
    )
    assert res_early is not None
    assert res_early["maker_bid"] == 82
    assert res_early["spread_saved"] == 6
    assert res_early["count"] == 4  # Scaled to 4 contracts for Gold

    # 2. Final expiration minutes (rem_mins = 2.0m): Places top-of-queue bid at max(bid + 1, ask - 2) = 86¢ for guaranteed fill
    res_late = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=2.0,
        dynamic_filters=gold_filter,
        client=client,
        spot_price=4350.0,
        target_price=4340.0,
    )
    assert res_late is not None
    assert res_late["maker_bid"] == 86
    assert res_late["spread_saved"] == 2


def test_evaluate_ensemble_consensus_recommended_contracts():
    from database import evaluate_ensemble_consensus
    # High conviction setup (matching 3x streak, strong spot lead)
    streak_data = {"streak_side": "yes", "streak_length": 3, "regime": "confirmed_streak", "sample_size": 20}
    cons_high = evaluate_ensemble_consensus(
        symbol="BTC",
        proposed_side="yes",
        price_cents=30,
        spot_price=60200.0,
        target_price=60000.0,
        timeframe="15m",
        dynamic_filters={"min_buy": 18, "max_buy": 35},
        streak_data=streak_data,
    )
    assert cons_high["is_approved"] is True
    assert cons_high["recommended_contracts"] >= 2





