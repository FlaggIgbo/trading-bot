from __future__ import annotations

from datetime import datetime, timezone

from unittest.mock import patch
import pytest

from main import extract_asset_symbol, extract_target_strike, process_hourly_market_group
from signals.base_client import BaseTradingClient


@pytest.fixture(autouse=True)
def mock_ungated_asset():
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


class DummyHourlyTradingClient(BaseTradingClient):
    exchange_name = "TestExchange"

    def __init__(self):
        super().__init__()
        self._positions = {}
        self.buys = []
        self.liquidations = []

    def get_market_position(self, ticker: str): return self._positions.get(ticker)
    def cancel_and_verify_resting_orders(self, ticker: str): return True
    def cancel_unfilled_buy_orders(self, ticker: str): return True
    def has_existing_position_or_order(self, ticker: str): return ticker in self._positions
    def clear_entry_price(self, ticker: str): pass

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
        count = kw.get("count", 1)
        self._positions[ticker] = {"ticker": ticker, "side": side, "count": count, "entry_price_cents": effective}
        res = {"ticker": ticker, "side": side, "price": effective, "count": count}
        self.buys.append(res)
        return res

    def buy_favorite_maker_order(self, ticker: str, side: str, maker_bid_cents: int, count: int = 1, **kw):
        self._positions[ticker] = {"ticker": ticker, "side": side, "count": count, "entry_price_cents": maker_bid_cents}
        res = {"ticker": ticker, "side": side, "buy_price_cents": maker_bid_cents, "price": maker_bid_cents, "count": count, "strategy_tag": "endgame_favorite_maker"}
        self.buys.append(res)
        return res


@pytest.fixture
def hourly_client():
    return DummyHourlyTradingClient()


@pytest.fixture(autouse=True)
def mock_ungated_asset(monkeypatch):
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


@pytest.mark.parametrize("series, ticker, expected", [
    ("KXBTCD", "KXBTCD-26AUG1521-T69399.99", "BTC"),
    ("KXETHD", "KXETHD-26AUG1617-T2609.99", "ETH"),
    ("KXDOGED", "KXDOGED-26AUG1617-T0.1549999", "DOGE"),
    ("KXZECH", "KXZECH-TEST", "ZEC"),
])
def test_extract_symbol_hourly(series, ticker, expected):
    assert extract_asset_symbol(series, ticker) == expected


@pytest.mark.parametrize("market, expected", [
    ({"floor_strike": 72249.99}, 72249.99),
    ({"floor_strike": None, "subtitle": "$72,250 or above"}, 72250.0),
    ({"floor_strike": None, "subtitle": None, "ticker": "KXDOGED-26AUG1617-T0.1549999"}, 0.1549999),
])
def test_extract_target_strike(market, expected):
    assert pytest.approx(extract_target_strike(market)) == expected


def test_hourly_multi_strike_single_directional_opportunity(hourly_client):
    # Strike A (Bullish YES @ 34¢) vs Strike B (Bearish NO @ 30¢) -> Picks single cheaper opportunity (NO @ 30¢, maker 29¢)
    markets = [
        {"ticker": "KXBTCD-A", "series_ticker": "KXBTCD", "floor_strike": 70000.0, "yes_ask_dollars": "0.34", "yes_bid_dollars": "0.32", "no_ask_dollars": "0.68", "no_bid_dollars": "0.66"},
        {"ticker": "KXBTCD-B", "series_ticker": "KXBTCD", "floor_strike": 74000.0, "yes_ask_dollars": "0.72", "yes_bid_dollars": "0.70", "no_ask_dollars": "0.30", "no_bid_dollars": "0.28"},
    ]
    snaps = process_hourly_market_group("BTC", markets, hourly_client, datetime(2026, 8, 15, 5, 0, 0, tzinfo=timezone.utc), 0, {"BTC": 71000.0}, False)

    assert len(hourly_client.buys) == 1
    assert hourly_client.buys[0]["ticker"] == "KXBTCD-B"
    assert hourly_client.buys[0]["side"] == "no"
    assert hourly_client.buys[0]["count"] == 3
    assert snaps[0]["tf"] == "1h"


@pytest.mark.parametrize("minute, init_pos, init_price, expected_liq, expected_action", [
    (10, ("KXBTCD-COL", "yes"), 48, "KXBTCD-COL", "10min_loss_cut"),
    (20, ("KXBTCD-CUT", "yes"), 48, "KXBTCD-CUT", "20min_loss_cut"),
    (50, ("KXBTCD-END", "yes"), 40, "KXBTCD-END", "50min_loss_cut"),
    (52, ("KXBTCD-CUT52", "yes"), 48, "KXBTCD-CUT52", "52_5min_loss_cut"),
    (57, ("KXBTCD-END57", "yes"), 40, "KXBTCD-END57", "57_5min_loss_cut"),
    (59, ("KXBTCD-END59", "yes"), 85, "KXBTCD-END59", "59min_crash_selloff_cut"),
])
def test_hourly_lifecycle_cut_losses(hourly_client, minute, init_pos, init_price, expected_liq, expected_action):
    ticker, side = init_pos
    hourly_client._positions[ticker] = {"ticker": ticker, "side": side, "entry_price_cents": init_price}

    markets = [{
        "ticker": ticker, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.08", "yes_bid_dollars": "0.06", "no_ask_dollars": "0.94", "no_bid_dollars": "0.92"
    }]

    snaps = process_hourly_market_group("BTC", markets, hourly_client, datetime(2026, 8, 15, 5, minute, 0, tzinfo=timezone.utc), minute, {"BTC": 71000.0}, False)

    assert len(hourly_client.liquidations) == 1
    assert hourly_client.liquidations[0]["ticker"] == expected_liq
    assert snaps[0]["trd"]["action"] == expected_action


def test_hourly_52_5min_cut_loss_and_favorite_maker(hourly_client):
    """Verify that hourly markets at :52.5 cut losses on dropping positions and scan Favorite Maker on unowned strikes."""
    # 1. Losing position at minute 52 (entry 48¢, current price 8¢ < threshold 15¢) -> 52_5min_loss_cut
    ticker_loser = "KXBTCD-LOSER52"
    hourly_client._positions[ticker_loser] = {"ticker": ticker_loser, "side": "yes", "entry_price_cents": 48}
    market_loser = {
        "ticker": ticker_loser, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.10", "yes_bid_dollars": "0.08", "no_ask_dollars": "0.92", "no_bid_dollars": "0.90"
    }
    snaps_loss = process_hourly_market_group(
        "BTC", [market_loser], hourly_client,
        datetime(2026, 8, 15, 5, 52, 30, tzinfo=timezone.utc), 52, {"BTC": 69000.0}, False
    )
    assert len(hourly_client.liquidations) == 1
    assert hourly_client.liquidations[0]["ticker"] == ticker_loser
    assert snaps_loss[0]["trd"]["action"] == "52_5min_loss_cut"

    # 2. Unowned confirmed favorite at minute 52 (YES ask 86¢) -> executes Favorite Maker buy
    hourly_client.liquidations.clear()
    hourly_client._positions.clear()
    market_fav = {
        "ticker": "KXBTCD-FAV52", "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.86", "yes_bid_dollars": "0.82", "no_ask_dollars": "0.18", "no_bid_dollars": "0.14",
        "close_time": "2026-08-15T06:00:00Z",
    }
    snaps_fav = process_hourly_market_group(
        "BTC", [market_fav], hourly_client,
        datetime(2026, 8, 15, 5, 52, 30, tzinfo=timezone.utc), 52, {"BTC": 71000.0}, False
    )
    assert len(hourly_client.buys) == 1
    assert hourly_client.buys[0]["ticker"] == "KXBTCD-FAV52"
    assert snaps_fav[0]["fav_maker"] is True
    assert snaps_fav[0]["trd"]["strategy_tag"] == "endgame_favorite_maker"


def test_hourly_55min_stop_loss_and_winner_banking(hourly_client):
    """Verify that hourly markets at :55 apply 10m-style checks (stop-loss on losers, banking on winners)."""
    # 1. Losing position at minute 55 (entry 48¢, current bid 8¢) -> stop loss liquidation
    ticker_loser = "KXBTCD-LOSER55"
    hourly_client._positions[ticker_loser] = {"ticker": ticker_loser, "side": "yes", "entry_price_cents": 48}
    market_loser = {
        "ticker": ticker_loser, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.10", "yes_bid_dollars": "0.08", "no_ask_dollars": "0.92", "no_bid_dollars": "0.90"
    }
    snaps_loss = process_hourly_market_group(
        "BTC", [market_loser], hourly_client,
        datetime(2026, 8, 15, 5, 55, 0, tzinfo=timezone.utc), 55, {"BTC": 69000.0}, False
    )
    assert len(hourly_client.liquidations) == 1
    assert hourly_client.liquidations[0]["ticker"] == ticker_loser
    assert snaps_loss[0]["trd"]["action"] == "stop_loss_sold"

    # 2. Winning position at minute 55 (entry 30¢, current bid 75¢) -> winner profit banking
    hourly_client.liquidations.clear()
    hourly_client._positions.clear()
    ticker_winner = "KXBTCD-WINNER55"
    hourly_client._positions[ticker_winner] = {"ticker": ticker_winner, "side": "yes", "entry_price_cents": 30}
    market_winner = {
        "ticker": ticker_winner, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.78", "yes_bid_dollars": "0.75", "no_ask_dollars": "0.25", "no_bid_dollars": "0.22"
    }
    snaps_win = process_hourly_market_group(
        "BTC", [market_winner], hourly_client,
        datetime(2026, 8, 15, 5, 55, 0, tzinfo=timezone.utc), 55, {"BTC": 71000.0}, False
    )
    assert len(hourly_client.liquidations) == 1
    assert hourly_client.liquidations[0]["ticker"] == ticker_winner
    assert snaps_win[0]["trd"]["action"] == "winner_profit_banked"


def test_hourly_57_5min_endgame_safety_cut_and_hold_winner(hourly_client):
    """Verify that hourly markets at :57.5 cut losses on losing positions and hold winning positions."""
    # 1. Losing position at minute 57 (entry 40¢, current bid 8¢) -> endgame cut loss (57_5min_loss_cut)
    ticker_loser = "KXBTCD-LOSER57"
    hourly_client._positions[ticker_loser] = {"ticker": ticker_loser, "side": "yes", "entry_price_cents": 40}
    market_loser = {
        "ticker": ticker_loser, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.10", "yes_bid_dollars": "0.08", "no_ask_dollars": "0.92", "no_bid_dollars": "0.90"
    }
    snaps_loss = process_hourly_market_group(
        "BTC", [market_loser], hourly_client,
        datetime(2026, 8, 15, 5, 57, 30, tzinfo=timezone.utc), 57, {"BTC": 69000.0}, False
    )
    assert len(hourly_client.liquidations) == 1
    assert hourly_client.liquidations[0]["ticker"] == ticker_loser
    assert snaps_loss[0]["trd"]["action"] == "57_5min_loss_cut"

    # 2. Winning position at minute 57 (entry 40¢, current bid 75¢) -> held into settlement (no liquidation)
    hourly_client.liquidations.clear()
    hourly_client._positions.clear()
    ticker_winner = "KXBTCD-WINNER57"
    hourly_client._positions[ticker_winner] = {"ticker": ticker_winner, "side": "yes", "entry_price_cents": 40}
    market_winner = {
        "ticker": ticker_winner, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.78", "yes_bid_dollars": "0.75", "no_ask_dollars": "0.25", "no_bid_dollars": "0.22"
    }
    snaps_win = process_hourly_market_group(
        "BTC", [market_winner], hourly_client,
        datetime(2026, 8, 15, 5, 57, 30, tzinfo=timezone.utc), 57, {"BTC": 71000.0}, False
    )
    assert len(hourly_client.liquidations) == 0  # Held to settlement


def test_hourly_favorite_maker_buys_winners_at_55_and_57_5(hourly_client):
    """Verify that hourly unowned favorite markets (74¢–93¢) execute Favorite Maker buys at :55 and :57.5."""
    # 1. At minute :55 UTC (5 minutes remaining before expiry)
    market_55 = {
        "ticker": "KXBTCD-FAV55", "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.86", "yes_bid_dollars": "0.82", "no_ask_dollars": "0.18", "no_bid_dollars": "0.14",
        "close_time": "2026-08-15T06:00:00Z",
    }
    snaps_55 = process_hourly_market_group(
        "BTC", [market_55], hourly_client,
        datetime(2026, 8, 15, 5, 55, 0, tzinfo=timezone.utc), 55, {"BTC": 71000.0}, False
    )
    assert len(hourly_client.buys) == 1
    assert hourly_client.buys[0]["ticker"] == "KXBTCD-FAV55"
    assert snaps_55[0]["fav_maker"] is True
    assert snaps_55[0]["trd"]["strategy_tag"] == "endgame_favorite_maker"

    # 2. At minute :57.5 UTC (2.5 minutes remaining before expiry)
    hourly_client.buys.clear()
    hourly_client._positions.clear()
    market_57 = {
        "ticker": "KXBTCD-FAV57", "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.88", "yes_bid_dollars": "0.84", "no_ask_dollars": "0.16", "no_bid_dollars": "0.12",
        "close_time": "2026-08-15T06:00:00Z",
    }
    snaps_57 = process_hourly_market_group(
        "BTC", [market_57], hourly_client,
        datetime(2026, 8, 15, 5, 57, 30, tzinfo=timezone.utc), 57, {"BTC": 71000.0}, False
    )
    assert len(hourly_client.buys) == 1
    assert hourly_client.buys[0]["ticker"] == "KXBTCD-FAV57"
    assert snaps_57[0]["fav_maker"] is True
    assert snaps_57[0]["trd"]["strategy_tag"] == "endgame_favorite_maker"


def test_hourly_mid_zone_allowed_during_exploration(hourly_client):
    markets = [{
        "ticker": "KXBTCD-MID", "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.50", "yes_bid_dollars": "0.48",
        "no_ask_dollars": "0.52", "no_bid_dollars": "0.50",
    }]
    # Under empirical rules, 50¢ at minute 0 is strictly avoided for live capital and logged counterfactually
    snaps_exp = process_hourly_market_group(
        "BTC", markets, hourly_client,
        datetime(2026, 8, 15, 5, 0, 0, tzinfo=timezone.utc),
        0, {"BTC": 71000.0}, False,
    )
    assert len(hourly_client.buys) == 0
    assert snaps_exp[0]["trd"]["action"] in ("counterfactual_empirical_reject", "counterfactual_mid_zone")

    # 06:00 UTC -> Mid-zone (50¢) strictly avoided for live capital, logged as counterfactual sample
    hourly_client.buys.clear()
    hourly_client._positions.clear()
    snaps_06 = process_hourly_market_group(
        "BTC", markets, hourly_client,
        datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc),
        0, {"BTC": 71000.0}, False,
    )
    assert len(hourly_client.buys) == 0
    assert snaps_06[0]["trd"]["action"] in ("counterfactual_empirical_reject", "counterfactual_mid_zone")
    assert snaps_06[0]["trd"]["dry_run"] is True


def test_hourly_wti_restriction_and_dynamic_sizing(hourly_client):
    """Verify that hourly WTI trades are sized to 1 contract due to restriction."""
    markets = [
        {"ticker": "KXWTIH-26AUG1715-T83.79", "series_ticker": "KXWTIH", "floor_strike": 83.79, "yes_ask_dollars": "0.20", "yes_bid_dollars": "0.18", "no_ask_dollars": "0.82", "no_bid_dollars": "0.80"},
    ]
    snaps = process_hourly_market_group("WTI", markets, hourly_client, datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc), 0, {"WTI": 84.00}, False)

    assert len(hourly_client.buys) == 1
    assert hourly_client.buys[0]["ticker"] == "KXWTIH-26AUG1715-T83.79"
    assert hourly_client.buys[0]["count"] == 1  # Clamped to 1 contract because WTI is restricted


def test_hourly_skips_5min_markets(hourly_client):
    """Verify that 5-minute markets mistakenly passed to hourly group are safely skipped and not traded."""
    markets = [
        {
            "ticker": "GEMI-BTC05M2608181235-UP",
            "series_ticker": "BTC05M",
            "floor_strike": 70000.0,
            "yes_ask_dollars": "0.25",
            "yes_bid_dollars": "0.23",
            "no_ask_dollars": "0.77",
            "no_bid_dollars": "0.75",
        }
    ]
    snaps = process_hourly_market_group(
        "BTC", markets, hourly_client,
        datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc),
        0, {"BTC": 71000.0}, False
    )

    assert len(hourly_client.buys) == 0
    assert len(snaps) == 0


def test_hourly_59min_crash_detection_and_winner_hold(hourly_client):
    """Verify that hourly markets at :59 cut losses on dropping/crashing positions and hold securely winning positions."""
    # 1. Losing position at minute 59 (entry 85¢, current price 10¢ < 50¢ floor) -> 59min_crash_selloff_cut
    ticker_loser = "KXBTCD-LOSER59"
    hourly_client._positions[ticker_loser] = {"ticker": ticker_loser, "side": "yes", "entry_price_cents": 85}
    market_loser = {
        "ticker": ticker_loser, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.12", "yes_bid_dollars": "0.10", "no_ask_dollars": "0.90", "no_bid_dollars": "0.88",
        "close_time": "2026-08-15T06:00:00Z",
    }
    snaps_loss = process_hourly_market_group(
        "BTC", [market_loser], hourly_client,
        datetime(2026, 8, 15, 5, 59, 0, tzinfo=timezone.utc), 59, {"BTC": 69000.0}, False
    )
    assert len(hourly_client.liquidations) == 1
    assert hourly_client.liquidations[0]["ticker"] == ticker_loser
    assert snaps_loss[0]["trd"]["action"] == "59min_crash_selloff_cut"
    assert snaps_loss[0]["trd"]["exit_price"] == 10
    assert snaps_loss[0]["trd"]["loss_cents"] == 75

    # 2. Winning position at minute 59 (entry 40¢, current bid 85¢ >= 50¢ floor) -> held into settlement (no liquidation)
    hourly_client.liquidations.clear()
    hourly_client._positions.clear()
    ticker_winner = "KXBTCD-WINNER59"
    hourly_client._positions[ticker_winner] = {"ticker": ticker_winner, "side": "yes", "entry_price_cents": 40}
    market_winner = {
        "ticker": ticker_winner, "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.88", "yes_bid_dollars": "0.85", "no_ask_dollars": "0.15", "no_bid_dollars": "0.12",
        "close_time": "2026-08-15T06:00:00Z",
    }
    snaps_win = process_hourly_market_group(
        "BTC", [market_winner], hourly_client,
        datetime(2026, 8, 15, 5, 59, 0, tzinfo=timezone.utc), 59, {"BTC": 71000.0}, False
    )
    assert len(hourly_client.liquidations) == 0  # Held to settlement



