"""
Unit Tests for Gemini Prediction Markets & Trading Client
==========================================================

Tests:
1. GeminiPredictionClient: public market fetching (15M crypto, 15M commodities, hourly crypto, hourly commodities, hourly indices)
2. GeminiTradingClient: authentication, balance, portfolio PnL, maker limit buys, take-profit orders, and liquidations
3. Market & Series Parity: validates all series and assets traded by Kalshi are supported on Gemini
4. Risk Management: cross-asset portfolio directional caps, stop-loss execution, and error handling
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from signals.gemini_client import (
    GEMINI_CATEGORIES,
    SERIES_15M_ALL,
    SERIES_15M_COMMODITIES,
    SERIES_15M_CRYPTO,
    SERIES_HOURLY_ALL,
    SERIES_HOURLY_COMMODITIES,
    SERIES_HOURLY_CRYPTO,
    SERIES_HOURLY_INDICES,
    GeminiClient,
    GeminiPredictionClient,
    GeminiTradingClient,
    parse_cents,
    parse_gemini_error,
)
from signals.kalshi_client import (
    SERIES_15M_ALL as KALSHI_SERIES_15M_ALL,
    SERIES_HOURLY_ALL as KALSHI_SERIES_HOURLY_ALL,
)


@pytest.fixture
def gemini_pred_client():
    return GeminiPredictionClient(base_url="https://api.sandbox.gemini.com")


@pytest.fixture
def gemini_trading_client():
    return GeminiTradingClient(
        base_url="https://api.sandbox.gemini.com",
        api_key="test_key",
        api_secret="test_secret",
        account="trading-bot",
    )


# ---------------------------------------------------------------------------
# 1. Series & Asset Market Parity Tests
# ---------------------------------------------------------------------------

def test_gemini_series_definitions():
    """Verify all 15M and hourly series categories are comprehensively defined."""
    # 15M Crypto
    assert "BTC15M" in SERIES_15M_CRYPTO
    assert "ETH15M" in SERIES_15M_CRYPTO
    assert "SOL15M" in SERIES_15M_CRYPTO
    assert "XRP15M" in SERIES_15M_CRYPTO
    assert "DOGE15M" in SERIES_15M_CRYPTO
    assert "BNB15M" in SERIES_15M_CRYPTO
    assert "HYPE15M" in SERIES_15M_CRYPTO
    assert "ADA15M" in SERIES_15M_CRYPTO
    assert "TON15M" in SERIES_15M_CRYPTO
    assert "BCH15M" in SERIES_15M_CRYPTO
    assert "NEAR15M" in SERIES_15M_CRYPTO
    assert "ZEC15M" in SERIES_15M_CRYPTO

    # 15M Commodities
    assert "GOLD15M" in SERIES_15M_COMMODITIES
    assert "WTI15M" in SERIES_15M_COMMODITIES
    assert "SILVER15M" in SERIES_15M_COMMODITIES

    # Hourly Crypto
    assert "BTCD" in SERIES_HOURLY_CRYPTO
    assert "ETHD" in SERIES_HOURLY_CRYPTO
    assert "SOLD" in SERIES_HOURLY_CRYPTO
    assert "XRPD" in SERIES_HOURLY_CRYPTO
    assert "DOGED" in SERIES_HOURLY_CRYPTO
    assert "BNBD" in SERIES_HOURLY_CRYPTO
    assert "HYPED" in SERIES_HOURLY_CRYPTO

    # Hourly Commodities & Indices
    assert "GOLDH" in SERIES_HOURLY_COMMODITIES
    assert "WTIH" in SERIES_HOURLY_COMMODITIES
    assert "SILVERH" in SERIES_HOURLY_COMMODITIES
    assert "INXU" in SERIES_HOURLY_INDICES
    assert "NASDAQ100U" in SERIES_HOURLY_INDICES


def test_gemini_kalshi_market_parity():
    """Verify Gemini supports all 17 underlying assets that Kalshi trades on."""
    underlying_assets = [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC",
        "GOLD", "WTI", "SILVER", "INX", "NASDAQ100"
    ]
    for asset in underlying_assets:
        # Check that either 15M or Hourly series exists on Gemini for this asset
        has_15m = any(asset in s for s in SERIES_15M_ALL)
        has_hourly = any(asset in s for s in SERIES_HOURLY_ALL)
        assert has_15m or has_hourly, f"Asset {asset} missing from Gemini series definitions"


# ---------------------------------------------------------------------------
# 2. Helpers & Utility Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dollars, cents, expected", [
    ("0.4500", None, 45),
    (None, 62, 62),
    ("0.9900", None, 99),
    ("0.0100", None, 1),
    ("1.0000", None, None),  # Out of range (1-99)
    ("0.0000", None, None),
])
def test_gemini_parse_cents(dollars, cents, expected):
    assert parse_cents(dollars, cents) == expected


def test_parse_gemini_error():
    err_resp = MagicMock()
    err_resp.text = '{"result":"error","reason":"InsufficientFunds","message":"Not enough available funds"}'
    reason, msg = parse_gemini_error(err_resp)
    assert reason == "InsufficientFunds"
    assert "Not enough" in msg


# ---------------------------------------------------------------------------
# 3. GeminiPredictionClient Market Fetching Tests
# ---------------------------------------------------------------------------

def test_gemini_get_15m_crypto_and_commodity_markets(gemini_pred_client):
    """Verify 15M market parsing handles crypto and commodities contracts correctly."""
    mock_events = {
        "data": [
            {
                "ticker": "BTC15M-26AUG181215",
                "series": "BTC15M",
                "expiryDate": "2026-08-18T12:15:00Z",
                "floor_strike": 60000.0,
                "contracts": [
                    {
                        "instrumentSymbol": "BTC15M-26AUG181215-60000",
                        "strike": {"value": "60000.0"},
                        "prices": {
                            "buy": {"yes": "0.4200", "no": "0.5800"},
                            "sell": {"yes": "0.4000", "no": "0.5600"},
                        },
                    }
                ],
            },
            {
                "ticker": "GOLD15M-26AUG181215",
                "series": "GOLD15M",
                "expiryDate": "2026-08-18T12:15:00Z",
                "floor_strike": 4400.0,
                "contracts": [
                    {
                        "instrumentSymbol": "GOLD15M-26AUG181215-4400",
                        "strike": {"value": "4400.0"},
                        "prices": {
                            "buy": {"yes": "0.7500", "no": "0.2500"},
                            "sell": {"yes": "0.7300", "no": "0.2300"},
                        },
                    }
                ],
            },
        ]
    }

    with patch.object(gemini_pred_client, "get_events", return_value=mock_events):
        with patch("signals.gemini_client.datetime") as mock_dt:
            # Set now to 5 minutes before expiry
            mock_dt.now.return_value = datetime(2026, 8, 18, 12, 10, 0, tzinfo=timezone.utc)
            mock_dt.fromisoformat = datetime.fromisoformat
            markets = gemini_pred_client.get_15m_crypto_markets()

            assert len(markets) == 2
            btc_mkt = next(m for m in markets if m["series_ticker"] == "BTC15M")
            assert btc_mkt["yes_ask"] == 42
            assert btc_mkt["yes_bid"] == 40
            assert btc_mkt["floor_strike"] == 60000.0
            assert btc_mkt["exchange"] == "gemini"

            gold_mkt = next(m for m in markets if m["series_ticker"] == "GOLD15M")
            assert gold_mkt["yes_ask"] == 75
            assert gold_mkt["no_ask"] == 25
            assert gold_mkt["floor_strike"] == 4400.0


def test_gemini_get_hourly_markets(gemini_pred_client):
    """Verify hourly market fetching parses crypto, commodity, and equity index events."""
    mock_events = {
        "data": [
            {
                "ticker": "BTCD-26AUG1813",
                "series": "BTCD",
                "expiryDate": "2026-08-18T13:00:00Z",
                "contracts": [
                    {
                        "instrumentSymbol": "BTCD-26AUG1813-70000",
                        "strike": {"value": "70000.0"},
                        "prices": {"buy": {"yes": "0.3400", "no": "0.6600"}, "sell": {"yes": "0.3200", "no": "0.6400"}},
                    }
                ],
            },
            {
                "ticker": "INXU-26AUG1813",
                "series": "INXU",
                "expiryDate": "2026-08-18T13:00:00Z",
                "contracts": [
                    {
                        "instrumentSymbol": "INXU-26AUG1813-7800",
                        "strike": {"value": "7800.0"},
                        "prices": {"buy": {"yes": "0.8200", "no": "0.1800"}, "sell": {"yes": "0.8000", "no": "0.1600"}},
                    }
                ],
            },
        ]
    }

    with patch.object(gemini_pred_client, "get_events", return_value=mock_events):
        now_utc = datetime(2026, 8, 18, 12, 30, 0, tzinfo=timezone.utc)
        markets = gemini_pred_client.get_hourly_crypto_markets(current_hour_only=True, now_utc=now_utc)

        assert len(markets) == 2
        assert any(m["series_ticker"] == "BTCD" for m in markets)
        assert any(m["series_ticker"] == "INXU" for m in markets)


# ---------------------------------------------------------------------------
# 4. GeminiTradingClient Authentication & Execution Tests
# ---------------------------------------------------------------------------

def test_gemini_auth_headers_generation(gemini_trading_client):
    headers = gemini_trading_client._build_auth_headers("/v1/balances")
    assert "X-GEMINI-APIKEY" in headers
    assert headers["X-GEMINI-APIKEY"] == "test_key"
    assert "X-GEMINI-PAYLOAD" in headers
    assert "X-GEMINI-SIGNATURE" in headers


def test_gemini_get_balance_and_portfolio_pnl(gemini_trading_client):
    mock_balances = [
        {"currency": "USD", "amount": "250.50", "available": "200.00"},
        {"currency": "GUSD", "amount": "50.00", "available": "50.00"},
        {"currency": "BTC", "amount": "0.05", "available": "0.05"},
    ]

    with patch.object(gemini_trading_client, "_post", return_value=mock_balances):
        bal = gemini_trading_client.get_balance()
        assert bal["available"] == 250.00
        assert bal["balance"] == 300.50

        pnl = gemini_trading_client.get_portfolio_pnl_summary()
        assert pnl["balance_dollars"] == 250.00
        assert pnl["exchange"] == "gemini"


def test_gemini_buy_and_set_take_profit(gemini_trading_client):
    ticker = "BTC15M-26AUG181215-60000"

    mock_res = {"orderId": 145828835378730783, "status": "accepted"}
    with patch.object(gemini_trading_client, "_post", return_value=mock_res):
        with patch.object(gemini_trading_client, "get_all_resting_orders", return_value={ticker: [{"orderId": 145828835378730783, "side": "buy"}]}):
            res = gemini_trading_client.buy_and_set_take_profit(
                ticker=ticker,
                side="yes",
                buy_price_cents=45,
                take_profit_cents=62,
                count=1,
                maker_bid_cents=40,
            )

            assert res["buy_price_cents"] == 41  # Maker bid + 1
            assert res["take_profit_cents"] == 62
            assert gemini_trading_client.get_entry_price(ticker) == 41
            assert gemini_trading_client.has_existing_position_or_order(ticker) is True


def test_gemini_buy_favorite_maker_order(gemini_trading_client):
    ticker = "ETH15M-FAVORITE"

    mock_res = {"orderId": 145828835378730784, "status": "accepted"}
    with patch.object(gemini_trading_client, "_post", return_value=mock_res):
        res = gemini_trading_client.buy_favorite_maker_order(
            ticker=ticker,
            side="yes",
            maker_bid_cents=80,
            target_ask_cents=82,
            count=1,
        )

        assert res["buy_price_cents"] == 80
        assert res["spread_saved_cents"] == 2
        assert res["strategy_tag"] == "endgame_favorite_maker"
        assert gemini_trading_client.get_strategy_tag(ticker) == "endgame_favorite_maker"


def test_gemini_liquidation_and_cleanup(gemini_trading_client):
    ticker = "SOL15M-TEST"
    gemini_trading_client.set_entry_price(ticker, 50)
    gemini_trading_client.set_profit_floor(ticker, 40)

    mock_res = {"orderId": 145828835378730785, "status": "filled"}
    with patch.object(gemini_trading_client, "_post", return_value=mock_res):
        with patch.object(gemini_trading_client, "get_all_active_positions", return_value={}):
            gemini_trading_client.execute_liquidation(ticker, "yes", 1, 35)

            assert gemini_trading_client.get_market_position(ticker) is None
            assert gemini_trading_client.get_entry_price(ticker) is None
            assert gemini_trading_client.get_profit_floor(ticker) is None


def test_gemini_directional_risk_cap(gemini_trading_client):
    mock_positions = {
        "BTC15M-1": {"ticker": "BTC15M-1", "side": "yes", "count": 1},
        "ETH15M-1": {"ticker": "ETH15M-1", "side": "yes", "count": 1},
        "SOL15M-1": {"ticker": "SOL15M-1", "side": "yes", "count": 1},
    }

    with patch.object(gemini_trading_client, "get_all_active_positions", return_value=mock_positions):
        # Directional cap for YES should now block 4th YES trade
        assert gemini_trading_client.check_crypto_portfolio_directional_cap("yes", max_concurrent=3) is False
        # NO trade should still be permitted
        assert gemini_trading_client.check_crypto_portfolio_directional_cap("no", max_concurrent=3) is True

