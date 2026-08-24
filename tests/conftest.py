from __future__ import annotations

from unittest.mock import patch
import pytest

import database


@pytest.fixture(autouse=True)
def reset_db_cache_and_isolate_tests(monkeypatch):
    """Ensure in-memory dynamic price filter cache is reset and test isolation."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    database._DYNAMIC_PRICE_MAP_CACHE = database.DEFAULT_DYNAMIC_PRICE_MAP
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield
    database._DYNAMIC_PRICE_MAP_CACHE = None
