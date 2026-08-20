from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from main import (
    batch_fetch_crypto_spot_prices,
    extract_asset_symbol,
    extract_price_cents,
    is_spot_momentum_aligned,
    parse_cents,
    process_single_market,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import KalshiClient, round_price_to_tick


class DummyTradingClient(BaseTradingClient):
    exchange_name = "TestExchange"

    def __init__(self):
        super().__init__()
        self._positions_cache = {}
        self._orders_cache = {}

    def get_all_active_positions(self, force_refresh: bool = False):
        return self._positions_cache

    def get_market(self, ticker: str) -> dict:
        return {"market": {"ticker": ticker}}

    def get_market_position(self, ticker: str):
        return self._positions_cache.get(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str):
        self._orders_cache.pop(ticker, None)
        return True

    def cancel_unfilled_buy_orders(self, ticker: str):
        return True

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self._positions_cache or ticker in self._orders_cache

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int):
        self._positions_cache.pop(ticker, None)
        self.clear_profit_floor(ticker)
        self.clear_entry_price(ticker)
        return {"status": "liquidated", "price": current_price}

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
        self._positions_cache[ticker] = {"side": side.lower(), "count": count, "entry_price_cents": price_cents}
        self.set_entry_price(ticker, price_cents)
        return {"status": "bought", "price": price_cents, "fill_count": count}

    def buy_and_set_take_profit(self, ticker: str, side: str, buy_price_cents: int, take_profit_cents: int = 82, count: int = 1, maker_bid_cents: int = None, **kw):
        effective = min(buy_price_cents, (maker_bid_cents + 1) if maker_bid_cents else buy_price_cents)
        self._positions_cache[ticker] = {"side": side.lower(), "count": count, "entry_price_cents": effective}
        self.set_entry_price(ticker, effective)
        return {"buy_price_cents": effective, "order_id": f"buy_{ticker}", "tp_order_id": f"tp_{ticker}"}


@pytest.fixture
def dummy_client():
    return DummyTradingClient()


@pytest.fixture
def mock_filter():
    return {
        "sl_threshold": 10, "sl_floor": 30, "cut_5min": 35, "endgame_12_5_floor": 25,
        "min_buy": 30, "max_buy": 52, "tp": 82, "avoid_mid_zone": True,
        "mid_zone_min": 37, "mid_zone_max": 43, "is_exploration": False,
    }


@pytest.fixture(autouse=True)
def mock_ungated_asset():
    with patch("main.is_asset_loss_gated", return_value=(False, 0.0)):
        yield


# --- Unit Tests: Helpers & Utility Functions ---

@pytest.mark.parametrize("input_val, expected", [
    (0.423, "0.42"), (0.001, "0.01"), (1.05, "0.99")
])
def test_round_price_to_tick(input_val, expected):
    assert round_price_to_tick(input_val) == expected


@pytest.mark.parametrize("val, default, expected", [("0.4500", 0, 45), (None, 65, 65)])
def test_parse_cents(val, default, expected):
    assert parse_cents(val, default) == expected


@pytest.mark.parametrize("series, ticker, expected", [
    ("KXBTC15M", "KXBTC15M-26AUG140015-15", "BTC"),
    ("KXETH15M", "KXETH15M-26AUG140015-15", "ETH"),
    ("ETH15M", "GEMINI-ETH15M-100", "ETH"),
    ("BTC15M", "GEMI-BTC15M2602251745-UP", "BTC"),
    ("BTC05M", "GEMI-BTC05M2602251745-UP", "BTC"),
    ("GOLD15M", "GEMI-GOLD15M2602251745-UP", "GOLD"),
    ("INXU", "GEMI-INXU-26AUG1813", "INX"),
    ("UNKNOWN", "XYZ123", "UNKNOWN"),
])
def test_extract_asset_symbol(series, ticker, expected):
    assert extract_asset_symbol(series, ticker) == expected


@pytest.mark.parametrize("side, spot, target, expected", [
    ("yes", 60000.0, 59000.0, True),
    ("no", 60000.0, 59000.0, False),
    ("yes", 58000.0, 59000.0, False),
    ("no", 58000.0, 59000.0, True),
    ("yes", 59005.0, 59000.0, True),  # ATM zone
    ("no", 59005.0, 59000.0, True),
])
def test_spot_momentum_alignment(side, spot, target, expected):
    assert is_spot_momentum_aligned(side, spot, target) is expected


def test_extract_price_cents():
    mkt = {"yes_ask_dollars": "0.4500", "no_ask": 55}
    assert extract_price_cents(mkt, "yes") == 45
    assert extract_price_cents(mkt, "no") == 55


def test_batch_fetch_crypto_spot_prices():
    with patch("main.get_crypto_spot_price", side_effect=lambda sym: 60000.0 if sym == "BTC" else 2000.0):
        res = batch_fetch_crypto_spot_prices(["BTC", "ETH", "BTC"])
        assert res == {"BTC": 60000.0, "ETH": 2000.0}


# --- Trading Engine & Strategy Timing Tests ---

@pytest.mark.parametrize("minute, expected_action, expected_trd_key", [
    (0, "snapshot_only", None),
    (2, "buy_opened", "buy_and_set_take_profit"),
    (10, "10min_stop_loss", "evaluate_and_execute_10min_stop_loss"),
    (12, "endgame_loss_cut", "evaluate_and_execute_12_5min_endgame_check"),
    (14, "14min_crash_selloff_cut", "evaluate_and_execute_crash_detection"),
])
def test_process_single_market_intervals(dummy_client, mock_filter, minute, expected_action, expected_trd_key):
    client = MagicMock(exchange_name="Kalshi")
    client.get_market_position.return_value = None
    client.has_existing_position_or_order.return_value = False

    if expected_trd_key:
        getattr(client, expected_trd_key).return_value = {
            "triggered": True, "action": expected_action, "side": "yes", "buy_price_cents": 34
        }

    market = {
        "ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.3400", "yes_bid_dollars": "0.3200",
        "no_ask_dollars": "0.6800", "no_bid_dollars": "0.6600",
        "floor_strike": 60000.0, "volume_24h": 1500,
    }

    neutral_streak = {"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}
    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value=neutral_streak):
        snapshot = process_single_market(
            market=market,
            is_trading_interval=(minute == 2),
            client=client,
            now_utc=datetime(2026, 8, 14, 0, minute, 30, tzinfo=timezone.utc),
            minute_past_hour=minute,
            spot_cache={"BTC": 60100.0},
            dry_run=False,
        )

    assert snapshot["tick"] == "KXBTC15M-TEST"
    if expected_action != "snapshot_only":
        assert snapshot["trd"]["action"] == expected_action
    else:
        assert snapshot["y"] == 34


# --- Kalshi Client Lifecycle & Order Payload Tests ---

def test_dummy_client_order_and_risk_lifecycle(dummy_client):
    ticker = "KXBTC15M-TEST"
    dummy_client.execute_buy(ticker, "yes", 1, 40)
    assert dummy_client.get_entry_price(ticker) == 40

    # 1. 10m bank winner profit (no high-price re-buy)
    with patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq, \
         patch.object(dummy_client, "execute_buy") as mock_buy:
        res = dummy_client.evaluate_and_execute_10min_strategy(ticker, 72, 26, 74, 28)
        assert res["action"] == "winner_profit_banked"
        assert res["profit_locked_cents"] == 32
        mock_liq.assert_called_once_with(ticker, "yes", 1, 72)
        mock_buy.assert_not_called()

    # 2. Cancellation failure prevents execution
    with patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=False), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq:
        res = dummy_client.evaluate_and_execute_5min_cut_loss(ticker, 20, 80, 30)
        assert res["error"] == "resting_orders_not_cleared"
        mock_liq.assert_not_called()


def test_kalshi_client_caching_and_order_limits():
    client = KalshiClient(api_key_id="k", private_key=MagicMock())
    positions_resp = {"market_positions": [{"ticker": "KXBTC15M-TEST", "position_fp": "1.0", "market_exposure_dollars": "0.42"}]}
    orders_resp = {"orders": [{"ticker": "KXBTC15M-TEST", "side": "ask", "client_order_id": "sell_tp_KXBTC15M-TEST_123"}]}

    with patch.object(client, "get_positions", return_value=positions_resp) as m_pos, \
         patch.object(client, "get_orders", return_value=orders_resp) as m_ord:

        for _ in range(5):  # Reduced from 50 iterations to verify caching without runtime hit
            assert client.get_market_position("KXBTC15M-MISSING") is None
            assert not client.has_resting_take_profit_order("KXBTC15M-MISSING")

        assert client.get_market_position("KXBTC15M-TEST")["count"] == 1
        assert client.has_resting_take_profit_order("KXBTC15M-TEST") is True
        assert m_pos.call_count == 1
        assert m_ord.call_count == 1


def test_generate_safe_client_order_id():
    from signals.kalshi_client import generate_safe_client_order_id
    # Ensure dots and illegal characters are sanitized
    cid = generate_safe_client_order_id("buy_no", "KXDOGED-26AUG1623-T0.0699999", 1786845635000)
    assert cid == "buy_no_KXDOGED-26AUG1623-T0_0699999_1786845635000"
    assert "." not in cid
    assert cid.startswith("buy_no_")

    cid_tp = generate_safe_client_order_id("sell_tp_yes", "KXXRPD-26AUG1623-T0.9999", 1786845635000)
    assert cid_tp == "sell_tp_yes_KXXRPD-26AUG1623-T0_9999_1786845635000"
    assert "." not in cid_tp
    assert "sell_tp_" in cid_tp


def test_kalshi_order_payload_sanitization():
    client = KalshiClient(api_key_id="k", private_key=MagicMock())
    ticker = "KXDOGED-26AUG1623-T0.0699999"

    with patch.object(client, "post", return_value={"order_id": "ord_123"}) as mock_post:
        client.buy_and_set_take_profit(
            ticker=ticker,
            side="no",
            buy_price_cents=62,
            take_profit_cents=88,
            count=1,
            maker_bid_cents=56,
        )
        assert mock_post.call_count == 1
        payload = mock_post.call_args[1]["payload"]
        assert "." not in payload["client_order_id"]
        assert payload["ticker"] == ticker
        assert payload["side"] == "ask"
        assert payload["price"] == "0.4300"
        assert payload["count"] == "1"
        assert client.get_target_tp(ticker) == 88


def test_target_tp_tracking_and_persistence():
    client = KalshiClient(api_key_id="k", private_key=MagicMock())
    ticker = "KXNEAR15M-TEST"

    with patch("database.save_active_position_cost") as mock_save:
        client.set_entry_price(ticker, price_cents=63, side="no", count=1, take_profit_cents=88)
        assert client.get_entry_price(ticker) == 63
        assert client.get_target_tp(ticker) == 88
        assert mock_save.call_count == 1
        assert mock_save.call_args[1]["target_tp_cents"] == 88

    client.clear_entry_price(ticker)
    assert client.get_entry_price(ticker) is None
    assert client.get_target_tp(ticker) is None


def test_resolve_safe_take_profit_for_position():
    from main import resolve_safe_take_profit_for_position
    client = KalshiClient(api_key_id="k", private_key=MagicMock())
    filters = {
        "tp": 62,
        "momentum_tp": 88,
        "momentum_min_buy": 56,
    }

    # Case 1: Recorded target TP exists (e.g. 88¢ from momentum trade)
    ticker1 = "KXNEAR15M-MOMENTUM"
    client.set_entry_price(ticker1, price_cents=63, side="no", take_profit_cents=88)
    assert resolve_safe_take_profit_for_position(ticker1, client, filters) == 88

    # Case 2: No explicit target TP, but entry price >= 56¢ (inferred momentum)
    ticker2 = "KXSOL15M-INFERRED-MOM"
    client.set_entry_price(ticker2, price_cents=58, side="yes")
    assert resolve_safe_take_profit_for_position(ticker2, client, filters) == 88

    # Case 3: Discount entry (e.g. 25¢)
    ticker3 = "KXBTC15M-DISCOUNT"
    client.set_entry_price(ticker3, price_cents=25, side="yes")
    assert resolve_safe_take_profit_for_position(ticker3, client, filters) == 62

    # Case 4: Entry price is high (e.g. 65¢) without explicit TP, TP must never be <= entry price
    ticker4 = "KXETH15M-HIGH-ENTRY"
    client.set_entry_price(ticker4, price_cents=65, side="no")
    safe_tp = resolve_safe_take_profit_for_position(ticker4, client, filters)
    assert safe_tp > 65
    assert safe_tp == 88


def test_place_take_profit_order_safety_guard():
    client = KalshiClient(api_key_id="k", private_key=MagicMock())
    ticker = "KXNEAR15M-GUARD-TEST"
    client.set_entry_price(ticker, price_cents=63, side="no")

    with patch.object(client, "post", return_value={"order_id": "tp_123"}) as mock_post:
        # Attempt to place TP at 62¢ on a 63¢ entry
        res = client.place_take_profit_order(ticker, side="no", count=1, take_profit_cents=62)
        assert res == {"order_id": "tp_123"}
        assert mock_post.call_count == 1
        payload = mock_post.call_args[1]["payload"]
        # NO trade: ask price for YES is (100 - TP). TP should have been auto-adjusted to 88¢ or (63+5=68) -> (100 - 88 = 12¢ or 100 - 68 = 32¢)
        # It must NOT be (100 - 62 = 38¢) which would sell NO at 62¢!
        assert payload["price"] != "0.3800"
        # TP was adjusted to 88¢, so price for YES bid is 100 - 88 = 12¢
        assert payload["price"] == "0.1200"


def test_cancel_order_safe_handles_404_cleanly():
    mock_key = MagicMock()
    mock_key.sign.return_value = b"mock_signature"
    client = KalshiClient(api_key_id="k", private_key=mock_key)
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = json.dumps({"error": {"code": "not_found", "message": "not found"}})

    with patch.object(client.session, "delete", return_value=mock_resp):
        # Should return True without raising or throwing error logs
        assert client.cancel_order_safe("23b665a7-dc84-4610-a618-e3f41d0b5890") is True


def test_strict_single_side_market_lock(dummy_client):
    ticker = "KXBTC15M-SINGLE-LOCK-TEST"
    assert dummy_client.is_side_allowed(ticker, "yes") is True
    assert dummy_client.is_side_allowed(ticker, "no") is True

    # Record trade on YES
    dummy_client.record_traded_market(ticker, "yes")
    assert dummy_client.get_traded_market_side(ticker) == "yes"
    assert dummy_client.is_side_allowed(ticker, "yes") is True
    assert dummy_client.is_side_allowed(ticker, "no") is False

    # Clear market
    dummy_client.clear_traded_market(ticker)
    assert dummy_client.is_side_allowed(ticker, "no") is True


def test_settlement_revenue_parsing_kalshi_v2():
    mock_key = MagicMock()
    mock_key.sign.return_value = b"mock_signature"
    client = KalshiClient(api_key_id="k", private_key=mock_key)

    mock_settlements = {
        "settlements": [
            {
                "ticker": "KXGOLD15M-WINNER-YES",
                "market_result": "yes",
                "yes_count_fp": "1.00",
                "no_count_fp": "0.00",
                "yes_total_cost_dollars": "0.3500",
                "no_total_cost_dollars": "0.0000",
                "revenue": 0,
                "value": 100,
                "settled_time": "2026-08-17T15:45:00Z",
            },
            {
                "ticker": "KXWTI15M-WINNER-NO",
                "market_result": "no",
                "yes_count_fp": "0.00",
                "no_count_fp": "1.00",
                "yes_total_cost_dollars": "0.0000",
                "no_total_cost_dollars": "0.2800",
                "revenue": 0,
                "value": 0,
                "settled_time": "2026-08-17T15:45:00Z",
            },
            {
                "ticker": "KXBTC15M-LOSER-YES",
                "market_result": "no",
                "yes_count_fp": "1.00",
                "no_count_fp": "0.00",
                "yes_total_cost_dollars": "0.4000",
                "no_total_cost_dollars": "0.0000",
                "revenue": 0,
                "value": 0,
                "settled_time": "2026-08-17T15:45:00Z",
            },
        ]
    }

    with patch.object(client, "get_balance", return_value={"balance_dollars": 10.0, "portfolio_value": 0}), \
         patch.object(client, "get_positions", return_value={"positions": []}), \
         patch.object(client, "get_settlements", return_value=mock_settlements):
        summary = client.get_portfolio_pnl_summary()

    records = summary["settlements"]
    assert len(records) == 3

    # Winner YES: revenue = $1.00, cost = $0.35, pnl = +$0.65
    assert records[0]["revenue_dollars"] == 1.00
    assert records[0]["cost_dollars"] == 0.35
    assert records[0]["pnl_dollars"] == 0.65

    # Winner NO: revenue = $1.00, cost = $0.28, pnl = +$0.72
    assert records[1]["revenue_dollars"] == 1.00
    assert records[1]["cost_dollars"] == 0.28
    assert records[1]["pnl_dollars"] == 0.72

    # Loser YES: revenue = $0.00, cost = $0.40, pnl = -$0.40
    assert records[2]["revenue_dollars"] == 0.00
    assert records[2]["cost_dollars"] == 0.40
    assert records[2]["pnl_dollars"] == -0.40

    assert summary["total_settled_revenue_dollars"] == 2.00
    assert summary["total_settled_cost_dollars"] == 1.03
    assert summary["total_realized_pnl_dollars"] == 0.97


def test_passive_maker_pricing_strictly_below_ask():
    mock_key = MagicMock()
    mock_key.sign.return_value = b"mock_signature"
    client = KalshiClient(api_key_id="k", private_key=mock_key)
    ticker = "KXBTC15M-MAKER-TEST"

    with patch.object(client, "post", return_value={"order_id": "ord_123", "fill_count": 0}) as mock_post:
        # Ask is 25¢, Maker bid is 20¢ -> Limit bid must be 21¢ (strictly < 25¢)
        res = client.buy_and_set_take_profit(
            ticker=ticker,
            side="yes",
            buy_price_cents=25,
            maker_bid_cents=20,
            take_profit_cents=62,
        )
        assert res["buy_price_cents"] == 21
        payload = mock_post.call_args[1]["payload"]
        assert payload["price"] == "0.2100"
        assert payload["post_only"] is True

        # Ask is 30¢, No bid given -> Limit bid must be 29¢ (strictly < 30¢)
        res2 = client.buy_and_set_take_profit(
            ticker=ticker,
            side="yes",
            buy_price_cents=30,
            maker_bid_cents=None,
            take_profit_cents=62,
        )
        assert res2["buy_price_cents"] == 29
        payload2 = mock_post.call_args[1]["payload"]
        assert payload2["price"] == "0.2900"
        assert payload2["post_only"] is True


# --- Time-Distance-to-Closure Validation & 5M Safety Tests ---

@pytest.mark.parametrize("now_dt, close_iso, tf, expected_valid, expected_rem_mins", [
    # 15M: At :05, expiry :15 -> 10 mins away (valid)
    (datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc), "2026-08-18T12:15:00Z", "15m", True, 10.0),
    # 15M: At :00:30, expiry :15 -> 14.5 mins away (valid)
    (datetime(2026, 8, 18, 12, 0, 30, tzinfo=timezone.utc), "2026-08-18T12:15:00Z", "15m", True, 14.5),
    # 15M: At :12:30, expiry :15 -> 2.5 mins away (valid)
    (datetime(2026, 8, 18, 12, 12, 30, tzinfo=timezone.utc), "2026-08-18T12:15:00Z", "15m", True, 2.5),
    # 15M: 5-minute market (expiry at :10 when now is :05 -> 5 mins away, non-15m boundary) -> INVALID
    (datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc), "2026-08-18T12:10:00Z", "15m", False, 5.0),
    # 15M: 5-minute market (expiry at :35 -> :35 % 15 != 0) -> INVALID
    (datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc), "2026-08-18T12:35:00Z", "15m", False, 30.0),
    # 15M: Market already closed (expiry :00 when now is :05) -> INVALID
    (datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc), "2026-08-18T12:00:00Z", "15m", False, -5.0),
    # 15M: Next cycle market (expiry :30 when now is :05 -> 25 mins away) -> INVALID
    (datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc), "2026-08-18T12:30:00Z", "15m", False, 25.0),
    # 1H: At :20, expiry :00 -> 40 mins away (valid)
    (datetime(2026, 8, 18, 12, 20, 0, tzinfo=timezone.utc), "2026-08-18T13:00:00Z", "1h", True, 40.0),
    # 1H: At :00:30, expiry :00 -> 59.5 mins away (valid)
    (datetime(2026, 8, 18, 12, 0, 30, tzinfo=timezone.utc), "2026-08-18T13:00:00Z", "1h", True, 59.5),
    # 1H: At :50, expiry :00 -> 10 mins away (valid)
    (datetime(2026, 8, 18, 12, 50, 0, tzinfo=timezone.utc), "2026-08-18T13:00:00Z", "1h", True, 10.0),
    # 1H: Non-hourly 15m/30m market (expiry at :30 when now is :20) -> INVALID (minute :30 != :00)
    (datetime(2026, 8, 18, 12, 20, 0, tzinfo=timezone.utc), "2026-08-18T12:30:00Z", "1h", False, 10.0),
    # 1H: Closed S&P market outside trading hours (expiry tomorrow 14:00 UTC when now is 20:00 UTC) -> INVALID
    (datetime(2026, 8, 18, 20, 0, 0, tzinfo=timezone.utc), "2026-08-19T14:00:00Z", "1h", False, 1080.0),
])
def test_validate_market_closure_time(now_dt, close_iso, tf, expected_valid, expected_rem_mins):
    from signals.base_client import validate_market_closure_time
    is_valid, reason, rem_mins = validate_market_closure_time(
        close_time_raw=close_iso,
        now_utc=now_dt,
        timeframe=tf,
    )
    assert is_valid is expected_valid
    if expected_rem_mins is not None:
        assert rem_mins == pytest.approx(expected_rem_mins, abs=0.1)


def test_kalshi_trading_client_order_rejection_on_5min_market():
    """Verify that Kalshi trading client refuses to place orders on 5-minute markets."""
    mock_key = MagicMock()
    client = KalshiClient(api_key_id="test_key", private_key=mock_key)
    five_min_ticker = "KXBTC05M-26AUG181235-35"

    with patch.object(client, "post") as mock_post:
        res1 = client.buy_and_set_take_profit(
            ticker=five_min_ticker, side="yes", buy_price_cents=45
        )
        assert res1.get("error") == "5min_market_rejected"

        res2 = client.buy_favorite_maker_order(
            ticker=five_min_ticker, side="yes", maker_bid_cents=80, target_ask_cents=82
        )
        assert res2.get("error") == "5min_market_rejected"

        res3 = client.execute_buy(
            ticker=five_min_ticker, side="yes", price_cents=50
        )
        assert res3.get("error") == "5min_market_rejected"

        # Ensure no HTTP requests were sent to the exchange
        mock_post.assert_not_called()


def test_kalshi_get_markets_filters_5m_and_time_distance():
    """Verify Kalshi get_15m_crypto_markets and get_hourly_crypto_markets filter 5m and distant markets."""
    mock_key = MagicMock()
    client = KalshiClient(api_key_id="test_key", private_key=mock_key)

    mock_15m_markets = {
        "markets": [
            # 5-minute market (must be excluded)
            {"ticker": "KXBTC05M-TEST", "close_time": "2026-08-18T12:10:00Z"},
            # 15-minute market (valid)
            {"ticker": "KXBTC15M-TEST", "close_time": "2026-08-18T12:15:00Z"},
            # 15-minute next cycle market (too far)
            {"ticker": "KXBTC15M-NEXT", "close_time": "2026-08-18T12:30:00Z"},
        ]
    }

    mock_hourly_markets = {
        "markets": [
            # 5-minute market (must be excluded)
            {"ticker": "KXBTC05M-TEST", "close_time": "2026-08-18T12:25:00Z"},
            # 15-minute market (must be excluded from hourly)
            {"ticker": "KXBTC15M-TEST", "close_time": "2026-08-18T12:30:00Z"},
            # Hourly market (valid)
            {"ticker": "KXBTCD-TEST", "close_time": "2026-08-18T13:00:00Z"},
            # S&P 500 market closing tomorrow (outside trading hours)
            {"ticker": "KXINXU-TOMORROW", "close_time": "2026-08-19T14:00:00Z"},
        ]
    }

    now_utc_15m = datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc)
    with patch.object(client, "get_markets", return_value=mock_15m_markets):
        mkts_15m = client.get_15m_crypto_markets(series_list=["KXBTC15M"], now_utc=now_utc_15m)
        assert len(mkts_15m) == 1
        assert mkts_15m[0]["ticker"] == "KXBTC15M-TEST"

    now_utc_1h = datetime(2026, 8, 18, 12, 20, 0, tzinfo=timezone.utc)
    with patch.object(client, "get_markets", return_value=mock_hourly_markets):
        mkts_1h = client.get_hourly_crypto_markets(series_list=["KXBTCD"], current_hour_only=True, now_utc=now_utc_1h)
        assert len(mkts_1h) == 1
        assert mkts_1h[0]["ticker"] == "KXBTCD-TEST"


def test_dummy_client_crash_detection_lifecycle(dummy_client):
    """Verify evaluate_and_execute_crash_detection handles crashes, losing positions, and holds winners."""
    ticker = "KXBTC15M-CRASH"

    # 1. No position -> returns triggered=False
    res_none = dummy_client.evaluate_and_execute_crash_detection(ticker, 20, 80)
    assert res_none == {"triggered": False}

    # 2. Position losing on entry (entry 85¢, current 20¢ < 50¢ floor) -> liquidates immediately
    dummy_client.execute_buy(ticker, "yes", 1, 85)
    with patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq:
        res_loss = dummy_client.evaluate_and_execute_crash_detection(
            ticker, current_yes_price=20, current_no_price=80, dry_run=False, crash_floor_cents=50
        )
        assert res_loss["triggered"] is True
        assert res_loss["action"] == "crash_selloff_cut"
        assert res_loss["loss_cents"] == 65
        assert res_loss["exit_price"] == 20
        mock_liq.assert_called_once_with(ticker, "yes", 1, 20)

    # 3. Position below crash floor despite modest gain (entry 20¢, current 35¢ < 50¢ floor) -> liquidates to bank profit before 0¢ settlement
    dummy_client.execute_buy(ticker, "yes", 1, 20)
    with patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq:
        res_floor = dummy_client.evaluate_and_execute_crash_detection(
            ticker, current_yes_price=35, current_no_price=65, dry_run=False, crash_floor_cents=50
        )
        assert res_floor["triggered"] is True
        assert res_floor["exit_price"] == 35
        assert res_floor["profit_locked_cents"] == 15
        mock_liq.assert_called_once_with(ticker, "yes", 1, 35)

    # 4. Position securely winning (entry 40¢, current 85¢ >= 50¢ floor) -> holds into settlement
    dummy_client.execute_buy(ticker, "yes", 1, 40)
    with patch.object(dummy_client, "execute_liquidation") as mock_liq:
        res_win = dummy_client.evaluate_and_execute_crash_detection(
            ticker, current_yes_price=85, current_no_price=15, dry_run=False, crash_floor_cents=50
        )
        assert res_win["triggered"] is False
        assert res_win["action"] == "holding_winner"
        mock_liq.assert_not_called()

    # 5. Dry-run mode previews liquidation without placing real orders
    dummy_client.execute_buy(ticker, "yes", 1, 85)
    with patch.object(dummy_client, "execute_liquidation") as mock_liq:
        res_dry = dummy_client.evaluate_and_execute_crash_detection(
            ticker, current_yes_price=15, current_no_price=85, dry_run=True, crash_floor_cents=50
        )
        assert res_dry["triggered"] is True
        assert res_dry["loss_cents"] == 70
        assert res_dry["exit_price"] == 15
        mock_liq.assert_not_called()


def test_15m_14min_crash_detection_process_market(dummy_client, mock_filter):
    """Verify that process_single_market at :14 executes crash detection and liquidates losing positions."""
    ticker = "KXBTC15M-14TEST"
    dummy_client.execute_buy(ticker, "yes", 1, 85)

    market_loss = {
        "ticker": ticker, "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.18", "yes_bid_dollars": "0.15",
        "no_ask_dollars": "0.85", "no_bid_dollars": "0.82",
        "floor_strike": 60000.0, "volume_24h": 1500,
        "close_time": "2026-08-14T00:15:00Z",
    }

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq:
        snapshot = process_single_market(
            market=market_loss,
            is_trading_interval=False,
            client=dummy_client,
            now_utc=datetime(2026, 8, 14, 0, 14, 0, tzinfo=timezone.utc),
            minute_past_hour=14,
            spot_cache={"BTC": 59900.0},
            dry_run=False,
        )
        assert snapshot["tick"] == ticker
        assert snapshot["trd"] is not None
        assert snapshot["trd"]["action"] == "14min_crash_selloff_cut"
        assert snapshot["trd"]["exit_price"] == 15
        assert snapshot["trd"]["loss_cents"] == 70
        mock_liq.assert_called_once_with(ticker, "yes", 1, 15)


def test_process_exchange_pipeline(dummy_client, mock_filter):
    """Verify _process_exchange_pipeline processes 15M and hourly markets and returns snapshots."""
    from main import _process_exchange_pipeline

    dummy_client.get_15m_crypto_markets = MagicMock(return_value=[
        {
            "ticker": "KXBTC15M-PIPE-TEST",
            "series_ticker": "KXBTC15M",
            "yes_ask_dollars": "0.40",
            "yes_bid_dollars": "0.38",
            "no_ask_dollars": "0.62",
            "no_bid_dollars": "0.60",
            "floor_strike": 60000.0,
            "volume_24h": 1500,
        }
    ])
    dummy_client.get_hourly_crypto_markets = MagicMock(return_value=[])
    dummy_client.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 100.0,
        "portfolio_value_dollars": 100.0,
        "total_realized_pnl_dollars": 0.0,
    })

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}):
        snaps = _process_exchange_pipeline(
            exchange_name="Kalshi",
            client=dummy_client,
            now_utc=datetime(2026, 8, 14, 0, 0, 30, tzinfo=timezone.utc),
            current_minute=0,
            minute_into_market_15m=0,
            is_15m_trading_interval=True,
            force_trade_mark=False,
            spot_price_cache={"BTC": 60100.0},
            dry_run=True,
        )

        assert len(snaps) == 1
        assert snaps[0]["ex"] == "kalshi"
        assert snaps[0]["tick"] == "KXBTC15M-PIPE-TEST"


def test_run_pipeline_pass_parallel_execution(mock_filter):
    """Verify run_pipeline_pass runs multiple exchange clients concurrently and aggregates snapshots."""
    from main import run_pipeline_pass

    client1 = DummyTradingClient()
    client1.exchange_name = "Kalshi"
    client1.get_15m_crypto_markets = MagicMock(return_value=[
        {
            "ticker": "KXBTC15M-PAR-1",
            "series_ticker": "KXBTC15M",
            "yes_ask_dollars": "0.40",
            "yes_bid_dollars": "0.38",
            "no_ask_dollars": "0.62",
            "no_bid_dollars": "0.60",
            "floor_strike": 60000.0,
        }
    ])
    client1.get_hourly_crypto_markets = MagicMock(return_value=[])
    client1.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 100.0,
        "portfolio_value_dollars": 100.0,
        "total_realized_pnl_dollars": 0.0,
    })

    client2 = DummyTradingClient()
    client2.exchange_name = "Gemini"
    client2.get_15m_crypto_markets = MagicMock(return_value=[
        {
            "ticker": "GEMI-BTC15M-PAR-2",
            "series_ticker": "BTC15M",
            "yes_ask_dollars": "0.42",
            "yes_bid_dollars": "0.39",
            "no_ask_dollars": "0.60",
            "no_bid_dollars": "0.58",
            "floor_strike": 60000.0,
        }
    ])
    client2.get_hourly_crypto_markets = MagicMock(return_value=[])
    client2.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 50.0,
        "portfolio_value_dollars": 50.0,
        "total_realized_pnl_dollars": 0.0,
    })

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}), \
         patch("main.batch_fetch_spot_prices", return_value={"BTC": 60100.0}), \
         patch("main.save_market_snapshots") as mock_save:

        run_pipeline_pass(
            clients=[("Kalshi", client1), ("Gemini", client2)],
            dry_run=False,
            force_trade_mark=True,
        )

        assert mock_save.called
        saved_snaps = mock_save.call_args[0][0]
        exchanges_saved = {s.get("ex") for s in saved_snaps}
        assert "kalshi" in exchanges_saved
        assert "gemini" in exchanges_saved


def test_process_exchange_pipeline_14min_selloff_no_positions(dummy_client, mock_filter):
    """Verify that at :14 mark with no held positions, 15M full market scan is bypassed."""
    from main import _process_exchange_pipeline

    dummy_client.get_15m_crypto_markets = MagicMock()
    dummy_client.get_hourly_crypto_markets = MagicMock()
    dummy_client.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 100.0,
        "portfolio_value_dollars": 100.0,
        "total_realized_pnl_dollars": 0.0,
    })

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}):
        snaps = _process_exchange_pipeline(
            exchange_name="Kalshi",
            client=dummy_client,
            now_utc=datetime(2026, 8, 14, 0, 14, 0, tzinfo=timezone.utc),
            current_minute=14,
            minute_into_market_15m=14,
            is_15m_trading_interval=False,
            force_trade_mark=False,
            spot_price_cache={"BTC": 60100.0},
            dry_run=True,
        )

        # Full market scans should be completely bypassed
        dummy_client.get_15m_crypto_markets.assert_not_called()
        assert len(snaps) == 0


def test_process_exchange_pipeline_14min_selloff_with_held_position(dummy_client, mock_filter):
    """Verify that at :14 mark, only held 15M contracts are fetched and checked."""
    from main import _process_exchange_pipeline

    ticker = "KXBTC15M-HELD14"
    dummy_client.execute_buy(ticker, "yes", 1, 85)

    market_held = {
        "ticker": ticker,
        "series_ticker": "KXBTC15M",
        "yes_ask_dollars": "0.20",
        "yes_bid_dollars": "0.15",
        "no_ask_dollars": "0.85",
        "no_bid_dollars": "0.80",
        "floor_strike": 60000.0,
        "close_time": "2026-08-14T00:15:00Z",
    }

    dummy_client.get_market = MagicMock(return_value={"market": market_held})
    dummy_client.get_15m_crypto_markets = MagicMock()
    dummy_client.get_hourly_crypto_markets = MagicMock()
    dummy_client.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 100.0,
        "portfolio_value_dollars": 100.0,
        "total_realized_pnl_dollars": 0.0,
    })

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}), \
         patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq:

        snaps = _process_exchange_pipeline(
            exchange_name="Kalshi",
            client=dummy_client,
            now_utc=datetime(2026, 8, 14, 0, 14, 0, tzinfo=timezone.utc),
            current_minute=14,
            minute_into_market_15m=14,
            is_15m_trading_interval=False,
            force_trade_mark=False,
            spot_price_cache={"BTC": 59900.0},
            dry_run=False,
        )

        dummy_client.get_15m_crypto_markets.assert_not_called()
        dummy_client.get_market.assert_called_once_with(ticker)
        assert len(snaps) == 1
        assert snaps[0]["tick"] == ticker
        assert snaps[0]["trd"]["action"] == "14min_crash_selloff_cut"
        mock_liq.assert_called_once_with(ticker, "yes", 1, 15)


def test_process_exchange_pipeline_59min_selloff_no_positions(dummy_client, mock_filter):
    """Verify that at :59 mark with no held positions, hourly strike scan is bypassed."""
    from main import _process_exchange_pipeline

    dummy_client.get_15m_crypto_markets = MagicMock()
    dummy_client.get_hourly_crypto_markets = MagicMock()
    dummy_client.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 100.0,
        "portfolio_value_dollars": 100.0,
        "total_realized_pnl_dollars": 0.0,
    })

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}):
        snaps = _process_exchange_pipeline(
            exchange_name="Kalshi",
            client=dummy_client,
            now_utc=datetime(2026, 8, 14, 0, 59, 0, tzinfo=timezone.utc),
            current_minute=59,
            minute_into_market_15m=14,
            is_15m_trading_interval=False,
            force_trade_mark=False,
            spot_price_cache={"BTC": 60100.0},
            dry_run=True,
        )

        dummy_client.get_15m_crypto_markets.assert_not_called()
        dummy_client.get_hourly_crypto_markets.assert_not_called()
        assert len(snaps) == 0


def test_process_exchange_pipeline_59min_selloff_with_held_position(dummy_client, mock_filter):
    """Verify that at :59 mark, only held hourly contracts are fetched and checked."""
    from main import _process_exchange_pipeline

    ticker = "KXBTCD-HELD59"
    dummy_client.execute_buy(ticker, "yes", 1, 80)

    market_held = {
        "ticker": ticker,
        "series_ticker": "KXBTCD",
        "yes_ask_dollars": "0.15",
        "yes_bid_dollars": "0.10",
        "no_ask_dollars": "0.90",
        "no_bid_dollars": "0.85",
        "floor_strike": 70000.0,
        "close_time": "2026-08-14T01:00:00Z",
    }

    dummy_client.get_market = MagicMock(return_value={"market": market_held})
    dummy_client.get_15m_crypto_markets = MagicMock()
    dummy_client.get_hourly_crypto_markets = MagicMock()
    dummy_client.get_portfolio_pnl_summary = MagicMock(return_value={
        "balance_dollars": 100.0,
        "portfolio_value_dollars": 100.0,
        "total_realized_pnl_dollars": 0.0,
    })

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter), \
         patch("main.get_asset_settlement_streak", return_value={"streak_side": None, "streak_length": 0, "regime": "neutral_flow", "streak_win_rate": 0.5}), \
         patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq:

        snaps = _process_exchange_pipeline(
            exchange_name="Kalshi",
            client=dummy_client,
            now_utc=datetime(2026, 8, 14, 0, 59, 0, tzinfo=timezone.utc),
            current_minute=59,
            minute_into_market_15m=14,
            is_15m_trading_interval=False,
            force_trade_mark=False,
            spot_price_cache={"BTC": 69000.0},
            dry_run=False,
        )

        dummy_client.get_hourly_crypto_markets.assert_not_called()
        dummy_client.get_market.assert_called_once_with(ticker)
        assert len(snaps) == 1
        assert snaps[0]["tick"] == ticker
        assert snaps[0]["trd"]["action"] == "59min_crash_selloff_cut"
        mock_liq.assert_called_once_with(ticker, "yes", 1, 10)







