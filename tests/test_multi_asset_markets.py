from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from database import DEFAULT_DYNAMIC_PRICE_MAP
from main import (
    PYTH_COMMODITY_FEEDS,
    INDEX_TICKER_MAP,
    SUPPORTED_SYMBOLS,
    batch_fetch_spot_prices,
    extract_asset_symbol,
    extract_target_strike,
    get_index_spot_price,
    get_pyth_commodity_spot_prices,
    get_spot_price,
    is_spot_momentum_aligned,
    process_hourly_market_group,
    process_single_market,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import (
    SERIES_15M_ALL,
    SERIES_15M_COMMODITIES,
    SERIES_HOURLY_ALL,
    SERIES_HOURLY_COMMODITIES,
    SERIES_HOURLY_INDICES,
    KalshiClient,
)


class DummyMultiAssetClient(BaseTradingClient):
    exchange_name = "Kalshi"

    def __init__(self):
        super().__init__()
        self._positions = {}
        self.buys = []
        self.liquidations = []

    def get_market_position(self, ticker: str):
        return self._positions.get(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str):
        return True

    def cancel_unfilled_buy_orders(self, ticker: str):
        return True

    def has_existing_position_or_order(self, ticker: str):
        return ticker in self._positions

    def clear_entry_price(self, ticker: str):
        pass

    def execute_liquidation(self, ticker: str, side: str, count: int, price: int):
        self._positions.pop(ticker, None)
        res = {"ticker": ticker, "side": side, "price": price}
        self.liquidations.append(res)
        return res

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
        self._positions[ticker] = {"ticker": ticker, "side": side, "count": count, "entry_price_cents": price_cents}
        self.set_entry_price(ticker, price_cents)
        res = {"ticker": ticker, "side": side, "price": price_cents}
        self.buys.append(res)
        return res

    def buy_and_set_take_profit(self, ticker: str, side: str, buy_price_cents: int, maker_bid_cents: int = None, **kw):
        effective = min(buy_price_cents, (maker_bid_cents + 1) if maker_bid_cents else buy_price_cents)
        self._positions[ticker] = {"ticker": ticker, "side": side, "count": 1, "entry_price_cents": effective}
        res = {"ticker": ticker, "side": side, "price": effective}
        self.buys.append(res)
        return res


@pytest.fixture
def multi_asset_client():
    return DummyMultiAssetClient()


@pytest.fixture
def mock_filter():
    return {
        "sl_threshold": 10, "sl_floor": 30, "cut_5min": 35, "endgame_12_5_floor": 25,
        "min_buy": 18, "max_buy": 44, "tp": 62, "avoid_mid_zone": True,
        "mid_zone_min": 37, "mid_zone_max": 43, "is_exploration": False,
        "momentum_min_buy": 56, "momentum_max_buy": 68, "momentum_tp": 88,
        "momentum_min_pct_dist": 0.10, "momentum_sl_floor": 40,
    }


# ---------------------------------------------------------------------------
# 1. Series Identifiers & Supported Symbols Tests
# ---------------------------------------------------------------------------

def test_series_lists_contain_new_markets():
    # 15M Commodities
    assert "KXGOLD15M" in SERIES_15M_COMMODITIES
    assert "KXWTI15M" in SERIES_15M_COMMODITIES
    assert "KXSILVER15M" in SERIES_15M_COMMODITIES
    for s in SERIES_15M_COMMODITIES:
        assert s in SERIES_15M_ALL

    # Hourly Commodities
    assert "KXGOLDH" in SERIES_HOURLY_COMMODITIES
    assert "KXSILVERH" in SERIES_HOURLY_COMMODITIES
    assert "KXWTIH" in SERIES_HOURLY_COMMODITIES
    for s in SERIES_HOURLY_COMMODITIES:
        assert s in SERIES_HOURLY_ALL

    # Hourly Indices
    assert "KXINXU" in SERIES_HOURLY_INDICES
    assert "KXNASDAQ100U" in SERIES_HOURLY_INDICES
    for s in SERIES_HOURLY_INDICES:
        assert s in SERIES_HOURLY_ALL

    # Supported Symbols
    for expected_sym in ("GOLD", "SILVER", "WTI", "INX", "NASDAQ100"):
        assert expected_sym in SUPPORTED_SYMBOLS
        assert expected_sym in DEFAULT_DYNAMIC_PRICE_MAP


# ---------------------------------------------------------------------------
# 2. Asset Symbol Extraction Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("series, ticker, expected", [
    ("KXGOLD15M", "KXGOLD15M-26AUG162345-45", "GOLD"),
    ("KXWTI15M", "KXWTI15M-26AUG162345-45", "WTI"),
    ("KXSILVER15M", "KXSILVER15M-26AUG162345-45", "SILVER"),
    ("KXGOLDH", "KXGOLDH-26AUG1701-T4427.99", "GOLD"),
    ("KXSILVERH", "KXSILVERH-26AUG1701-T66.449", "SILVER"),
    ("KXWTIH", "KXWTIH-26AUG1701-T83.49", "WTI"),
    ("KXINXU", "KXINXU-26AUG17H1000-T7919.9999", "INX"),
    ("KXNASDAQ100U", "KXNASDAQ100U-26AUG17H1000-T31989.99", "NASDAQ100"),
])
def test_extract_asset_symbol_commodities_and_indices(series, ticker, expected):
    assert extract_asset_symbol(series, ticker) == expected


# ---------------------------------------------------------------------------
# 3. Strike Extraction Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("market, expected", [
    # Gold 15m with floor_strike
    ({"floor_strike": 4393.04, "ticker": "KXGOLD15M-26AUG162345-45"}, 4393.04),
    # Silver 15m with yes_sub_title
    ({"floor_strike": None, "yes_sub_title": "Target Price: $65.584", "ticker": "KXSILVER15M"}, 65.584),
    # WTI Hourly with -T in ticker
    ({"floor_strike": None, "ticker": "KXWTIH-26AUG1701-T83.49"}, 83.49),
    # S&P 500 Hourly with floor_strike and subtitle
    ({"floor_strike": 7920, "subtitle": "7,920.0099 or above", "ticker": "KXINXU-26AUG17H1000-T7919.9999"}, 7920.0),
    # Nasdaq 100 Hourly with subtitle string
    ({"floor_strike": None, "subtitle": "31,990 or above", "ticker": "KXNASDAQ100U"}, 31990.0),
])
def test_extract_target_strike_commodities_and_indices(market, expected):
    assert pytest.approx(extract_target_strike(market)) == expected


# ---------------------------------------------------------------------------
# 4. Spot Price Fetching Tests (Pyth & Yahoo Finance)
# ---------------------------------------------------------------------------

def test_pyth_commodity_spot_prices_mocked():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "ok"
    mock_response.json.return_value = {
        "parsed": [
            {
                "id": PYTH_COMMODITY_FEEDS["GOLD"],
                "price": {"price": "439500000", "expo": -5},
            },
            {
                "id": PYTH_COMMODITY_FEEDS["SILVER"],
                "price": {"price": "656000", "expo": -4},
            },
            {
                "id": PYTH_COMMODITY_FEEDS["WTI"],
                "price": {"price": "820500", "expo": -4},
            },
        ]
    }

    with patch("main.http_session.get", return_value=mock_response):
        prices = get_pyth_commodity_spot_prices(["GOLD", "SILVER", "WTI"])
        assert prices["GOLD"] == 4395.0
        assert prices["SILVER"] == 65.6
        assert prices["WTI"] == 82.05


def test_yahoo_index_spot_price_mocked():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "ok"
    mock_response.json.return_value = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 7785.50,
                    }
                }
            ]
        }
    }

    with patch("main.http_session.get", return_value=mock_response):
        spx_price = get_index_spot_price("INX")
        assert spx_price == 7785.50


def test_unified_batch_fetch_spot_prices_mocked():
    def mock_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        if "hermes.pyth.network" in url:
            resp.json.return_value = {
                "parsed": [
                    {"id": PYTH_COMMODITY_FEEDS["GOLD"], "price": {"price": "440000", "expo": -2}},
                    {"id": PYTH_COMMODITY_FEEDS["SILVER"], "price": {"price": "6500", "expo": -2}},
                    {"id": PYTH_COMMODITY_FEEDS["WTI"], "price": {"price": "8200", "expo": -2}},
                ]
            }
        elif "finance.yahoo.com" in url:
            ticker = "^GSPC" if "%5EGSPC" in url or "^GSPC" in url else "^NDX"
            val = 7800.0 if "^GSPC" in ticker else 30000.0
            resp.json.return_value = {"chart": {"result": [{"meta": {"regularMarketPrice": val}}]}}
        elif "api.coinbase.com" in url:
            resp.json.return_value = {"data": {"amount": "65000.00"}}
        return resp

    with patch("main.http_session.get", side_effect=mock_get):
        symbols = ["BTC", "GOLD", "SILVER", "WTI", "INX", "NASDAQ100"]
        cache = batch_fetch_spot_prices(symbols)
        assert cache["BTC"] == 65000.0
        assert cache["GOLD"] == 4400.0
        assert cache["SILVER"] == 65.0
        assert cache["WTI"] == 82.0
        assert cache["INX"] == 7800.0
        assert cache["NASDAQ100"] == 30000.0


# ---------------------------------------------------------------------------
# 5. Momentum Alignment for Commodities and Indices
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side, spot, target, expected", [
    # Gold (high dollar value)
    ("yes", 4450.0, 4400.0, True),
    ("no", 4450.0, 4400.0, False),
    ("yes", 4350.0, 4400.0, False),
    ("no", 4350.0, 4400.0, True),
    # S&P 500 (high index value)
    ("yes", 7950.0, 7900.0, True),
    ("no", 7950.0, 7900.0, False),
    # Silver (small dollar value)
    ("yes", 66.50, 65.00, True),
    ("no", 64.00, 65.00, True),
])
def test_spot_momentum_alignment_multi_asset(side, spot, target, expected):
    assert is_spot_momentum_aligned(side, spot, target) is expected


# ---------------------------------------------------------------------------
# 6. Hourly Multi-Strike Execution for Commodities & Indices
# ---------------------------------------------------------------------------

def test_hourly_multi_strike_commodity_dual_buy(multi_asset_client, mock_filter):
    markets = [
        {"ticker": "KXGOLDH-A", "series_ticker": "KXGOLDH", "floor_strike": 4380.0, "yes_ask_dollars": "0.34", "yes_bid_dollars": "0.32", "no_ask_dollars": "0.68", "no_bid_dollars": "0.66"},
        {"ticker": "KXGOLDH-B", "series_ticker": "KXGOLDH", "floor_strike": 4420.0, "yes_ask_dollars": "0.72", "yes_bid_dollars": "0.70", "no_ask_dollars": "0.30", "no_bid_dollars": "0.28"},
    ]
    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter):
        snaps = process_hourly_market_group(
            symbol="GOLD",
            markets=markets,
            client=multi_asset_client,
            now_utc=datetime(2026, 8, 17, 1, 0, 30, tzinfo=timezone.utc),
            minute_past_hour=0,
            spot_cache={"GOLD": 4400.0},
            dry_run=False,
        )

    assert len(multi_asset_client.buys) == 2
    assert snaps[0]["tf"] == "1h"
    assert snaps[0]["sym"] == "GOLD"


def test_hourly_multi_strike_index_tiebreaker(multi_asset_client, mock_filter):
    # Same contract ticker with cheaper NO (32¢ NO vs 68¢ YES)
    markets = [
        {
            "ticker": "KXINXU-26AUG17H1000-T7919.9999",
            "series_ticker": "KXINXU",
            "floor_strike": 7920.0,
            "yes_ask_dollars": "0.68",
            "yes_bid_dollars": "0.66",
            "no_ask_dollars": "0.32",
            "no_bid_dollars": "0.30",
        }
    ]
    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter):
        snaps = process_hourly_market_group(
            symbol="INX",
            markets=markets,
            client=multi_asset_client,
            now_utc=datetime(2026, 8, 17, 14, 0, 30, tzinfo=timezone.utc),
            minute_past_hour=0,
            spot_cache={"INX": 7915.0},
            dry_run=False,
        )

    assert len(multi_asset_client.buys) == 1
    assert multi_asset_client.buys[0]["side"] == "no"
    assert snaps[0]["sym"] == "INX"
