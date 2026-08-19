from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from database import (
    evaluate_ensemble_consensus,
    get_asset_settlement_streak,
)
from main import (
    process_hourly_market_group,
    process_single_market,
)
from signals.base_client import BaseTradingClient


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
        self._positions_cache[ticker] = {
            "side": side.lower(),
            "count": count,
            "entry_price_cents": price_cents,
        }
    def buy_and_set_take_profit(self, ticker: str, side: str, buy_price_cents: int, maker_bid_cents: int = None, **kw):
        effective_buy = maker_bid_cents if maker_bid_cents is not None else buy_price_cents
        self._positions_cache[ticker] = {
            "side": side.lower(),
            "count": kw.get("count", 1),
            "entry_price_cents": effective_buy,
        }
        self.set_entry_price(ticker, effective_buy)
        return {
            "status": "placed",
            "buy_price_cents": effective_buy,
            "order_id": f"ord_{ticker}",
            "client_order_id": f"cli_{ticker}",
            "tp_order_id": f"tp_{ticker}",
        }

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


# ===========================================================================
# 1. Settlement Streak Autocorrelation Tests
# ===========================================================================

def test_settlement_streak_consecutive_yes():
    """Verify get_asset_settlement_streak correctly detects consecutive YES runs."""
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value.limit.return_value = [
        {"market_result": "yes", "settled_time": "2026-08-17T20:00:00Z"},
        {"market_result": "yes", "settled_time": "2026-08-17T19:45:00Z"},
        {"market_result": "yes", "settled_time": "2026-08-17T19:30:00Z"},
        {"market_result": "no", "settled_time": "2026-08-17T19:15:00Z"},
    ]

    with patch("database.get_mongo_collection", return_value=mock_coll):
        streak = get_asset_settlement_streak("BTC", timeframe="15m", force_refresh=True)

    assert streak["symbol"] == "BTC"
    assert streak["timeframe"] == "15m"
    assert streak["streak_side"] == "yes"
    assert streak["streak_length"] == 3
    assert streak["recent_outcomes"] == ["yes", "yes", "yes", "no"]
    assert streak["yes_win_rate_pct"] == 75.0
    assert streak["regime"] == "strong_yes_streak"


def test_settlement_streak_consecutive_no():
    """Verify get_asset_settlement_streak correctly detects consecutive NO runs."""
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value.limit.return_value = [
        {"market_result": "no", "settled_time": "2026-08-17T20:00:00Z"},
        {"market_result": "no", "settled_time": "2026-08-17T19:45:00Z"},
        {"market_result": "yes", "settled_time": "2026-08-17T19:30:00Z"},
    ]

    with patch("database.get_mongo_collection", return_value=mock_coll):
        streak = get_asset_settlement_streak("ETH", timeframe="15m", force_refresh=True)

    assert streak["streak_side"] == "no"
    assert streak["streak_length"] == 2
    assert streak["regime"] == "moderate_no_streak"


def test_settlement_streak_fallback_when_empty():
    """Verify get_asset_settlement_streak returns safe neutral defaults when DB is empty."""
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value.limit.return_value = []

    with patch("database.get_mongo_collection", return_value=mock_coll):
        streak = get_asset_settlement_streak("SOL", timeframe="15m", force_refresh=True)

    assert streak["symbol"] == "SOL"
    assert streak["streak_side"] is None
    assert streak["streak_length"] == 0
    assert streak["sample_size"] == 0
    assert streak["regime"] == "neutral_flow"


# ===========================================================================
# 2. Ensemble Consensus Decision Engine Tests
# ===========================================================================

def test_ensemble_consensus_streak_aligned_boost():
    """Verify that proposing a trade aligned with an active settlement streak receives high conviction."""
    streak_data = {
        "symbol": "BTC",
        "timeframe": "15m",
        "streak_side": "yes",
        "streak_length": 3,
        "recent_outcomes": ["yes", "yes", "yes"],
        "yes_win_rate_pct": 100.0,
        "sample_size": 3,
        "regime": "strong_yes_streak",
    }

    decision = evaluate_ensemble_consensus(
        symbol="BTC",
        proposed_side="yes",
        price_cents=25,
        spot_price=65100.0,
        target_price=65000.0,  # +0.15% lead
        timeframe="15m",
        dynamic_filters={"min_buy": 18, "max_buy": 30},
        lead_changes=0,
        streak_data=streak_data,
    )

    assert decision["is_approved"] is True
    assert decision["streak_side"] == "yes"
    assert decision["streak_length"] == 3
    assert decision["consensus_score"] >= 0.70
    assert decision["confidence"] >= 0.75
    assert "streak_confirmed_yes" in decision["regime"]


def test_ensemble_consensus_neutral_flow_volume_preservation():
    """Verify that a cold start or neutral history does NOT block standard discount trades."""
    streak_data = {
        "symbol": "NEAR",
        "timeframe": "15m",
        "streak_side": None,
        "streak_length": 0,
        "recent_outcomes": [],
        "yes_win_rate_pct": 50.0,
        "sample_size": 0,
        "regime": "neutral_flow",
    }

    decision = evaluate_ensemble_consensus(
        symbol="NEAR",
        proposed_side="yes",
        price_cents=22,
        spot_price=4.50,
        target_price=4.50,
        timeframe="15m",
        dynamic_filters={"min_buy": 18, "max_buy": 30},
        lead_changes=0,
        streak_data=streak_data,
    )

    # Volume preservation guarantee: must approve standard discount setups even with zero prior streak data
    assert decision["is_approved"] is True
    assert decision["consensus_score"] >= 0.20
    assert decision["regime"] == "neutral_flow"


def test_ensemble_consensus_blocks_severe_counter_streak_fading():
    """Verify that trying to fade a 3x streak without strong breakout momentum is rejected."""
    streak_data = {
        "symbol": "BTC",
        "timeframe": "15m",
        "streak_side": "no",
        "streak_length": 3,
        "recent_outcomes": ["no", "no", "no"],
        "yes_win_rate_pct": 0.0,
        "sample_size": 3,
        "regime": "strong_no_streak",
    }

    # Proposing BUY_YES @ 26¢ when spot is only +0.02% above target (weak edge fighting 3x NO streak)
    decision = evaluate_ensemble_consensus(
        symbol="BTC",
        proposed_side="yes",
        price_cents=26,
        spot_price=64010.0,
        target_price=64000.0,  # +0.015% (weak)
        timeframe="15m",
        dynamic_filters={"min_buy": 18, "max_buy": 30},
        lead_changes=0,
        streak_data=streak_data,
    )

    assert decision["is_approved"] is False
    assert decision["regime"] == "counter_streak_fading_blocked"
    assert "opposes severe 3x NO" in decision["reason"]


def test_ensemble_consensus_permits_counter_streak_on_strong_breakout():
    """Verify that a massive spot breakout (>= 0.20%) overrides an opposing streak."""
    streak_data = {
        "symbol": "BTC",
        "timeframe": "15m",
        "streak_side": "no",
        "streak_length": 3,
        "recent_outcomes": ["no", "no", "no"],
        "yes_win_rate_pct": 0.0,
        "sample_size": 3,
        "regime": "strong_no_streak",
    }

    # Proposing BUY_YES @ 26¢ when spot has a massive +0.35% surge above target
    decision = evaluate_ensemble_consensus(
        symbol="BTC",
        proposed_side="yes",
        price_cents=26,
        spot_price=64250.0,
        target_price=64000.0,  # +0.39% lead
        timeframe="15m",
        dynamic_filters={"min_buy": 18, "max_buy": 30},
        lead_changes=0,
        streak_data=streak_data,
    )

    assert decision["is_approved"] is True
    assert decision["consensus_score"] >= 0.20


def test_ensemble_consensus_blocks_choppy_market():
    """Verify that markets with > 2 lead changes across 50¢ are blocked by the ensemble."""
    decision = evaluate_ensemble_consensus(
        symbol="ETH",
        proposed_side="yes",
        price_cents=24,
        spot_price=2600.0,
        target_price=2595.0,
        timeframe="15m",
        dynamic_filters={"min_buy": 18, "max_buy": 30, "max_lead_changes": 2},
        lead_changes=4,  # Choppy market!
    )

    assert decision["is_approved"] is False
    assert decision["regime"] == "choppy_oscillation_blocked"


# ===========================================================================
# 3. 15-Minute Pipeline Integration Tests
# ===========================================================================

def test_15min_pipeline_records_consensus_and_streak_metadata():
    """Verify process_single_market records streak and consensus metadata into snapshot documents."""
    client = MockTradingClient()
    market = {
        "ticker": "KXBTC15M-26AUG172000-00",
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.24",
        "yes_bid_dollars": "0.20",
        "no_ask_dollars": "0.65",
        "no_bid_dollars": "0.60",
        "close_time": "2026-08-17T20:00:00Z",
        "floor_strike": 64000.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 19, 45, 30, tzinfo=timezone.utc)

    mock_streak = {
        "symbol": "BTC",
        "timeframe": "15m",
        "streak_side": "yes",
        "streak_length": 2,
        "recent_outcomes": ["yes", "yes"],
        "yes_win_rate_pct": 100.0,
        "sample_size": 2,
        "regime": "moderate_yes_streak",
    }

    mock_filter = {
        "min_buy": 18,
        "max_buy": 30,
        "tp": 62,
        "enable_favorite_maker": False,
        "enable_momentum_buys": True,
        "avoid_mid_zone": True,
        "mid_zone_min": 31,
        "mid_zone_max": 61,
        "contracts_per_trade": 1,
        "is_restricted": False,
        "is_exploration": False,
    }

    with patch("main.get_asset_settlement_streak", return_value=mock_streak), \
         patch("main.get_dynamic_filter_for_market", return_value=mock_filter):
        snap = process_single_market(
            market=market,
            is_trading_interval=True,
            client=client,
            now_utc=now_utc,
            minute_past_hour=0,  # :00 mark
            spot_cache={"BTC": 64150.0},
            dry_run=False,
        )

    assert snap is not None
    assert snap["strk"] == 2
    assert snap["strk_side"] == "yes"
    assert "c_score" in snap
    assert snap["c_score"] is not None
    assert "c_regime" in snap
    assert "trd" in snap
    assert snap["trd"]["action"] == "buy_opened"
    assert snap["trd"]["side"] == "yes"
    assert snap["trd"]["consensus_score"] is not None
    assert snap["trd"]["streak_length"] == 2


# ===========================================================================
# 4. Hourly Multi-Strike Pipeline Integration Tests
# ===========================================================================

def test_hourly_pipeline_records_consensus_and_streak_metadata():
    """Verify process_hourly_market_group executes candidates passing consensus and enriches snapshots."""
    client = MockTradingClient()
    markets = [
        {
            "ticker": "KXBTCD-26AUG17-T64000",
            "series_ticker": "KXBTCD",
            "yes_ask_dollars": "0.28",
            "yes_bid_dollars": "0.24",
            "no_ask_dollars": "0.65",
            "no_bid_dollars": "0.60",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 64000.0,
            "exchange": "kalshi",
        },
        {
            "ticker": "KXBTCD-26AUG17-T65000",
            "series_ticker": "KXBTCD",
            "yes_ask_dollars": "0.35",
            "yes_bid_dollars": "0.30",
            "no_ask_dollars": "0.60",
            "no_bid_dollars": "0.55",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 65000.0,
            "exchange": "kalshi",
        },
    ]
    now_utc = datetime(2026, 8, 17, 20, 0, 30, tzinfo=timezone.utc)

    mock_streak = {
        "symbol": "BTC",
        "timeframe": "1h",
        "streak_side": "yes",
        "streak_length": 3,
        "recent_outcomes": ["yes", "yes", "yes"],
        "yes_win_rate_pct": 100.0,
        "sample_size": 3,
        "regime": "strong_yes_streak",
    }

    mock_filter_1h = {
        "min_buy": 18,
        "max_buy": 44,
        "tp": 62,
        "enable_favorite_maker": False,
        "enable_momentum_buys": True,
        "avoid_mid_zone": True,
        "mid_zone_min": 37,
        "mid_zone_max": 55,
        "contracts_per_trade": 1,
        "is_restricted": False,
        "is_exploration": False,
    }

    with patch("main.get_asset_settlement_streak", return_value=mock_streak), \
         patch("main.get_dynamic_filter_for_market", return_value=mock_filter_1h):
        snaps = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=0,  # :00 mark
            spot_cache={"BTC": 64100.0},
            dry_run=False,
        )

    assert len(snaps) == 2
    # Strike 1 was valid discount buy and approved by ensemble consensus
    strike_1 = next(s for s in snaps if s["tick"] == "KXBTCD-26AUG17-T64000")
    assert strike_1["strk"] == 3
    assert strike_1["strk_side"] == "yes"
    assert "trd" in strike_1
    assert strike_1["trd"]["action"] == "buy_opened"
    assert strike_1["trd"]["side"] == "yes"
    assert strike_1["trd"]["consensus_score"] is not None
    assert strike_1["trd"]["streak_length"] == 3
