"""
15-Minute Crypto Prediction Trading & Data Pipeline Bot
=========================================================

High School Computer Science Explainer:
--------------------------------------
This bot runs continuously 24/7 as a Render Background Worker.

1. Why We Execute 30 Seconds Past the 5-Minute Mark (:00:30, :05:30, :10:30...):
   - At exactly :00.000, :15.000, etc., Kalshi's exchange engine is actively expiring 
     the old 15-minute contracts and initializing the new ones.
   - Quicker API requests right at :00.000 can catch the orderbook in a brief reset state 
     (returning `None` for bid/ask prices).
   - By adding a 30-second buffer (:00:30, :05:30, :10:30, :15:30, :20:30, :25:30, :30:30, 
     :35:30, :40:30, :45:30, :50:30, :55:30), we give Kalshi's API 30 seconds to populate 
     active, live bids and asks!

2. Trading Strategy (25¢ - 45¢ Buy Rule on 15-Min Marks):
   - Every 15-minute mark (:00:30, :15:30, :30:30, :45:30):
     - If YES price is between 25¢ and 45¢ (inclusive):
         1. Buy 1 share of YES.
         2. Place a resting Sell limit order at 75¢ (Take Profit auto-sell).
     - If NO price is between 25¢ and 45¢ (inclusive):
         1. Buy 1 share of NO.
         2. Place a resting Sell limit order at 75¢ (Take Profit auto-sell).

3. Data Logging & MongoDB Atlas (Every 5 Minutes):
   - At every 5-minute mark (:00:30, :05:30, :10:30...), ultra-compact snapshot records 
     (~50 bytes each) are saved to MongoDB Atlas for volatility graphing.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone

from database import save_market_snapshots
from signals.kalshi_client import KalshiClient, SERIES_15M_CRYPTO

# Set up logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def calculate_seconds_until_next_5min_mark(offset_seconds: float = 30.0) -> float:
    """
    Calculates the exact number of seconds to sleep until 30 seconds past the next 5-minute clock mark
    (:00:30, :05:30, :10:30, :15:30, :20:30, :25:30, :30:30, :35:30, :40:30, :45:30, :50:30, :55:30).

    Args:
        offset_seconds: Number of seconds to delay past the 5-minute mark (default: 30.0s).

    Returns:
        float: Seconds to sleep.
    """
    now = datetime.now(timezone.utc)
    # Calculate seconds past the previous 5-minute mark (:00, :05, :10...)
    seconds_past_5min = (now.minute % 5) * 60 + now.second + now.microsecond / 1_000_000.0

    # Wait time = 300 seconds (5 mins) - elapsed seconds + offset_seconds
    wait_seconds = 300.0 - seconds_past_5min + offset_seconds

    # Adjust if wait_seconds exceeds 300.0 (e.g. if we just passed the :30 mark by a few milliseconds)
    if wait_seconds >= 300.0:
        wait_seconds -= 300.0

    return max(0.5, wait_seconds)


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """Extracts short crypto asset symbol (e.g. 'BTC', 'ETH', 'SOL')."""
    combined = f"{series_ticker} {ticker}".upper()
    for symbol in ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]:
        if symbol in combined:
            return symbol
    return series_ticker or "CRYPTO"


def extract_price_cents(market: dict, side: str, kalshi_client: KalshiClient | None = None) -> int | None:
    """
    Extracts the price in cents (1-99) for either 'yes' or 'no' from a Kalshi market dict.
    Supports dollar string fields (e.g. '0.3500') and integer cent fields.
    If market summary returns None, attempts a fallback API call for fresh market detail.
    """
    side_clean = side.lower()

    if side_clean == "yes":
        dollar_str = market.get("yes_ask_dollars") or market.get("yes_bid_dollars")
        cent_val = market.get("yes_ask") or market.get("yes_bid") or market.get("last_price")
    else:
        dollar_str = market.get("no_ask_dollars") or market.get("no_bid_dollars")
        cent_val = market.get("no_ask") or market.get("no_bid")

    # Fallback: If prices are None in summary payload, fetch detailed single-market object
    if dollar_str is None and cent_val is None and kalshi_client is not None:
        ticker = market.get("ticker")
        if ticker:
            try:
                detail = kalshi_client.get_market(ticker).get("market", {})
                if side_clean == "yes":
                    dollar_str = detail.get("yes_ask_dollars") or detail.get("yes_bid_dollars")
                    cent_val = detail.get("yes_ask") or detail.get("yes_bid") or detail.get("last_price")
                else:
                    dollar_str = detail.get("no_ask_dollars") or detail.get("no_bid_dollars")
                    cent_val = detail.get("no_ask") or detail.get("no_bid")
            except Exception as err:
                log.debug(f"Fallback get_market detail failed for {ticker}: {err}")

    if dollar_str is not None:
        try:
            val = int(round(float(dollar_str) * 100))
            if 1 <= val <= 99:
                return val
        except (ValueError, TypeError):
            pass

    if cent_val is not None:
        try:
            val = int(cent_val)
            if 1 <= val <= 99:
                return val
        except (ValueError, TypeError):
            pass

    return None


def process_single_market(
    market: dict,
    is_trading_interval: bool,
    kalshi_client: KalshiClient,
    now_utc: datetime,
    minute_past_hour: int,
    dry_run: bool = False,
) -> dict:
    """
    Processes a single 15-minute crypto market:
    1. Reads YES and NO prices (with fallback to fresh detail if needed).
    2. Evaluates 25¢-45¢ buy condition if it is a 15-minute trading mark (:00, :15, :30, :45).
    3. Returns an ultra-compact snapshot dictionary to save into MongoDB.
    """
    ticker = market.get("ticker", "UNKNOWN")
    series_ticker = market.get("series_ticker", "UNKNOWN")
    asset_symbol = extract_asset_symbol(series_ticker, ticker)

    # Extract prices in cents (with Kalshi client fallback for live prices)
    yes_price_cents = extract_price_cents(market, "yes", kalshi_client=kalshi_client)
    no_price_cents = extract_price_cents(market, "no", kalshi_client=kalshi_client)

    trade_executed = False
    trade_details = None

    # Step 2: Trading Decision (Only on 15-minute marks: :00, :15, :30, :45)
    if is_trading_interval:
        # Check YES price condition: between 25¢ and 45¢ (inclusive)
        if yes_price_cents is not None and 25 <= yes_price_cents <= 45:
            if dry_run:
                log.info(
                    f"🎯 [DRY-RUN MATCH] {ticker} -> YES price is {yes_price_cents}¢ "
                    f"(between 25¢ and 45¢). WOULD BUY 1 SHARE OF [YES] + SET 75¢ AUTO-SELL."
                )
                trade_executed = True
                trade_details = {
                    "dry_run": True,
                    "side": "yes",
                    "buy": yes_price_cents,
                    "tp": 75,
                }
            else:
                log.info(
                    f"[STRATEGY MATCH] {ticker} YES price is {yes_price_cents}¢ "
                    f"(between 25¢ and 45¢). Executing Buy + 75¢ Auto-Sell!"
                )
                try:
                    result = kalshi_client.buy_and_set_take_profit(
                        ticker=ticker,
                        side="yes",
                        buy_price_cents=yes_price_cents,
                        take_profit_cents=75,
                        count=1,
                    )
                    trade_executed = True
                    trade_details = {
                        "side": "yes",
                        "buy": yes_price_cents,
                        "tp": 75,
                    }
                except Exception as error:
                    log.error(f"Failed to execute YES trade on {ticker}: {error}")

        # Check NO price condition: between 25¢ and 45¢ (inclusive)
        elif no_price_cents is not None and 25 <= no_price_cents <= 45:
            if dry_run:
                log.info(
                    f"🎯 [DRY-RUN MATCH] {ticker} -> NO price is {no_price_cents}¢ "
                    f"(between 25¢ and 45¢). WOULD BUY 1 SHARE OF [NO] + SET 75¢ AUTO-SELL."
                )
                trade_executed = True
                trade_details = {
                    "dry_run": True,
                    "side": "no",
                    "buy": no_price_cents,
                    "tp": 75,
                }
            else:
                log.info(
                    f"[STRATEGY MATCH] {ticker} NO price is {no_price_cents}¢ "
                    f"(between 25¢ and 45¢). Executing Buy + 75¢ Auto-Sell!"
                )
                try:
                    result = kalshi_client.buy_and_set_take_profit(
                        ticker=ticker,
                        side="no",
                        buy_price_cents=no_price_cents,
                        take_profit_cents=75,
                        count=1,
                    )
                    trade_executed = True
                    trade_details = {
                        "side": "no",
                        "buy": no_price_cents,
                        "tp": 75,
                    }
                except Exception as error:
                    log.error(f"Failed to execute NO trade on {ticker}: {error}")

    # Step 3: Build ultra-compact MongoDB snapshot document (~50 bytes)
    compact_document = {
        "ts": int(now_utc.timestamp()),   # Unix epoch timestamp (int)
        "m": minute_past_hour,            # Minute int (e.g. 0, 5, 15)
        "sym": asset_symbol,              # Asset symbol ("BTC", "ETH", "SOL", etc.)
        "tick": ticker,                   # Full market ticker
        "y": yes_price_cents,             # YES price in cents
        "n": no_price_cents,              # NO price in cents
    }

    if trade_executed and trade_details:
        compact_document["trd"] = trade_details

    return compact_document


def run_pipeline_pass(dry_run: bool = False, force_trade_mark: bool = False):
    """
    Executes a single market processing & trading pass.
    """
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute
    timestamp_iso = now_utc.isoformat()

    is_trading_interval = (current_minute % 15 == 0) or force_trade_mark

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {timestamp_iso}")
    log.info(f"Current Minute Past Hour: :{current_minute:02d}:{now_utc.second:02d}")

    if dry_run:
        log.info("--> MODE: [DRY-RUN PREVIEW] (No real orders will be placed)")

    if is_trading_interval:
        log.info("--> INTERVAL: 15-MINUTE TRADING & DATA COLLECTION MARK (:00, :15, :30, :45)")
    else:
        log.info("--> INTERVAL: DATA COLLECTION ONLY MARK (:05, :10, :20, :25, :35, :40, :50, :55)")
    log.info("=================================================================")

    # Initialize Kalshi API Client
    try:
        client = KalshiClient()
    except Exception as error:
        log.error(f"Could not initialize KalshiClient: {error}")
        return

    # Query active 15-minute crypto markets
    log.info("Fetching active 15-minute crypto markets from Kalshi...")
    try:
        markets = client.get_15m_crypto_markets(status="open")
        log.info(f"Retrieved {len(markets)} active 15-minute crypto markets.")
    except Exception as error:
        log.error(f"Failed to fetch 15-minute crypto markets: {error}")
        return

    # Process each market: check buy conditions and collect snapshots
    snapshots_to_save = []
    for market in markets:
        snapshot = process_single_market(
            market=market,
            is_trading_interval=is_trading_interval,
            kalshi_client=client,
            now_utc=now_utc,
            minute_past_hour=current_minute,
            dry_run=dry_run,
        )
        snapshots_to_save.append(snapshot)

        ticker = snapshot["tick"]
        sym = snapshot["sym"]
        y_cents = snapshot["y"]
        n_cents = snapshot["n"]
        log.info(f"  • Market [{sym} | {ticker}] -> YES: {y_cents}¢ | NO: {n_cents}¢")

    # Filter out incomplete snapshots (where both YES and NO prices are None)
    valid_snapshots = [s for s in snapshots_to_save if s.get("y") is not None or s.get("n") is not None]

    if valid_snapshots:
        log.info("\nSaving ultra-compact market snapshot data to MongoDB Atlas...")
        save_market_snapshots(valid_snapshots)
    else:
        log.warning("\nNo valid price snapshots retrieved in this pass. Skipping MongoDB Atlas save.")
    log.info("Pass complete.")


def main():
    """
    Main bot execution loop.
    Runs continuously on Render Background Worker by default, aligning to 30s past 5-minute marks.
    Use --once for a single pass or --dry-run for preview mode.
    """
    args = sys.argv[1:]
    dry_run = "--dry-run" in args or "-d" in args
    force_trade_mark = "--force-trade" in args or "-f" in args
    run_once = "--once" in args or "-o" in args

    if run_once or dry_run:
        # Run a single pass and exit (useful for testing or single-run execution)
        run_pipeline_pass(dry_run=dry_run, force_trade_mark=force_trade_mark)
    else:
        # Continuous Render Background Worker loop
        log.info("Starting Trading Bot in Continuous Render Background Worker Mode...")
        while True:
            try:
                run_pipeline_pass(dry_run=dry_run, force_trade_mark=force_trade_mark)
            except Exception as error:
                log.error(f"Unexpected error in loop pass: {error}")

            # Calculate exact seconds to sleep until 30 seconds past the next 5-minute mark
            sleep_seconds = calculate_seconds_until_next_5min_mark(offset_seconds=30.0)
            next_mark_min = (datetime.now(timezone.utc).minute // 5 * 5 + 5) % 60
            log.info(
                f"\n[CLOCK ALIGNMENT] Sleeping {sleep_seconds:.1f}s until 30s past next 5-minute mark (:{next_mark_min:02d}:30)...\n"
            )
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
