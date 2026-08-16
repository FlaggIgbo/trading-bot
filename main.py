"""
15-Minute Crypto Prediction Trading & Data Pipeline Bot
=========================================================

What is this bot?
------------------------------------------------------
Imagine a prediction game where you can bet on whether cryptocurrency prices (like Bitcoin or Ethereum)
will go UP or DOWN over the next 15 minutes.
- On prediction exchanges (like Kalshi and Gemini), contracts trade between 1¢ and 99¢.
- If you are right when the 15 minutes end, your contract pays out $1.00 (100¢).
- If you are wrong, the contract settles at $0.00 (0¢).

How this bot makes smart decisions:
-----------------------------------
1. The 30-Second Buffer (:00:30, :05:30, :10:30...):
   - At the exact start of every 15 minutes (:00.000), exchanges are busy closing old contracts and opening new ones.
   - If we ask the exchange for prices at :00.000, the orderbook might be empty (returning 'None').
   - By waiting 30 seconds (:00:30), we give market makers time to post real buy and sell prices.

2. Batch Spot Price Lookup (Coinbase Ground Truth):
   - Instead of asking Coinbase 20+ times sequentially for prices, we fetch all crypto spot prices in one batch.
   - This avoids hitting API rate limits and keeps our bot running lightning fast.

3. Spot Momentum Reality Check (Preventing "Cheap Traps"):
   - Just because a 'YES' contract is cheap (e.g. 42¢) doesn't mean it's a good deal.
   - We check Coinbase's real-time asset price against the strike target:
     * If Bitcoin is currently ABOVE the strike target, we only allow 'YES' buys.
     * If Bitcoin is currently BELOW the strike target, we only allow 'NO' buys.
     * If it's a dead heat (within ±0.05%), both sides are permitted.

4. Dynamic Price Ranges & Empirical Calibration (MongoDB Data-Driven):
   - Rather than using static hardcoded prices for all coins, our buy/bid price ranges (min_buy, max_buy)
     and take-profit sell targets (e.g. 76¢–85¢) are dynamically calculated directly from
     historical trade performance, win rates, and volatility data stored in MongoDB Atlas (auto-refreshed every 6 hours).
   - Runway Timing Gating (Recommendation 1):
     * Minute 00 Entry (15m runway): Broad buy corridor up to 52¢ (e.g. 30¢–52¢) to capture high-EV entries.
     * Minute 05 Entry (10m runway): Strictly clamped to <= 38¢ (e.g. 28¢–38¢) to prevent theta decay drag.
   - Low-EV Middle Zone Avoidance (Recommendation 3):
     * The 37¢–43¢ band is dynamically avoided unless Coinbase spot momentum strongly confirms the directional move (> 0.08% in the money).
     * This focuses capital into deep discounts (<= 36¢) and high-conviction momentum entries (44¢–48¢).

5. Asset-Specific Dynamic Risk Management (Stop-Loss, Auto-Sell Take-Profit & 12.5-Min Endgame Check):
   - Dynamic Risk Calibration (Recommendation 2):
     * Bouncy / High-Win Coins (e.g. DOGE, BNB): Given wider breathing room (15¢ SL threshold, 25¢ floor, 34¢ 5-min cut, 22¢ endgame floor) to prevent false-alarm exits on mean-reverting pullbacks.
     * Trending Coins (e.g. BTC, SOL, XRP): Kept on tight, decisive risk controls (10¢ SL threshold, 30¢ floor, 40¢ 5-min cut, 25¢ endgame floor).
     * High-Volatility Coins (e.g. ZEC, NEAR, HYPE): Dynamically given adaptive thresholds (12¢ SL, 26¢ floor, 36¢ 5-min cut, 24¢ endgame floor).
   - Automatic Profit Taking: Resting limit sell orders are placed at dynamic take-profit targets (e.g. 82¢, or 76¢ at 5-min mark).
   - 10-Minute Winner Roll & Stop-Loss (:10 mark):
     * If winning big (>= 70¢ / in profit): Sells at Bid to bank realized cash profit, and immediately re-buys at Ask to reset cost basis and run that contract to 100¢ ($1.00 payout)!
     * If losing, executes dynamic stop-loss liquidation according to the coin's empirical thresholds.
   - 12.5-Minute Endgame Safety Check (:12.5 mark - 2.5m before expiration):
     * Inspects all active holdings. If losing (price < entry_price or price < dynamic endgame floor), liquidates via taker execution to protect banked gains and salvage capital before expiration.

6. Persistent Connections & Resource Protection:
   - Exchange clients and HTTP sessions are initialized ONCE and kept alive across 24/7 background operation.
   - This prevents socket/connection leaks, rate-limit throttling, and unnecessary authentication overhead.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from database import (
    get_dynamic_filter_for_market,
    get_dynamic_price_filter_map,
    is_default_exploration_window,
    save_market_snapshots,
    save_portfolio_pnl,
    save_settlements,
)
from signals.base_client import BaseTradingClient, LogTag

# from signals.gemini_client import GeminiClient
from signals.kalshi_client import KalshiClient

# Set up clean, human-readable logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Supported 15-minute crypto symbols across exchanges
SUPPORTED_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC")

# Persistent HTTP session with connection pooling for Coinbase spot checks
http_session = requests.Session()


def calculate_seconds_until_next_5min_mark(offset_seconds: float = 30.0) -> float:
    """
    Calculates exact seconds to sleep until 30 seconds past the NEXT 5-minute mark (:00:30, :05:30, etc.).

    Safety Guards:
    - Computes target datetime explicitly based on UTC clock.
    - If the calculated wait is under 15 seconds (e.g. if a run finished right around :05:30),
      it advances to the following 5-minute bucket so the bot never double-fires in the same interval.
    """
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    next_5min_bucket = (current_minute // 5 + 1) * 5

    target_minute = next_5min_bucket % 60
    hour_increment = next_5min_bucket // 60

    target_dt = now.replace(
        minute=target_minute,
        second=int(offset_seconds),
        microsecond=0,
    )
    if hour_increment > 0:
        target_dt = target_dt + timedelta(hours=hour_increment)

    wait_seconds = (target_dt - now).total_seconds()

    # Guarantee at least 15 seconds sleep to avoid same-interval re-execution on timing drift
    if wait_seconds < 15.0:
        wait_seconds += 300.0

    return max(15.0, wait_seconds)


def calculate_seconds_until_next_checkpoint(offset_seconds: float = 30.0) -> float:
    """
    Calculates exact seconds to sleep until the NEXT scheduled checkpoint in the 15-minute cycle:
    - Minute :00:30 (Maker Buy Entry & Auto-Sell TP setup)
    - Minute :05:30 (Maker Buy Entry & 5-Min Cut-Loss)
    - Minute :10:30 (10-Min Stop-Loss & Let-Winners-Run TP cancellation)
    - Minute :12:30 (12.5-Min Endgame Check: 2.5m before expiration, sell if losing to salvage capital)
    """
    now = datetime.now(timezone.utc)
    current_minute = now.minute

    # Checkpoint minutes within each 15-minute block
    block_checkpoints = (0, 5, 10, 12)
    block_start_minute = (current_minute // 15) * 15

    candidates = []
    # Generate candidate timestamps across current and next 15m blocks
    for block_offset in (0, 15):
        for cp in block_checkpoints:
            target_min = block_start_minute + block_offset + cp
            target_hour_inc = target_min // 60
            normalized_min = target_min % 60
            dt = now.replace(minute=normalized_min, second=int(offset_seconds), microsecond=0)
            if target_hour_inc > 0:
                dt = dt + timedelta(hours=target_hour_inc)
            candidates.append(dt)

    # Pick the earliest candidate that is at least 15 seconds in the future
    future_candidates = [c for c in candidates if (c - now).total_seconds() >= 15.0]
    next_dt = min(future_candidates) if future_candidates else candidates[-1]

    wait_seconds = (next_dt - now).total_seconds()
    return max(15.0, wait_seconds)


# Alias for backward compatibility
calculate_seconds_until_next_check = calculate_seconds_until_next_checkpoint


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """
    Extracts the short crypto symbol (e.g. 'BTC', 'ETH', 'SOL') from full ticker strings.
    Example: 'KXBTC15M-26AUG141315-15' -> 'BTC', 'btc-updown-15m-1786835700' -> 'BTC'
    """
    # 1. Check ticker prefix if hyphen-separated (e.g. btc-updown-15m-...)
    ticker_prefix = ticker.split("-")[0].upper() if "-" in ticker else ""
    if ticker_prefix in SUPPORTED_SYMBOLS:
        return ticker_prefix

    # 2. Check series ticker directly (e.g. KXBTC15M -> BTC, BTC15M -> BTC)
    series_clean = series_ticker.replace("15M", "").replace("KX", "").upper()
    if series_clean in SUPPORTED_SYMBOLS:
        return series_clean

    # 3. Search with word boundary regex
    combined = f"{series_ticker} {ticker}".upper()
    for sym in SUPPORTED_SYMBOLS:
        if re.search(rf"\b{sym}\b", combined):
            return sym

    return next((sym for sym in SUPPORTED_SYMBOLS if sym in combined), series_ticker or "CRYPTO")


def parse_cents(val_dollars: str | float | None, val_cents: int | str | None = None) -> int | None:
    """
    Safely converts string dollars (e.g. '0.3500') or cent integers (e.g. 35) to an integer cent value (1-99).
    Returns None if the price is missing, 0, or invalid.
    """
    for val, multiplier in ((val_dollars, 100), (val_cents, 1)):
        if val is not None:
            try:
                res = int(round(float(val) * multiplier))
                if 1 <= res <= 99:
                    return res
            except (ValueError, TypeError):
                continue
    return None


def get_crypto_spot_price(symbol: str) -> float | None:
    """
    Fetches real-time underlying crypto spot price from Coinbase public API.
    Uses persistent HTTP connection pooling and handles HTML/JSON errors defensively.
    """
    try:
        resp = http_session.get(
            f"https://api.coinbase.com/v2/prices/{symbol.upper()}-USD/spot",
            timeout=3.5,
        )
        if resp.status_code == 200 and resp.text:
            data = resp.json()
            if isinstance(data, dict) and "data" in data and "amount" in data["data"]:
                return round(float(data["data"]["amount"]), 4)
    except (requests.RequestException, ValueError, json.JSONDecodeError, KeyError) as error:
        log.debug(f"Could not fetch spot price for {symbol}: {error}")
    return None


def batch_fetch_crypto_spot_prices(symbols: tuple[str, ...] | list[str]) -> dict[str, float | None]:
    """
    Fetches spot prices for all unique crypto symbols in a single pass.

    Why batch?
    Instead of making 20+ blocking HTTP calls (one for each market ticker on each exchange),
    we fetch each coin's price once at the start of the 5-minute cycle and cache it in memory.
    """
    spot_cache: dict[str, float | None] = {}
    for sym in set(symbols):
        spot_cache[sym] = get_crypto_spot_price(sym)
    return spot_cache


def is_spot_momentum_aligned(
    side: str,
    spot_price: float | None,
    target_price: float | None,
    otm_pct_threshold: float = 0.05,
) -> bool:
    """
    Spot Momentum Reality Check (Coinbase Spot vs. Exchange Strike Target):
    -----------------------------------------------------------------------
    Prediction contract prices show betting sentiment, but Coinbase spot shows real-world truth.
    We check the real asset price against the strike target before making any trade:
      - Dead-Heat Zone (|pct_dist| <= 0.05%): The price is right on the line; allow either YES or NO.
      - Bullish Momentum (spot >= target): Only permit YES buys (avoids buying cheap NO on a rallying asset).
      - Bearish Momentum (spot < target): Only permit NO buys (avoids buying cheap YES on a falling asset).
    """
    if spot_price is None or target_price is None or target_price <= 0:
        return True

    pct_dist = ((spot_price - target_price) / target_price) * 100.0
    if abs(pct_dist) <= otm_pct_threshold:
        return True

    return (pct_dist >= 0) if side.lower() == "yes" else (pct_dist < 0)


def extract_price_cents(market: dict, side: str, client: BaseTradingClient | None = None) -> int | None:
    """
    Extracts the Ask price in cents (1-99) for either 'yes' or 'no'.
    Falls back to querying the market book if the payload lacks top-of-book quotes.
    """
    prefix = side.lower()
    dollar_str = market.get(f"{prefix}_ask_dollars")
    cent_val = market.get(f"{prefix}_ask")

    # Fallback to market detail / orderbook if quotes are missing from the list response
    if dollar_str is None and cent_val is None and client and market.get("ticker"):
        try:
            detail = client.get_market(market["ticker"]).get("market", {})
            dollar_str = detail.get(f"{prefix}_ask_dollars")
            cent_val = detail.get(f"{prefix}_ask")
        except Exception as err:
            log.debug(f"Fallback get_market failed for {market.get('ticker')}: {err}")

    return parse_cents(dollar_str, cent_val)


def determine_counterfactual_outcome(
    trade_side: str,
    action_type: str,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None = None,
) -> str:
    """
    Determines post-mortem trade outcomes for data analytics and strategy evaluation.
    Tells us if our Stop-Loss or Take-Profit decisions helped or hurt the final result.
    """
    if market_result in ("yes", "no"):
        winning_side = market_result.lower()
    elif spot_price is not None and target_price is not None:
        winning_side = "yes" if spot_price >= target_price else "no"
    else:
        return "pending_settlement"

    won = (trade_side.lower() == winning_side)
    if action_type in ("stop_loss_sold", "early_loss_cut", "profit_floor_sold", "endgame_loss_cut"):
        return "sold_early_would_have_won" if won else "sl_cap_saved"
    if action_type == "winner_rolled_rebuy":
        return "winner_roll_won" if won else "winner_roll_cut_loss"
    if action_type == "take_profit_sold":
        return "tp_full_win" if won else "sold_early_saved_from_loss"

    return "open_position"


def _execute_buy_flow(
    ticker: str,
    side: str,
    price_cents: int,
    maker_bid: int | None,
    min_buy: int,
    max_buy: int,
    take_profit_cents: int,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None,
    client: BaseTradingClient,
    dry_run: bool,
    is_exploration: bool = False,
) -> tuple[bool, dict | None]:
    """Helper function to execute Maker Buy order and setup resting Take-Profit Auto-Sell."""
    side_lower = side.lower()
    side_upper = side.upper()
    ex_name = getattr(client, "exchange_name", "Exchange")
    momentum_label = "Bullish" if side_lower == "yes" else "Bearish"

    log_prefix = LogTag.DRY_RUN_MATCH.value if dry_run else LogTag.BUY_MATCH.value
    regime_tag = " [EXPLORATION REGIME]" if is_exploration else ""
    log_action = (
        f"WOULD BUY 1 SHARE OF [{side_upper}] + SET {take_profit_cents}¢ AUTO-SELL."
        if dry_run
        else f"Executing Maker Buy + {take_profit_cents}¢ Auto-Sell!"
    )
    log.info(
        f"{log_prefix}{regime_tag} [{ex_name}] {ticker} -> {side_upper} price is {price_cents}¢ "
        f"(between {min_buy}¢ and {max_buy}¢ & {momentum_label} Spot Momentum!). {log_action}"
    )

    effective_buy = price_cents
    if not dry_run:
        try:
            buy_res = client.buy_and_set_take_profit(
                ticker=ticker,
                side=side_lower,
                buy_price_cents=price_cents,
                take_profit_cents=take_profit_cents,
                count=1,
                maker_bid_cents=maker_bid,
            )
            effective_buy = buy_res.get("buy_price_cents", price_cents)
        except Exception as error:
            log.error(f"❌ [{ex_name}] Failed to execute {side_upper} trade on {ticker}: {error}")
            return False, None

    trade_details = {
        **({"dry_run": True} if dry_run else {}),
        "ex": ex_name.lower(),
        "action": "buy_opened",
        "regime": "default_window" if is_exploration else "calibrated",
        "side": side_lower,
        "buy": effective_buy,
        "tp": take_profit_cents,
        "sl": 10,
        "outcome": determine_counterfactual_outcome(side_lower, "buy_opened", spot_price, target_price, market_result),
    }
    return True, trade_details


def process_single_market(
    market: dict,
    is_trading_interval: bool,
    client: BaseTradingClient,
    now_utc: datetime,
    minute_past_hour: int,
    spot_cache: dict[str, float | None] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Evaluates pricing, risk rules, trade opportunities, and generates snapshot documents
    for a single 15-minute crypto prediction market.
    """
    ticker = market.get("ticker", "UNKNOWN")
    series_ticker = market.get("series_ticker", "UNKNOWN")
    asset_symbol = extract_asset_symbol(series_ticker, ticker)

    # 1. Parse top-of-book prices and bid-ask spread
    yes_price_cents = extract_price_cents(market, "yes", client=client)
    no_price_cents = extract_price_cents(market, "no", client=client)

    quotes = {
        k: parse_cents(market.get(f"{k}_dollars"), market.get(k))
        for k in ("yes_ask", "yes_bid", "no_ask", "no_bid")
    }
    sprd = (
        (quotes["yes_ask"] - quotes["yes_bid"])
        if (quotes["yes_ask"] is not None and quotes["yes_bid"] is not None)
        else None
    )

    # 2. Extract spot price from cache & strike target metrics
    spot_price = spot_cache.get(asset_symbol) if spot_cache is not None else get_crypto_spot_price(asset_symbol)
    target_price = market.get("floor_strike")

    price_diff, pct_dist = None, None
    if spot_price is not None and target_price is not None:
        try:
            p_float, tgt_float = float(spot_price), float(target_price)
            price_diff = round(p_float - tgt_float, 4)
            if tgt_float > 0:
                pct_dist = round((price_diff / tgt_float) * 100.0, 4)
        except (ValueError, TypeError):
            pass

    # 3. Time decay and market volume
    rem_mins = None
    if close_iso := (market.get("close_time") or market.get("expected_expiration_time")):
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            rem_mins = round(max(0.0, (close_dt - now_utc).total_seconds() / 60.0), 1)
        except Exception:
            pass

    vol_count = None
    if (vol_raw := (market.get("volume_fp") or market.get("volume_24h_fp"))) is not None:
        try:
            vol_count = int(round(float(vol_raw)))
        except (ValueError, TypeError):
            pass

    # 4. Strategy Rules & Dynamic Price Filter Lookup
    # Strategy Concept: Instead of hardcoded one-size-fits-all numbers, we ask
    # our in-memory engine for the optimal buy & sell targets for THIS specific coin and timing mark!
    minute_into_market = minute_past_hour % 15
    is_12_5min_mark = (minute_into_market == 12)
    is_stop_loss_interval = (minute_into_market == 10)
    is_5min_mark = (minute_into_market == 5)

    dynamic_filters = get_dynamic_filter_for_market(asset_symbol, is_5min_mark, now_utc=now_utc)
    min_buy_price = dynamic_filters["min_buy"]
    max_buy_price = dynamic_filters["max_buy"]
    take_profit_cents = dynamic_filters["tp"]
    is_exploration = dynamic_filters.get("is_exploration", False)

    trade_executed = False
    trade_details = None

    # -----------------------------------------------------------------------
    # Step 4A: 12.5-Minute Endgame Safety Check (:12.5 mark - 2.5m before expiration)
    # At minute 12.5: If we are losing (loss > 0 or price < dynamic endgame floor e.g. < 25¢),
    # liquidate immediately so we don't lose all capital gains or suffer a 0¢ expiration loss!
    # If winning, let it run to 100¢ ($1.00 settlement).
    # -----------------------------------------------------------------------
    if is_12_5min_mark:
        sellable_yes_price = quotes["yes_bid"] if quotes["yes_bid"] is not None else yes_price_cents
        sellable_no_price = quotes["no_bid"] if quotes["no_bid"] is not None else no_price_cents
        endgame_floor = dynamic_filters.get("endgame_12_5_floor", 25)

        eg_res = client.evaluate_and_execute_12_5min_endgame_check(
            ticker=ticker,
            current_yes_price=sellable_yes_price,
            current_no_price=sellable_no_price,
            dry_run=dry_run,
            endgame_floor=endgame_floor,
        )
        if isinstance(eg_res, dict) and eg_res.get("triggered"):
            trade_executed = True
            eg_side = eg_res.get("side", "yes")
            ex_name = getattr(client, "exchange_name", "Exchange")
            action_name = eg_res.get("action", "endgame_loss_cut")
            trade_details = {
                "ex": ex_name.lower(),
                "action": action_name,
                "side": eg_side,
                "exit_price": eg_res.get("exit_price"),
                "loss_cents": eg_res.get("loss_cents"),
                "outcome": determine_counterfactual_outcome(
                    eg_side, action_name, spot_price, target_price, market.get("result")
                ),
            }
            if "profit_locked_cents" in eg_res:
                trade_details["profit_locked_cents"] = eg_res["profit_locked_cents"]

    # -----------------------------------------------------------------------
    # Step 4B: 10-Minute Winner-Roll & Stop-Loss Evaluation (:10 mark)
    # At minute 10:
    # 1. Winning positions (>= 70¢ / in profit): SELL at Bid to bank realized profit,
    #    and immediately RE-BUY at Ask price to let that new position run to 100¢ ($1.00)!
    # 2. Losing positions: Execute immediate stop-loss liquidation.
    # -----------------------------------------------------------------------
    elif is_stop_loss_interval:
        sellable_yes_price = quotes["yes_bid"] if quotes["yes_bid"] is not None else yes_price_cents
        sellable_no_price = quotes["no_bid"] if quotes["no_bid"] is not None else no_price_cents

        sl_threshold = dynamic_filters.get("sl_threshold", 10)
        sl_floor = dynamic_filters.get("sl_floor", 30)

        sl_res = client.evaluate_and_execute_10min_strategy(
            ticker=ticker,
            current_yes_bid=sellable_yes_price,
            current_no_bid=sellable_no_price,
            current_yes_ask=quotes.get("yes_ask"),
            current_no_ask=quotes.get("no_ask"),
            dry_run=dry_run,
            stop_loss_threshold_cents=sl_threshold,
            price_floor=sl_floor,
        )
        if isinstance(sl_res, dict) and sl_res.get("triggered"):
            trade_executed = True
            sl_side = sl_res.get("side", "yes")
            ex_name = getattr(client, "exchange_name", "Exchange")
            action_name = sl_res.get("action", "stop_loss_sold")
            trade_details = {
                "ex": ex_name.lower(),
                "action": action_name,
                "side": sl_side,
                "exit_price": sl_res.get("exit_price"),
                "outcome": determine_counterfactual_outcome(
                    sl_side, action_name, spot_price, target_price, market.get("result")
                ),
            }
            if "loss_cents" in sl_res:
                trade_details["loss_cents"] = sl_res["loss_cents"]
            if "profit_locked_cents" in sl_res:
                trade_details["profit_locked_cents"] = sl_res["profit_locked_cents"]
            if "rebuy_price_cents" in sl_res:
                trade_details["rebuy_price_cents"] = sl_res["rebuy_price_cents"]

    # -----------------------------------------------------------------------
    # Step 4C: Buy Strategy & 5-Min Early Cut-Loss Check (:00, :05, :15, :20...)
    # Exclusively gated: We do NOT evaluate new buys if Stop-Loss just liquidated this market.
    # -----------------------------------------------------------------------
    elif is_trading_interval:
        if not dry_run:
            client.cancel_unfilled_buy_orders(ticker)

        cut_loss_executed = False
        pos = client.get_market_position(ticker)

        # 5-Minute Early Cut-Loss Check: Evaluate all active positions at the 5-minute mark
        if pos is not None and is_5min_mark:
            side = pos["side"]
            cut_threshold = dynamic_filters.get("cut_5min", 40)
            cut_res = client.evaluate_and_execute_5min_cut_loss(
                ticker=ticker,
                current_yes_price=yes_price_cents,
                current_no_price=no_price_cents,
                min_price_threshold=cut_threshold,
                dry_run=dry_run,
            )
            if isinstance(cut_res, dict) and cut_res.get("triggered"):
                trade_executed = True
                ex_name = getattr(client, "exchange_name", "Exchange")
                trade_details = {
                    "ex": ex_name.lower(),
                    "action": "early_loss_cut",
                    "side": side,
                    "exit_price": cut_res.get("exit_price"),
                    "loss_cents": cut_res.get("loss_cents"),
                    "outcome": determine_counterfactual_outcome(
                        side, "early_loss_cut", spot_price, target_price, market.get("result")
                    ),
                }
                pos = None
                cut_loss_executed = True

        # Position Gates & Entry Evaluation
        ex_name = getattr(client, "exchange_name", "Exchange")
        has_floor = getattr(client, "has_profit_floor", lambda t: False)(ticker)
        if cut_loss_executed:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] 5-Min Cut-Loss executed for {ticker}. Skipping buy evaluation.")
        elif pos is not None:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Already hold active {pos['side'].upper()} pos of {pos['count']} contract(s) for {ticker}. Skipping buy.")
            if not is_stop_loss_interval and not client.has_resting_take_profit_order(ticker) and not has_floor and not dry_run:
                client.place_take_profit_order(
                    ticker=ticker,
                    side=pos["side"],
                    count=pos["count"],
                    take_profit_cents=take_profit_cents,
                )
        elif client.has_existing_position_or_order(ticker):
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Already hold an active position or resting order for {ticker}. Skipping buy.")
        else:
            # Screen for candidates within target buy price, avoiding low-EV middle dead zone, and aligned with Spot Momentum
            avoid_mid_zone = dynamic_filters.get("avoid_mid_zone", True)
            mid_min = dynamic_filters.get("mid_zone_min", 37)
            mid_max = dynamic_filters.get("mid_zone_max", 43)

            candidate_sides = []
            for side, p_cents, aligned in [
                ("yes", yes_price_cents, is_spot_momentum_aligned("yes", spot_price, target_price)),
                ("no", no_price_cents, is_spot_momentum_aligned("no", spot_price, target_price)),
            ]:
                if p_cents is not None and min_buy_price <= p_cents <= max_buy_price and aligned:
                    # Recommendation 3: Avoid 37¢-43¢ middle zone unless spot momentum is strong
                    in_mid_zone = (mid_min <= p_cents <= mid_max)
                    if avoid_mid_zone and in_mid_zone:
                        # Check if spot is strongly in the money (> 0.08% dist)
                        pct_dist_val = 0.0
                        if spot_price and target_price and target_price > 0:
                            pct_dist_val = ((spot_price - target_price) / target_price) * 100.0

                        is_strong_momentum = (pct_dist_val >= 0.08) if side == "yes" else (pct_dist_val <= -0.08)
                        if not is_strong_momentum:
                            log.info(
                                f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢) "
                                f"falls in low-EV middle band ({mid_min}¢-{mid_max}¢) without strong spot edge. Skipping."
                            )
                            continue

                    candidate_sides.append((side, p_cents))

            if candidate_sides:
                chosen_side, chosen_price = min(candidate_sides, key=lambda x: x[1])
                chosen_bid = quotes["yes_bid"] if chosen_side == "yes" else quotes["no_bid"]
                trade_executed, trade_details = _execute_buy_flow(
                    ticker,
                    chosen_side,
                    chosen_price,
                    chosen_bid,
                    min_buy_price,
                    max_buy_price,
                    take_profit_cents,
                    spot_price,
                    target_price,
                    market.get("result"),
                    client,
                    dry_run,
                    is_exploration=is_exploration,
                )

    # 5. Build Compact Snapshot Document (~100 bytes for storage efficiency)
    compact_document = {
        "ts": int(now_utc.timestamp()),
        "m": minute_past_hour,
        "sym": asset_symbol,
        "tick": ticker,
        "ex": market.get("exchange", "kalshi"),
        "y": yes_price_cents,
        "n": no_price_cents,
        "y_bid": quotes["yes_bid"],
        "y_ask": quotes["yes_ask"],
        "n_bid": quotes["no_bid"],
        "n_ask": quotes["no_ask"],
        "sprd": sprd,
        "p": spot_price,
        "tgt": target_price,
        "diff": price_diff,
        "pct": pct_dist,
        "rem": rem_mins,
        "vol": vol_count,
    }

    if trade_executed and trade_details:
        compact_document["trd"] = trade_details

    return compact_document


def run_pipeline_pass(
    clients: list[tuple[str, BaseTradingClient]] | None = None,
    dry_run: bool = False,
    force_trade_mark: bool = False,
):
    """
    Executes a single market processing & trading pass across all active exchanges.
    Accepts long-lived exchange clients to prevent socket leaks and connection overhead.
    """
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute

    minute_into_market = current_minute % 15
    is_trading_interval = (minute_into_market in (0, 5)) or force_trade_mark
    is_10min_interval = (minute_into_market == 10) and not force_trade_mark
    is_12_5min_interval = (minute_into_market == 12) and not force_trade_mark

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {now_utc.isoformat()} (Minute :{current_minute:02d}:{now_utc.second:02d})")
    if dry_run:
        log.info(f"--> MODE: {LogTag.DRY_RUN_PREVIEW.value} (No real orders will be placed)")
    if is_default_exploration_window(now_utc):
        log.info("--> REGIME: 🌍 [EXPLORATION REGIME] Active UTC window 05:00–07:00 (Midnight–2AM ET): Trading on default broad corridors (:00 -> 30¢–52¢, :05 -> 28¢–38¢) for empirical discovery.")
    log.info(
        "--> INTERVAL: "
        + ("BUY TRADING & 5-MIN CUT-LOSS / DATA COLLECTION MARK (:00, :05, :15, :20...)" if is_trading_interval
           else "10-MIN STOP-LOSS & LET-RUN EVALUATION MARK (:10, :25, :40, :55)" if is_10min_interval
           else "12.5-MIN ENDGAME SAFETY CHECK MARK (:12.5, :27.5, :42.5, :57.5)" if is_12_5min_interval
           else "DATA COLLECTION ONLY MARK")
    )
    log.info("=================================================================")

    # Ensure dynamic price filters are in memory (auto-refreshes every 6 hours, at 07:00 UTC anchor, or on cold start)
    get_dynamic_price_filter_map(now_utc=now_utc)

    # Initialize fallback clients if persistent instances were not passed
    active_clients = []
    if clients is None:
        try:
            active_clients.append(("Kalshi", KalshiClient()))
        except Exception as error:
            log.error(f"Could not initialize KalshiClient: {error}")

        # Gemini trading disabled / commented out:
        # try:
        #     active_clients.append(("Gemini", GeminiClient()))
        # except Exception as error:
        #     log.error(f"Could not initialize GeminiClient: {error}")
    else:
        active_clients = clients

    if not active_clients:
        log.error("No active exchange clients initialized. Exiting pipeline pass.")
        return

    # Batch-fetch all underlying spot prices from Coinbase once per cycle
    spot_price_cache = batch_fetch_crypto_spot_prices(SUPPORTED_SYMBOLS)

    snapshots_to_save = []
    for exchange_name, client in active_clients:
        log.info(f"\n--- [{exchange_name.upper()} EXCHANGE PIPELINE] ---")

        # Fetch real-time Portfolio PnL & Balance
        try:
            pnl = client.get_portfolio_pnl_summary()
            pnl["ts"] = int(now_utc.timestamp())
            pnl["exchange"] = exchange_name.lower()
            sign = "+" if pnl.get("total_realized_pnl_dollars", 0) >= 0 else ""

            log.info(
                f"{LogTag.PORTFOLIO_PNL.value} [{exchange_name}] Cash: ${pnl.get('balance_dollars', 0.0):.2f} | "
                f"Port Value: ${pnl.get('portfolio_value_dollars', 0.0):.2f} | "
                f"Settled PnL: {sign}${pnl.get('total_realized_pnl_dollars', 0.0):.2f} "
                f"({sign}{pnl.get('total_realized_pnl_cents', 0)}¢ across {pnl.get('settlements_count', 0)} mkts) | "
                f"Active: {pnl.get('open_positions_count', 0)} pos (${pnl.get('open_exposure_dollars', 0.0):.2f})"
            )

            if not dry_run:
                save_portfolio_pnl(pnl)
                if pnl.get("settlements"):
                    save_settlements(pnl["settlements"])
        except Exception as err:
            log.warning(f"Could not retrieve portfolio PnL summary from {exchange_name}: {err}")

        log.info(f"Fetching active 15-minute crypto markets from {exchange_name}...")
        try:
            markets = client.get_15m_crypto_markets(status="open")
            log.info(f"Retrieved {len(markets)} active 15-minute crypto markets from {exchange_name}.")
        except Exception as error:
            log.error(f"Failed to fetch 15-minute crypto markets from {exchange_name}: {error}")
            continue

        for market in markets:
            snapshot = process_single_market(
                market,
                is_trading_interval,
                client,
                now_utc,
                current_minute,
                spot_cache=spot_price_cache,
                dry_run=dry_run,
            )
            snapshot["ex"] = exchange_name.lower()
            snapshots_to_save.append(snapshot)

            diff_val = snapshot.get("diff")
            diff_str = f"+{diff_val}" if (diff_val is not None and diff_val > 0) else str(diff_val)
            log.info(
                f"  • [{exchange_name}] Market [{snapshot['sym']} | {snapshot['tick']}] -> "
                f"YES: {snapshot['y']}¢ | NO: {snapshot['n']}¢ | "
                f"Price: ${snapshot.get('p')} | Target: ${snapshot.get('tgt')} | Diff: {diff_str}"
            )

    # Save valid market snapshots to MongoDB Atlas
    # We save snapshots on regular checkpoints (:00, :05, :10, :12.5) OR whenever a trade occurs to keep database tiny
    is_standard_save_mark = (minute_into_market in (0, 5, 10, 12))
    valid_snapshots = [
        s for s in snapshots_to_save
        if (s.get("y") is not None or s.get("n") is not None) and (is_standard_save_mark or s.get("trd") is not None)
    ]
    if valid_snapshots and not dry_run:
        log.info("\nSaving ultra-compact market snapshot data to MongoDB Atlas...")
        save_market_snapshots(valid_snapshots)
    elif dry_run:
        log.info(f"\n{LogTag.DRY_RUN_PREVIEW.value} Skipped saving snapshots to MongoDB Atlas in Dry-Run mode.")
    elif not is_standard_save_mark:
        log.info("\nInterim data-collection pass complete (compact storage policy: snapshots saved at :00, :05, :10, :12.5 marks).")
    else:
        log.warning("\nNo valid price snapshots retrieved in this pass. Skipping MongoDB Atlas save.")
    log.info("Pass complete.")


def main():
    """Main continuous background worker loop."""
    args = sys.argv[1:]
    dry_run = "--dry-run" in args or "-d" in args
    force_trade_mark = "--force-trade" in args or "-f" in args
    run_once = "--once" in args or "-o" in args

    # Initialize long-lived persistent exchange clients ONCE at startup
    persistent_clients = []
    try:
        persistent_clients.append(("Kalshi", KalshiClient()))
    except Exception as error:
        log.error(f"Could not initialize persistent KalshiClient: {error}")

    # Gemini trading disabled / commented out:
    # try:
    #     persistent_clients.append(("Gemini", GeminiClient()))
    # except Exception as error:
    #     log.error(f"Could not initialize persistent GeminiClient: {error}")

    if run_once or dry_run:
        run_pipeline_pass(clients=persistent_clients, dry_run=dry_run, force_trade_mark=force_trade_mark)
        return

    log.info("Starting Trading Bot in Continuous Render Background Worker Mode...")

    while True:
        try:
            run_pipeline_pass(clients=persistent_clients, dry_run=dry_run, force_trade_mark=force_trade_mark)
        except Exception as error:
            log.error(f"Unexpected error in loop pass: {error}")

        sleep_seconds = calculate_seconds_until_next_checkpoint(offset_seconds=30.0)
        target_utc = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
        log.info(
            f"\n{LogTag.CLOCK_ALIGNMENT.value} Sleeping {sleep_seconds:.1f}s until next checkpoint at UTC {target_utc.strftime('%H:%M:%S')}...\n"
        )
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
