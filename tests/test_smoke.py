import pytest
from unittest.mock import MagicMock, patch

from database import (
    calculate_dynamic_contracts_count,
    get_dynamic_filter_for_market,
    get_active_position_doc,
)
from main import (
    parse_cents,
    extract_price_cents,
    is_spot_momentum_aligned,
    evaluate_favorite_maker_opportunity,
)
from signals.base_client import BaseTradingClient
from signals.kalshi_client import round_price_to_tick


class SmokeMockClient(BaseTradingClient):
    def __init__(self):
        super().__init__()
        self.exchange_name = "Kalshi"
        self.market_positions = {}
        self.resting_orders = {}

    def get_market_position(self, ticker: str):
        return self.market_positions.get(ticker)

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self.market_positions or ticker in self.resting_orders

    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int, order_type: str = "limit"):
        return {"status": "success", "order_id": "smoke-buy-123"}

    def execute_liquidation(self, ticker: str, side: str, count: int, price_cents: int):
        return {"status": "success", "order_id": "smoke-liq-123"}

    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def init_smoke_cache():
    import time
    import database
    database._DYNAMIC_PRICE_MAP_CACHE = dict(database.DEFAULT_DYNAMIC_PRICE_MAP)
    database._LAST_MAP_REFRESH_TIME = time.time()
    yield
    database._DYNAMIC_PRICE_MAP_CACHE = None
    database._LAST_MAP_REFRESH_TIME = 0.0


@pytest.mark.smoke
def test_smoke_pricing_and_rounding():
    assert parse_cents("0.4500", 0) == 45
    assert parse_cents(None, 82) == 82
    assert round_price_to_tick(0.423) == "0.42"
    assert round_price_to_tick(0.001) == "0.01"

    market = {"yes_ask_dollars": "0.3400", "yes_bid_dollars": "0.3200"}
    assert extract_price_cents(market, "yes") == 34


@pytest.mark.smoke
def test_smoke_momentum_alignment():
    # Spot is above strike (60100 > 60000) -> Bullish, YES allowed, NO blocked
    assert is_spot_momentum_aligned("yes", 60100.0, 60000.0) is True
    assert is_spot_momentum_aligned("no", 60100.0, 60000.0) is False

    # Spot is below strike (59900 < 60000) -> Bearish, NO allowed, YES blocked
    assert is_spot_momentum_aligned("yes", 59900.0, 60000.0) is False
    assert is_spot_momentum_aligned("no", 59900.0, 60000.0) is True


@pytest.mark.smoke
def test_smoke_single_side_market_lock():
    client = SmokeMockClient()
    mock_doc = {"ticker": "KXBTC15M-SMOKE", "ex": "kalshi", "side": "yes", "entry_price_cents": 80}

    with patch("database.get_mongo_collection") as mock_col_getter:
        mock_col = MagicMock()
        mock_col.find_one.return_value = mock_doc
        mock_col_getter.return_value = mock_col

        assert client.is_side_allowed("KXBTC15M-SMOKE", "yes") is True
        assert client.is_side_allowed("KXBTC15M-SMOKE", "no") is False


@pytest.mark.smoke
def test_smoke_dynamic_contracts_count():
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=84, is_restricted=True) == 1
    assert calculate_dynamic_contracts_count(balance_cents=5000, buy_price_cents=84, is_restricted=False, recent_win_rate=0.95) >= 3
    assert calculate_dynamic_contracts_count(balance_cents=500, buy_price_cents=84, is_restricted=False) == 1


@pytest.mark.smoke
def test_smoke_dynamic_filter_lookup():
    f_15m = get_dynamic_filter_for_market("BTC", is_5min_mark=False, timeframe="15m")
    assert "min_buy" in f_15m
    assert "max_buy" in f_15m
    assert "tp" in f_15m

    f_1h = get_dynamic_filter_for_market("GOLD", is_5min_mark=False, timeframe="1h")
    assert f_1h["tp"] >= 50


@pytest.mark.smoke
def test_smoke_favorite_maker_evaluation():
    client = SmokeMockClient()
    market = {"ticker": "KXBTC15M-SMOKE"}
    quotes = {"yes_ask": 88, "yes_bid": 84, "no_ask": 16, "no_bid": 12}
    dynamic_filters = {
        "enable_favorite_maker": True,
        "favorite_maker_min_buy": 80,
        "favorite_maker_max_buy": 93,
        "recent_win_rate": 0.90,
    }

    res = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=dynamic_filters,
        client=client,
        spot_price=60100.0,
        target_price=60000.0,
    )
    assert res is not None
    assert res["side"] == "yes"
    assert res["maker_bid"] == 86
    assert res["spread_saved"] == 2


@pytest.mark.smoke
def test_smoke_empirical_entry_rules():
    from database import is_valid_empirical_entry

    # 15-Minute Markets
    assert is_valid_empirical_entry(25, 1, "15m")[0] is True   # Peak 1: Open Discount
    assert is_valid_empirical_entry(35, 3, "15m")[0] is True   # Peak 1: Open Discount
    assert is_valid_empirical_entry(25, 7, "15m")[0] is True   # Peak 1b: Mid Discount
    assert is_valid_empirical_entry(45, 2, "15m")[0] is True   # Peak 2: Value Reversion
    assert is_valid_empirical_entry(60, 4, "15m")[0] is False  # Valley of Death Banned
    assert is_valid_empirical_entry(75, 13, "15m")[0] is False # Valley of Death Banned
    assert is_valid_empirical_entry(85, 13, "15m")[0] is False # Peak 3 Maker Only

    # 1-Hour Markets
    assert is_valid_empirical_entry(20, 5, "1h")[0] is True    # Peak 1: Open Discount
    assert is_valid_empirical_entry(25, 30, "1h")[0] is True   # Peak 1b: Mid Discount
    assert is_valid_empirical_entry(45, 30, "1h")[0] is True   # Peak 2: Mid-Hour Value
    assert is_valid_empirical_entry(60, 35, "1h")[0] is True   # Peak 2: Mid-Hour Momentum
    assert is_valid_empirical_entry(50, 15, "1h")[0] is False  # Valley of Death Early Chop
    assert is_valid_empirical_entry(75, 30, "1h")[0] is False  # Valley of Death Fake Favorite
    assert is_valid_empirical_entry(90, 55, "1h")[0] is False  # Peak 3 Maker Only
