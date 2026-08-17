from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from database import save_portfolio_pnl
from main import (
    batch_fetch_crypto_spot_prices,
    calculate_seconds_until_next_5min_mark,
    calculate_seconds_until_next_checkpoint,
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

    def get_market_position(self, ticker: str):
        return self._positions_cache.get(ticker)

    def cancel_and_verify_resting_orders(self, ticker: str):
        self._orders_cache.pop(ticker, None)
        return True

    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int):
        self._positions_cache.pop(ticker, None)
        self.clear_profit_floor(ticker)
        self.clear_entry_price(ticker)
        return {"status": "liquidated", "price": current_price}

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int):
        self._positions_cache[ticker] = {"side": side.lower(), "count": count, "entry_price_cents": price_cents}
        self.set_entry_price(ticker, price_cents)
        return {"status": "bought", "price": price_cents, "fill_count": count}


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


@pytest.mark.parametrize("ts, expected", [
    (datetime(2026, 8, 15, 14, 0, 35, tzinfo=timezone.utc), 115.0),
    (datetime(2026, 8, 15, 14, 2, 35, tzinfo=timezone.utc), 145.0),
    (datetime(2026, 8, 15, 14, 12, 35, tzinfo=timezone.utc), 175.0),
])
def test_checkpoint_schedule_timing(ts, expected):
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = ts
        assert calculate_seconds_until_next_checkpoint() == expected


# --- Trading Engine & Strategy Timing Tests ---

@pytest.mark.parametrize("minute, expected_action, expected_trd_key", [
    (0, "snapshot_only", None),
    (2, "buy_opened", "buy_and_set_take_profit"),
    (10, "winner_rolled_rebuy", "evaluate_and_execute_10min_strategy"),
    (12, "endgame_loss_cut", "evaluate_and_execute_12_5min_endgame_check"),
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
        "yes_ask_dollars": "0.4200", "yes_bid_dollars": "0.4000",
        "no_ask_dollars": "0.6000", "no_bid_dollars": "0.5800",
        "floor_strike": 60000.0, "volume_24h": 1500,
    }

    with patch("main.get_dynamic_filter_for_market", return_value=mock_filter):
        snapshot = process_single_market(
            market=market,
            is_trading_interval=(minute in (0, 2)),
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
        assert snapshot["y"] == 42


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




