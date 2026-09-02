"""
Unit tests for MongoDB storage optimization:
- Filtering 1¢/99¢ extreme/untradeable hourly strike noise
- Ensuring created_at is always a valid datetime object
- Pruning orphaned documents (null/missing created_at)
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from database import prune_old_market_snapshots, save_market_snapshots


def test_save_market_snapshots_enforces_datetime():
    """Ensure save_market_snapshots always sets created_at as a valid datetime object."""
    mock_coll = MagicMock()
    with patch("database.get_mongo_collection", return_value=mock_coll):
        snaps = [
            {"tick": "KXBTC15M-TEST", "ts": 1787200000, "y": 50, "n": 50, "created_at": None},
            {"tick": "KXETH15M-TEST", "ts": 1787200000, "y": 60, "n": 40},
        ]
        result = save_market_snapshots(snaps)
        assert result is True
        assert mock_coll.insert_many.called
        inserted = mock_coll.insert_many.call_args[0][0]
        assert len(inserted) == 2
        for doc in inserted:
            assert isinstance(doc["created_at"], datetime)
            assert doc["created_at"].tzinfo == timezone.utc


def test_prune_old_market_snapshots_includes_orphans():
    """Verify prune_old_market_snapshots queries for expired timestamps and null/missing created_at."""
    mock_coll = MagicMock()
    mock_coll.delete_many.return_value.deleted_count = 42
    mock_db = MagicMock()
    mock_db.get_collection.return_value = mock_coll
    mock_client = MagicMock()
    mock_client.get_database.return_value = mock_db

    with patch("database._GLOBAL_MONGO_CLIENT", mock_client):
        deleted = prune_old_market_snapshots(retention_hours=24)
        assert deleted == 42
        assert mock_coll.delete_many.called
        query = mock_coll.delete_many.call_args[0][0]
        assert "$or" in query
        or_clauses = query["$or"]
        # Check that null and missing created_at are included
        has_null_ca = any(c.get("created_at") is None for c in or_clauses)
        has_missing_ca = any(c.get("created_at") == {"$exists": False} for c in or_clauses)
        has_ts_cutoff = any("ts" in c and "$lt" in c["ts"] for c in or_clauses)
        assert has_null_ca
        assert has_missing_ca
        assert has_ts_cutoff


from signals.base_client import BaseTradingClient


@pytest.fixture(autouse=True)
def mock_ungated_asset():
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


def test_main_snapshot_filtering_logic():
    """Verify extreme 1¢/99¢ untradeable hourly strikes with 0 volume are filtered out."""
    from main import run_pipeline_pass

    class DummyClient(BaseTradingClient):
        exchange_name = "Kalshi"
        def __init__(self):
            super().__init__()
            self._positions_cache = {}

        def get_all_active_positions(self):
            return {}
        def get_market_position(self, ticker: str):
            return None
        def cancel_unfilled_buy_orders(self, ticker: str):
            return True
        def cancel_and_verify_resting_orders(self, ticker: str):
            return True
        def has_existing_position_or_order(self, ticker: str) -> bool:
            return False
        def get_15m_crypto_markets(self, status="open"):
            return [
                {
                    "ticker": "KXBTC15M-26AUG201200-00",
                    "series_ticker": "KXBTC15M",
                    "yes_ask_dollars": "0.50",
                    "yes_bid_dollars": "0.48",
                    "no_ask_dollars": "0.52",
                    "no_bid_dollars": "0.50",
                    "floor_strike": 60000.0,
                    "close_time": "2026-08-20T23:59:00Z",
                    "volume_24h": 100,
                }
            ]
        def get_hourly_crypto_markets(self, status="open", current_hour_only=True, now_utc=None):
            return [
                # 1. Tradeable hourly strike (YES: 45¢) -> SHOULD be saved
                {
                    "ticker": "KXBTCH-26AUG2012-T60000",
                    "series_ticker": "KXBTCH",
                    "yes_ask_dollars": "0.45",
                    "yes_bid_dollars": "0.43",
                    "no_ask_dollars": "0.57",
                    "no_bid_dollars": "0.55",
                    "strike_price": 60000.0,
                    "close_time": "2026-08-20T23:59:00Z",
                    "volume_24h": 50,
                },
                # 2. Extreme 1¢/99¢ strike with 0 volume -> SHOULD be filtered out
                {
                    "ticker": "KXBTCH-26AUG2012-T50000",
                    "series_ticker": "KXBTCH",
                    "yes_ask_dollars": "0.99",
                    "yes_bid_dollars": "0.98",
                    "no_ask_dollars": "0.02",
                    "no_bid_dollars": "0.01",
                    "strike_price": 50000.0,
                    "close_time": "2026-08-20T23:59:00Z",
                    "volume_24h": 0,
                },
                # 3. Extreme 1¢/99¢ strike WITH active volume -> SHOULD be saved
                {
                    "ticker": "KXBTCH-26AUG2012-T52000",
                    "series_ticker": "KXBTCH",
                    "yes_ask_dollars": "0.99",
                    "yes_bid_dollars": "0.98",
                    "no_ask_dollars": "0.02",
                    "no_bid_dollars": "0.01",
                    "strike_price": 52000.0,
                    "close_time": "2026-08-20T23:59:00Z",
                    "volume_fp": "25.0",
                },
            ]
        def get_portfolio_pnl_summary(self):
            return {
                "balance_dollars": 100.0,
                "portfolio_value_dollars": 100.0,
                "total_realized_pnl_dollars": 0.0,
            }
        def get_market_orderbook(self, ticker):
            return {"yes_bids": [], "no_bids": []}
        def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
            return {"status": "bought", "price": price_cents, "fill_count": count}
        def buy_and_set_take_profit(self, ticker: str, side: str, buy_price_cents: int, maker_bid_cents: int = None, **kw):
            return {"status": "bought", "buy_price_cents": buy_price_cents}
        def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int):
            return {"status": "liquidated", "price": current_price}

    client = DummyClient()
    mock_filter = {
        "sl_threshold": 10, "sl_floor": 30, "cut_5min": 35, "endgame_12_5_floor": 25,
        "min_buy": 30, "max_buy": 52, "tp": 82, "avoid_mid_zone": True,
        "mid_zone_min": 37, "mid_zone_max": 43, "is_exploration": False,
    }

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}), \
         patch("main.batch_fetch_spot_prices", return_value={"BTC": 60000.0}), \
         patch("main.validate_market_closure_time", return_value=(True, "ok", 10.0)), \
         patch("main.save_market_snapshots") as mock_save:

        run_pipeline_pass(
            clients=[("Kalshi", client)],
            dry_run=False,
            force_trade_mark=True,
        )

        assert mock_save.called
        saved = mock_save.call_args[0][0]
        saved_ticks = [s.get("tick") for s in saved]
        assert "KXBTC15M-26AUG201200-00" in saved_ticks
        assert "KXBTCH-26AUG2012-T60000" in saved_ticks
        assert "KXBTCH-26AUG2012-T52000" in saved_ticks
        assert "KXBTCH-26AUG2012-T50000" not in saved_ticks


def test_save_settlements_enrichment_from_active_positions():
    """Verify save_settlements enriches settlement records with entry-time metadata from active_positions."""
    from database import save_settlements, save_active_position_cost

    mock_settle_col = MagicMock()
    mock_active_col = MagicMock()

    # Mock active_positions find_one returning entry metadata
    mock_active_col.find_one.return_value = {
        "ticker": "KXBTC15M-TEST-SETTLE",
        "ex": "kalshi",
        "tf": "15m",
        "sym": "BTC",
        "side": "yes",
        "count": 2,
        "entry_price_cents": 35,
        "spot_price_at_entry": 60500.0,
        "target_strike_price": 60000.0,
        "strike_distance_diff": 500.0,
        "strike_distance_pct": 0.8333,
        "minute_into_candle": 2,
        "hour_utc": 14,
        "day_of_week": "Thursday",
        "market_session": "us_nyse_session",
        "strategy_tag": "discount_corridor",
        "order_role": "maker",
        "spread_cents_at_entry": 1,
    }

    def mock_get_col(col_name):
        return mock_settle_col if col_name == "settlements" else mock_active_col

    with patch("database.get_mongo_collection", side_effect=mock_get_col):
        settlements_input = [
            {
                "ticker": "KXBTC15M-TEST-SETTLE",
                "settled_time": "2026-08-20T14:15:00Z",
                "market_result": "yes",
                "cost_dollars": 0.70,
                "revenue_dollars": 2.00,
                "pnl_dollars": 1.30,
                "yes_count": 2,
                "no_count": 0,
            }
        ]

        saved = save_settlements(settlements_input)
        assert saved == 1
        assert mock_settle_col.update_one.called
        update_call = mock_settle_col.update_one.call_args
        filter_q = update_call[0][0]
        set_doc = update_call[0][1]["$set"]

        assert filter_q["ticker"] == "KXBTC15M-TEST-SETTLE"
        assert set_doc["entry_price_cents"] == 35
        assert set_doc["spot_price_at_entry"] == 60500.0
        assert set_doc["target_strike_price"] == 60000.0
        assert set_doc["strategy_tag"] == "discount_corridor"
        assert set_doc["order_role"] == "maker"
        assert set_doc["market_session"] == "us_nyse_session"
        assert set_doc["trade_won"] is True
        assert set_doc["entered_side"] == "yes"

        # Verify active_positions cleaned up after settlement
        assert mock_active_col.delete_one.called
        assert mock_active_col.delete_one.call_args[0][0]["ticker"] == "KXBTC15M-TEST-SETTLE"


def test_daily_asset_loss_gating_midnight_et_reset():
    """Verify is_asset_loss_gated calculates losses since midnight ET and correctly gates/resets."""
    from datetime import datetime, timezone, timedelta
    from database import get_midnight_et_cutoff, get_asset_realized_pnl_dollars, is_asset_loss_gated

    # 1. Verify get_midnight_et_cutoff produces correct UTC timestamp and ISO string
    # For a fixed UTC datetime: 2026-08-20 18:00:00 UTC (which is 14:00:00 EDT)
    fixed_now_utc = datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc)
    midnight_iso, midnight_ts = get_midnight_et_cutoff(now_utc=fixed_now_utc)
    # Midnight ET on Aug 20 in EDT (UTC-4) is 2026-08-20 04:00:00 UTC
    assert midnight_iso == "2026-08-20T04:00:00Z"
    assert midnight_ts == datetime(2026, 8, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp()

    # 2. Test get_asset_realized_pnl_dollars with mocked MongoDB collection
    mock_settle_col = MagicMock()
    
    # Mock aggregate returning a loss of -$1.50 today
    mock_settle_col.aggregate.return_value = [{"_id": None, "total_pnl": -1.50}]

    with patch("database.get_mongo_collection", return_value=mock_settle_col):
        pnl = get_asset_realized_pnl_dollars("SILVER", timeframe="1h", since_midnight_et=True, now_utc=fixed_now_utc)
        assert pnl == -1.50
        assert mock_settle_col.aggregate.called

        # Check match query passed to aggregation pipeline
        pipeline = mock_settle_col.aggregate.call_args[0][0]
        match_stage = pipeline[0]["$match"]
        assert "$and" in match_stage
        
        # Verify is_asset_loss_gated gates when loss >= $1.00 ($1.50 loss)
        is_gated, loss_val = is_asset_loss_gated("SILVER", timeframe="1h", max_loss_dollars=1.0, now_utc=fixed_now_utc)
        assert is_gated is True
        assert loss_val == -1.50

    # 3. Mock aggregate returning no loss today ($0.00 PnL, e.g. empty results or small gain)
    mock_settle_col.aggregate.return_value = []
    with patch("database.get_mongo_collection", return_value=mock_settle_col):
        is_gated, loss_val = is_asset_loss_gated("SILVER", timeframe="1h", max_loss_dollars=1.0, now_utc=fixed_now_utc)
        assert is_gated is False
        assert loss_val == 0.0

    # 4. Mock aggregate returning -$0.50 (under $1.00 limit -> not gated)
    mock_settle_col.aggregate.return_value = [{"_id": None, "total_pnl": -0.50}]
    with patch("database.get_mongo_collection", return_value=mock_settle_col):
        is_gated, loss_val = is_asset_loss_gated("BTC", timeframe="15m", max_loss_dollars=1.0, now_utc=fixed_now_utc)
        assert is_gated is False
        assert loss_val == -0.50


def test_prune_old_portfolio_pnl():
    """Verify prune_old_portfolio_pnl deletes records older than cutoff days."""
    from database import prune_old_portfolio_pnl

    mock_coll = MagicMock()
    mock_coll.delete_many.return_value.deleted_count = 150
    mock_db = MagicMock()
    mock_db.get_collection.return_value = mock_coll
    mock_client = MagicMock()
    mock_client.get_database.return_value = mock_db

    with patch("database._GLOBAL_MONGO_CLIENT", mock_client):
        deleted = prune_old_portfolio_pnl(retention_days=7)
        assert deleted == 150
        assert mock_coll.delete_many.called
        query = mock_coll.delete_many.call_args[0][0]
        assert "ts" in query
        assert "$lt" in query["ts"]


def test_prune_old_active_positions():
    """Verify prune_old_active_positions deletes records older than cutoff hours."""
    from database import prune_old_active_positions

    mock_coll = MagicMock()
    mock_coll.delete_many.return_value.deleted_count = 85
    mock_db = MagicMock()
    mock_db.get_collection.return_value = mock_coll
    mock_client = MagicMock()
    mock_client.get_database.return_value = mock_db

    with patch("database._GLOBAL_MONGO_CLIENT", mock_client):
        deleted = prune_old_active_positions(retention_hours=48)
        assert deleted == 85
        assert mock_coll.delete_many.called
        query = mock_coll.delete_many.call_args[0][0]
        assert "updated_at" in query
        assert "$lt" in query["updated_at"]


def test_prune_market_lead_changes_cache():
    """Verify prune_market_lead_changes_cache removes stale tickers from in-memory dictionary."""
    import time
    from main import _MARKET_LEAD_CHANGES, prune_market_lead_changes_cache

    _MARKET_LEAD_CHANGES.clear()
    now = time.time()
    # Add active ticker (recent snapshot)
    _MARKET_LEAD_CHANGES["KXBTC15M-ACTIVE"] = [{"p": 50, "ts": now - 300}]
    # Add stale ticker (> 1 hour old)
    _MARKET_LEAD_CHANGES["KXBTC15M-STALE"] = [{"p": 40, "ts": now - 4000}]
    # Add empty ticker
    _MARKET_LEAD_CHANGES["KXBTC15M-EMPTY"] = []

    pruned = prune_market_lead_changes_cache(max_age_seconds=3600.0)
    assert pruned == 2
    assert "KXBTC15M-ACTIVE" in _MARKET_LEAD_CHANGES
    assert "KXBTC15M-STALE" not in _MARKET_LEAD_CHANGES
    assert "KXBTC15M-EMPTY" not in _MARKET_LEAD_CHANGES

