"""
Test Suite for Titanium Conviction Tier (90¢–92¢) in Favorite Maker Strategy
----------------------------------------------------------------------------
Verifies:
1. Approved Titanium YES & NO entries within 90¢–92¢ corridor.
2. Strict Hard Ceiling Gate: ALL entries >= 93¢ (93¢, 94¢, 95¢, 98¢) are strictly blocked.
3. Ultra-Strict Spot Lead Gate: >= 0.60% (15m crypto), >= 0.40% (1h crypto), >= 0.80% (commodities).
4. Ultra-Tight Runway Gate: rem_mins <= 4.0m (15m), rem_mins <= 10.0m (1h).
5. Tight Spread Gate: spread_cents <= 4¢ strictly enforced.
6. Conservative Sizing: strictly 1 contract max (no compounding even with large balance / high WR).
7. Mandatory Spot Confirmation: cannot enter Titanium tier blind without spot & target prices.
8. Gemini Preservation: 4-Vector Rule strictly preserves <= 81¢ cap (Titanium blocked on Gemini).
9. Execution Flow: _execute_favorite_maker_flow tags as titanium_favorite_maker and titanium_conviction.
"""

import pytest
from main import evaluate_favorite_maker_opportunity, _execute_favorite_maker_flow


class MockTitaniumClient:
    def __init__(self, exchange_name="kalshi"):
        self.exchange_name = exchange_name
        self.buys = []
        self.fav_buys = []
        self.positions = {}
        self._positions_cache = {}
        self.paper_positions = []
        self.entry_prices = {}

    def has_existing_position_or_order(self, ticker: str) -> bool:
        return ticker in self.positions or ticker in self._positions_cache

    def is_side_allowed(self, ticker: str, side: str) -> bool:
        return True

    def get_balance_cents(self) -> int:
        return 50000  # $500 balance (would normally qualify for maximum contract compounding)

    def buy_favorite_maker_order(self, ticker: str, side: str, maker_bid_cents: int, count: int = 1, **kw):
        res = {
            "ticker": ticker,
            "side": side,
            "buy_price_cents": maker_bid_cents,
            "count": count,
            "client_order_id": f"ord_{ticker}",
            "order_id": f"oid_{ticker}",
        }
        self.fav_buys.append(res)
        return res

    def set_entry_price(self, ticker: str, price_cents: int, side: str, count: int = 1, **kw):
        self.entry_prices[ticker] = {
            "price_cents": price_cents,
            "side": side,
            "count": count,
            **kw,
        }

    def record_paper_position(self, ticker: str, side: str, count: int, entry_price_cents: int, **kw):
        self.paper_positions.append({
            "ticker": ticker,
            "side": side,
            "count": count,
            "entry_price_cents": entry_price_cents,
            **kw,
        })


@pytest.fixture
def default_dynamic_filters():
    return {
        "enable_favorite_maker": True,
        "favorite_maker_min_buy": 86,
        "favorite_maker_max_buy": 89,  # Standard default from database
        "enable_titanium_tier": True,
        "recent_win_rate": 0.95,  # High win rate
        "contracts_per_trade": 1,
    }


def test_titanium_yes_entry_approved(default_dynamic_filters):
    """Verify 91¢ YES favorite is approved when meeting all Titanium criteria."""
    client = MockTitaniumClient()
    market = {"ticker": "KXBTC15M-26AUG170200-00", "series_ticker": "KXBTC15M"}
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}  # Spread = 3¢ (<= 4¢)

    target = 60000.0
    spot = target * 1.0065  # +0.65% spot lead (>= 0.60%)

    opp = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,  # <= 4.0m
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )

    assert opp is not None
    assert opp["side"] == "yes"
    assert opp["target_ask"] == 91
    assert opp["maker_bid"] == 89  # max(88+1, 91-2) = 89
    assert opp["favorite_type"] == "titanium_favorite_yes"
    assert opp["count"] == 1  # Strictly 1 contract (no compounding)


def test_titanium_no_entry_approved(default_dynamic_filters):
    """Verify 92¢ NO favorite is approved when meeting all Titanium criteria."""
    client = MockTitaniumClient()
    market = {"ticker": "KXETH15M-26AUG170200-00", "series_ticker": "KXETH15M"}
    quotes = {"yes_ask": 11, "yes_bid": 8, "no_ask": 92, "no_bid": 89}  # Spread = 3¢ (<= 4¢)

    target = 3000.0
    spot = target * (1.0 - 0.0070)  # +0.70% spot lead for NO (>= 0.60%)

    opp = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=2.5,  # <= 4.0m
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )

    assert opp is not None
    assert opp["side"] == "no"
    assert opp["target_ask"] == 92
    assert opp["maker_bid"] == 90  # max(89+1, 92-2) = 90
    assert opp["favorite_type"] == "titanium_favorite_no"
    assert opp["count"] == 1  # Strictly 1 contract


def test_hard_ceiling_strictly_blocks_at_or_above_93(default_dynamic_filters):
    """Contracts with ask >= 93¢ or maker_bid >= 93¢ must be strictly blocked under all circumstances."""
    client = MockTitaniumClient()
    market = {"ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M"}
    target = 60000.0
    spot = target * 1.0150  # Huge +1.50% spot lead

    for bad_ask in [93, 94, 95, 96, 98]:
        quotes = {"yes_ask": bad_ask, "yes_bid": bad_ask - 1, "no_ask": 100 - bad_ask + 1, "no_bid": 100 - bad_ask}
        opp = evaluate_favorite_maker_opportunity(
            market=market,
            quotes=quotes,
            rem_mins=2.0,
            dynamic_filters=default_dynamic_filters,
            client=client,
            spot_price=spot,
            target_price=target,
        )
        assert opp is None, f"Contract with ask {bad_ask}¢ must be strictly blocked by Hard Ceiling Gate!"

    # Also test NO branch
    for bad_ask in [93, 94, 95, 99]:
        quotes = {"yes_ask": 100 - bad_ask, "yes_bid": 100 - bad_ask - 1, "no_ask": bad_ask, "no_bid": bad_ask - 1}
        opp_no = evaluate_favorite_maker_opportunity(
            market=market,
            quotes=quotes,
            rem_mins=2.0,
            dynamic_filters=default_dynamic_filters,
            client=client,
            spot_price=target * 0.985,
            target_price=target,
        )
        assert opp_no is None, f"Contract with NO ask {bad_ask}¢ must be strictly blocked by Hard Ceiling Gate!"


def test_titanium_spot_lead_gate_15m_crypto(default_dynamic_filters):
    """15M crypto requires >= 0.60% spot lead for Titanium tier."""
    client = MockTitaniumClient()
    market = {"ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M"}
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}
    target = 60000.0

    # 1. Spot lead 0.50% (< 0.60%) -> Rejected!
    opp_insufficient = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=target * 1.0050,
        target_price=target,
    )
    assert opp_insufficient is None

    # 2. Spot lead 0.61% (>= 0.60%) -> Approved!
    opp_sufficient = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=target * 1.0061,
        target_price=target,
    )
    assert opp_sufficient is not None
    assert opp_sufficient["favorite_type"] == "titanium_favorite_yes"


def test_titanium_spot_lead_gate_1h_crypto(default_dynamic_filters):
    """1H crypto requires >= 0.40% spot lead for Titanium tier."""
    client = MockTitaniumClient()
    market = {"ticker": "KXBTCH-TEST", "series_ticker": "KXBTCH"}
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}
    target = 60000.0

    # 1. Spot lead 0.30% (< 0.40%) -> Rejected!
    opp_insufficient = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=5.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=target * 1.0030,
        target_price=target,
    )
    assert opp_insufficient is None

    # 2. Spot lead 0.42% (>= 0.40%) -> Approved!
    opp_sufficient = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=5.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=target * 1.0042,
        target_price=target,
    )
    assert opp_sufficient is not None


def test_titanium_spot_lead_gate_commodities(default_dynamic_filters):
    """Commodities (WTI, GOLD, SILVER) require >= 0.80% spot lead for Titanium tier."""
    client = MockTitaniumClient()
    market = {"ticker": "KXWTI15M-TEST", "series_ticker": "KXWTI15M"}
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}
    target = 70.0

    # 1. Spot lead 0.65% (< 0.80%) -> Rejected!
    opp_insufficient = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=target * 1.0065,
        target_price=target,
    )
    assert opp_insufficient is None

    # 2. Spot lead 0.85% (>= 0.80%) -> Approved!
    opp_sufficient = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=target * 1.0085,
        target_price=target,
    )
    assert opp_sufficient is not None


def test_titanium_runway_gate_15m_and_1h(default_dynamic_filters):
    """Titanium tier enforces rem_mins <= 4.0m on 15m and <= 10.0m on 1h."""
    client = MockTitaniumClient()
    target = 60000.0
    spot = target * 1.0075  # High lead
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}

    # 15m market: rem_mins 4.5m (> 4.0m) -> Rejected!
    market_15m = {"ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M"}
    opp_15m_too_early = evaluate_favorite_maker_opportunity(
        market=market_15m,
        quotes=quotes,
        rem_mins=4.5,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_15m_too_early is None

    # 15m market: rem_mins 3.5m (<= 4.0m) -> Approved!
    opp_15m_ok = evaluate_favorite_maker_opportunity(
        market=market_15m,
        quotes=quotes,
        rem_mins=3.5,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_15m_ok is not None

    # 1h market: rem_mins 12.0m (> 10.0m) -> Rejected!
    market_1h = {"ticker": "KXBTCH-TEST", "series_ticker": "KXBTCH"}
    opp_1h_too_early = evaluate_favorite_maker_opportunity(
        market=market_1h,
        quotes=quotes,
        rem_mins=12.0,
        max_rem_mins=25.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_1h_too_early is None

    # 1h market: rem_mins 8.0m (<= 10.0m) -> Approved!
    opp_1h_ok = evaluate_favorite_maker_opportunity(
        market=market_1h,
        quotes=quotes,
        rem_mins=8.0,
        max_rem_mins=25.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_1h_ok is not None


def test_titanium_spread_gate_max_4_cents(default_dynamic_filters):
    """Titanium tier strictly rejects spreads > 4¢."""
    client = MockTitaniumClient()
    market = {"ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M"}
    target = 60000.0
    spot = target * 1.0080

    # Spread 5¢ (Ask 91, Bid 86) -> Rejected!
    quotes_spread_5 = {"yes_ask": 91, "yes_bid": 86, "no_ask": 14, "no_bid": 9}
    assert evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_spread_5,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    ) is None

    # Spread 4¢ (Ask 91, Bid 87) -> Approved!
    quotes_spread_4 = {"yes_ask": 91, "yes_bid": 87, "no_ask": 13, "no_bid": 9}
    opp_4 = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_spread_4,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_4 is not None

    # Spread 2¢ (Ask 91, Bid 89) -> Approved!
    quotes_spread_2 = {"yes_ask": 91, "yes_bid": 89, "no_ask": 11, "no_bid": 9}
    opp_2 = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_spread_2,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_2 is not None


def test_titanium_conservative_single_contract_sizing(default_dynamic_filters):
    """Titanium tier forces strictly 1 contract max regardless of bankroll or win rate."""
    client = MockTitaniumClient()
    client.get_balance_cents = lambda: 500000  # $5,000 balance!
    target = 60000.0
    spot = target * 1.0080

    # 1. Standard 88¢ favorite scales to 4 contracts due to large balance and 95% WR
    quotes_88 = {"yes_ask": 88, "yes_bid": 85, "no_ask": 15, "no_bid": 12}
    opp_std = evaluate_favorite_maker_opportunity(
        market={"ticker": "KXBTC15M-STD", "series_ticker": "KXBTC15M"},
        quotes=quotes_88,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_std is not None
    assert opp_std["count"] == 4

    # 2. Titanium 91¢ favorite strictly sizes at 1 contract
    quotes_91 = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}
    opp_titanium = evaluate_favorite_maker_opportunity(
        market={"ticker": "KXBTC15M-TITANIUM", "series_ticker": "KXBTC15M"},
        quotes=quotes_91,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_titanium is not None
    assert opp_titanium["count"] == 1


def test_titanium_spot_confirmation_mandatory(default_dynamic_filters):
    """Titanium tier rejects entry if spot_price or target_price is None or missing."""
    client = MockTitaniumClient()
    market = {"ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M"}
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}

    # Missing spot_price
    assert evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=None,
        target_price=60000.0,
    ) is None

    # Missing target_price
    assert evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=client,
        spot_price=60500.0,
        target_price=None,
    ) is None


def test_gemini_preserves_81_cent_ceiling_and_blocks_titanium(default_dynamic_filters):
    """Gemini exchange is strictly capped at <= 81¢ and blocks Titanium tier."""
    gemini_client = MockTitaniumClient(exchange_name="gemini")
    market = {"ticker": "GEMI-BTC-TEST", "exchange": "gemini"}
    target = 60000.0
    spot = target * 1.0100

    # 91¢ on Gemini -> strictly rejected
    quotes_91 = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}
    opp_gemini_91 = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_91,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=gemini_client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_gemini_91 is None

    # 80¢ on Gemini -> accepted within [74¢, 81¢] corridor
    quotes_80 = {"yes_ask": 80, "yes_bid": 78, "no_ask": 22, "no_bid": 20}
    opp_gemini_80 = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_80,
        rem_mins=3.0,
        dynamic_filters=default_dynamic_filters,
        client=gemini_client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_gemini_80 is not None
    assert opp_gemini_80["maker_bid"] <= 81


def test_execute_favorite_maker_flow_titanium_metadata_and_ceiling():
    """_execute_favorite_maker_flow correctly tags Titanium trades and blocks >= 93¢."""
    client = MockTitaniumClient()
    ticker = "KXBTC15M-TITANIUM"

    # 1. Block >= 93¢ defense-in-depth
    executed, details = _execute_favorite_maker_flow(
        ticker=ticker,
        side="yes",
        maker_bid=93,
        target_ask=94,
        spot_price=60500.0,
        target_price=60000.0,
        market_result=None,
        client=client,
        dry_run=False,
    )
    assert executed is False
    assert details is None

    # 2. Approved Titanium execution (maker_bid 90, target_ask 91)
    executed_ok, details_ok = _execute_favorite_maker_flow(
        ticker=ticker,
        side="yes",
        maker_bid=90,
        target_ask=91,
        spot_price=60500.0,
        target_price=60000.0,
        market_result=None,
        client=client,
        dry_run=False,
        count=1,
        favorite_type="titanium_favorite_yes",
        rem_mins=3.2,
    )
    assert executed_ok is True
    assert details_ok["is_titanium"] is True
    assert details_ok["strategy_tag"] == "titanium_favorite_maker"
    assert details_ok["corridor"] == "titanium_conviction"
    assert details_ok["favorite_type"] == "titanium_favorite_yes"

    # Verify enriched entry price persistence
    saved_meta = client.entry_prices.get(ticker)
    assert saved_meta is not None
    assert saved_meta["strategy_tag"] == "titanium_favorite_maker"
    assert saved_meta["regime"] == "titanium_conviction"
    assert saved_meta["seconds_remaining"] == 192.0  # 3.2 * 60
