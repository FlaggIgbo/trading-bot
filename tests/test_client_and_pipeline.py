from __future__ import annotations

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

    # 1. 10m roll winner
    with patch.object(dummy_client, "cancel_and_verify_resting_orders", return_value=True), \
         patch.object(dummy_client, "execute_liquidation") as mock_liq, \
         patch.object(dummy_client, "execute_buy") as mock_buy:
        res = dummy_client.evaluate_and_execute_10min_strategy(ticker, 72, 26, 74, 28)
        assert res["action"] == "winner_rolled_rebuy"
        mock_liq.assert_called_once_with(ticker, "yes", 1, 72)
        mock_buy.assert_called_once_with(ticker, "yes", 1, 74)

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

