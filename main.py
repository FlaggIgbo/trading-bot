"""
15-Minute & Hourly Crypto Prediction Trading & Data Pipeline Bot
=================================================================

What is this bot?
------------------------------------------------------
A high-performance algorithmic trading and empirical microstructure data engine for
Kalshi crypto prediction markets across 15-minute and Hourly multi-strike events.
Contracts settle at either $1.00 (100¢) if YES occurs, or $0.00 if NO occurs.

Core Architecture & Risk Engines:
----------------------------------
1. Synchronized Checkpoint Execution (:00:30, :05:30, :10:30, :12:30, :20:30, :40:30, :50:30):
   - Waits 30s past clock intervals to allow market-maker books to establish depth and liquidity.
2. Concurrent Thread-Pooled Spot Verification:
   - Queries Coinbase spot prices for all 12 crypto assets in parallel via ThreadPoolExecutor (<300ms total).
3. Dynamic 6-Hour Database Calibration Engine:
   - Dynamically computes volatility, empirical win rates, and optimal price bands from MongoDB Atlas.
4. Hourly Multi-Strike Selection & Tiebreakers:
   - Scans all strike options, picking cheapest YES and cheapest NO within valid corridors.
   - Dual buys on different strikes; resolves ties on same contract by price and spot momentum.
5. Crash-Proof Persistent Trade State:
   - Stores active position cost bases in MongoDB Atlas `active_positions` to survive container restarts.
6. Error-Isolated Market Boundaries:
   - Wraps each individual market and symbol group in defensive error handlers so one payload glitch never halts the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Default expiration in seconds for Maker limit buy orders to protect against adverse price drops
BUY_ORDER_EXPIRATION_SECONDS = int(os.getenv("BUY_ORDER_EXPIRATION_SECONDS", "45"))

# Persistent HTTP session with connection pooling sized for parallel thread queries
http_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)


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


def calculate_seconds_until_next_checkpoint(open_delay_seconds: float = 30.0, offset_seconds: float | None = None) -> float:
    """
    Calculates exact seconds to sleep until the NEXT scheduled checkpoint:
    
    15-Minute Market Checkpoints (Every 15m block):
    - Minute :00:30, :15:30, :30:30, :45:30 (Open Entry, +30s open delay to allow bets to settle)
    - Minute :02:30, :17:30, :32:30, :47:30 (2.5m Disaster Cut-Loss & 2nd Chance Entry)
    - Minute :05:00, :20:00, :35:00, :50:00 (5m Cut-Loss & Tight Entry, exact timestamp without buffer)
    - Minute :07:30, :22:30, :37:30, :52:30 (7.5m Mid-Candle Cut-Loss & Tight Entry)
    - Minute :10:00, :25:00, :40:00, :55:00 (10m Stop-Loss & Winner-Roll, exact timestamp without buffer)
    - Minute :12:30, :27:30, :42:30, :57:30 (12.5m Endgame Check: 2.5m before expiration)

    Hourly Multi-Strike Market Checkpoints (Every 10m past the hour):
    - Minute :00:30 (Open Multi-Strike Entry, +30s open delay)
    - Minute :10:00 (10m Disaster Cut-Loss & 2nd Chance Entry, exact timestamp)
    - Minute :20:00 (20m Cut-Loss & Tighter Entry, exact timestamp)
    - Minute :30:00 (30m Mid-Hour Cut-Loss & Tighter Entry, exact timestamp without delay)
    - Minute :40:00 (40m Winner-Roll & Stop-Loss, exact timestamp)
    - Minute :50:00 (50m Endgame Check, exact timestamp)
    """
    if offset_seconds is not None:
        open_delay_seconds = offset_seconds

    now = datetime.now(timezone.utc)

    # Complete 60-minute scheduled checkpoints (minute, second)
    open_delay = int(open_delay_seconds)
    hourly_checkpoints = (
        (0, open_delay),   # 15M :00 open (+30s), 1H :00 open (+30s)
        (2, 30),           # 15M :02:30
        (5, 0),            # 15M :05:00
        (7, 30),           # 15M :07:30
        (10, 0),           # 15M :10:00, 1H :10:00
        (12, 30),          # 15M :12:30
        (15, open_delay),  # 15M :15:30 open (+30s)
        (17, 30),          # 15M :17:30
        (20, 0),           # 15M :20:00, 1H :20:00
        (22, 30),          # 15M :22:30
        (25, 0),           # 15M :25:00
        (27, 30),          # 15M :27:30
        (30, 0),           # 1H :30:00 mid-hour check (exact timestamp, NO delay)
        (30, open_delay),  # 15M :30:30 open (+30s)
        (32, 30),          # 15M :32:30
        (35, 0),           # 15M :35:00
        (37, 30),          # 15M :37:30
        (40, 0),           # 15M :40:00, 1H :40:00
        (42, 30),          # 15M :42:30
        (45, open_delay),  # 15M :45:30 open (+30s)
        (47, 30),          # 15M :47:30
        (50, 0),           # 15M :50:00, 1H :50:00
        (52, 30),          # 15M :52:30
        (55, 0),           # 15M :55:00
        (57, 30),          # 15M :57:30
    )

    candidates = []
    # Generate candidate timestamps across current hour and next hour
    for hour_offset in (0, 1):
        base_hour_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour_offset)
        for cp_min, cp_sec in hourly_checkpoints:
            dt = base_hour_dt.replace(minute=cp_min, second=cp_sec)
            candidates.append(dt)

    # Pick the earliest candidate that is at least 10 seconds in the future
    future_candidates = [c for c in candidates if (c - now).total_seconds() >= 10.0]
    next_dt = min(future_candidates) if future_candidates else candidates[-1]

    wait_seconds = (next_dt - now).total_seconds()
    return max(10.0, wait_seconds)


# Alias for backward compatibility
calculate_seconds_until_next_check = calculate_seconds_until_next_checkpoint


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """
    Extracts the short crypto symbol (e.g. 'BTC', 'ETH', 'SOL') from full ticker strings.
    Example:
      'KXBTC15M-26AUG141315-15' -> 'BTC'
      'KXBTCD-26AUG1521-T69399.99' -> 'BTC'
      'KXSOLD-26AUG1617-T99.9999' -> 'SOL'
      'KXNEARH-...' -> 'NEAR'
      'btc-updown-15m-1786835700' -> 'BTC'
    """
    # 1. Check ticker prefix if hyphen-separated (e.g. btc-updown-15m-...)
    ticker_prefix = ticker.split("-")[0].upper() if "-" in ticker else ""
    if ticker_prefix in SUPPORTED_SYMBOLS:
        return ticker_prefix

    # 2. Check series ticker or ticker prefix against supported symbols
    series_upper = (series_ticker or "").upper()
    for sym in SUPPORTED_SYMBOLS:
        if series_upper.startswith(f"KX{sym}") or series_upper.startswith(sym) or ticker_prefix.startswith(f"KX{sym}"):
            return sym

    # 3. Clean known prefixes/suffixes (KX, 15M, D, H, W) and match
    series_clean = series_upper.replace("15M", "").replace("KX", "").rstrip("DHWE").upper()
    if series_clean in SUPPORTED_SYMBOLS:
        return series_clean

    # 4. Search with word boundary regex
    combined = f"{series_ticker} {ticker}".upper()
    for sym in SUPPORTED_SYMBOLS:
        if re.search(rf"\b{sym}\b", combined):
            return sym

    return next((sym for sym in SUPPORTED_SYMBOLS if sym in combined), series_ticker or "CRYPTO")


def extract_target_strike(market: dict) -> float | None:
    """
    Safely extracts the strike / target price from a market dict across 15M and hourly formats.
    Checks:
    1. 'floor_strike', 'cap_strike', 'strike_price'
    2. 'subtitle' (e.g. '$72,250 or above')
    3. 'ticker' (e.g. 'KXBTCD-26AUG1617-T72249.99' or 'KXDOGED-26AUG1617-T0.1549999')
    """
    for k in ("floor_strike", "cap_strike", "strike_price"):
        val = market.get(k)
        if val is not None:
            try:
                f_val = float(val)
                if f_val > 0:
                    return f_val
            except (ValueError, TypeError):
                pass

    if subtitle := market.get("subtitle"):
        if m := re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", str(subtitle)):
            try:
                return float(m.group(1).replace(",", ""))
            except (ValueError, TypeError):
                pass

    if ticker := market.get("ticker"):
        if m := re.search(r"-T([0-9]+(?:\.[0-9]+)?)", str(ticker)):
            try:
                return float(m.group(1))
            except (ValueError, TypeError):
                pass

    return None


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


def batch_fetch_crypto_spot_prices(
    symbols: tuple[str, ...] | list[str],
    max_workers: int = 12,
) -> dict[str, float | None]:
    """
    Fetches spot prices for all unique crypto symbols concurrently in parallel using a thread pool.

    Why parallel batching?
    -----------------------
    Instead of making 12 sequential blocking HTTP calls (which could stall for up to 42s during Coinbase latency),
    we fire all requests in parallel on a lightweight thread pool. Total runtime is bounded by the single
    slowest network round-trip (~150ms-300ms total), guaranteeing we never miss checkpoint timing windows.
    """
    unique_symbols = list(set(symbols))
    spot_cache: dict[str, float | None] = {sym: None for sym in unique_symbols}

    if not unique_symbols:
        return spot_cache

    with ThreadPoolExecutor(max_workers=min(len(unique_symbols), max_workers)) as executor:
        future_to_sym = {
            executor.submit(get_crypto_spot_price, sym): sym
            for sym in unique_symbols
        }
        for future in as_completed(future_to_sym):
            sym = future_to_sym[future]
            try:
                spot_cache[sym] = future.result()
            except Exception as err:
                log.debug(f"Spot price lookup failed for {sym}: {err}")
                spot_cache[sym] = None

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
    corridor_mode: str = "discount",
) -> tuple[bool, dict | None]:
    """Helper function to execute Maker Buy order and setup resting Take-Profit Auto-Sell."""
    side_lower = side.lower()
    side_upper = side.upper()
    ex_name = getattr(client, "exchange_name", "Exchange")
    momentum_label = "Bullish" if side_lower == "yes" else "Bearish"
    corridor_tag = " [MOMENTUM BREAKOUT]" if corridor_mode == "momentum" else ""

    log_prefix = LogTag.DRY_RUN_MATCH.value if dry_run else LogTag.BUY_MATCH.value
    regime_tag = " [EXPLORATION REGIME]" if is_exploration else ""
    log_action = (
        f"WOULD BUY 1 SHARE OF [{side_upper}] + SET {take_profit_cents}¢ AUTO-SELL."
        if dry_run
        else f"Executing Maker Buy + {take_profit_cents}¢ Auto-Sell!"
    )
    log.info(
        f"{log_prefix}{regime_tag}{corridor_tag} [{ex_name}] {ticker} -> {side_upper} price is {price_cents}¢ "
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
                buy_order_timeout_seconds=BUY_ORDER_EXPIRATION_SECONDS,
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
        "corridor": corridor_mode,
        "side": side_lower,
        "buy": effective_buy,
        "tp": take_profit_cents,
        "sl": 25,
        "outcome": determine_counterfactual_outcome(side_lower, "buy_opened", spot_price, target_price, market_result),
        "entry_ts": int(time.time()),
        "spot_at_entry": spot_price,
        "target_at_entry": target_price,
    }
    if not dry_run and isinstance(buy_res, dict):
        if cid := buy_res.get("client_order_id"):
            trade_details["client_order_id"] = cid
        if oid := buy_res.get("order_id"):
            trade_details["order_id"] = oid
        if tp_oid := buy_res.get("tp_order_id"):
            trade_details["tp_order_id"] = tp_oid

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
    target_price = extract_target_strike(market)

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
    is_7_5min_mark = (minute_into_market == 7)
    is_5min_mark = (minute_into_market == 5)
    is_2_5min_mark = (minute_into_market == 2)
    is_00_mark = (minute_into_market == 0)

    # :05 and :07.5 use tighter dynamic price filters (is_5min_mark=True); :00 and :02.5 use broad open filters
    is_tight_corridor = (minute_into_market in (5, 7))
    dynamic_filters = get_dynamic_filter_for_market(asset_symbol, is_5min_mark=is_tight_corridor, now_utc=now_utc)
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
    # Step 4C: Buy Strategy & Early Cut-Loss Check (:00, :02.5, :05, :07.5...)
    # - :00 & :02.5: Broad corridor buy entries + :02.5 disaster cut-loss (<25¢).
    # - :05 & :07.5: Tighter corridor buy entries + early cut-loss checks (<34¢-40¢).
    # Exclusively gated: We do NOT evaluate new buys if Stop-Loss just liquidated this market.
    # -----------------------------------------------------------------------
    elif is_trading_interval:
        if not dry_run:
            client.cancel_unfilled_buy_orders(ticker)

        cut_loss_executed = False
        pos = client.get_market_position(ticker)

        # Early Cut-Loss Check: Evaluate active positions at :02.5, :05, and :07.5 marks
        if pos is not None and (is_2_5min_mark or is_5min_mark or is_7_5min_mark):
            side = pos["side"]
            if is_2_5min_mark:
                cut_threshold = max(20, dynamic_filters.get("cut_5min", 40) - 5)
                action_tag = "2_5min_loss_cut"
            elif is_7_5min_mark:
                cut_threshold = dynamic_filters.get("cut_5min", 40)
                action_tag = "7_5min_loss_cut"
            else:
                cut_threshold = dynamic_filters.get("cut_5min", 40)
                action_tag = "early_loss_cut"

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
                    "action": action_tag,
                    "side": side,
                    "exit_price": cut_res.get("exit_price"),
                    "loss_cents": cut_res.get("loss_cents"),
                    "outcome": determine_counterfactual_outcome(
                        side, action_tag, spot_price, target_price, market.get("result")
                    ),
                }
                pos = None
                cut_loss_executed = True

        # Position Gates & Entry Evaluation
        ex_name = getattr(client, "exchange_name", "Exchange")
        has_floor = getattr(client, "has_profit_floor", lambda t: False)(ticker)
        if cut_loss_executed:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Early Cut-Loss executed for {ticker}. Skipping buy evaluation.")
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
            # Screen for candidates across Dual Corridors:
            # 1. Discount Corridor (18¢ - 36¢ clean, 37¢ - 44¢ conditional on strong spot momentum)
            # 2. Dead Coin-Flip Middle Zone (45¢ - 55¢) -> Strictly rejected
            # 3. Momentum Breakout Corridor (56¢ - 68¢ at open, 56¢ - 65¢ at :05) -> High TP (88¢) with confirmed spot lead (|pct_dist| >= 0.10%)
            avoid_mid_zone = dynamic_filters.get("avoid_mid_zone", True)

            pct_dist_val = 0.0
            if spot_price and target_price and target_price > 0:
                pct_dist_val = ((spot_price - target_price) / target_price) * 100.0

            candidate_sides = []
            for side, p_cents, aligned in [
                ("yes", yes_price_cents, is_spot_momentum_aligned("yes", spot_price, target_price)),
                ("no", no_price_cents, is_spot_momentum_aligned("no", spot_price, target_price)),
            ]:
                if p_cents is None or not aligned:
                    continue

                # Corridor 1: Discount / Underdog Corridor
                if min_buy_price <= p_cents <= max_buy_price:
                    if avoid_mid_zone and (37 <= p_cents <= max_buy_price):
                        is_strong_edge = (pct_dist_val >= 0.08) if side == "yes" else (pct_dist_val <= -0.08)
                        if not is_strong_edge:
                            log.info(
                                f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢) "
                                f"falls in conditional band (37¢-{max_buy_price}¢) without strong spot edge. Skipping."
                            )
                            continue

                    candidate_sides.append({
                        "side": side,
                        "price": p_cents,
                        "tp": take_profit_cents,
                        "min_buy": min_buy_price,
                        "max_buy": max_buy_price,
                        "mode": "discount",
                    })

                # Dead Coin-Flip Middle Zone (45¢ - 55¢)
                elif avoid_mid_zone and (45 <= p_cents <= 55):
                    log.info(
                        f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢) "
                        f"falls in low-EV coin-flip middle zone (45¢-55¢). Skipping."
                    )

                # Corridor 2: Momentum / Trend Breakout Corridor
                elif dynamic_filters.get("enable_momentum_buys", False):
                    mom_min = dynamic_filters.get("momentum_min_buy", 56)
                    mom_max = dynamic_filters.get("momentum_max_buy", 68 if not is_5min_mark else 65)
                    min_lead = dynamic_filters.get("momentum_min_pct_dist", 0.10)
                    has_breakout_lead = (pct_dist_val >= min_lead) if side == "yes" else (pct_dist_val <= -min_lead)

                    if mom_min <= p_cents <= mom_max:
                        if has_breakout_lead:
                            mom_tp = dynamic_filters.get("momentum_tp", 88)
                            log.info(
                                f"🚀 [MOMENTUM BREAKOUT SIGNAL] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                                f"Spot lead {pct_dist_val:+.2f}% exceeds {min_lead}%. Targeting {mom_tp}¢ TP!"
                            )
                            candidate_sides.append({
                                "side": side,
                                "price": p_cents,
                                "tp": mom_tp,
                                "min_buy": mom_min,
                                "max_buy": mom_max,
                                "mode": "momentum",
                            })
                        else:
                            log.info(
                                f"🚫 [MOMENTUM LEAD REJECTED] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                                f"Spot lead {pct_dist_val:+.2f}% is below required {min_lead}% breakout lead threshold. Skipping."
                            )

            if candidate_sides:
                chosen_cand = min(candidate_sides, key=lambda x: x["price"])
                chosen_side = chosen_cand["side"]
                chosen_price = chosen_cand["price"]
                chosen_tp = chosen_cand["tp"]
                chosen_min = chosen_cand["min_buy"]
                chosen_max = chosen_cand["max_buy"]
                chosen_mode = chosen_cand["mode"]
                chosen_bid = quotes["yes_bid"] if chosen_side == "yes" else quotes["no_bid"]
                trade_executed, trade_details = _execute_buy_flow(
                    ticker=ticker,
                    side=chosen_side,
                    price_cents=chosen_price,
                    maker_bid=chosen_bid,
                    min_buy=chosen_min,
                    max_buy=chosen_max,
                    take_profit_cents=chosen_tp,
                    spot_price=spot_price,
                    target_price=target_price,
                    market_result=market.get("result"),
                    client=client,
                    dry_run=dry_run,
                    is_exploration=is_exploration,
                    corridor_mode=chosen_mode,
                )

    # 5. Build Compact Snapshot Document
    compact_document = {
        "ts": int(now_utc.timestamp()),
        "m": minute_past_hour,
        "tf": "15m",
        "sym": asset_symbol,
        "tick": ticker,
        "ex": market.get("exchange", "kalshi"),
        "y": yes_price_cents,
        "n": no_price_cents,
        "y_bid": quotes["yes_bid"],
        "y_ask": quotes["yes_ask"],
        "n_bid": quotes["no_bid"],
        "n_ask": quotes["no_ask"],
        "y_bid_sz": parse_cents(None, market.get("yes_bid_size") or market.get("yes_bid_count")),
        "y_ask_sz": parse_cents(None, market.get("yes_ask_size") or market.get("yes_ask_count")),
        "n_bid_sz": parse_cents(None, market.get("no_bid_size") or market.get("no_bid_count")),
        "n_ask_sz": parse_cents(None, market.get("no_ask_size") or market.get("no_ask_count")),
        "sprd": sprd,
        "p": spot_price,
        "tgt": target_price,
        "diff": price_diff,
        "pct": pct_dist,
        "rem": rem_mins,
        "vol": vol_count,
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time") or market.get("expected_expiration_time"),
        "imp_prob": round(yes_price_cents / 100.0, 4) if yes_price_cents is not None else None,
    }

    if trade_executed and trade_details:
        compact_document["trd"] = trade_details

    return compact_document


def process_hourly_market_group(
    symbol: str,
    markets: list[dict],
    client: BaseTradingClient,
    now_utc: datetime,
    minute_past_hour: int,
    spot_cache: dict[str, float | None] | None = None,
    dry_run: bool = False,
    force_trade_mark: bool = False,
    circuit_breaker_tripped: bool = False,
) -> list[dict]:
    """
    Evaluates pricing, risk rules, multi-strike selection, tiebreaker resolution,
    and lifecycle checkpoints for hourly crypto prediction markets belonging to a single asset.
    """
    if not markets:
        return []

    ex_name = getattr(client, "exchange_name", "Exchange")
    spot_price = spot_cache.get(symbol) if spot_cache is not None else get_crypto_spot_price(symbol)

    # Checkpoint interval flags
    is_hourly_00_or_10_entry = ((minute_past_hour in (0, 10)) or force_trade_mark) and not circuit_breaker_tripped
    is_hourly_20_or_30_entry = ((minute_past_hour in (20, 30)) or force_trade_mark) and not circuit_breaker_tripped
    is_hourly_trading_interval = is_hourly_00_or_10_entry or is_hourly_20_or_30_entry
    is_hourly_cut_loss = (minute_past_hour in (10, 20, 30)) and not force_trade_mark
    is_hourly_40min_stop_loss = (minute_past_hour == 40) and not force_trade_mark
    is_hourly_50min_endgame = (minute_past_hour == 50) and not force_trade_mark

    # Dynamic filter parameters (20-min and 30-min use tighter filter, 00-min and 10-min use broad open filter)
    is_tight_corridor = (minute_past_hour in (20, 30))
    dynamic_filters = get_dynamic_filter_for_market(symbol, is_5min_mark=is_tight_corridor, now_utc=now_utc)
    min_buy_price = dynamic_filters["min_buy"]
    max_buy_price = dynamic_filters["max_buy"]
    take_profit_cents = dynamic_filters["tp"]
    is_exploration = dynamic_filters.get("is_exploration", False)
    avoid_mid_zone = dynamic_filters.get("avoid_mid_zone", True)

    snapshots: list[dict] = []
    trade_executed_by_ticker: dict[str, dict] = {}

    # Pre-parse quote & strike metrics for all strikes in the group
    parsed_markets = []
    for market in markets:
        ticker = market.get("ticker", "UNKNOWN")
        series_ticker = market.get("series_ticker", "UNKNOWN")
        target_price = extract_target_strike(market)

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

        price_diff, pct_dist = None, None
        if spot_price is not None and target_price is not None:
            try:
                p_float, tgt_float = float(spot_price), float(target_price)
                price_diff = round(p_float - tgt_float, 4)
                if tgt_float > 0:
                    pct_dist = round((price_diff / tgt_float) * 100.0, 4)
            except (ValueError, TypeError):
                pass

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

        parsed_markets.append({
            "market": market,
            "ticker": ticker,
            "series_ticker": series_ticker,
            "target_price": target_price,
            "yes_price_cents": yes_price_cents,
            "no_price_cents": no_price_cents,
            "quotes": quotes,
            "sprd": sprd,
            "price_diff": price_diff,
            "pct_dist": pct_dist,
            "rem_mins": rem_mins,
            "vol_count": vol_count,
        })

    # -----------------------------------------------------------------------
    # Step 1: Hourly 50-Minute Endgame Safety Check (:50 mark)
    # -----------------------------------------------------------------------
    if is_hourly_50min_endgame:
        for item in parsed_markets:
            ticker = item["ticker"]
            pos = client.get_market_position(ticker)
            if pos is not None:
                quotes = item["quotes"]
                sellable_yes = quotes["yes_bid"] if quotes["yes_bid"] is not None else item["yes_price_cents"]
                sellable_no = quotes["no_bid"] if quotes["no_bid"] is not None else item["no_price_cents"]
                endgame_floor = dynamic_filters.get("endgame_12_5_floor", 15)

                eg_res = client.evaluate_and_execute_12_5min_endgame_check(
                    ticker=ticker,
                    current_yes_price=sellable_yes,
                    current_no_price=sellable_no,
                    dry_run=dry_run,
                    endgame_floor=endgame_floor,
                )
                if isinstance(eg_res, dict) and eg_res.get("triggered"):
                    trade_executed_by_ticker[ticker] = {
                        "ex": ex_name.lower(),
                        "action": "50min_loss_cut",
                        "side": eg_res.get("side", "yes"),
                        "exit_price": eg_res.get("exit_price"),
                        "loss_cents": eg_res.get("loss_cents"),
                        "outcome": determine_counterfactual_outcome(
                            eg_res.get("side", "yes"),
                            "50min_loss_cut",
                            spot_price,
                            item["target_price"],
                            item["market"].get("result"),
                        ),
                    }
                    if "profit_locked_cents" in eg_res:
                        trade_executed_by_ticker[ticker]["profit_locked_cents"] = eg_res["profit_locked_cents"]

    # -----------------------------------------------------------------------
    # Step 2: Hourly 40-Minute Stop-Loss & Winner-Roll Check (:40 mark)
    # -----------------------------------------------------------------------
    elif is_hourly_40min_stop_loss:
        for item in parsed_markets:
            ticker = item["ticker"]
            pos = client.get_market_position(ticker)
            if pos is not None:
                quotes = item["quotes"]
                sellable_yes = quotes["yes_bid"] if quotes["yes_bid"] is not None else item["yes_price_cents"]
                sellable_no = quotes["no_bid"] if quotes["no_bid"] is not None else item["no_price_cents"]

                sl_threshold = dynamic_filters.get("sl_threshold", 25)
                sl_floor = dynamic_filters.get("sl_floor", 15)

                sl_res = client.evaluate_and_execute_10min_strategy(
                    ticker=ticker,
                    current_yes_bid=sellable_yes,
                    current_no_bid=sellable_no,
                    current_yes_ask=quotes.get("yes_ask"),
                    current_no_ask=quotes.get("no_ask"),
                    dry_run=dry_run,
                    stop_loss_threshold_cents=sl_threshold,
                    price_floor=sl_floor,
                )
                if isinstance(sl_res, dict) and sl_res.get("triggered"):
                    sl_side = sl_res.get("side", "yes")
                    action_name = sl_res.get("action", "stop_loss_sold")
                    trade_details = {
                        "ex": ex_name.lower(),
                        "action": action_name,
                        "side": sl_side,
                        "exit_price": sl_res.get("exit_price"),
                        "outcome": determine_counterfactual_outcome(
                            sl_side, action_name, spot_price, item["target_price"], item["market"].get("result")
                        ),
                    }
                    if "loss_cents" in sl_res:
                        trade_details["loss_cents"] = sl_res["loss_cents"]
                    if "profit_locked_cents" in sl_res:
                        trade_details["profit_locked_cents"] = sl_res["profit_locked_cents"]
                    if "rebuy_price_cents" in sl_res:
                        trade_details["rebuy_price_cents"] = sl_res["rebuy_price_cents"]
                    trade_executed_by_ticker[ticker] = trade_details

    # -----------------------------------------------------------------------
    # Step 3: Hourly Buy Entry & Early Cut-Loss (:00, :10, :20, :30)
    # -----------------------------------------------------------------------
    elif is_hourly_trading_interval:
        # Step 3A: Hourly Early Cut-Loss Check (:10, :20, :30 marks)
        cut_loss_tickers = set()
        if is_hourly_cut_loss:
            if minute_past_hour == 10:
                cut_threshold = max(15, dynamic_filters.get("cut_5min", 15) - 5)
                action_tag = "10min_loss_cut"
            elif minute_past_hour == 20:
                cut_threshold = dynamic_filters.get("cut_5min", 15)
                action_tag = "20min_loss_cut"
            elif minute_past_hour == 30:
                cut_threshold = dynamic_filters.get("cut_5min", 15)
                action_tag = "30min_loss_cut"
            else:
                cut_threshold = dynamic_filters.get("cut_5min", 15)
                action_tag = "early_loss_cut"

            for item in parsed_markets:
                ticker = item["ticker"]
                pos = client.get_market_position(ticker)
                if pos is not None:
                    side = pos["side"]
                    cut_res = client.evaluate_and_execute_5min_cut_loss(
                        ticker=ticker,
                        current_yes_price=item["yes_price_cents"],
                        current_no_price=item["no_price_cents"],
                        min_price_threshold=cut_threshold,
                        dry_run=dry_run,
                    )
                    if isinstance(cut_res, dict) and cut_res.get("triggered"):
                        cut_loss_tickers.add(ticker)
                        trade_details = {
                            "ex": ex_name.lower(),
                            "action": action_tag,
                            "side": side,
                            "exit_price": cut_res.get("exit_price"),
                            "loss_cents": cut_res.get("loss_cents"),
                            "outcome": determine_counterfactual_outcome(
                                side, action_tag, spot_price, item["target_price"], item["market"].get("result")
                            ),
                        }
                        trade_executed_by_ticker[ticker] = trade_details

        # Step 3B: Prune stale unfilled buy orders
        if not dry_run:
            for item in parsed_markets:
                client.cancel_unfilled_buy_orders(item["ticker"])

        # Step 3C: Collect valid BUY_YES and BUY_NO candidates across all strike options
        valid_yes_candidates = []
        valid_no_candidates = []

        for item in parsed_markets:
            ticker = item["ticker"]
            target_price = item["target_price"]
            quotes = item["quotes"]
            pct_dist = item["pct_dist"]
            pct_dist_val = pct_dist if pct_dist is not None else 0.0

            # Position check: skip if we already hold an active position or resting order
            if ticker in cut_loss_tickers:
                continue
            if client.has_existing_position_or_order(ticker):
                pos = client.get_market_position(ticker)
                has_floor = getattr(client, "has_profit_floor", lambda t: False)(ticker)
                if pos is not None and not client.has_resting_take_profit_order(ticker) and not has_floor and not dry_run:
                    client.place_take_profit_order(
                        ticker=ticker,
                        side=pos["side"],
                        count=pos["count"],
                        take_profit_cents=take_profit_cents,
                    )
                continue

            # Evaluate YES
            yes_p = item["yes_price_cents"]
            if yes_p is not None:
                if is_spot_momentum_aligned("yes", spot_price, target_price):
                    # 1. Discount Corridor (18¢ - 44¢)
                    if min_buy_price <= yes_p <= max_buy_price:
                        if avoid_mid_zone and (37 <= yes_p <= max_buy_price) and pct_dist_val < 0.08:
                            log.info(
                                f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (YES @ {yes_p}¢) "
                                f"falls in conditional band (37¢-{max_buy_price}¢) without strong spot edge. Skipping."
                            )
                        else:
                            valid_yes_candidates.append({
                                "item": item,
                                "ticker": ticker,
                                "side": "yes",
                                "price": yes_p,
                                "bid": quotes["yes_bid"],
                                "target_price": target_price,
                                "pct_dist": pct_dist,
                                "tp": take_profit_cents,
                                "min_buy": min_buy_price,
                                "max_buy": max_buy_price,
                                "mode": "discount",
                            })
                    # 2. Dead Coin-Flip Middle Zone (45¢ - 55¢)
                    elif avoid_mid_zone and (45 <= yes_p <= 55):
                        log.info(
                            f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (YES @ {yes_p}¢) "
                            f"falls in low-EV coin-flip middle zone (45¢-55¢). Skipping."
                        )
                    # 3. Momentum Breakout Corridor (56¢ - 68¢ at open, 56¢ - 65¢ at :20)
                    elif dynamic_filters.get("enable_momentum_buys", False):
                        mom_min = dynamic_filters.get("momentum_min_buy", 56)
                        mom_max = dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65)
                        min_lead = dynamic_filters.get("momentum_min_pct_dist", 0.10)

                        if mom_min <= yes_p <= mom_max:
                            if pct_dist_val >= min_lead:
                                mom_tp = dynamic_filters.get("momentum_tp", 88)
                                log.info(
                                    f"🚀 [MOMENTUM BREAKOUT SIGNAL] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                    f"Spot lead {pct_dist_val:+.2f}% exceeds {min_lead}%. Targeting {mom_tp}¢ TP!"
                                )
                                valid_yes_candidates.append({
                                    "item": item,
                                    "ticker": ticker,
                                    "side": "yes",
                                    "price": yes_p,
                                    "bid": quotes["yes_bid"],
                                    "target_price": target_price,
                                    "pct_dist": pct_dist,
                                    "tp": mom_tp,
                                    "min_buy": mom_min,
                                    "max_buy": mom_max,
                                    "mode": "momentum",
                                })
                            else:
                                log.info(
                                    f"🚫 [MOMENTUM LEAD REJECTED] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                    f"Spot lead {pct_dist_val:+.2f}% is below required {min_lead}% breakout lead threshold. Skipping."
                                )
                else:
                    log.info(
                        f"🚫 [SPOT MOMENTUM REJECTED] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                        f"Spot ${spot_price} < Target ${target_price} (Diff: {item['price_diff']}). Bearish spot momentum."
                    )

            # Evaluate NO
            no_p = item["no_price_cents"]
            if no_p is not None:
                if is_spot_momentum_aligned("no", spot_price, target_price):
                    # 1. Discount Corridor (18¢ - 44¢)
                    if min_buy_price <= no_p <= max_buy_price:
                        if avoid_mid_zone and (37 <= no_p <= max_buy_price) and pct_dist_val > -0.08:
                            log.info(
                                f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (NO @ {no_p}¢) "
                                f"falls in conditional band (37¢-{max_buy_price}¢) without strong spot edge. Skipping."
                            )
                        else:
                            valid_no_candidates.append({
                                "item": item,
                                "ticker": ticker,
                                "side": "no",
                                "price": no_p,
                                "bid": quotes["no_bid"],
                                "target_price": target_price,
                                "pct_dist": pct_dist,
                                "tp": take_profit_cents,
                                "min_buy": min_buy_price,
                                "max_buy": max_buy_price,
                                "mode": "discount",
                            })
                    # 2. Dead Coin-Flip Middle Zone (45¢ - 55¢)
                    elif avoid_mid_zone and (45 <= no_p <= 55):
                        log.info(
                            f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (NO @ {no_p}¢) "
                            f"falls in low-EV coin-flip middle zone (45¢-55¢). Skipping."
                        )
                    # 3. Momentum Breakout Corridor (56¢ - 68¢ at open, 56¢ - 65¢ at :20)
                    elif dynamic_filters.get("enable_momentum_buys", False):
                        mom_min = dynamic_filters.get("momentum_min_buy", 56)
                        mom_max = dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65)
                        min_lead = dynamic_filters.get("momentum_min_pct_dist", 0.10)

                        if mom_min <= no_p <= mom_max:
                            if pct_dist_val <= -min_lead:
                                mom_tp = dynamic_filters.get("momentum_tp", 88)
                                log.info(
                                    f"🚀 [MOMENTUM BREAKOUT SIGNAL] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                    f"Spot lead {pct_dist_val:+.2f}% exceeds {min_lead}%. Targeting {mom_tp}¢ TP!"
                                )
                                valid_no_candidates.append({
                                    "item": item,
                                    "ticker": ticker,
                                    "side": "no",
                                    "price": no_p,
                                    "bid": quotes["no_bid"],
                                    "target_price": target_price,
                                    "pct_dist": pct_dist,
                                    "tp": mom_tp,
                                    "min_buy": mom_min,
                                    "max_buy": mom_max,
                                    "mode": "momentum",
                                })
                            else:
                                log.info(
                                    f"🚫 [MOMENTUM LEAD REJECTED] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                    f"Spot lead {pct_dist_val:+.2f}% is below required {min_lead}% breakout lead threshold. Skipping."
                                )
                else:
                    log.info(
                        f"🚫 [SPOT MOMENTUM REJECTED] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                        f"Spot ${spot_price} >= Target ${target_price} (Diff: {item['price_diff']}). Bullish spot momentum."
                    )

        # Step 3D: Multi-Strike Selection & Tiebreaker Execution
        best_yes = min(valid_yes_candidates, key=lambda c: c["price"]) if valid_yes_candidates else None
        best_no = min(valid_no_candidates, key=lambda c: c["price"]) if valid_no_candidates else None

        candidates_to_execute = []

        if best_yes and best_no:
            if best_yes["ticker"] == best_no["ticker"]:
                # Conflict: Same contract ticker cannot hold both YES and NO
                # Tiebreaker: Pick the strictly cheaper side; if tied in price, use spot momentum direction
                if best_yes["price"] < best_no["price"]:
                    chosen = best_yes
                    log.info(
                        f"{LogTag.TIEBREAKER_CHOSEN.value} [{ex_name}] {chosen['ticker']}: "
                        f"BUY_YES ({best_yes['price']}¢) is cheaper than BUY_NO ({best_no['price']}¢). Picking BUY_YES."
                    )
                elif best_no["price"] < best_yes["price"]:
                    chosen = best_no
                    log.info(
                        f"{LogTag.TIEBREAKER_CHOSEN.value} [{ex_name}] {chosen['ticker']}: "
                        f"BUY_NO ({best_no['price']}¢) is cheaper than BUY_YES ({best_yes['price']}¢). Picking BUY_NO."
                    )
                else:
                    pct_val = best_yes.get("pct_dist") or 0.0
                    chosen = best_yes if pct_val >= 0 else best_no
                    log.info(
                        f"{LogTag.TIEBREAKER_CHOSEN.value} [{ex_name}] {chosen['ticker']}: "
                        f"YES and NO tied at {chosen['price']}¢. Picking {chosen['side'].upper()} based on spot momentum ({pct_val:+.2f}%)."
                    )
                candidates_to_execute.append(chosen)
            else:
                # Different strike contracts: Execute DUAL BUY!
                log.info(
                    f"{LogTag.DUAL_BUY_MATCH.value} [{ex_name}] Hourly Multi-Strike Dual Buy for {symbol}: "
                    f"Buying YES on {best_yes['ticker']} (@ {best_yes['price']}¢) AND NO on {best_no['ticker']} (@ {best_no['price']}¢)."
                )
                candidates_to_execute.append(best_yes)
                candidates_to_execute.append(best_no)
        elif best_yes:
            candidates_to_execute.append(best_yes)
        elif best_no:
            candidates_to_execute.append(best_no)

        # Execute candidate orders
        for cand in candidates_to_execute:
            item = cand["item"]
            ticker = cand["ticker"]
            side = cand["side"]
            price_cents = cand["price"]
            maker_bid = cand["bid"]

            executed, details = _execute_buy_flow(
                ticker=ticker,
                side=side,
                price_cents=price_cents,
                maker_bid=maker_bid,
                min_buy=cand.get("min_buy", min_buy_price),
                max_buy=cand.get("max_buy", max_buy_price),
                take_profit_cents=cand.get("tp", take_profit_cents),
                spot_price=spot_price,
                target_price=cand["target_price"],
                market_result=item["market"].get("result"),
                client=client,
                dry_run=dry_run,
                is_exploration=is_exploration,
                corridor_mode=cand.get("mode", "discount"),
            )
            if executed and details:
                trade_executed_by_ticker[ticker] = details

    # -----------------------------------------------------------------------
    # Step 4: Build Compact Snapshot Documents for all strikes in the group
    # -----------------------------------------------------------------------
    for item in parsed_markets:
        ticker = item["ticker"]
        quotes = item["quotes"]

        compact_doc = {
            "ts": int(now_utc.timestamp()),
            "m": minute_past_hour,
            "tf": "1h",
            "sym": symbol,
            "tick": ticker,
            "ex": item["market"].get("exchange", "kalshi"),
            "y": item["yes_price_cents"],
            "n": item["no_price_cents"],
            "y_bid": quotes["yes_bid"],
            "y_ask": quotes["yes_ask"],
            "n_bid": quotes["no_bid"],
            "n_ask": quotes["no_ask"],
            "y_bid_sz": parse_cents(None, item["market"].get("yes_bid_size") or item["market"].get("yes_bid_count")),
            "y_ask_sz": parse_cents(None, item["market"].get("yes_ask_size") or item["market"].get("yes_ask_count")),
            "n_bid_sz": parse_cents(None, item["market"].get("no_bid_size") or item["market"].get("no_bid_count")),
            "n_ask_sz": parse_cents(None, item["market"].get("no_ask_size") or item["market"].get("no_ask_count")),
            "sprd": item["sprd"],
            "p": spot_price,
            "tgt": item["target_price"],
            "diff": item["price_diff"],
            "pct": item["pct_dist"],
            "rem": item["rem_mins"],
            "vol": item["vol_count"],
            "open_time": item["market"].get("open_time"),
            "close_time": item["market"].get("close_time") or item["market"].get("expected_expiration_time"),
            "imp_prob": round(item["yes_price_cents"] / 100.0, 4) if item["yes_price_cents"] is not None else None,
        }
        if ticker in trade_executed_by_ticker:
            compact_doc["trd"] = trade_executed_by_ticker[ticker]

        snapshots.append(compact_doc)

    return snapshots


# Session Trading Loss Circuit Breaker ($5.00 max trading loss, immune to cash withdrawals)
_SESSION_INITIAL_REALIZED_PNL: float | None = None
MAX_SESSION_LOSS_DOLLARS: float = 5.00


def run_pipeline_pass(
    clients: list[tuple[str, BaseTradingClient]] | None = None,
    dry_run: bool = False,
    force_trade_mark: bool = False,
):
    """
    Executes a single market processing & trading pass across all active exchanges
    for BOTH 15-minute and Hourly multi-strike crypto prediction markets.
    Accepts long-lived exchange clients to prevent socket leaks and connection overhead.
    """
    global _SESSION_INITIAL_REALIZED_PNL
    
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute

    minute_into_market_15m = current_minute % 15
    is_15m_open_ready = (minute_into_market_15m == 0) and (now_utc.second >= 20 or force_trade_mark)
    is_15m_trading_interval = is_15m_open_ready or (minute_into_market_15m in (2, 5, 7)) or force_trade_mark
    is_15m_10min_interval = (minute_into_market_15m == 10) and not force_trade_mark
    is_15m_12_5min_interval = (minute_into_market_15m == 12) and not force_trade_mark

    is_hourly_00_or_10_entry = (current_minute in (0, 10)) or force_trade_mark
    is_hourly_20_or_30_mark = (current_minute in (20, 30)) and not force_trade_mark
    is_hourly_40_mark = (current_minute == 40) and not force_trade_mark
    is_hourly_50_mark = (current_minute == 50) and not force_trade_mark

    # Human-readable interval descriptions
    intervals_active = []
    if is_15m_trading_interval:
        if minute_into_market_15m == 2:
            intervals_active.append("15M :02.5 2ND CHANCE BUY & DISASTER CUT")
        elif minute_into_market_15m == 5:
            intervals_active.append("15M :05 BUY ENTRY / 5M CUT")
        elif minute_into_market_15m == 7:
            intervals_active.append("15M :07.5 MID-CANDLE BUY ENTRY / CUT")
        else:
            intervals_active.append("15M :00 BUY ENTRY")
    elif is_15m_10min_interval:
        intervals_active.append("15M WINNER ROLL / SL")
    elif is_15m_12_5min_interval:
        intervals_active.append("15M ENDGAME SAFETY CHECK")

    if current_minute == 0 or force_trade_mark:
        intervals_active.append("HOURLY 00M MULTI-STRIKE ENTRY")
    elif current_minute == 10:
        intervals_active.append("HOURLY 10M 2ND CHANCE ENTRY & DISASTER CUT")
    elif current_minute == 20:
        intervals_active.append("HOURLY 20M CUT-LOSS & 3RD ENTRY")
    elif current_minute == 30:
        intervals_active.append("HOURLY 30M MID-HOUR CUT-LOSS & ENTRY")
    elif current_minute == 40:
        intervals_active.append("HOURLY 40M WINNER ROLL & SL")
    elif current_minute == 50:
        intervals_active.append("HOURLY 50M ENDGAME SAFETY CHECK")

    interval_summary = " + ".join(intervals_active) if intervals_active else "DATA COLLECTION ONLY MARK"

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {now_utc.isoformat()} (Minute :{current_minute:02d}:{now_utc.second:02d})")
    if dry_run:
        log.info(f"--> MODE: {LogTag.DRY_RUN_PREVIEW.value} (No real orders will be placed)")
    if is_default_exploration_window(now_utc):
        log.info("--> REGIME: 🌍 [EXPLORATION REGIME] Active UTC window 05:00–07:00 (1AM–3AM ET): Trading on default broad corridors (:00 -> 18¢–44¢, :05/20m -> 18¢–30¢) for empirical discovery.")
    log.info(f"--> INTERVAL: {interval_summary}")
    log.info("=================================================================")

    # Ensure dynamic price filters are in memory (auto-refreshes every 6 hours, at 11:00 UTC anchor, or on cold start)
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
        circuit_breaker_tripped = False
        try:
            pnl = client.get_portfolio_pnl_summary()
            pnl["ts"] = int(now_utc.timestamp())
            pnl["exchange"] = exchange_name.lower()
            sign = "+" if pnl.get("total_realized_pnl_dollars", 0) >= 0 else ""

            # Circuit Breaker Evaluation (Trading loss >= $5.00 limit, immune to cash withdrawals)
            current_realized_pnl = float(pnl.get("total_realized_pnl_dollars") or 0.0)
            if _SESSION_INITIAL_REALIZED_PNL is None:
                _SESSION_INITIAL_REALIZED_PNL = current_realized_pnl

            session_trading_loss = round(_SESSION_INITIAL_REALIZED_PNL - current_realized_pnl, 2)
            circuit_breaker_tripped = (session_trading_loss >= MAX_SESSION_LOSS_DOLLARS)

            log.info(
                f"{LogTag.PORTFOLIO_PNL.value} [{exchange_name}] Cash: ${pnl.get('balance_dollars', 0.0):.2f} | "
                f"Port Value: ${pnl.get('portfolio_value_dollars', 0.0):.2f} | "
                f"Settled PnL: {sign}${pnl.get('total_realized_pnl_dollars', 0.0):.2f} "
                f"({sign}{pnl.get('total_realized_pnl_cents', 0)}¢ across {pnl.get('settlements_count', 0)} mkts) | "
                f"Active: {pnl.get('open_positions_count', 0)} pos (${pnl.get('open_exposure_dollars', 0.0):.2f})"
            )

            if circuit_breaker_tripped and not force_trade_mark:
                log.warning(
                    f"🛑 [LOSS CIRCUIT BREAKER ACTIVE] Session trading loss is -${session_trading_loss:.2f} "
                    f"(exceeds ${MAX_SESSION_LOSS_DOLLARS:.2f} limit). New buy entries are halted to preserve capital."
                )
                is_15m_trading_interval = False

            if not dry_run:
                save_portfolio_pnl(pnl)
                if pnl.get("settlements"):
                    save_settlements(pnl["settlements"])
        except Exception as err:
            log.warning(f"Could not retrieve portfolio PnL summary from {exchange_name}: {err}")

        # -------------------------------------------------------------------
        # 1. 15-Minute Crypto Markets Pipeline
        # -------------------------------------------------------------------
        log.info(f"Fetching active 15-minute crypto markets from {exchange_name}...")
        try:
            markets_15m = client.get_15m_crypto_markets(status="open")
            log.info(f"Retrieved {len(markets_15m)} active 15-minute crypto markets from {exchange_name}.")
        except Exception as error:
            log.error(f"Failed to fetch 15-minute crypto markets from {exchange_name}: {error}")
            markets_15m = []

        for market in markets_15m:
            tick = market.get("ticker", "UNKNOWN")
            try:
                snapshot = process_single_market(
                    market,
                    is_15m_trading_interval,
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
                    f"  • [{exchange_name}] [15M] [{snapshot['sym']} | {snapshot['tick']}] -> "
                    f"YES: {snapshot['y']}¢ | NO: {snapshot['n']}¢ | "
                    f"Price: ${snapshot.get('p')} | Target: ${snapshot.get('tgt')} | Diff: {diff_str}"
                )
            except Exception as mkt_err:
                log.error(f"❌ [{exchange_name}] Unhandled error processing 15M market {tick}: {mkt_err}", exc_info=True)

        # -------------------------------------------------------------------
        # 2. Hourly Multi-Strike Crypto Markets Pipeline
        # -------------------------------------------------------------------
        if hasattr(client, "get_hourly_crypto_markets"):
            log.info(f"Fetching active hourly crypto markets from {exchange_name}...")
            try:
                markets_1h = client.get_hourly_crypto_markets(
                    status="open", current_hour_only=True, now_utc=now_utc
                )
                log.info(f"Retrieved {len(markets_1h)} active hourly crypto strike markets from {exchange_name}.")
            except Exception as error:
                log.error(f"Failed to fetch hourly crypto markets from {exchange_name}: {error}")
                markets_1h = []

            # Group hourly markets by crypto symbol (e.g. BTC, ETH, SOL, etc.)
            hourly_by_symbol = defaultdict(list)
            for m in markets_1h:
                series_tick = m.get("series_ticker", "UNKNOWN")
                tick = m.get("ticker", "UNKNOWN")
                sym = extract_asset_symbol(series_tick, tick)
                hourly_by_symbol[sym].append(m)

            for sym, sym_markets in hourly_by_symbol.items():
                try:
                    hourly_snapshots = process_hourly_market_group(
                        symbol=sym,
                        markets=sym_markets,
                        client=client,
                        now_utc=now_utc,
                        minute_past_hour=current_minute,
                        spot_cache=spot_price_cache,
                        dry_run=dry_run,
                        force_trade_mark=force_trade_mark,
                        circuit_breaker_tripped=circuit_breaker_tripped,
                    )
                    for s in hourly_snapshots:
                        s["ex"] = exchange_name.lower()
                        snapshots_to_save.append(s)

                        # Render Log Optimization:
                        # Only console.log strikes where YES or NO is in the broad tradeable corridor (20¢ - 80¢)
                        # or where a trade action occurred. This eliminates 300+ lines of out-of-the-money 1¢ strikes.
                        y_val = s.get("y")
                        n_val = s.get("n")
                        has_trade = s.get("trd") is not None
                        in_broad_corridor = (
                            (y_val is not None and 20 <= y_val <= 80)
                            or (n_val is not None and 20 <= n_val <= 80)
                        )

                        if has_trade or in_broad_corridor:
                            diff_val = s.get("diff")
                            diff_str = f"+{diff_val}" if (diff_val is not None and diff_val > 0) else str(diff_val)
                            log.info(
                                f"  • [{exchange_name}] [1H] [{s['sym']} | {s['tick']}] -> "
                                f"YES: {s['y']}¢ | NO: {s['n']}¢ | "
                                f"Price: ${s.get('p')} | Target: ${s.get('tgt')} | Diff: {diff_str}"
                            )
                except Exception as sym_err:
                    log.error(f"❌ [{exchange_name}] Unhandled error processing hourly group for {sym}: {sym_err}", exc_info=True)

    # Save valid market snapshots to MongoDB Atlas
    # We save snapshots on regular checkpoints (:00, :02.5, :05, :07.5, :10, :12.5, :20, :30, :40, :50) OR whenever a trade occurs
    is_standard_save_mark = (minute_into_market_15m in (0, 2, 5, 7, 10, 12)) or (current_minute in (0, 10, 20, 30, 40, 50))
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
        log.info("\nInterim data-collection pass complete (compact storage policy: snapshots saved at :00, :02.5, :05, :07.5, :10, :12.5, :20, :30, :40, :50 marks).")
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
