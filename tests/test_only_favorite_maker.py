from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from database import get_dynamic_filter_for_market
from main import (
    _execute_buy_flow,
    evaluate_favorite_maker_opportunity,
    process_hourly_market_group,
    process_single_market,
)
from signals.base_client import BaseTradingClient


class MockOnlyFavClient(BaseTradingClient):
    exchange_name = "Kalshi"

    def __init__(self):
        super().__init__()
        self._positions = {}
        self.buys = []
        self.fav_buys = []

    def get_market_position(self, ticker: str):
        return self._positions.get(ticker)

    def cancel_unfilled_buy_orders(self, ticker: str):
        return True

    def cancel_and_verify_resting_orders(self, ticker: str):
        return True

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self._positions

    def execute_liquidation(self, ticker: str, side: str, count: int, price: int):
        self._positions.pop(ticker, None)
        return {"ticker": ticker, "side": side, "price": price}

    def clear_entry_price(self, ticker: str):
        pass

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
        res = {"ticker": ticker, "side": side, "count": count, "price_cents": price_cents}
        self.buys.append(res)
        return res

    def buy_and_set_take_profit(self, ticker: str, side: str, buy_price_cents: int, count: int = 1, **kw):
        res = {"ticker": ticker, "side": side, "buy_price_cents": buy_price_cents, "count": count}
        self.buys.append(res)
        return res

    def buy_favorite_maker_order(self, ticker: str, side: str, maker_bid_cents: int, count: int = 1, **kw):
        self._positions[ticker] = {"ticker": ticker, "side": side, "count": count, "entry_price_cents": maker_bid_cents}
        res = {"ticker": ticker, "side": side, "buy_price_cents": maker_bid_cents, "price": maker_bid_cents, "count": count, "strategy_tag": "endgame_favorite_maker"}
        self.fav_buys.append(res)
        return res


def test_dynamic_filter_reflects_only_favorite_maker_env(monkeypatch):
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "true")
    filt = get_dynamic_filter_for_market("BTC", is_5min_mark=False, timeframe="15m")
    assert filt["only_favorite_maker"] is True
    assert filt["enable_favorite_maker"] is True
    assert filt["favorite_maker_min_buy"] == 80
    assert filt["favorite_maker_max_buy"] == 93


def test_15m_market_skips_general_corridors_and_logs_counterfactual(monkeypatch):
    """When ONLY_FAVORITE_MAKER=true, 15m general discount buy (28¢) is logged counterfactually without live execution."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "true")
    client = MockOnlyFavClient()
    market = {
        "ticker": "KXBTC15M-26AUG170200-00",
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.28",
        "yes_bid_dollars": "0.24",
        "no_ask_dollars": "0.76",
        "no_bid_dollars": "0.72",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 1, 45, 0, tzinfo=timezone.utc)  # minute 0

    snap = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"BTC": 60050.0},
        dry_run=False,
    )

    assert len(client.buys) == 0
    assert len(client.fav_buys) == 0
    assert snap is not None
    assert "trd" in snap
    assert snap["trd"]["action"] == "counterfactual_empirical_reject"
    assert snap["trd"]["corridor"] == "discount"
    assert snap["trd"]["buy"] == 28


def test_15m_market_executes_endgame_favorite_maker_live(monkeypatch):
    """When ONLY_FAVORITE_MAKER=true, confirmed endgame favorite (84¢) executes resting maker limit bid."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "true")
    client = MockOnlyFavClient()
    market = {
        "ticker": "KXSOL15M-26AUG170200-00",
        "series_ticker": "KXSOL15M",
        "yes_ask_dollars": "0.84",
        "yes_bid_dollars": "0.80",
        "no_ask_dollars": "0.20",
        "no_bid_dollars": "0.16",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 140.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 1, 57, 0, tzinfo=timezone.utc)  # 3 minutes remaining

    snap = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"SOL": 142.5},
        dry_run=False,
    )

    assert len(client.fav_buys) == 1
    assert client.fav_buys[0]["strategy_tag"] == "endgame_favorite_maker"
    assert client.fav_buys[0]["buy_price_cents"] == 82  # maker bid (1¢ above 80 bid, 2¢ below 84 ask)
    assert snap["fav_maker"] is True


def test_1h_market_group_executes_favorite_maker_only(monkeypatch):
    """When ONLY_FAVORITE_MAKER=true, hourly market executes favorite maker on strike A and counterfactually tracks strike B."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "true")
    client = MockOnlyFavClient()
    markets = [
        {
            "ticker": "KXETHD-26AUG17-T2600",
            "series_ticker": "KXETHD",
            "yes_ask_dollars": "0.86",
            "yes_bid_dollars": "0.82",
            "no_ask_dollars": "0.18",
            "no_bid_dollars": "0.14",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 2600.0,
            "exchange": "kalshi",
        },
        {
            "ticker": "KXETHD-26AUG17-T2700",
            "series_ticker": "KXETHD",
            "yes_ask_dollars": "0.32",
            "yes_bid_dollars": "0.30",
            "no_ask_dollars": "0.72",
            "no_bid_dollars": "0.70",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 2700.0,
            "exchange": "kalshi",
        },
    ]
    now_utc = datetime(2026, 8, 17, 20, 50, 0, tzinfo=timezone.utc)  # 10m remaining

    snaps = process_hourly_market_group(
        symbol="ETH",
        markets=markets,
        client=client,
        now_utc=now_utc,
        minute_past_hour=10,
        spot_cache={"ETH": 2650.0},
        dry_run=False,
    )

    assert len(client.fav_buys) == 1
    assert len(client.buys) == 0
    assert client.fav_buys[0]["ticker"] == "KXETHD-26AUG17-T2600"
    assert client.fav_buys[0]["buy_price_cents"] == 84  # maker bid inside spread


def test_is_asset_loss_gated_threshold_math():
    """Verify is_asset_loss_gated strictly gates at -$1.00 and allows trades at > -$1.00."""
    from database import is_asset_loss_gated

    with patch("database.get_asset_realized_pnl_dollars", return_value=-0.50):
        gated, pnl = is_asset_loss_gated("SOL", timeframe="15m", max_loss_dollars=1.0)
        assert gated is False
        assert pnl == -0.50

    with patch("database.get_asset_realized_pnl_dollars", return_value=-0.99):
        gated, pnl = is_asset_loss_gated("SOL", timeframe="15m", max_loss_dollars=1.0)
        assert gated is False

    with patch("database.get_asset_realized_pnl_dollars", return_value=-1.00):
        gated, pnl = is_asset_loss_gated("SOL", timeframe="15m", max_loss_dollars=1.0)
        assert gated is True
        assert pnl == -1.00

    with patch("database.get_asset_realized_pnl_dollars", return_value=-3.50):
        gated, pnl = is_asset_loss_gated("SOL", timeframe="15m", max_loss_dollars=1.0)
        assert gated is True
        assert pnl == -3.50


def test_asset_loss_gated_blocks_live_trading_in_15m_and_1h():
    """Verify that when an asset exceeds -$1.00 daily loss, 15m and 1h pipelines halt live execution and run in dry-run mode."""
    client = MockOnlyFavClient()
    now_utc = datetime(2026, 8, 17, 1, 57, 0, tzinfo=timezone.utc)

    market_15m = {
        "ticker": "KXSOL15M-26AUG170200-00",
        "series_ticker": "KXSOL15M",
        "yes_ask_dollars": "0.84",
        "yes_bid_dollars": "0.80",
        "no_ask_dollars": "0.20",
        "no_bid_dollars": "0.16",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 140.0,
        "exchange": "kalshi",
    }

    # Simulate $1.50 daily loss on SOL 15m
    with patch("main.is_asset_loss_gated", return_value=(True, -1.50)):
        snap_15m = process_single_market(
            market=market_15m,
            is_trading_interval=True,
            client=client,
            now_utc=now_utc,
            minute_past_hour=0,
            spot_cache={"SOL": 142.5},
            dry_run=False,  # Live trading requested, but gated
        )
        assert len(client.fav_buys) == 0  # No live order placed
        assert len(client.buys) == 0
        assert snap_15m is not None
        assert snap_15m["trd"]["dry_run"] is True  # Forced to dry-run mode

    # Simulate $2.00 daily loss on ETH 1h
    market_1h = [
        {
            "ticker": "KXETHD-26AUG17-T2600",
            "series_ticker": "KXETHD",
            "yes_ask_dollars": "0.86",
            "yes_bid_dollars": "0.82",
            "no_ask_dollars": "0.18",
            "no_bid_dollars": "0.14",
            "close_time": "2026-08-17T21:00:00Z",
            "floor_strike": 2600.0,
            "exchange": "kalshi",
        }
    ]
    with patch("main.is_asset_loss_gated", return_value=(True, -2.00)):
        snaps_1h = process_hourly_market_group(
            symbol="ETH",
            markets=market_1h,
            client=client,
            now_utc=datetime(2026, 8, 17, 20, 50, 0, tzinfo=timezone.utc),
            minute_past_hour=10,
            spot_cache={"ETH": 2650.0},
            dry_run=False,  # Live trading requested, but gated
        )
        assert len(client.fav_buys) == 0  # No live order placed
        assert len(client.buys) == 0
        assert snaps_1h[0]["trd"]["dry_run"] is True  # Forced to dry-run mode


def test_check_spot_lead_loss_gate_logic():
    """Verify check_spot_lead_loss_gate enforces >= 0.30% spot lead when loss > $0.50."""
    from database import check_spot_lead_loss_gate

    # Case 1: Normal loss (-$0.20 <= $0.50 threshold) -> always allowed
    allowed, lead, reason = check_spot_lead_loss_gate("yes", 100.0, 100.0, asset_realized_pnl=-0.20)
    assert allowed is True

    # Case 2: Elevated loss (-$0.60 > $0.50 threshold) with YES trade
    # Spot = 100.20, Target = 100.00 -> lead = +0.20% (< 0.30%) -> rejected
    allowed, lead, reason = check_spot_lead_loss_gate("yes", 100.20, 100.00, asset_realized_pnl=-0.60)
    assert allowed is False
    assert round(lead, 2) == 0.20

    # Spot = 100.35, Target = 100.00 -> lead = +0.35% (>= 0.30%) -> permitted
    allowed, lead, reason = check_spot_lead_loss_gate("yes", 100.35, 100.00, asset_realized_pnl=-0.60)
    assert allowed is True
    assert round(lead, 2) == 0.35

    # Case 3: Elevated loss (-$0.75) with NO trade
    # Spot = 99.85, Target = 100.00 -> lead = +0.15% (< 0.30%) -> rejected
    allowed, lead, reason = check_spot_lead_loss_gate("no", 99.85, 100.00, asset_realized_pnl=-0.75)
    assert allowed is False
    assert round(lead, 2) == 0.15

    # Spot = 99.60, Target = 100.00 -> lead = +0.40% (>= 0.30%) -> permitted
    allowed, lead, reason = check_spot_lead_loss_gate("no", 99.60, 100.00, asset_realized_pnl=-0.75)
    assert allowed is True
    assert round(lead, 2) == 0.40


def test_hard_position_gate_prevents_dual_sided_orders():
    """Verify that _execute_favorite_maker_flow and _execute_buy_flow reject orders when has_existing_position_or_order is True."""
    from main import _execute_favorite_maker_flow, _execute_buy_flow

    client = MockOnlyFavClient()
    client.has_existing_position_or_order = lambda ticker: True

    # 1. Favorite maker flow blocked
    success, details = _execute_favorite_maker_flow(
        ticker="KXGOLDH-TEST",
        side="yes",
        maker_bid=85,
        target_ask=88,
        spot_price=4650.0,
        target_price=4640.0,
        market_result="yes",
        client=client,
        dry_run=False,
    )
    assert success is False
    assert details is None
    assert len(client.fav_buys) == 0

    # 2. General buy flow blocked
    success_buy, details_buy = _execute_buy_flow(
        ticker="KXGOLDH-TEST",
        side="no",
        price_cents=30,
        maker_bid=29,
        min_buy=1,
        max_buy=35,
        take_profit_cents=65,
        spot_price=4630.0,
        target_price=4640.0,
        market_result="no",
        client=client,
        dry_run=False,
    )
    assert success_buy is False
    assert details_buy is None
    assert len(client.buys) == 0

