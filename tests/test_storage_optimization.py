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
