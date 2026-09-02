from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from database import DEFAULT_DYNAMIC_PRICE_MAP
from main import (
    PYTH_COMMODITY_FEEDS,
    SUPPORTED_SYMBOLS,
    batch_fetch_spot_prices,
    extract_asset_symbol,
    extract_target_strike,
    get_index_spot_price,
    get_pyth_commodity_spot_prices,
    is_spot_momentum_aligned,
    process_hourly_market_group,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import (
    SERIES_15M_ALL,
    SERIES_15M_COMMODITIES,
    SERIES_HOURLY_ALL,
    SERIES_HOURLY_COMMODITIES,
    SERIES_HOURLY_INDICES,
)


@pytest.fixture(autouse=True)
def mock_ungated_asset():
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


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


@pytest.fixture(autouse=True)
def mock_ungated_asset():
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


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
            if "XAU-USD" in url:
                resp.json.return_value = {"data": {"amount": "4400.00"}}
            elif "XAG-USD" in url:
                resp.json.return_value = {"data": {"amount": "65.00"}}
            else:
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

def test_hourly_multi_strike_commodity_single_directional(multi_asset_client, mock_filter):
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

    assert len(multi_asset_client.buys) == 1
    assert multi_asset_client.buys[0]["ticker"] == "KXGOLDH-B"
    assert multi_asset_client.buys[0]["side"] == "no"
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


# ---------------------------------------------------------------------------
# 6. Timeframe Detection Tests (5M vs 15M vs 1H)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker, expected_tf", [
    ("GEMI-BTC05M2606011000-UP", "5m"),
    ("BTC05M", "5m"),
    ("GOLD05M", "5m"),
    ("KXBTC5M", "5m"),
    ("GEMI-ETH5M2606011000-UP", "5m"),
    ("GEMI-BTC15M2602251745-UP", "15m"),
    ("KXBTC15M-26AUG141315-15", "15m"),
    ("KXGOLD15M-26AUG162345-45", "15m"),
    ("KXWTI15M", "15m"),
    ("KXSILVER15M", "15m"),
    ("GEMI-BTCH2608181300-UP", "1h"),
    ("KXBTCD-26AUG1521-T69399.99", "1h"),
    ("KXGOLDH-26AUG1701-T4427.99", "1h"),
    ("KXINXU-26AUG17H1000-T7919.9999", "1h"),
    ("KXNASDAQ100U-26AUG17H1000-T31989.99", "1h"),
])
def test_detect_market_timeframe_5m_15m_1h(ticker, expected_tf):
    from database import detect_market_timeframe
    assert detect_market_timeframe(ticker) == expected_tf


# ---------------------------------------------------------------------------
# 7. Commodity Spot Fallback & Gating Integrity Tests
# ---------------------------------------------------------------------------

def test_pyth_commodity_spot_prices_fallback_to_yahoo(monkeypatch):
    """When Pyth Hermes returns 401/error, ensure automatic fallback to Yahoo Finance for Gold/Silver/WTI."""
    from main import get_pyth_commodity_spot_prices

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = "Mocked"

        def json(self):
            return self._json

    def mock_get(url, *args, **kwargs):
        if "hermes.pyth.network" in url:
            return MockResponse(401, {"error": "unauthorized"})
        if "query1.finance.yahoo.com" in url or "query2.finance.yahoo.com" in url:
            if "GC=F" in url:
                return MockResponse(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 4500.50}}]}})
            if "SI=F" in url:
                return MockResponse(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 68.25}}]}})
            if "CL=F" in url:
                return MockResponse(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 82.75}}]}})
        return MockResponse(404, {})

    monkeypatch.setattr("main.http_session.get", mock_get)

    spots = get_pyth_commodity_spot_prices(["GOLD", "SILVER", "WTI"])
    assert spots["GOLD"] == 4500.50
    assert spots["SILVER"] == 68.25
    assert spots["WTI"] == 82.75


def test_commodity_favorite_maker_requires_spot_price(multi_asset_client, mock_filter):
    """Ensures commodities (GOLD, SILVER, WTI) reject favorite maker entries if spot price is missing."""
    from main import evaluate_favorite_maker_opportunity

    gold_mkt = {
        "ticker": "KXGOLDH-26AUG1800-T4400.00",
        "series_ticker": "KXGOLDH",
        "floor_strike": 4400.0,
    }
    quotes = {
        "yes_ask": 88,
        "yes_bid": 84,
        "no_ask": 16,
        "no_bid": 12,
    }

    # With missing spot price -> Rejected
    opp_no_spot = evaluate_favorite_maker_opportunity(
        market=gold_mkt,
        quotes=quotes,
        rem_mins=5.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=None,
        target_price=4400.0,
    )
    assert opp_no_spot is None

    # With sufficient spot lead (spot=4430.0 >= 4400.0 * 1.005) -> Accepted
    opp_with_spot = evaluate_favorite_maker_opportunity(
        market=gold_mkt,
        quotes=quotes,
        rem_mins=5.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=4430.0,
        target_price=4400.0,
    )
    assert opp_with_spot is not None
    assert opp_with_spot["side"] == "yes"


def test_silver_stricter_spot_lead_gate(multi_asset_client, mock_filter):
    """Ensures SILVER requires >= 0.50% spot lead, rejecting insufficient leads like 0.35%."""
    from main import evaluate_favorite_maker_opportunity

    silver_mkt = {
        "ticker": "KXSILVER15M-26AUG170200-00",
        "series_ticker": "KXSILVER15M",
        "floor_strike": 65.0,
    }
    quotes = {
        "yes_ask": 88,
        "yes_bid": 84,
        "no_ask": 16,
        "no_bid": 12,
    }

    # 0.35% spot lead (65.0 * 1.0035 = 65.2275) -> Rejected (< 0.50%)
    opp_weak_lead = evaluate_favorite_maker_opportunity(
        market=silver_mkt,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=65.2275,
        target_price=65.0,
    )
    assert opp_weak_lead is None

    # 0.60% spot lead (65.0 * 1.0060 = 65.39) -> Approved (>= 0.50%)
    opp_strong_lead = evaluate_favorite_maker_opportunity(
        market=silver_mkt,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=65.39,
        target_price=65.0,
    )
    assert opp_strong_lead is not None
    assert opp_strong_lead["side"] == "yes"


def test_commodity_wide_spread_rejection_forensic(multi_asset_client, mock_filter):
    """Forensic unit tests matching the real-world KXSILVERH and KXGOLDH failures:
    Verifies that wide-spread (>8¢) or illiquid order books on commodities are strictly rejected.
    """
    from main import evaluate_favorite_maker_opportunity

    # 1. Real KXSILVERH-26SEP0219-T65.349 failure case (Bid: 71¢, Ask: 93¢ -> 22¢ spread)
    silver_mkt = {
        "ticker": "KXSILVERH-26SEP0219-T65.349",
        "series_ticker": "KXSILVERH",
        "floor_strike": 65.349,
    }
    quotes_silver_wide = {
        "yes_ask": 93,
        "yes_bid": 71,
        "no_ask": 29,
        "no_bid": 7,
    }
    opp_silver = evaluate_favorite_maker_opportunity(
        market=silver_mkt,
        quotes=quotes_silver_wide,
        rem_mins=24.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=66.00,  # strong spot lead
        target_price=65.349,
    )
    assert opp_silver is None, "Expected KXSILVERH with 22¢ spread to be rejected"

    # 2. Real KXGOLDH-26SEP0219-T4383.99 failure case (Bid: 66¢, Ask: 86¢ -> 20¢ spread)
    gold_mkt = {
        "ticker": "KXGOLDH-26SEP0219-T4383.99",
        "series_ticker": "KXGOLDH",
        "floor_strike": 4383.99,
    }
    quotes_gold_wide = {
        "yes_ask": 86,
        "yes_bid": 66,
        "no_ask": 34,
        "no_bid": 14,
    }
    opp_gold = evaluate_favorite_maker_opportunity(
        market=gold_mkt,
        quotes=quotes_gold_wide,
        rem_mins=16.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=4420.0,
        target_price=4383.99,
    )
    assert opp_gold is None, "Expected KXGOLDH with 20¢ spread to be rejected"

    # 3. Real KXSILVERH-26SEP0219-T65.299 failure case (Bid: 37¢, Ask: 89¢ -> 52¢ spread)
    quotes_silver_extreme = {
        "yes_ask": 89,
        "yes_bid": 37,
        "no_ask": 63,
        "no_bid": 11,
    }
    opp_silver_extreme = evaluate_favorite_maker_opportunity(
        market=silver_mkt,
        quotes=quotes_silver_extreme,
        rem_mins=4.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=66.00,
        target_price=65.349,
    )
    assert opp_silver_extreme is None, "Expected KXSILVERH with 52¢ spread to be rejected"

    # 4. NO-side wide spread rejection on commodity
    quotes_no_wide = {
        "yes_ask": 35,
        "yes_bid": 10,
        "no_ask": 90,
        "no_bid": 65,  # 25¢ spread on NO
    }
    opp_no_wide = evaluate_favorite_maker_opportunity(
        market=silver_mkt,
        quotes=quotes_no_wide,
        rem_mins=10.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=64.00,  # spot below strike -> favorable for NO
        target_price=65.349,
    )
    assert opp_no_wide is None, "Expected NO side with 25¢ spread to be rejected"


def test_commodity_valley_of_death_floor_and_execution_gates(multi_asset_client, mock_filter):
    """Verifies that even if spread filter is relaxed, maker bids < 82¢ on commodities
    or inside the Valley of Death (51¢–80¢) are strictly blocked from execution."""
    from main import evaluate_favorite_maker_opportunity, _execute_favorite_maker_flow

    gold_mkt = {
        "ticker": "KXGOLDH-26SEP0219-T4383.99",
        "series_ticker": "KXGOLDH",
        "floor_strike": 4383.99,
    }
    # Relax spread gate to test that maker_bid floor specifically catches it
    relaxed_filter = dict(mock_filter)
    relaxed_filter["max_commodity_spread"] = 30

    # Bid 71¢, Ask 93¢ -> maker_bid would be 73¢ (inside Valley of Death 51¢–80¢ and < 82¢ floor)
    quotes = {"yes_ask": 93, "yes_bid": 71, "no_ask": 29, "no_bid": 7}
    opp = evaluate_favorite_maker_opportunity(
        market=gold_mkt,
        quotes=quotes,
        rem_mins=10.0,
        dynamic_filters=relaxed_filter,
        client=multi_asset_client,
        spot_price=4420.0,
        target_price=4383.99,
    )
    assert opp is None, "Maker bid of 73¢ must be rejected by commodity floor / Valley of Death gate"

    # Direct test of _execute_favorite_maker_flow defense-in-depth gate
    multi_asset_client.has_existing_position_or_order = lambda t: False
    success_vod, details_vod = _execute_favorite_maker_flow(
        ticker="KXGOLDH-26SEP0219-T4383.99",
        side="yes",
        maker_bid=73,  # Valley of Death
        target_ask=93,
        spot_price=4420.0,
        target_price=4383.99,
        market_result=None,
        client=multi_asset_client,
        dry_run=False,
    )
    assert success_vod is False
    assert details_vod is None

    # Underdog bid (<= 50¢) directly blocked
    success_underdog, details_underdog = _execute_favorite_maker_flow(
        ticker="KXSILVERH-26SEP0219-T65.299",
        side="yes",
        maker_bid=39,
        target_ask=89,
        spot_price=66.0,
        target_price=65.299,
        market_result=None,
        client=multi_asset_client,
        dry_run=False,
    )
    assert success_underdog is False
    assert details_underdog is None


def test_commodity_tight_spread_favorite_accepted(multi_asset_client, mock_filter):
    """Verifies that liquid, healthy commodity favorite contracts (spread <= 8¢, maker_bid >= 82¢)
    are approved normally."""
    from main import evaluate_favorite_maker_opportunity

    silver_mkt = {
        "ticker": "KXSILVERH-VALID",
        "series_ticker": "KXSILVERH",
        "floor_strike": 65.0,
    }
    # Tight spread (3¢: Bid 86¢, Ask 89¢) -> maker_bid will be 87¢ (>= 82¢ floor)
    quotes = {"yes_ask": 89, "yes_bid": 86, "no_ask": 14, "no_bid": 11}
    opp = evaluate_favorite_maker_opportunity(
        market=silver_mkt,
        quotes=quotes,
        rem_mins=5.0,
        dynamic_filters=mock_filter,
        client=multi_asset_client,
        spot_price=65.40,  # 0.61% spot lead >= 0.50%
        target_price=65.0,
    )
    assert opp is not None
    assert opp["side"] == "yes"
    assert opp["maker_bid"] == 87
    assert opp["maker_bid"] >= 82


# ---------------------------------------------------------------------------
# 9. Coinbase Spot Pricing Integrity Tests (Gold, Silver, Crypto)
# ---------------------------------------------------------------------------

def test_coinbase_commodity_spot_prices_gold_and_silver_mocked(monkeypatch):
    """Verifies that get_pyth_commodity_spot_prices uses Coinbase as primary for GOLD and SILVER."""
    from main import get_pyth_commodity_spot_prices, get_coinbase_spot_price

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = "Mocked"

        def json(self):
            return self._json

    def mock_get(url, *args, **kwargs):
        if "api.coinbase.com" in url:
            if "XAU-USD" in url:
                return MockResponse(200, {"data": {"amount": "4383.25"}})
            if "XAG-USD" in url:
                return MockResponse(200, {"data": {"amount": "65.31"}})
        return MockResponse(404, {})

    monkeypatch.setattr("main.http_session.get", mock_get)

    gold_spot = get_coinbase_spot_price("XAU-USD")
    assert gold_spot == 4383.25

    silver_spot = get_coinbase_spot_price("XAG-USD")
    assert silver_spot == 65.31

    spots = get_pyth_commodity_spot_prices(["GOLD", "SILVER"])
    assert spots["GOLD"] == 4383.25
    assert spots["SILVER"] == 65.31


def test_coinbase_commodity_spot_prices_paxg_fallback_mocked(monkeypatch):
    """When XAU-USD fails, verify PAXG-USD on Coinbase is used as gold fallback."""
    from main import get_pyth_commodity_spot_prices

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = "Mocked"

        def json(self):
            return self._json

    def mock_get(url, *args, **kwargs):
        if "api.coinbase.com" in url:
            if "XAU-USD" in url:
                return MockResponse(404, {"error": "not found"})
            if "PAXG-USD" in url:
                return MockResponse(200, {"data": {"amount": "4385.50"}})
        return MockResponse(404, {})

    monkeypatch.setattr("main.http_session.get", mock_get)

    spots = get_pyth_commodity_spot_prices(["GOLD"])
    assert spots["GOLD"] == 4385.50


def test_coinbase_crypto_fallback_mocked(monkeypatch):
    """Verifies that get_crypto_spot_price falls back to Coinbase public API when CFB is unavailable."""
    from main import get_crypto_spot_price

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = "Mocked"

        def json(self):
            return self._json

    def mock_get(url, *args, **kwargs):
        if "api.coinbase.com" in url and "BTC-USD" in url:
            return MockResponse(200, {"data": {"amount": "67420.50"}})
        return MockResponse(404, {})

    monkeypatch.setattr("main.http_session.get", mock_get)

    # Without a KalshiClient or CFB price, should hit Coinbase
    btc_price = get_crypto_spot_price("BTC", client=None)
    assert btc_price == 67420.50


def test_wti_routes_to_yahoo_when_coinbase_has_no_oil(monkeypatch):
    """Verifies that WTI (Oil) correctly falls back to Yahoo (CL=F) since Coinbase has no oil pairs."""
    from main import get_pyth_commodity_spot_prices

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.text = "Mocked"

        def json(self):
            return self._json

    def mock_get(url, *args, **kwargs):
        if "hermes.pyth.network" in url:
            return MockResponse(401, {"error": "unauthorized"})
        if "finance.yahoo.com" in url and "CL=F" in url:
            return MockResponse(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 90.75}}]}})
        return MockResponse(404, {})

    monkeypatch.setattr("main.http_session.get", mock_get)

    spots = get_pyth_commodity_spot_prices(["WTI"])
    assert spots["WTI"] == 90.75



