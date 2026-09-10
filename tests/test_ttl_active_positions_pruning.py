from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from database import prune_stale_active_positions


def test_prune_stale_active_positions_expired_unconfirmed():
    """Verify contracts expired > 2 hours ago without confirmed exchange positions are pruned."""
    now_ref = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    # Mock active positions docs in MongoDB
    doc_expired_unconfirmed = {
        "_id": "id_1",
        "ticker": "KXWTIH-26SEP1012-T98.79",  # Expiry 12:00 UTC -> 4h ago (> 2h)
        "ex": "kalshi",
        "entry_ts": int((now_ref - timedelta(hours=5)).timestamp()),
    }
    doc_recent_unconfirmed = {
        "_id": "id_2",
        "ticker": "KXWTIH-26SEP1015-T99.19",  # Expiry 15:00 UTC -> 1h ago (<= 2h)
        "ex": "kalshi",
        "entry_ts": int((now_ref - timedelta(hours=2)).timestamp()),
    }
    doc_future = {
        "_id": "id_3",
        "ticker": "KXWTIH-26SEP1017-T99.59",  # Expiry 17:00 UTC -> in the future
        "ex": "kalshi",
        "entry_ts": int((now_ref - timedelta(minutes=30)).timestamp()),
    }

    mock_col = MagicMock()
    mock_col.find.return_value = [doc_expired_unconfirmed, doc_recent_unconfirmed, doc_future]

    deleted_ids = []
    def fake_delete_one(query):
        deleted_ids.append(query["_id"])
        return MagicMock(deleted_count=1)

    mock_col.delete_one.side_effect = fake_delete_one

    pruned = prune_stale_active_positions(
        confirmed_tickers={"KXWTIH-26SEP1017-T99.59"},
        exchange="kalshi",
        max_age_hours=2.0,
        now_utc=now_ref,
        active_col=mock_col,
    )

    # Only doc_expired_unconfirmed should be deleted
    assert pruned == 1
    assert deleted_ids == ["id_1"]


def test_prune_stale_active_positions_expired_but_confirmed():
    """Verify contracts expired > 2 hours ago that ARE confirmed on exchange are PRESERVED."""
    now_ref = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    doc_expired_confirmed = {
        "_id": "id_1",
        "ticker": "KXWTIH-26SEP1012-T98.79",  # Expiry 12:00 UTC -> 4h ago (> 2h)
        "ex": "kalshi",
    }

    mock_col = MagicMock()
    mock_col.find.return_value = [doc_expired_confirmed]
    mock_col.delete_one = MagicMock()

    # Ticker is in confirmed positions!
    pruned = prune_stale_active_positions(
        confirmed_tickers={"KXWTIH-26SEP1012-T98.79"},
        exchange="kalshi",
        max_age_hours=2.0,
        now_utc=now_ref,
        active_col=mock_col,
    )

    assert pruned == 0
    assert not mock_col.delete_one.called


def test_prune_stale_active_positions_with_client_object():
    """Verify prune_stale_active_positions queries client.get_all_active_positions() when client is passed."""
    now_ref = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    mock_client = MagicMock()
    mock_client.exchange_name = "kalshi"
    mock_client.get_all_active_positions.return_value = {
        "KXWTIH-26SEP1015-T99.19": {"count": 1}
    }

    doc_1 = {
        "_id": "id_1",
        "ticker": "KXWTIH-26SEP1010-T95.00",  # Expiry 10:00 UTC -> 6h ago
        "ex": "kalshi",
    }
    doc_2 = {
        "_id": "id_2",
        "ticker": "KXWTIH-26SEP1015-T99.19",  # Confirmed on client
        "ex": "kalshi",
    }

    mock_col = MagicMock()
    mock_col.find.return_value = [doc_1, doc_2]

    pruned = prune_stale_active_positions(
        client=mock_client,
        max_age_hours=2.0,
        now_utc=now_ref,
        active_col=mock_col,
    )

    assert pruned == 1
    mock_col.delete_one.assert_called_once_with({"_id": "id_1"})


def test_prune_stale_active_positions_fallback_to_entry_ts():
    """Verify fallback to entry_ts + duration when ticker pattern cannot be parsed."""
    now_ref = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    doc_unparseable_old = {
        "_id": "id_1",
        "ticker": "CUSTOM-CONTRACT-123",
        "ex": "kalshi",
        "tf": "1h",
        "entry_ts": int((now_ref - timedelta(hours=4)).timestamp()),  # entered 4h ago -> expired 3h ago (> 2h)
    }
    doc_unparseable_recent = {
        "_id": "id_2",
        "ticker": "CUSTOM-CONTRACT-456",
        "ex": "kalshi",
        "tf": "1h",
        "entry_ts": int((now_ref - timedelta(minutes=90)).timestamp()),  # entered 1.5h ago -> expired 0.5h ago (<= 2h)
    }

    mock_col = MagicMock()
    mock_col.find.return_value = [doc_unparseable_old, doc_unparseable_recent]

    pruned = prune_stale_active_positions(
        confirmed_tickers=set(),
        exchange="kalshi",
        max_age_hours=2.0,
        now_utc=now_ref,
        active_col=mock_col,
    )

    assert pruned == 1
    mock_col.delete_one.assert_called_once_with({"_id": "id_1"})
