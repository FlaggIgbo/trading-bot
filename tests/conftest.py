"""
Pytest configuration and global safety fixtures.
Ensures all test cases strictly target Kalshi's demo environment (demo-api.kalshi.co)
and have live order execution disabled so tests never make actual trades on production kalshi.com.
"""

import pytest
from ingestion.api.client import DEFAULT_DEMO_API_KEY_ID, DEMO_BASE_URL


@pytest.fixture(autouse=True)
def enforce_kalshi_demo_environment(monkeypatch):
    """
    Automatically forces KALSHI_BASE_URL to the Kalshi Demo environment,
    sets the demo API key ID, and disables live orders during all test runs,
    overriding any local .env values.
    """
    monkeypatch.setenv("KALSHI_BASE_URL", DEMO_BASE_URL)
    monkeypatch.setenv("KALSHI_DEMO_BASE_URL", DEMO_BASE_URL)
    monkeypatch.setenv("KALSHI_USE_DEMO", "true")
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", DEFAULT_DEMO_API_KEY_ID)
    monkeypatch.setenv("KALSHI_API_KEY_ID", DEFAULT_DEMO_API_KEY_ID)
    monkeypatch.setenv("ENABLE_LIVE_ORDERS", "false")
