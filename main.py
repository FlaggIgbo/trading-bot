"""
15-Minute Crypto Prediction Trading & Data Pipeline Bot
=========================================================

What is this bot? (A High School Friendly Explanation)
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

4. Smart Risk Management (Stop-Loss & Take-Profit):
   - Automatic Profit Taking: Whenever we buy a contract, we immediately put in an Auto-Sell limit order
     at 82¢ (or 76¢ at the 5-min mark) to lock in profit before expiration.
   - Early Cut-Loss (:05 mark): If a position bought at :00 drops below 40¢, we sell early to cut losses.
   - Stop-Loss (:10 mark): If our position is losing 10¢ or drops to <= 30¢, we sell immediately to protect capital.
   - Let Winners Run: If our position is winning big (>= 70¢ at the 10-min mark), we cancel the 82¢ auto-sell
     and let it run to collect the full 100¢ ($1.00) payout at expiration!

5. Persistent Connections & Resource Protection:
   - Exchange clients and HTTP sessions are initialized ONCE and kept alive across 24/7 background operation.
   - This prevents socket/connection leaks, rate-limit throttling, and unnecessary authentication overhead.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from database import save_market_snapshots, save_portfolio_pnl, save_settlements
from signals.base_client import BaseTradingClient, LogTag
from signals.gemini_client import GeminiClient
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


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """
    Extracts the short crypto symbol (e.g. 'BTC', 'ETH', 'SOL') from full ticker strings.
    Example: 'KXBTC15M-26AUG141315-15' -> 'BTC'
    """
    combined = f"{series_ticker} {ticker}".upper()
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
    if action_type in ("stop_loss_sold", "early_loss_cut"):
        return "sold_early_would_have_won" if won else "sl_cap_saved"
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
) -> tuple[bool, dict | None]:
    """Helper function to execute Maker Buy order and setup resting Take-Profit Auto-Sell."""
    side_lower = side.lower()
    side_upper = side.upper()
    ex_name = getattr(client, "exchange_name", "Exchange")
    momentum_label = "Bullish" if side_lower == "yes" else "Bearish"

    log_prefix = LogTag.DRY_RUN_MATCH.value if dry_run else LogTag.BUY_MATCH.value
    log_action = (
        f"WOULD BUY 1 SHARE OF [{side_upper}] + SET {take_profit_cents}¢ AUTO-SELL."
        if dry_run
        else f"Executing Maker Buy + {take_profit_cents}¢ Auto-Sell!"
    )
    log.info(
        f"{log_prefix} [{ex_name}] {ticker} -> {side_upper} price is {price_cents}¢ "
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

    # 4. Strategy Rules & Interval Evaluation
    minute_into_market = minute_past_hour % 15
    is_stop_loss_interval = (minute_into_market == 10)
    is_5min_mark = (minute_into_market == 5)
    min_buy_price = 40 if is_5min_mark else 35
    max_buy_price = 49
    take_profit_cents = 76 if is_5min_mark else 82

    trade_executed = False
    trade_details = None

    # -----------------------------------------------------------------------
    # Step 4A: Stop-Loss Evaluation (:10, :25, :40, :55)
    # At minute 10 of the 15-minute contract, check if active positions are losing.
    # If a position has lost >= 10¢ or dropped to <= 30¢, sell immediately to cut loss.
    # -----------------------------------------------------------------------
    if is_stop_loss_interval:
        sl_res = client.evaluate_and_execute_stop_loss(
            ticker=ticker,
            current_yes_price=yes_price_cents,
            current_no_price=no_price_cents,
            dry_run=dry_run,
            stop_loss_threshold_cents=10,
        )
        if isinstance(sl_res, dict) and sl_res.get("triggered"):
            trade_executed = True
            sl_side = sl_res.get("side", "yes")
            ex_name = getattr(client, "exchange_name", "Exchange")
            trade_details = {
                "ex": ex_name.lower(),
                "action": "stop_loss_sold",
                "side": sl_side,
                "exit_price": sl_res.get("exit_price"),
                "loss_cents": sl_res.get("loss_cents"),
                "outcome": determine_counterfactual_outcome(
                    sl_side, "stop_loss_sold", spot_price, target_price, market.get("result")
                ),
            }

    # -----------------------------------------------------------------------
    # Step 4B: Buy Strategy & 5-Min Early Cut-Loss Check (:00, :05, :15, :20...)
    # Exclusively gated: We do NOT evaluate new buys if Stop-Loss just liquidated this market.
    # -----------------------------------------------------------------------
    elif is_trading_interval:
        if not dry_run:
            client.cancel_unfilled_buy_orders(ticker)

        cut_loss_executed = False
        pos = client.get_market_position(ticker)

        # 5-Minute Early Cut-Loss Check: If entry from :00 drops below 40¢ at :05, exit immediately
        if pos is not None and is_5min_mark:
            side = pos["side"]
            pos_price = yes_price_cents if side == "yes" else no_price_cents
            if pos_price is not None and pos_price < 40:
                cut_res = client.evaluate_and_execute_5min_cut_loss(
                    ticker=ticker,
                    current_yes_price=yes_price_cents,
                    current_no_price=no_price_cents,
                    min_price_threshold=40,
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
        if cut_loss_executed:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] 5-Min Cut-Loss executed for {ticker}. Skipping buy evaluation.")
        elif pos is not None:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Already hold active {pos['side'].upper()} pos of {pos['count']} contract(s) for {ticker}. Skipping buy.")
            if not is_stop_loss_interval and not client.has_resting_take_profit_order(ticker) and not dry_run:
                client.place_take_profit_order(
                    ticker=ticker,
                    side=pos["side"],
                    count=pos["count"],
                    take_profit_cents=take_profit_cents,
                )
        elif client.has_existing_position_or_order(ticker):
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Already hold an active position or resting order for {ticker}. Skipping buy.")
        else:
            # Screen for candidates within target buy price and aligned with Spot Momentum
            candidate_sides = []
            for side, p_cents, aligned in [
                ("yes", yes_price_cents, is_spot_momentum_aligned("yes", spot_price, target_price)),
                ("no", no_price_cents, is_spot_momentum_aligned("no", spot_price, target_price)),
            ]:
                if p_cents is not None and min_buy_price <= p_cents <= max_buy_price and aligned:
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
    is_stop_loss_interval = (minute_into_market == 10) and not force_trade_mark

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {now_utc.isoformat()} (Minute :{current_minute:02d}:{now_utc.second:02d})")
    if dry_run:
        log.info(f"--> MODE: {LogTag.DRY_RUN_PREVIEW.value} (No real orders will be placed)")
    log.info(
        "--> INTERVAL: "
        + ("BUY TRADING & 5-MIN CUT-LOSS / DATA COLLECTION MARK (:00, :05, :15, :20...)" if is_trading_interval
           else "STOP-LOSS EVALUATION & DATA COLLECTION MARK (:10, :25...)" if is_stop_loss_interval
           else "DATA COLLECTION ONLY MARK")
    )
    log.info("=================================================================")

    # Initialize fallback clients if persistent instances were not passed
    active_clients = []
    if clients is None:
        try:
            active_clients.append(("Kalshi", KalshiClient()))
        except Exception as error:
            log.error(f"Could not initialize KalshiClient: {error}")

        try:
            active_clients.append(("Gemini", GeminiClient()))
        except Exception as error:
            log.error(f"Could not initialize GeminiClient: {error}")
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
    valid_snapshots = [s for s in snapshots_to_save if s.get("y") is not None or s.get("n") is not None]
    if valid_snapshots and not dry_run:
        log.info("\nSaving ultra-compact market snapshot data to MongoDB Atlas...")
        save_market_snapshots(valid_snapshots)
    elif dry_run:
        log.info(f"\n{LogTag.DRY_RUN_PREVIEW.value} Skipped saving snapshots to MongoDB Atlas in Dry-Run mode.")
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

    try:
        persistent_clients.append(("Gemini", GeminiClient()))
    except Exception as error:
        log.error(f"Could not initialize persistent GeminiClient: {error}")

    if run_once or dry_run:
        run_pipeline_pass(clients=persistent_clients, dry_run=dry_run, force_trade_mark=force_trade_mark)
        return

    log.info("Starting Trading Bot in Continuous Render Background Worker Mode...")
    last_executed_bucket: tuple[int, int, int, int, int] | None = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            current_bucket = (now.year, now.month, now.day, now.hour, now.minute // 5)

            # Guard against double-executing within the same 5-minute bucket
            if current_bucket != last_executed_bucket or force_trade_mark:
                last_executed_bucket = current_bucket
                run_pipeline_pass(clients=persistent_clients, dry_run=dry_run, force_trade_mark=force_trade_mark)
            else:
                log.warning("Already executed pass for this 5-minute bucket. Waiting for next mark...")

        except Exception as error:
            log.error(f"Unexpected error in loop pass: {error}")

        sleep_seconds = calculate_seconds_until_next_5min_mark(offset_seconds=30.0)
        next_mark_min = (datetime.now(timezone.utc).minute // 5 * 5 + 5) % 60
        log.info(
            f"\n{LogTag.CLOCK_ALIGNMENT.value} Sleeping {sleep_seconds:.1f}s until 30s past next 5-minute mark (:{next_mark_min:02d}:30)...\n"
        )
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
