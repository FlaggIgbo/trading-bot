"""
Tests for main bot entry point.
"""

from unittest.mock import patch, MagicMock
from main import main


@patch("main.KalshiClient")
@patch("main.RiskEngine")
def test_main_single_cycle(mock_engine_cls, mock_client_cls):
    # Mock client and engine
    mock_client = MagicMock()
    mock_client.get_open_markets.return_value = [{"ticker": "TEST-1", "status": "open"}]
    mock_client_cls.return_value = mock_client

    mock_engine = MagicMock()
    mock_engine_cls.return_value = mock_engine

    # Run main with single_cycle=True
    main(single_cycle=True, sleep_seconds=0)

    # Verify ingestion fetched markets and risk engine processed them
    mock_client.get_open_markets.assert_called_once()
    mock_engine.evaluate_all.assert_called_once_with([{"ticker": "TEST-1", "status": "open"}])
