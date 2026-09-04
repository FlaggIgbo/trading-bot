from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from database import calculate_micro_win_take_profit, get_dynamic_filter_for_market
from main import process_single_market, process_hourly_market_group
from signals.base_client import BaseTradingClient


class MockMicroWinClient(BaseTradingClient):
    exchange_name = "Kalshi"

    def __init__(self):
        super().__init__()
        self._positions = {}
        self._entry_prices = {}
        self._target_tps = {}
        self.buys = []
        self.liquidations = []

    def get_market_position(self, ticker: str):
        return self._positions.get(ticker)

    def set_position(self, ticker: str, side: str, count: int, entry_price_cents: int, target_tp_cents: int | None = None):
        self._positions[ticker] = {"side": side, "count": count, "entry_price_cents": entry_price_cents}
        self._entry_prices[ticker] = entry_price_cents
        if target_tp_cents:
            self._target_tps[ticker] = target_tp_cents

    def get_entry_price(self, ticker: str) -> int | None:
        return self._entry_prices.get(ticker)

    def get_target_tp(self, ticker: str) -> int | None:
        return self._target_tps.get(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str):
        return True

    def cancel_unfilled_buy_orders(self, ticker: str):
        return True

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self._positions

    def has_resting_take_profit_order(self, ticker: str) -> bool:
        return False

    def place_take_profit_order(self, ticker: str, side: str, count: int, take_profit_cents: int):
        self._target_tps[ticker] = take_profit_cents
        return {"order_id": "tp_123", "take_profit_cents": take_profit_cents}

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
        res = {"ticker": ticker, "side": side, "count": count, "price_cents": price_cents}
        self.buys.append(res)
        return res

    def execute_liquidation(self, ticker: str, side: str, count: int, price: int):
        self._positions.pop(ticker, None)
        res = {"ticker": ticker, "side": side, "count": count, "price": price}
        self.liquidations.append(res)
        return res

    def clear_entry_price(self, ticker: str):
        self._entry_prices.pop(ticker, None)
        self._target_tps.pop(ticker, None)

    def clear_strategy_tag(self, ticker: str):
        pass

    def clear_profit_floor(self, ticker: str):
        pass

    def buy_and_set_take_profit(self, ticker: str, side: str, buy_price_cents: int, count: int = 1, take_profit_cents: int = 62, **kw):
        res = {"ticker": ticker, "side": side, "buy_price_cents": buy_price_cents, "count": count, "take_profit_cents": take_profit_cents}
        self.buys.append(res)
        self.set_position(ticker, side, count, buy_price_cents, take_profit_cents)
        return res

    def buy_favorite_maker_order(self, ticker: str, side: str, maker_bid_cents: int, count: int = 1, take_profit_cents: int | None = None, **kw):
        res = {"ticker": ticker, "side": side, "buy_price_cents": maker_bid_cents, "count": count, "take_profit_cents": take_profit_cents}
        self.buys.append(res)
        self.set_position(ticker, side, count, maker_bid_cents, take_profit_cents)
        return res


def test_calculate_micro_win_take_profit_tiers():
    """Verify micro-win TP calculations across discount, value, and favorite tiers."""
    # Deep Discount Tier (<= 25¢): Target ~30-40% gain (+5¢ to +8¢)
    assert calculate_micro_win_take_profit(10) == 14  # +4¢ / +40%
    assert calculate_micro_win_take_profit(15) == 20  # +5¢ / +33.3%
    assert calculate_micro_win_take_profit(20) == 27  # +7¢ / +35%
    assert calculate_micro_win_take_profit(25) == 34  # +9¢ / +36%

    # Value Tier (26¢ - 45¢): Target ~20-30% gain (+6¢ to +9¢)
    assert calculate_micro_win_take_profit(30) == 37  # +7¢ / +23.3%
    assert calculate_micro_win_take_profit(40) == 49  # +9¢ / +22.5%

    # Favorite Maker Tier (> 70¢): Target +5¢ to +7¢ capped at 95¢ (sell before 100¢ binary cliff!)
    assert calculate_micro_win_take_profit(86) == 92  # +6¢ gain
    assert calculate_micro_win_take_profit(88) == 94  # +6¢ gain
    assert calculate_micro_win_take_profit(90) == 95  # capped at 95¢

    # Invariants
    for p in range(1, 99):
        tp = calculate_micro_win_take_profit(p)
        assert tp > p
        assert tp <= 99


def test_continuous_micro_profit_harvester_banks_profit_15m():
    """Verify 15M market position in profit (+6¢ on 20¢ entry) is sold early to bank cash."""
    client = MockMicroWinClient()
    ticker = "KXBTC15M-26AUG170200-00"
    client.set_position(ticker, side="yes", count=1, entry_price_cents=20, target_tp_cents=27)

    market = {
        "ticker": ticker,
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.28",
        "yes_bid_dollars": "0.26",  # Market bid is 26¢ -> +6¢ profit!
        "no_ask_dollars": "0.74",
        "no_bid_dollars": "0.72",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 1, 48, 0, tzinfo=timezone.utc)  # minute 3 (not a scheduled stop loss interval)

    snap = process_single_market(
        market=market,
        is_trading_interval=False,
        client=client,
        now_utc=now_utc,
        minute_past_hour=3,
        spot_cache={"BTC": 60050.0},
        dry_run=False,
    )

    assert len(client.liquidations) == 1
    assert client.liquidations[0]["ticker"] == ticker
    assert client.liquidations[0]["price"] == 26
    assert client.get_market_position(ticker) is None
    assert snap is not None
    assert snap.get("trd", {}).get("action") == "winner_profit_banked"
    assert snap.get("trd", {}).get("profit_locked_cents") == 6


def test_continuous_micro_profit_harvester_banks_profit_hourly():
    """Verify Hourly market position in profit (+8¢ on 30¢ entry) is sold early to bank cash."""
    client = MockMicroWinClient()
    ticker = "KXGOLDH-26AUG1800-T4400"
    client.set_position(ticker, side="yes", count=1, entry_price_cents=30, target_tp_cents=38)

    markets = [{
        "ticker": ticker,
        "series_ticker": "KXGOLDH",
        "yes_ask_dollars": "0.40",
        "yes_bid_dollars": "0.38",  # Market bid is 38¢ -> +8¢ profit!
        "no_ask_dollars": "0.62",
        "no_bid_dollars": "0.60",
        "close_time": "2026-08-18T03:00:00Z",
        "floor_strike": 4400.0,
        "exchange": "kalshi",
    }]
    now_utc = datetime(2026, 8, 18, 2, 18, 0, tzinfo=timezone.utc)  # minute 18

    snaps = process_hourly_market_group(
        symbol="GOLD",
        markets=markets,
        client=client,
        now_utc=now_utc,
        minute_past_hour=18,
        spot_cache={"GOLD": 4410.0},
        dry_run=False,
    )

    assert len(client.liquidations) == 1
    assert client.liquidations[0]["ticker"] == ticker
    assert client.liquidations[0]["price"] == 38
    assert client.get_market_position(ticker) is None
    assert len(snaps) == 1
    assert snaps[0].get("trd", {}).get("action") == "winner_profit_banked"
    assert snaps[0].get("trd", {}).get("profit_locked_cents") == 8


def test_favorite_maker_takes_early_micro_profit():
    """Verify Favorite Maker position (entered at 86¢) exits at 92¢ instead of holding to 100¢."""
    client = MockMicroWinClient()
    ticker = "KXSOL15M-FAV-TEST"
    client.set_position(ticker, side="yes", count=1, entry_price_cents=86, target_tp_cents=92)

    market = {
        "ticker": ticker,
        "series_ticker": "KXSOL15M",
        "yes_ask_dollars": "0.94",
        "yes_bid_dollars": "0.92",  # Market bid reached 92¢ -> +6¢ profit!
        "no_ask_dollars": "0.08",
        "no_bid_dollars": "0.06",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 140.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 1, 52, 0, tzinfo=timezone.utc)  # minute 7

    snap = process_single_market(
        market=market,
        is_trading_interval=False,
        client=client,
        now_utc=now_utc,
        minute_past_hour=7,
        spot_cache={"SOL": 142.0},
        dry_run=False,
    )

    assert len(client.liquidations) == 1
    assert client.liquidations[0]["ticker"] == ticker
    assert client.liquidations[0]["price"] == 92
    assert client.get_market_position(ticker) is None
    assert snap.get("trd", {}).get("action") == "winner_profit_banked"
    assert snap.get("trd", {}).get("profit_locked_cents") == 6


def test_tri_peak_prioritizes_peak1_over_peak3_when_both_present(monkeypatch):
    """Verify that Peak 1 (Asymmetric Deep Discount, 25¢) is heavily prioritized over Peak 3 (Favorite Maker, 88¢)."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    client = MockMicroWinClient()
    ticker = "KXBTC15M-TRI-PEAK-1"

    market = {
        "ticker": ticker,
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.25",
        "yes_bid_dollars": "0.23",
        "no_ask_dollars": "0.88",
        "no_bid_dollars": "0.86",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    # Minute 0: Entry interval with 15 minutes remaining
    now_utc = datetime(2026, 8, 17, 1, 45, 0, tzinfo=timezone.utc)

    snap = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"BTC": 60100.0},
        dry_run=False,
    )

    # Must execute Peak 1 (YES @ 25¢) and NOT Peak 3 (NO favorite)
    assert len(client.buys) == 1
    assert client.buys[0]["side"] == "yes"
    assert client.buys[0]["buy_price_cents"] == 25
    assert snap["fav_maker"] is False


def test_peak2_early_value_requires_upward_price_signal(monkeypatch):
    """Verify Peak 2 (Early Value, 45¢) is rejected without an upward signal, but executed when signal is present."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    market = {
        "ticker": "KXBTC15M-PEAK2-SIGNAL",
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.45",
        "yes_bid_dollars": "0.43",
        "no_ask_dollars": "0.57",
        "no_bid_dollars": "0.55",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    now_utc = datetime(2026, 8, 17, 1, 45, 0, tzinfo=timezone.utc)

    # Case A: Spot is flat at strike (lead = 0.0%), no upward price momentum signal
    client_a = MockMicroWinClient()
    snap_a = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client_a,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"BTC": 60000.0},  # flat at strike, no upward price signal
        dry_run=False,
    )
    # Blocked because lead is not positive to confirm upward price momentum
    assert len(client_a.buys) == 0

    # Case B: Spot has clear upward lead (lead = 0.15% > 0.05%) confirming upward momentum
    client_b = MockMicroWinClient()
    snap_b = process_single_market(
        market=market,
        is_trading_interval=True,
        client=client_b,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"BTC": 60090.0},  # +0.15% lead
        dry_run=False,
    )
    assert len(client_b.buys) == 1
    assert client_b.buys[0]["side"] == "yes"
    assert client_b.buys[0]["buy_price_cents"] == 45


def test_peak3_endgame_requires_substantial_belief(monkeypatch):
    """Verify Peak 3 (Endgame Favorite Maker) is rejected when spot clearance is < 0.40% on 15M, but allowed when >= 0.40%."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    market = {
        "ticker": "KXBTC15M-PEAK3-BELIEF",
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.88",
        "yes_bid_dollars": "0.84",
        "no_ask_dollars": "0.16",
        "no_bid_dollars": "0.12",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    # 3 minutes remaining (endgame window)
    now_utc = datetime(2026, 8, 17, 1, 57, 0, tzinfo=timezone.utc)

    # Case A: Spot lead is 0.05% (< 0.40% required buffer for substantial belief)
    client_a = MockMicroWinClient()
    snap_a = process_single_market(
        market=market,
        is_trading_interval=False,
        client=client_a,
        now_utc=now_utc,
        minute_past_hour=12,
        spot_cache={"BTC": 60030.0},  # +0.05% lead
        dry_run=False,
    )
    assert len(client_a.buys) == 0
    assert snap_a["fav_maker"] is False

    # Case B: Spot lead is 0.50% (>= 0.40% required buffer for substantial belief)
    client_b = MockMicroWinClient()
    snap_b = process_single_market(
        market=market,
        is_trading_interval=False,
        client=client_b,
        now_utc=now_utc,
        minute_past_hour=12,
        spot_cache={"BTC": 60300.0},  # +0.50% lead
        dry_run=False,
    )
    assert len(client_b.buys) == 1
    assert client_b.buys[0]["side"] == "yes"
    assert client_b.buys[0]["buy_price_cents"] == 86  # maker bid
    assert snap_b["fav_maker"] is True


def test_hourly_tri_peak_prioritizes_peak1_over_peak3(monkeypatch):
    """Verify Hourly market group heavily prioritizes Peak 1 strike over Peak 3 Favorite strike."""
    monkeypatch.setenv("ONLY_FAVORITE_MAKER", "false")
    client = MockMicroWinClient()
    markets = [
        # Strike A: 25¢ YES Peak 1 Deep Discount
        {
            "ticker": "KXBTC1H-26AUG1800-T59900",
            "series_ticker": "KXBTC1H",
            "yes_ask_dollars": "0.25",
            "yes_bid_dollars": "0.23",
            "no_ask_dollars": "0.77",
            "no_bid_dollars": "0.75",
            "close_time": "2026-08-18T00:00:00Z",
            "floor_strike": 59900.0,
            "exchange": "kalshi",
        },
        # Strike B: 88¢ YES Peak 3 Favorite Maker
        {
            "ticker": "KXBTC1H-26AUG1800-T59500",
            "series_ticker": "KXBTC1H",
            "yes_ask_dollars": "0.88",
            "yes_bid_dollars": "0.84",
            "no_ask_dollars": "0.16",
            "no_bid_dollars": "0.12",
            "close_time": "2026-08-18T00:00:00Z",
            "floor_strike": 59500.0,
            "exchange": "kalshi",
        },
    ]
    # Minute 0: Hourly trading interval
    now_utc = datetime(2026, 8, 17, 23, 0, 0, tzinfo=timezone.utc)

    snaps = process_hourly_market_group(
        symbol="BTC",
        markets=markets,
        client=client,
        now_utc=now_utc,
        minute_past_hour=0,
        spot_cache={"BTC": 60000.0},
        dry_run=False,
    )

    # Must execute Peak 1 (Strike A @ 25¢) and NOT Favorite Maker
    assert len(client.buys) == 1
    assert client.buys[0]["ticker"] == "KXBTC1H-26AUG1800-T59900"
    assert client.buys[0]["buy_price_cents"] == 25


def test_continuous_micro_profit_harvester_banks_profit_at_endgame_marks():
    """Verify that micro-win harvester banks profits at minute 12.5 and minute 14 instead of holding to expiration."""
    client = MockMicroWinClient()
    ticker = "KXBTC15M-ENDGAME-WIN"
    client.set_position(ticker, side="yes", count=1, entry_price_cents=20, target_tp_cents=27)

    market = {
        "ticker": ticker,
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.30",
        "yes_bid_dollars": "0.28",  # Market bid is 28¢ -> +8¢ profit!
        "no_ask_dollars": "0.72",
        "no_bid_dollars": "0.70",
        "close_time": "2026-08-17T02:00:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    # Minute 12.5 (previously blocked by is_12_5min_mark lock)
    now_utc_12 = datetime(2026, 8, 17, 1, 57, 30, tzinfo=timezone.utc)
    snap_12 = process_single_market(
        market=market,
        is_trading_interval=False,
        client=client,
        now_utc=now_utc_12,
        minute_past_hour=12,
        spot_cache={"BTC": 60080.0},
        dry_run=False,
    )

    assert len(client.liquidations) == 1
    assert client.liquidations[0]["ticker"] == ticker
    assert client.liquidations[0]["price"] == 28
    assert client.get_market_position(ticker) is None
    assert snap_12.get("trd", {}).get("action") == "winner_profit_banked"
    assert snap_12.get("trd", {}).get("profit_locked_cents") == 8


def test_relaxed_empirical_entry_time_window_m0_to_m10():
    """Verify that empirical entry allows discount & value entries across m=0..10, while banning the Valley of Death."""
    from database import is_valid_empirical_entry

    # Discount corridor (1¢-35¢): allowed across m=0..10
    for m in range(0, 11):
        valid, reason = is_valid_empirical_entry(25, m, "15m")
        assert valid is True, f"Failed at minute {m} for 25¢"
        assert "discount" in reason

    # Value corridor (36¢-50¢): allowed across m=0..10
    for m in range(0, 11):
        valid, reason = is_valid_empirical_entry(45, m, "15m")
        assert valid is True, f"Failed at minute {m} for 45¢"
        assert "value" in reason

    # Valley of Death (51¢-80¢): STRICTLY BANNED across ALL minutes
    for m in range(0, 15):
        for p in [51, 55, 60, 65, 70, 75, 80]:
            valid, reason = is_valid_empirical_entry(p, m, "15m")
            assert valid is False, f"Failed: {p}¢ was allowed at minute {m}"
            assert "valley_of_death_banned" in reason

    # Endgame taker buys (minutes 11..14) are rejected
    for m in [11, 12, 13, 14]:
        valid, reason = is_valid_empirical_entry(25, m, "15m")
        assert valid is False
        assert "disallowed_zone" in reason


def test_calculate_micro_win_take_profit_dynamic_volatility_and_decay():
    """Verify dynamic TP scaling with high-volatility assets and late-window decay."""
    # High-volatility asset (SOL) aims for higher target gain
    tp_sol = calculate_micro_win_take_profit(15, timeframe="15m", asset_symbol="SOL", minute_into_market=2)
    tp_btc = calculate_micro_win_take_profit(15, timeframe="15m", asset_symbol="BTC", minute_into_market=2)
    assert tp_sol >= tp_btc

    # Late-window entry (minute 8) tightens target to bank before expiration theta decay
    tp_early = calculate_micro_win_take_profit(86, timeframe="15m", asset_symbol="DEFAULT", minute_into_market=2)
    tp_late = calculate_micro_win_take_profit(86, timeframe="15m", asset_symbol="DEFAULT", minute_into_market=8)
    assert tp_late <= tp_early
    assert tp_late == 91  # +5¢ fast exit instead of +6¢
    assert tp_early == 92


def test_micro_win_harvester_banks_both_wings_of_dual_hedge():
    """Verify that micro-win harvester independently banks profits on both Strike A YES and Strike B NO when each hits profit target."""
    client = MockMicroWinClient()
    ticker_a = "KXBTC15M-A"
    ticker_b = "KXBTC15M-B"

    # Set up both positions from dual strangle entry:
    # Leg A: Bought YES @ 20¢, target TP 27¢ (+7¢)
    # Leg B: Bought NO @ 22¢, target TP 29¢ (+7¢)
    client.set_position(ticker_a, side="yes", count=1, entry_price_cents=20, target_tp_cents=27)
    client.set_position(ticker_b, side="no", count=1, entry_price_cents=22, target_tp_cents=29)

    market_a = {
        "ticker": ticker_a,
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.29",
        "yes_bid_dollars": "0.27",  # Leg A hit +7¢ profit!
        "no_ask_dollars": "0.73",
        "no_bid_dollars": "0.71",
        "close_time": "2026-08-17T01:45:00Z",
        "floor_strike": 60000.0,
        "exchange": "kalshi",
    }
    market_b = {
        "ticker": ticker_b,
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.73",
        "yes_bid_dollars": "0.71",
        "no_ask_dollars": "0.31",
        "no_bid_dollars": "0.29",  # Leg B hit +7¢ profit!
        "close_time": "2026-08-17T01:45:00Z",
        "floor_strike": 64000.0,
        "exchange": "kalshi",
    }

    now_utc = datetime(2026, 8, 17, 1, 35, 0, tzinfo=timezone.utc)
    snap_a = process_single_market(market=market_a, is_trading_interval=False, client=client, now_utc=now_utc, minute_past_hour=35, spot_cache={"BTC": 62000.0}, dry_run=False)
    snap_b = process_single_market(market=market_b, is_trading_interval=False, client=client, now_utc=now_utc, minute_past_hour=35, spot_cache={"BTC": 62000.0}, dry_run=False)

    assert len(client.liquidations) == 2
    assert client.liquidations[0]["ticker"] == ticker_a
    assert client.liquidations[0]["price"] == 27
    assert client.liquidations[1]["ticker"] == ticker_b
    assert client.liquidations[1]["price"] == 29
    assert snap_a["trd"]["action"] == "winner_profit_banked"
    assert snap_b["trd"]["action"] == "winner_profit_banked"
    assert snap_a["trd"]["profit_locked_cents"] == 7
    assert snap_b["trd"]["profit_locked_cents"] == 7



