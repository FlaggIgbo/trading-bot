"""
Unit tests for:
1. Synthetic settlement resolver in database.py (resolves expired Gemini positions and cleans them up from active_positions).
2. Strict single-fill check & 92¢ cumulative cost basis cap in KalshiClient (eliminating cost > $1.00 drag).
"""

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from database import resolve_synthetic_gemini_settlements, save_settlements
from signals.kalshi_client import KalshiClient


# ==============================================================================
# 1. GEMINI SYNTHETIC SETTLEMENT RESOLVER & CLEANUP TESTS
# ==============================================================================

def test_resolve_synthetic_gemini_settlements_expired():
    """Verify resolve_synthetic_gemini_settlements correctly identifies expired Gemini contracts and determines outcome."""
    mock_active_col = MagicMock()
    mock_snap_col = MagicMock()

    # Expired 1h contract from 2026-09-08 18:00 UTC
    mock_active_col.find.return_value = [
        {
            "ticker": "GEMI-BTC2609081800-HI78500",
            "ex": "gemini",
            "sym": "BTC",
            "tf": "1h",
            "side": "yes",
            "count": 1,
            "entry_price_cents": 78,
            "total_cost_dollars": 0.78,
            "strategy_tag": "endgame_favorite_maker",
        },
        # Contract expiring in future (2026-09-10 18:00 UTC)
        {
            "ticker": "GEMI-BTC2609101800-HI78500",
            "ex": "gemini",
            "sym": "BTC",
            "tf": "1h",
            "side": "yes",
            "count": 1,
            "entry_price_cents": 78,
            "total_cost_dollars": 0.78,
        }
    ]

    # No early exit snapshot
    mock_snap_col.find_one.return_value = None

    now_ref = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    synth = resolve_synthetic_gemini_settlements(active_col=mock_active_col, snap_col=mock_snap_col, now_utc=now_ref)

    assert len(synth) == 1
    record = synth[0]
    assert record["ticker"] == "GEMI-BTC2609081800-HI78500"
    assert record["ex"] == "gemini"
    assert record["cost_dollars"] == 0.78
    assert record["revenue_dollars"] == 1.00
    assert record["pnl_dollars"] == 0.22
    assert record["trade_won"] is True
    assert record["synthetic"] is True


def test_resolve_synthetic_gemini_settlements_with_early_cut_loss():
    """Verify synthetic resolver recognizes early cut loss from market_snapshots."""
    mock_active_col = MagicMock()
    mock_snap_col = MagicMock()

    mock_active_col.find.return_value = [
        {
            "ticker": "GEMI-BTC2609091400-HI79400",
            "ex": "gemini",
            "sym": "BTC",
            "tf": "1h",
            "side": "yes",
            "count": 1,
            "entry_price_cents": 74,
            "total_cost_dollars": 0.74,
        }
    ]

    # Snapshot with early 50min_loss_cut
    mock_snap_col.find_one.return_value = {
        "tick": "GEMI-BTC2609091400-HI79400",
        "trd": {
            "action": "50min_loss_cut",
            "exit_price": 23,
            "loss_cents": 51,
        }
    }

    now_ref = datetime(2026, 9, 9, 15, 0, 0, tzinfo=timezone.utc)
    synth = resolve_synthetic_gemini_settlements(active_col=mock_active_col, snap_col=mock_snap_col, now_utc=now_ref)

    assert len(synth) == 1
    record = synth[0]
    assert record["ticker"] == "GEMI-BTC2609091400-HI79400"
    assert record["revenue_dollars"] == 0.23
    assert record["cost_dollars"] == 0.74
    assert record["pnl_dollars"] == -0.51
    assert record["trade_won"] is False


def test_save_settlements_cleans_up_active_positions_for_gemini():
    """Verify save_settlements persists synthetic Gemini settlements and cleans up active_positions."""
    mock_active_col = MagicMock()
    mock_snap_col = MagicMock()
    mock_settle_col = MagicMock()

    expired_active_doc = {
        "ticker": "GEMI-BTC2609081200-HI78000",
        "ex": "gemini",
        "sym": "BTC",
        "tf": "1h",
        "side": "no",
        "count": 1,
        "entry_price_cents": 75,
        "total_cost_dollars": 0.75,
        "order_role": "maker",
        "strategy_tag": "endgame_favorite_maker",
    }
    mock_active_col.find.return_value = [expired_active_doc]
    mock_active_col.find_one.return_value = expired_active_doc
    mock_snap_col.find_one.return_value = None

    with patch("database.get_mongo_collection") as mock_get_col:
        def get_col_side_effect(name):
            if name == "active_positions":
                return mock_active_col
            elif name == "market_snapshots":
                return mock_snap_col
            elif name == "settlements":
                return mock_settle_col
            return MagicMock()

        mock_get_col.side_effect = get_col_side_effect

        saved = save_settlements(settlements=[], force_refresh=True)

        assert saved == 1
        assert mock_settle_col.update_one.called
        # Verify active position cleanup
        assert mock_active_col.delete_one.called
        del_filter = mock_active_col.delete_one.call_args[0][0]
        assert del_filter == {"ticker": "GEMI-BTC2609081200-HI78000", "ex": "gemini"}


# ==============================================================================
# 2. KALSHI STRICT SINGLE-FILL & 92¢ COST CAP TESTS
# ==============================================================================

@pytest.fixture
def kalshi_client():
    client = KalshiClient(api_key_id="test-uuid", private_key=MagicMock(), allow_live_orders=False)
    return client


def test_kalshi_single_fill_blocks_reentry_after_fill(kalshi_client):
    """Verify KalshiClient blocks a second order once a contract has had a fill in the session."""
    ticker = "KXWTIH-26SEP0915-T95.00"

    # Initially allowed
    assert kalshi_client.has_existing_position_or_order(ticker) is False

    # Simulate fill
    kalshi_client._ticker_filled_contracts.add(ticker)
    kalshi_client._ticker_cumulative_cost_cents[ticker] = 84

    # Now strictly blocked
    assert kalshi_client.has_existing_position_or_order(ticker) is True
    assert kalshi_client.has_exceeded_single_fill_or_cost_cap(ticker) is True

    # Buy order rejected
    res = kalshi_client.execute_buy(ticker=ticker, side="yes", price_cents=80)
    assert res.get("error") == "single_fill_or_cost_cap_exceeded"


def test_kalshi_single_fill_blocks_reentry_after_liquidation(kalshi_client):
    """Verify KalshiClient blocks re-entering a ticker after an early loss cut/liquidation."""
    ticker = "KXWTIH-26SEP0916-T95.50"

    # Set paper position and simulate liquidation
    kalshi_client.record_paper_position(ticker, "yes", 1, 84)
    kalshi_client.execute_liquidation(ticker, "yes", 1, 50)

    # Contract must be marked as liquidated and filled
    assert ticker in kalshi_client._ticker_liquidated_contracts
    assert ticker in kalshi_client._ticker_filled_contracts

    # Re-entry must be blocked
    assert kalshi_client.has_existing_position_or_order(ticker) is True
    fav_res = kalshi_client.buy_favorite_maker_order(ticker=ticker, side="yes", maker_bid_cents=82, target_ask_cents=84)
    assert fav_res.get("error") == "single_fill_or_cost_cap_exceeded"


def test_kalshi_cost_basis_cap_92_cents(kalshi_client):
    """Verify cumulative cost basis limit of 92¢ strictly blocks orders when reached/exceeded."""
    ticker = "KXWTIH-26SEP0917-T96.00"

    # Set cost basis to 92¢
    kalshi_client._ticker_cumulative_cost_cents[ticker] = 92
    assert kalshi_client.has_exceeded_single_fill_or_cost_cap(ticker, max_cost_basis_cents=92) is True
    assert kalshi_client.has_existing_position_or_order(ticker) is True

    # Set cost basis below 92¢ (e.g. 80¢)
    kalshi_client._ticker_cumulative_cost_cents[ticker] = 80
    # If not filled and not liquidated, below 92¢ is allowed
    assert kalshi_client.has_exceeded_single_fill_or_cost_cap(ticker, max_cost_basis_cents=92) is False
