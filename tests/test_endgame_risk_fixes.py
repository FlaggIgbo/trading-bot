"""Unit tests for the three Endgame Maker risk protections:
1. Crypto 15M Spot Lead Gate raised to >= 0.40% (eliminating noise flips).
2. Cap Max Entry at <= 89¢ (eliminating the 90¢-93¢ 12:1 tail-risk trap).
3. Removal of 'Hold to Settle' immunity at 12.5m mark (hard cut if loss >= 15¢ or price < 70¢).
"""

from unittest.mock import MagicMock
import pytest
from main import evaluate_favorite_maker_opportunity
from tests.test_reddit_improvements import MockTradingClient


def test_15m_crypto_spot_lead_gate():
    """Verify that 15M crypto contracts require >= 0.40% spot clearance."""
    client = MockTradingClient()
    market = {"ticker": "KXXRP15M-26SEP040830-30"}
    quotes = {"yes_ask": 88, "yes_bid": 84, "no_ask": 16, "no_bid": 12}
    dynamic_filters = {
        "enable_favorite_maker": True,
        "favorite_maker_min_buy": 86,
        "favorite_maker_max_buy": 89,
    }

    # Case A: Spot lead is 0.22% on 15M crypto (similar to today's loss: target $1.4487, spot $1.4519)
    # 0.22% < 0.40% threshold -> Must be rejected!
    target = 1.4487
    spot_weak = target * 1.0022  # +0.22% lead
    opp_weak = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=dynamic_filters,
        client=client,
        spot_price=spot_weak,
        target_price=target,
    )
    assert opp_weak is None

    # Case B: Spot lead is 0.45% on 15M crypto
    # 0.45% >= 0.40% threshold -> Approved!
    spot_strong = target * 1.0045  # +0.45% lead
    opp_strong = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=dynamic_filters,
        client=client,
        spot_price=spot_strong,
        target_price=target,
    )
    assert opp_strong is not None
    assert opp_strong["side"] == "yes"
    assert opp_strong["maker_bid"] == 86


def test_max_entry_capped_at_89_cents():
    """Verify that favorite maker bids are strictly capped at <= 89¢, eliminating the 90¢-93¢ zone."""
    client = MockTradingClient()
    market = {"ticker": "KXBTC15M-HIGH-ASK"}
    # Spread is 91¢ ask / 88¢ bid
    quotes = {"yes_ask": 91, "yes_bid": 88, "no_ask": 12, "no_bid": 9}
    dynamic_filters = {
        "enable_favorite_maker": True,
        "favorite_maker_min_buy": 86,
        "favorite_maker_max_buy": 93,  # Attempting to pass 93 from un-updated config
    }

    # Spot lead 0.50% (sufficient)
    target = 60000.0
    spot = target * 1.0050

    # Since ask is 91¢, normally min(yes_ask - 1, ...) would produce 90¢.
    # With the 89¢ cap, effective_max_fav is 89¢, so the 91¢ ask exceeds 89¢ and is rejected!
    opp_above_89 = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes,
        rem_mins=3.0,
        dynamic_filters=dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_above_89 is None

    # When ask is 89¢ (bid 86¢), it is approved and maker bid is <= 88¢
    quotes_89 = {"yes_ask": 89, "yes_bid": 86, "no_ask": 14, "no_bid": 11}
    opp_at_89 = evaluate_favorite_maker_opportunity(
        market=market,
        quotes=quotes_89,
        rem_mins=3.0,
        dynamic_filters=dynamic_filters,
        client=client,
        spot_price=spot,
        target_price=target,
    )
    assert opp_at_89 is not None
    assert opp_at_89["maker_bid"] <= 89


def test_12_5min_endgame_breakdown_cut_vs_secure_hold():
    """Verify that a favorite maker position is cut at 12.5m if it deteriorates (< 70¢ or loss >= 15¢)."""
    client = MockTradingClient()
    ticker_breakdown = "KXETH15M-BREAKDOWN"

    # 1. Breakdown scenario: Entered at 88¢, now dropped to 65¢ (loss = 23¢ >= 15¢ and price < 70¢)
    client.set_entry_price(ticker_breakdown, price_cents=88, side="yes", count=1, strategy_tag="endgame_favorite_maker")
    client._positions_cache[ticker_breakdown] = {"side": "yes", "count": 1, "entry_price_cents": 88}

    res_cut = client.evaluate_and_execute_12_5min_endgame_check(
        ticker=ticker_breakdown,
        current_yes_price=65,
        current_no_price=35,
        dry_run=False,
        endgame_floor=25,
    )
    assert res_cut["triggered"] is True
    assert res_cut["action"] == "endgame_loss_cut"
    assert res_cut["exit_price"] == 65
    assert res_cut["loss_cents"] == 23
    assert client.get_market_position(ticker_breakdown) is None

    # 2. Secure hold scenario: Entered at 88¢, now at 84¢ (loss = 4¢ < 15¢ and price >= 70¢)
    ticker_secure = "KXETH15M-SECURE"
    client.set_entry_price(ticker_secure, price_cents=88, side="yes", count=1, strategy_tag="endgame_favorite_maker")
    client._positions_cache[ticker_secure] = {"side": "yes", "count": 1, "entry_price_cents": 88}

    res_hold = client.evaluate_and_execute_12_5min_endgame_check(
        ticker=ticker_secure,
        current_yes_price=84,
        current_no_price=16,
        dry_run=False,
        endgame_floor=25,
    )
    assert res_hold["triggered"] is False
    assert res_hold["action"] == "favorite_maker_holding_to_settle"
    assert client.get_market_position(ticker_secure) is not None
