from __future__ import annotations

from datetime import datetime, timezone
import pytest

from main import extract_asset_symbol, extract_target_strike, process_hourly_market_group
from signals.base_client import BaseTradingClient


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
        self._positions[ticker] = {"ticker": ticker, "side": side, "count": 1, "entry_price_cents": effective}
        res = {"ticker": ticker, "side": side, "price": effective}
        self.buys.append(res)
        return res


@pytest.fixture
def hourly_client():
    return DummyHourlyTradingClient()


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


def test_hourly_multi_strike_dual_and_tiebreaker(hourly_client):
    # Strike A (Bullish YES) + Strike B (Bearish NO) -> Dual Buy
    markets = [
        {"ticker": "KXBTCD-A", "series_ticker": "KXBTCD", "floor_strike": 70000.0, "yes_ask_dollars": "0.34", "yes_bid_dollars": "0.32", "no_ask_dollars": "0.68", "no_bid_dollars": "0.66"},
        {"ticker": "KXBTCD-B", "series_ticker": "KXBTCD", "floor_strike": 74000.0, "yes_ask_dollars": "0.72", "yes_bid_dollars": "0.70", "no_ask_dollars": "0.30", "no_bid_dollars": "0.28"},
    ]
    snaps = process_hourly_market_group("BTC", markets, hourly_client, datetime(2026, 8, 15, 5, 0, 0, tzinfo=timezone.utc), 0, {"BTC": 71000.0}, False)
    
    assert len(hourly_client.buys) == 2
    assert snaps[0]["tf"] == "1h"


@pytest.mark.parametrize("minute, init_pos, init_price, expected_liq, expected_action", [
    (10, ("KXBTCD-COL", "yes"), 48, "KXBTCD-COL", "10min_loss_cut"),
    (20, ("KXBTCD-CUT", "yes"), 48, "KXBTCD-CUT", "20min_loss_cut"),
    (50, ("KXBTCD-END", "yes"), 40, "KXBTCD-END", "50min_loss_cut"),
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


def test_hourly_mid_zone_allowed_during_exploration(hourly_client):
    markets = [{
        "ticker": "KXBTCD-MID", "series_ticker": "KXBTCD", "floor_strike": 70000.0,
        "yes_ask_dollars": "0.50", "yes_bid_dollars": "0.48",
        "no_ask_dollars": "0.52", "no_bid_dollars": "0.50",
    }]
    # 05:00 UTC -> Mid-zone (50¢) allowed
    snaps_exp = process_hourly_market_group(
        "BTC", markets, hourly_client,
        datetime(2026, 8, 15, 5, 0, 0, tzinfo=timezone.utc),
        0, {"BTC": 71000.0}, False,
    )
    assert len(hourly_client.buys) == 1
    assert hourly_client.buys[0]["price"] in (49, 50)
    assert snaps_exp[0]["trd"]["corridor"] == "mid_zone"

    # 06:00 UTC -> Mid-zone (50¢) strictly avoided for live capital, logged as counterfactual sample
    hourly_client.buys.clear()
    hourly_client._positions.clear()
    snaps_06 = process_hourly_market_group(
        "BTC", markets, hourly_client,
        datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc),
        0, {"BTC": 71000.0}, False,
    )
    assert len(hourly_client.buys) == 0
    assert snaps_06[0]["trd"]["action"] == "counterfactual_mid_zone"
    assert snaps_06[0]["trd"]["dry_run"] is True

