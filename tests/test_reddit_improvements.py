from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from database import (
    get_active_position_cost,
    get_active_position_strategy_tag,
    run_graveyard_analytics_report,
    save_active_position_cost,
)
from main import (
    _execute_favorite_maker_flow,
    evaluate_favorite_maker_opportunity,
    get_crypto_spot_price,
    get_kalshi_cfbenchmarks_spot_price,
    get_market_lead_changes,
    get_market_settlement_source,
    is_market_choppy,
    process_single_market,
    record_market_price_snapshot,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import (
    CFBENCHMARKS_CRYPTO_INDEX_MAP,
    KalshiClient,
)


class MockTradingClient(BaseTradingClient):
    exchange_name = "Kalshi"

    def __init__(self):
        super().__init__()
        self._positions_cache = {}
        self._mock_orders = []

    def get_market_position(self, ticker: str):
        return self._positions_cache.get(ticker)

    def cancel_unfilled_buy_orders(self, ticker: str):
        return True

    def cancel_and_verify_resting_orders(self, ticker: str):
        return True

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self._positions_cache

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
        self._positions_cache[ticker] = {"side": side.lower(), "count": count, "entry_price_cents": price_cents}
        self.set_entry_price(ticker, price_cents)
        return {"status": "bought", "price": price_cents, "fill_count": count}

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int):
        self._positions_cache.pop(ticker, None)
        self.clear_profit_floor(ticker)
        self.clear_entry_price(ticker)
        return {"status": "liquidated", "price": current_price}

    def buy_favorite_maker_order(
        self,
        ticker: str,
        side: str,
        maker_bid_cents: int,
        target_ask_cents: int,
        count: int = 1,
        buy_order_timeout_seconds: int = 60,
    ):
        self._positions_cache[ticker] = {
            "side": side.lower(),
            "count": count,
            "entry_price_cents": maker_bid_cents,
        }
        self.set_entry_price(
            ticker=ticker,
            price_cents=maker_bid_cents,
            side=side.lower(),
            count=count,
            strategy_tag="endgame_favorite_maker",
            spread_saved_cents=target_ask_cents - maker_bid_cents,
            order_role="maker",
        )
        order_dict = {
            "order_id": f"fav_order_{ticker}",
            "client_order_id": f"client_fav_{ticker}",
            "buy_price_cents": maker_bid_cents,
            "side": side.lower(),
        }
        self._mock_orders.append(order_dict)
        return order_dict

    def get_all_active_positions(self):
        return self._positions_cache


# ---------------------------------------------------------------------------
# 1. CF Benchmarks Real-Time Index Ingestion & Settlement Oracle Tests
# ---------------------------------------------------------------------------

def test_cfbenchmarks_crypto_index_mapping():
    """Verify all major Kalshi crypto series map to their official CF Benchmarks RTI tickers."""
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["BTC"] == "BRTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["ETH"] == "ETHUSD_RTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["SOL"] == "SOLUSD_RTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["DOGE"] == "DOGEUSD_RTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["XRP"] == "XRPUSD_RTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["HYPE"] == "HYPEUSD_RTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["NEAR"] == "NEARUSD_RTI"
    assert CFBENCHMARKS_CRYPTO_INDEX_MAP["ZEC"] == "ZECUSD_RTI"


def test_cfbenchmarks_price_fetching_and_twap():
    """Verify KalshiClient.get_cfbenchmarks_price correctly parses ticks and computes 60s TWAP."""
    client = KalshiClient(api_key_id="k", private_key=MagicMock())

    sample_ticks = {
        "data": {
            "serverTime": "2026-08-17T20:00:00Z",
            "payload": [
                {"value": "64000.00", "time": 1787014200000},  # Older than 60s
                {"value": "64100.00", "time": 1787014250000},  # In 60s window
                {"value": "64200.00", "time": 1787014280000},  # In 60s window
                {"value": "64300.00", "time": 1787014300000},  # Latest tick
            ],
        }
    }

    with patch.object(client, "get_cfbenchmarks_values", return_value=sample_ticks):
        # 1. Latest spot price (no TWAP)
        latest = client.get_cfbenchmarks_price("BTC", calc_twap_seconds=0)
        assert latest == 64300.00

        # 2. 60-second TWAP average (last 3 ticks: 64100, 64200, 64300 -> avg = 64200.0)
        twap = client.get_cfbenchmarks_price("BTC", calc_twap_seconds=60)
        assert twap == 64200.0


def test_get_market_settlement_source():
    """Verify dynamic settlement source identification matches official series rules."""
    client = MockTradingClient()

    # Crypto -> CF Benchmarks
    btc_mkt = {"series_ticker": "KXBTC15M", "ticker": "KXBTC15M-26AUG170200-00"}
    assert get_market_settlement_source(btc_mkt, client=client) == "CF Benchmarks"

    # Commodities -> Pyth Network
    gold_mkt = {"series_ticker": "KXGOLD15M", "ticker": "KXGOLD15M-26AUG170200-00"}
    assert get_market_settlement_source(gold_mkt, client=client) == "Pyth Network"

    # Indices -> Yahoo Finance
    spx_mkt = {"series_ticker": "KXINXU", "ticker": "KXINXU-26AUG170200-00"}
    assert get_market_settlement_source(spx_mkt, client=client) == "Yahoo Finance"


# ---------------------------------------------------------------------------
# 2. Endgame Favorite Maker Strategy (74¢–93¢) Tests
# ---------------------------------------------------------------------------

def test_evaluate_favorite_maker_opportunity_yes_and_no():
    """Verify Favorite Maker accurately identifies YES and NO favorites in 74¢–93¢ corridor."""
    client = MockTradingClient()
    filters = {"favorite_maker_min_buy": 74, "favorite_maker_max_buy": 93, "max_lead_changes": 2}

    # Case 1: YES is Favorite (Ask: 82¢, Bid: 79¢) -> Should place maker bid at 80¢ (saving +2¢)
    market_yes = {"ticker": "KXBTC15M-FAV-YES"}
    quotes_yes = {"yes_ask": 82, "yes_bid": 79, "no_ask": 21, "no_bid": 18}
    fav_yes = evaluate_favorite_maker_opportunity(
        market=market_yes, quotes=quotes_yes, rem_mins=3.5, dynamic_filters=filters, client=client
    )
    assert fav_yes is not None
    assert fav_yes["side"] == "yes"
    assert fav_yes["maker_bid"] == 80
    assert fav_yes["spread_saved"] == 2

    # Case 2: NO is Favorite (NO Ask: 85¢, NO Bid: 81¢) -> Should place maker bid at 83¢
    market_no = {"ticker": "KXETH15M-FAV-NO"}
    quotes_no = {"yes_ask": 19, "yes_bid": 15, "no_ask": 85, "no_bid": 81}
    fav_no = evaluate_favorite_maker_opportunity(
        market=market_no, quotes=quotes_no, rem_mins=2.0, dynamic_filters=filters, client=client
    )
    assert fav_no is not None
    assert fav_no["side"] == "no"
    assert fav_no["maker_bid"] == 83
    assert fav_no["spread_saved"] == 2

    # Case 3: Coin-Flip (Ask: 55¢) -> Not a favorite, should return None
    quotes_mid = {"yes_ask": 55, "yes_bid": 50, "no_ask": 50, "no_bid": 45}
    assert evaluate_favorite_maker_opportunity(
        market=market_yes, quotes=quotes_mid, rem_mins=3.0, dynamic_filters=filters, client=client
    ) is None

    # Case 4: Expired or too early window (rem_mins = 10.0m) -> Should return None
    assert evaluate_favorite_maker_opportunity(
        market=market_yes, quotes=quotes_yes, rem_mins=10.0, dynamic_filters=filters, client=client
    ) is None


def test_favorite_maker_execution_and_mongo_snapshot():
    """Verify Favorite Maker order executes resting maker limit bid and records strategy_tag."""
    client = MockTradingClient()
    market = {
        "ticker": "KXSOL15M-26AUG170200-00",
        "series_ticker": "KXSOL15M",
        "yes_ask_dollars": "0.84",
        "yes_bid_dollars": "0.80",
        "no_ask_dollars": "0.20",
        "no_bid_dollars": "0.16",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 140.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 1, 57, 0, tzinfo=timezone.utc)  # 3 minutes remaining

    snap = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=now_utc,
        minute_past_hour=0,  # :00 mark (trading interval)
        spot_cache={"SOL": 142.5},
        dry_run=False,
    )

    assert snap is not None
    assert snap["fav"] == "yes"
    assert snap["fav_maker"] is True
    assert "trd" in snap
    assert snap["trd"]["strategy_tag"] == "endgame_favorite_maker"
    assert snap["trd"]["order_role"] == "maker"
    assert client.get_strategy_tag("KXSOL15M-26AUG170200-00") == "endgame_favorite_maker"


# ---------------------------------------------------------------------------
# 3. EV Preservation: Stop-Loss Bypass on Favorites Tests
# ---------------------------------------------------------------------------

def test_ev_preservation_favorite_holding_bypasses_stop_losses():
    """Verify that positions entered as favorites (>=70¢ or endgame_favorite_maker) bypass panic stop loss."""
    client = MockTradingClient()
    ticker = "KXBTC15M-FAVORITE-HOLD"
    client.set_entry_price(
        ticker=ticker,
        price_cents=80,
        side="yes",
        count=1,
        strategy_tag="endgame_favorite_maker",
    )
    client._positions_cache[ticker] = {"side": "yes", "count": 1, "entry_price_cents": 80}

    # 1. 10-Minute Stop Loss Check (:10 mark) when bid dips to 45¢ (underwater)
    res_10m = client.evaluate_and_execute_10min_strategy(
        ticker=ticker,
        current_yes_bid=45,
        current_no_bid=50,
        dry_run=False,
        stop_loss_threshold_cents=10,
        price_floor=30,
    )
    # Must NOT trigger panic stop-loss liquidation!
    assert res_10m["action"] == "favorite_maker_holding"
    assert res_10m["triggered"] is False
    assert client.get_market_position(ticker) is not None  # Position retained!

    # 2. 5-Minute Early Cut Loss Check when price dips to 20¢
    res_5m = client.evaluate_and_execute_5min_cut_loss(
        ticker=ticker,
        current_yes_price=20,
        current_no_price=75,
        min_price_threshold=35,
        dry_run=False,
    )
    assert res_5m["action"] == "favorite_maker_holding"
    assert res_5m["triggered"] is False

    # 3. 12.5-Minute Endgame Check -> Should hold to settlement payout
    res_12m = client.evaluate_and_execute_12_5min_endgame_check(
        ticker=ticker,
        current_yes_price=55,
        current_no_price=40,
        dry_run=False,
        endgame_floor=25,
    )
    assert res_12m["action"] == "favorite_maker_holding_to_settle"
    assert res_12m["triggered"] is False


# ---------------------------------------------------------------------------
# 4. Cross-Asset Portfolio Directional Risk Cap (Comment p3ok6ft) Tests
# ---------------------------------------------------------------------------

def test_cross_asset_portfolio_directional_risk_cap():
    """Verify that concurrent directional exposure across correlated cryptos is capped at max limit (3)."""
    client = KalshiClient(api_key_id="k", private_key=MagicMock())

    with patch.object(
        client,
        "get_all_active_positions",
        return_value={
            "KXBTC15M-1": {"side": "yes", "count": 1},
            "KXETH15M-1": {"side": "yes", "count": 1},
            "KXSOL15M-1": {"side": "yes", "count": 1},
        },
    ):
        # 3 YES crypto positions active -> Directional counts: {"yes": 3, "no": 0}
        counts = client.get_crypto_directional_counts()
        assert counts["yes"] == 3
        assert counts["no"] == 0

        # Attempting a 4th YES crypto trade must be blocked!
        assert client.check_crypto_portfolio_directional_cap("yes", max_concurrent=3) is False

        # Attempting a NO crypto trade is allowed (0 NO positions active)
        assert client.check_crypto_portfolio_directional_cap("no", max_concurrent=3) is True


# ---------------------------------------------------------------------------
# 5. Market Choppiness / Lead-Change Filter Tests
# ---------------------------------------------------------------------------

def test_market_choppiness_lead_change_filter():
    """Verify that markets bouncing across 50¢ more than 2 times are flagged as choppy and skipped."""
    ticker_clean = "KXBTC15M-CLEAN-TREND"
    ticker_choppy = "KXETH15M-CHOPPY-LEAD-CHANGES"

    t0 = time.time()
    # Clean trend: 30¢ -> 40¢ -> 60¢ -> 80¢ (1 lead change across 50¢)
    record_market_price_snapshot(ticker_clean, 30, t0 - 300)
    record_market_price_snapshot(ticker_clean, 40, t0 - 200)
    record_market_price_snapshot(ticker_clean, 60, t0 - 100)
    record_market_price_snapshot(ticker_clean, 80, t0)

    assert get_market_lead_changes(ticker_clean) == 1
    assert is_market_choppy(ticker_clean, max_lead_changes=2) is False

    # Choppy whipsaw: 65¢ (>=50) -> 35¢ (<50) -> 70¢ (>=50) -> 40¢ (<50) -> 75¢ (>=50) (4 lead changes!)
    record_market_price_snapshot(ticker_choppy, 65, t0 - 400)
    record_market_price_snapshot(ticker_choppy, 35, t0 - 300)
    record_market_price_snapshot(ticker_choppy, 70, t0 - 200)
    record_market_price_snapshot(ticker_choppy, 40, t0 - 100)
    record_market_price_snapshot(ticker_choppy, 75, t0)

    assert get_market_lead_changes(ticker_choppy) == 4
    assert is_market_choppy(ticker_choppy, max_lead_changes=2) is True


# ---------------------------------------------------------------------------
# 6. MongoDB Atlas Graveyard Analytics & Position Persistence Tests
# ---------------------------------------------------------------------------

def test_active_position_persistence_with_strategy_tag():
    """Verify MongoDB persistence stores and restores strategy_tag, spread_saved, and order_role."""
    mock_collection = MagicMock()
    mock_collection.find_one.return_value = {
        "ticker": "KXBTC15M-TEST-PERSIST",
        "entry_price_cents": 82,
        "strategy_tag": "endgame_favorite_maker",
        "order_role": "maker",
        "spread_saved_cents": 2,
    }

    with patch("database.get_mongo_collection", return_value=mock_collection):
        # Save
        saved = save_active_position_cost(
            ticker="KXBTC15M-TEST-PERSIST",
            side="yes",
            count=1,
            entry_price_cents=82,
            strategy_tag="endgame_favorite_maker",
            spread_saved_cents=2,
            order_role="maker",
        )
        assert saved is True
        assert mock_collection.update_one.called

        # Retrieve
        cost = get_active_position_cost("KXBTC15M-TEST-PERSIST")
        tag = get_active_position_strategy_tag("KXBTC15M-TEST-PERSIST")
        assert cost == 82
        assert tag == "endgame_favorite_maker"


def test_run_graveyard_analytics_report():
    """Verify run_graveyard_analytics_report computes price bands, maker vs taker, and correlation matrix."""
    mock_settle_col = MagicMock()
    # Mock sample historical settlements representing empirical findings
    mock_settlements = [
        # Heavy Favorites (74¢–93¢): Wins
        {"ticker": "KXBTC15M-1", "entry_price_cents": 80, "pnl_cents": 20, "order_role": "maker", "spread_saved_cents": 2, "lowest_price_during_hold": 65, "settled_time_ts": 1787014200, "market_result": "yes"},
        {"ticker": "KXETH15M-1", "entry_price_cents": 85, "pnl_cents": 15, "order_role": "maker", "spread_saved_cents": 2, "lowest_price_during_hold": 70, "settled_time_ts": 1787014200, "market_result": "yes"},
        # Longshots (<40¢): Loses (Negative EV Favorite-Longshot Bias)
        {"ticker": "KXDOGE15M-1", "entry_price_cents": 25, "pnl_cents": -25, "order_role": "taker", "settled_time_ts": 1787014200, "market_result": "no"},
    ]
    mock_settle_col.find.return_value.sort.return_value.limit.return_value = mock_settlements

    with patch("database.get_mongo_collection", return_value=mock_settle_col):
        report = run_graveyard_analytics_report(max_history_hours=72)
        assert report["total_analyzed_settlements"] == 3
        # Check heavy favorites band
        assert report["price_bands"]["heavy_fav_74_93c"]["trades"] == 2
        assert report["price_bands"]["heavy_fav_74_93c"]["win_rate_pct"] == 100.0
        # Check longshots band
        assert report["price_bands"]["longshots_<40c"]["trades"] == 1
        assert report["price_bands"]["longshots_<40c"]["win_rate_pct"] == 0.0
        # Check dipped favorite recovery
        assert report["dipped_favorite_recovery"]["total_dipped_favorites"] == 2
        assert report["dipped_favorite_recovery"]["recovered_to_win"] == 2
        assert report["dipped_favorite_recovery"]["recovery_rate_pct"] == 100.0
        # Check BTC-ETH correlation
        assert "BTC_ETH" in report["crypto_correlation_matrix"]
        assert report["crypto_correlation_matrix"]["BTC_ETH"]["co_directional_pct"] == 100.0


# ---------------------------------------------------------------------------
# 7. Hourly Markets Parity Tests (Favorite Maker, Choppiness, Risk Caps)
# ---------------------------------------------------------------------------

def test_hourly_market_favorite_maker_priority_execution():
    """Verify that hourly multi-strike groups detect and execute Favorite Maker orders as Priority 1."""
    from main import process_hourly_market_group

    client = MockTradingClient()
    # Create 3 strikes for hourly ETH
    markets = [
        {
            "ticker": "KXETHD-26AUG17-T2600",
            "series_ticker": "KXETHD",
            "yes_ask_dollars": "0.86",
            "yes_bid_dollars": "0.82",
            "no_ask_dollars": "0.18",
            "no_bid_dollars": "0.14",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 2600.0,
            "exchange": "kalshi",
        },
        {
            "ticker": "KXETHD-26AUG17-T2700",
            "series_ticker": "KXETHD",
            "yes_ask_dollars": "0.35",
            "yes_bid_dollars": "0.30",
            "no_ask_dollars": "0.70",
            "no_bid_dollars": "0.65",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 2700.0,
            "exchange": "kalshi",
        },
    ]
    now_utc = datetime(2026, 8, 17, 20, 50, 0, tzinfo=timezone.utc)  # 10m remaining in hour

    snaps = process_hourly_market_group(
        symbol="ETH",
        markets=markets,
        client=client,
        now_utc=now_utc,
        minute_past_hour=10,  # :10 mark
        spot_cache={"ETH": 2650.0},
        dry_run=False,
    )

    assert len(snaps) == 2
    # First strike should be identified and executed as Favorite Maker (YES Ask: 86¢, Maker Bid: 84¢)
    strike_fav = next(s for s in snaps if s["tick"] == "KXETHD-26AUG17-T2600")
    assert strike_fav["fav"] == "yes"
    assert strike_fav["fav_maker"] is True
    assert "trd" in strike_fav
    assert strike_fav["trd"]["strategy_tag"] == "endgame_favorite_maker"
    assert strike_fav["trd"]["order_role"] == "maker"
    assert client.get_strategy_tag("KXETHD-26AUG17-T2600") == "endgame_favorite_maker"


def test_hourly_market_choppiness_rejection():
    """Verify that hourly multi-strike scanning filters out choppy contracts with > 2 lead changes."""
    from main import process_hourly_market_group

    client = MockTradingClient()
    ticker = "KXBTCD-26AUG17-T64000"
    t0 = time.time()
    # Create 4 flips across 50¢
    record_market_price_snapshot(ticker, 60, t0 - 400)
    record_market_price_snapshot(ticker, 30, t0 - 300)
    record_market_price_snapshot(ticker, 65, t0 - 200)
    record_market_price_snapshot(ticker, 35, t0 - 100)
    record_market_price_snapshot(ticker, 60, t0)

    assert get_market_lead_changes(ticker) == 4

    markets = [
        {
            "ticker": ticker,
            "series_ticker": "KXBTCD",
            "yes_ask_dollars": "0.30",
            "yes_bid_dollars": "0.25",
            "no_ask_dollars": "0.75",
            "no_bid_dollars": "0.70",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 64000.0,
            "exchange": "kalshi",
        }
    ]
    now_utc = datetime(2026, 8, 17, 20, 0, 0, tzinfo=timezone.utc)

    snaps = process_hourly_market_group(
        symbol="BTC",
        markets=markets,
        client=client,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"BTC": 64500.0},
        dry_run=False,
    )
    assert len(snaps) == 1
    # Choppy market must not have executed a new buy order
    assert "trd" not in snaps[0] or snaps[0]["trd"].get("action") != "buy_opened"

