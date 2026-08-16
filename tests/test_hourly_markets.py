"""
Unit tests for Kalshi Hourly Multi-Strike Crypto Prediction Markets.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from main import (
    extract_asset_symbol,
    extract_target_strike,
    process_hourly_market_group,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import KalshiClient


class DummyHourlyTradingClient(BaseTradingClient):
    exchange_name = "TestExchange"

    def __init__(self):
        super().__init__()
        self._positions_cache: dict[str, dict] = {}
        self._orders_cache: dict[str, list[dict]] = {}
        self.executed_buys: list[dict] = []
        self.executed_liquidations: list[dict] = []

    def get_market_position(self, ticker: str) -> dict | None:
        return self._positions_cache.get(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        if ticker in self._orders_cache:
            self._orders_cache.pop(ticker, None)
            return True
        return False

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self._positions_cache or ticker in self._orders_cache

    def cancel_unfilled_buy_orders(self, ticker: str) -> int:
        return 0

    def has_resting_take_profit_order(self, ticker: str) -> bool:
        return False

    def place_take_profit_order(self, ticker: str, side: str, count: int = 1, take_profit_cents: int = 82) -> dict:
        return {"status": "tp_placed", "ticker": ticker, "tp": take_profit_cents}

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        self._positions_cache.pop(ticker, None)
        self.clear_profit_floor(ticker)
        self.clear_entry_price(ticker)
        res = {"status": "liquidated", "ticker": ticker, "side": side, "price": current_price}
        self.executed_liquidations.append(res)
        return res

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        self._positions_cache[ticker] = {
            "ticker": ticker,
            "side": side.lower(),
            "count": count,
            "entry_price_cents": price_cents,
        }
        self.set_entry_price(ticker, price_cents)
        res = {"status": "bought", "ticker": ticker, "side": side, "price": price_cents}
        self.executed_buys.append(res)
        return res

    def buy_and_set_take_profit(
        self,
        ticker: str,
        side: str,
        buy_price_cents: int,
        take_profit_cents: int = 82,
        count: int = 1,
        maker_bid_cents: int | None = None,
    ) -> dict:
        effective_buy = min(buy_price_cents, (maker_bid_cents + 1) if maker_bid_cents is not None else buy_price_cents)
        self.execute_buy(ticker, side, count, effective_buy)
        return {
            "buy_price_cents": effective_buy,
            "take_profit_cents": take_profit_cents,
            "side": side,
        }


class TestHourlyCryptoMarkets(unittest.TestCase):
    def test_extract_asset_symbol_hourly_series(self):
        """Tests that all Kalshi hourly series tickers map to correct asset symbols."""
        self.assertEqual(extract_asset_symbol("KXBTCD", "KXBTCD-26AUG1521-T69399.99"), "BTC")
        self.assertEqual(extract_asset_symbol("KXETHD", "KXETHD-26AUG1617-T2609.99"), "ETH")
        self.assertEqual(extract_asset_symbol("KXSOLD", "KXSOLD-26AUG1617-T99.9999"), "SOL")
        self.assertEqual(extract_asset_symbol("KXXRPD", "KXXRPD-26AUG1617-T1.3799"), "XRP")
        self.assertEqual(extract_asset_symbol("KXDOGED", "KXDOGED-26AUG1617-T0.1549999"), "DOGE")
        self.assertEqual(extract_asset_symbol("KXBNBD", "KXBNBD-26AUG1617-T704.99"), "BNB")
        self.assertEqual(extract_asset_symbol("KXHYPED", "KXHYPED-26AUG1617-T74.9999"), "HYPE")
        self.assertEqual(extract_asset_symbol("KXTOND", "KXTOND-26AUG1617-T3.499"), "TON")
        self.assertEqual(extract_asset_symbol("KXNEARD", "KXNEARD-26AUG1617-T3.999"), "NEAR")
        self.assertEqual(extract_asset_symbol("KXZECD", "KXZECD-26AUG1617-T34.99"), "ZEC")
        self.assertEqual(extract_asset_symbol("KXNEARH", "KXNEARH-TEST"), "NEAR")
        self.assertEqual(extract_asset_symbol("KXZECH", "KXZECH-TEST"), "ZEC")

    def test_extract_target_strike(self):
        """Tests strike target extraction across various market schemas."""
        # 1. Direct floor_strike field
        m1 = {"floor_strike": 72249.99, "ticker": "KXBTCD-TEST"}
        self.assertAlmostEqual(extract_target_strike(m1), 72249.99)

        # 2. Subtitle with currency format
        m2 = {"floor_strike": None, "subtitle": "$72,250 or above", "ticker": "KXBTCD-TEST"}
        self.assertAlmostEqual(extract_target_strike(m2), 72250.0)

        # 3. Ticker target suffix (-T...)
        m3 = {"floor_strike": None, "subtitle": None, "ticker": "KXDOGED-26AUG1617-T0.1549999"}
        self.assertAlmostEqual(extract_target_strike(m3), 0.1549999)

    def test_kalshi_client_get_hourly_crypto_markets(self):
        """Tests that KalshiClient queries hourly series and filters for current hour."""
        client = KalshiClient(api_key_id="test_key", private_key=MagicMock())

        now = datetime(2026, 8, 15, 20, 10, 0, tzinfo=timezone.utc)
        mock_response = {
            "markets": [
                {
                    "ticker": "KXBTCD-CURRENT",
                    "series_ticker": "KXBTCD",
                    "close_time": "2026-08-15T21:00:00Z",
                },
                {
                    "ticker": "KXBTCD-FAR-FUTURE",
                    "series_ticker": "KXBTCD",
                    "close_time": "2026-08-20T21:00:00Z",
                },
            ]
        }

        with patch.object(client, "get_markets", return_value=mock_response):
            res = client.get_hourly_crypto_markets(current_hour_only=True, now_utc=now)
            self.assertTrue(len(res) > 0)
            tickers = [m["ticker"] for m in res]
            self.assertIn("KXBTCD-CURRENT", tickers)
            self.assertNotIn("KXBTCD-FAR-FUTURE", tickers)

    def test_hourly_multi_strike_dual_buy_on_different_contracts(self):
        """
        Tests multi-strike scanning at Minute 00:
        Strike A ($70k) has cheapest BUY_YES at 34¢ (spot is $71k -> bullish momentum).
        Strike B ($72k) has cheapest BUY_NO at 36¢ (spot is $71k -> bearish momentum vs 72k strike).
        Since they are on DIFFERENT contracts, bot should execute DUAL BUY (both Strike A YES and Strike B NO).
        """
        client = DummyHourlyTradingClient()
        now_utc = datetime(2026, 8, 15, 20, 0, 30, tzinfo=timezone.utc)
        spot_cache = {"BTC": 71000.0}

        markets = [
            # Strike A ($70,000): spot is 71k (> 70k) -> YES aligned. Ask is 34¢ (in corridor 32¢-52¢).
            {
                "ticker": "KXBTCD-STRIKE-A",
                "series_ticker": "KXBTCD",
                "floor_strike": 70000.0,
                "yes_ask_dollars": "0.3400",
                "yes_bid_dollars": "0.3300",
                "no_ask_dollars": "0.6800",
                "no_bid_dollars": "0.6600",
            },
            # Strike B ($72,000): spot is 71k (< 72k) -> NO aligned. Ask is 36¢ (in corridor 32¢-52¢).
            {
                "ticker": "KXBTCD-STRIKE-B",
                "series_ticker": "KXBTCD",
                "floor_strike": 72000.0,
                "yes_ask_dollars": "0.6600",
                "yes_bid_dollars": "0.6400",
                "no_ask_dollars": "0.3600",
                "no_bid_dollars": "0.3500",
            },
            # Strike C ($74,000): NO ask is 48¢ (valid but 36¢ on Strike B is cheaper).
            {
                "ticker": "KXBTCD-STRIKE-C",
                "series_ticker": "KXBTCD",
                "floor_strike": 74000.0,
                "yes_ask_dollars": "0.5500",
                "yes_bid_dollars": "0.5300",
                "no_ask_dollars": "0.4800",
                "no_bid_dollars": "0.4600",
            },
        ]

        snapshots = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=0,
            spot_cache=spot_cache,
            dry_run=False,
        )

        # Verify dual buys executed
        self.assertEqual(len(client.executed_buys), 2)
        buys_by_ticker = {b["ticker"]: b for b in client.executed_buys}

        self.assertIn("KXBTCD-STRIKE-A", buys_by_ticker)
        self.assertEqual(buys_by_ticker["KXBTCD-STRIKE-A"]["side"], "yes")
        self.assertEqual(buys_by_ticker["KXBTCD-STRIKE-A"]["price"], 34)

        self.assertIn("KXBTCD-STRIKE-B", buys_by_ticker)
        self.assertEqual(buys_by_ticker["KXBTCD-STRIKE-B"]["side"], "no")
        self.assertEqual(buys_by_ticker["KXBTCD-STRIKE-B"]["price"], 36)

        # Verify snapshot documents produced with tf="1h"
        self.assertEqual(len(snapshots), 3)
        for s in snapshots:
            self.assertEqual(s["tf"], "1h")
            self.assertEqual(s["sym"], "BTC")

    def test_hourly_same_ticker_tiebreaker_cheaper_selected(self):
        """
        Tests tiebreaker on the SAME contract ticker:
        On Strike A, BUY_YES is 52¢ and BUY_NO is 48¢ (both within broad corridor, spot ATM).
        Bot cannot buy both on same contract; should pick BUY_NO because 48¢ < 52¢.
        """
        client = DummyHourlyTradingClient()
        now_utc = datetime(2026, 8, 15, 20, 0, 30, tzinfo=timezone.utc)
        # Spot is right at strike (dead-heat ATM within 0.05%) so both YES and NO are permitted
        spot_cache = {"BTC": 70000.0}

        markets = [
            {
                "ticker": "KXBTCD-SAME-CONTRACT",
                "series_ticker": "KXBTCD",
                "floor_strike": 70000.0,
                "yes_ask_dollars": "0.5200",
                "yes_bid_dollars": "0.5000",
                "no_ask_dollars": "0.4800",
                "no_bid_dollars": "0.4600",
            },
        ]

        snapshots = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=0,
            spot_cache=spot_cache,
            dry_run=False,
        )

        self.assertEqual(len(client.executed_buys), 1)
        executed_buy = client.executed_buys[0]
        self.assertEqual(executed_buy["ticker"], "KXBTCD-SAME-CONTRACT")
        # 48¢ NO is cheaper than 52¢ YES -> picked NO
        self.assertEqual(executed_buy["side"], "no")
        self.assertEqual(executed_buy["price"], 47)  # Maker bid + 1 = 46 + 1 = 47

    def test_hourly_same_ticker_equal_price_spot_momentum_tiebreaker(self):
        """
        Tests tiebreaker on same contract when YES and NO prices are identical (e.g. 46¢ vs 46¢):
        Picks side with positive spot momentum advantage.
        """
        client = DummyHourlyTradingClient()
        now_utc = datetime(2026, 8, 15, 20, 0, 30, tzinfo=timezone.utc)
        # Spot 70010 vs strike 70000 (+0.014% dist) -> slightly positive
        spot_cache = {"BTC": 70010.0}

        markets = [
            {
                "ticker": "KXBTCD-EQUAL-PRICE",
                "series_ticker": "KXBTCD",
                "floor_strike": 70000.0,
                "yes_ask_dollars": "0.4600",
                "yes_bid_dollars": "0.4400",
                "no_ask_dollars": "0.4600",
                "no_bid_dollars": "0.4400",
            },
        ]

        snapshots = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=0,
            spot_cache=spot_cache,
            dry_run=False,
        )

        self.assertEqual(len(client.executed_buys), 1)
        self.assertEqual(client.executed_buys[0]["side"], "yes")

    def test_hourly_20min_cut_loss_and_second_entry(self):
        """
        Tests Minute 20:
        1. Open position on Strike A entered at 48¢ drops to 32¢ (< 40¢ cut threshold) -> early loss cut.
        2. Tighter entry corridor (28¢-38¢) evaluates new opportunity on Strike B (35¢) and buys it.
        """
        client = DummyHourlyTradingClient()
        # Seed an open losing position on Strike A
        client.execute_buy("KXBTCD-STRIKE-A", "yes", 1, 48)
        client.executed_buys.clear()

        now_utc = datetime(2026, 8, 15, 20, 20, 30, tzinfo=timezone.utc)
        spot_cache = {"BTC": 71000.0}

        markets = [
            # Strike A: open position dropping to 32¢
            {
                "ticker": "KXBTCD-STRIKE-A",
                "series_ticker": "KXBTCD",
                "floor_strike": 70000.0,
                "yes_ask_dollars": "0.3200",
                "yes_bid_dollars": "0.3000",
                "no_ask_dollars": "0.7000",
                "no_bid_dollars": "0.6800",
            },
            # Strike B: new opportunity at 35¢ (in tight corridor 28¢-38¢)
            {
                "ticker": "KXBTCD-STRIKE-B",
                "series_ticker": "KXBTCD",
                "floor_strike": 70500.0,
                "yes_ask_dollars": "0.3500",
                "yes_bid_dollars": "0.3400",
                "no_ask_dollars": "0.6700",
                "no_bid_dollars": "0.6500",
            },
        ]

        snapshots = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=20,
            spot_cache=spot_cache,
            dry_run=False,
        )

        # Verify Strike A was liquidated by 20m cut-loss
        self.assertEqual(len(client.executed_liquidations), 1)
        self.assertEqual(client.executed_liquidations[0]["ticker"], "KXBTCD-STRIKE-A")

        # Verify Strike B was bought in 20m corridor
        self.assertEqual(len(client.executed_buys), 1)
        self.assertEqual(client.executed_buys[0]["ticker"], "KXBTCD-STRIKE-B")
        self.assertEqual(client.executed_buys[0]["price"], 35)

    def test_hourly_40min_winner_roll_and_stop_loss(self):
        """
        Tests Minute 40:
        1. Winning position (entered at 36¢, now 74¢ >= 70¢): rolls winner (sells at Bid, re-buys at Ask).
        2. Losing position (entered at 45¢, now 28¢ with loss 17¢ >= 10¢): triggers stop-loss liquidation.
        """
        client = DummyHourlyTradingClient()
        # Seed winning position on Strike A and losing position on Strike B
        client.execute_buy("KXBTCD-WINNER", "yes", 1, 36)
        client.execute_buy("KXBTCD-LOSER", "no", 1, 45)
        client.executed_buys.clear()

        now_utc = datetime(2026, 8, 15, 20, 40, 30, tzinfo=timezone.utc)
        spot_cache = {"BTC": 71000.0}

        markets = [
            # Strike Winner: YES bid is 74¢
            {
                "ticker": "KXBTCD-WINNER",
                "series_ticker": "KXBTCD",
                "floor_strike": 70000.0,
                "yes_ask_dollars": "0.7600",
                "yes_bid_dollars": "0.7400",
                "no_ask_dollars": "0.2600",
                "no_bid_dollars": "0.2400",
            },
            # Strike Loser: NO bid is 28¢ (loss = 45 - 28 = 17¢ >= 10¢ SL)
            {
                "ticker": "KXBTCD-LOSER",
                "series_ticker": "KXBTCD",
                "floor_strike": 72000.0,
                "yes_ask_dollars": "0.7400",
                "yes_bid_dollars": "0.7200",
                "no_ask_dollars": "0.3000",
                "no_bid_dollars": "0.2800",
            },
        ]

        snapshots = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=40,
            spot_cache=spot_cache,
            dry_run=False,
        )

        # Winner was liquidated to bank profit & re-bought at Ask
        # Loser was liquidated to cut loss
        self.assertEqual(len(client.executed_liquidations), 2)
        liq_tickers = [l["ticker"] for l in client.executed_liquidations]
        self.assertIn("KXBTCD-WINNER", liq_tickers)
        self.assertIn("KXBTCD-LOSER", liq_tickers)

        # Re-buy occurred for Winner
        self.assertEqual(len(client.executed_buys), 1)
        self.assertEqual(client.executed_buys[0]["ticker"], "KXBTCD-WINNER")
        self.assertEqual(client.executed_buys[0]["price"], 76)

    def test_hourly_50min_endgame_safety_check(self):
        """
        Tests Minute 50 (10m before hourly close):
        1. Losing position at 22¢ (< 25¢ endgame floor) -> liquidated immediately to salvage capital.
        2. Winning position at 88¢ (> entry 38¢) -> held through final 10m for $1.00 payout.
        """
        client = DummyHourlyTradingClient()
        client.execute_buy("KXBTCD-LOSING-POS", "yes", 1, 40)
        client.execute_buy("KXBTCD-WINNING-POS", "yes", 1, 38)
        client.executed_buys.clear()

        now_utc = datetime(2026, 8, 15, 20, 50, 30, tzinfo=timezone.utc)
        spot_cache = {"BTC": 71000.0}

        markets = [
            {
                "ticker": "KXBTCD-LOSING-POS",
                "series_ticker": "KXBTCD",
                "floor_strike": 70000.0,
                "yes_ask_dollars": "0.2400",
                "yes_bid_dollars": "0.2200",
                "no_ask_dollars": "0.8000",
                "no_bid_dollars": "0.7800",
            },
            {
                "ticker": "KXBTCD-WINNING-POS",
                "series_ticker": "KXBTCD",
                "floor_strike": 69000.0,
                "yes_ask_dollars": "0.9000",
                "yes_bid_dollars": "0.8800",
                "no_ask_dollars": "0.1200",
                "no_bid_dollars": "0.1000",
            },
        ]

        snapshots = process_hourly_market_group(
            symbol="BTC",
            markets=markets,
            client=client,
            now_utc=now_utc,
            minute_past_hour=50,
            spot_cache=spot_cache,
            dry_run=False,
        )

        # Losing position was liquidated to salvage capital
        self.assertEqual(len(client.executed_liquidations), 1)
        self.assertEqual(client.executed_liquidations[0]["ticker"], "KXBTCD-LOSING-POS")
        self.assertEqual(client.executed_liquidations[0]["price"], 22)

        # Winning position remains active
        self.assertIsNotNone(client.get_market_position("KXBTCD-WINNING-POS"))


if __name__ == "__main__":
    unittest.main()
