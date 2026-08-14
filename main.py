"""
15-Minute Crypto Prediction Trading & Data Pipeline Bot
=========================================================

High School Computer Science Explainer:
--------------------------------------
This bot runs automatically every 5 minutes (triggered by a host like Render.com).

1. How the Schedule Works (Modulo Operator %):
   - The current minute is fetched (e.g. 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55).
   - We use the modulo operator `% 15` to test if the minute is divisible by 15.
   - If `minute % 15 == 0` (i.e. minute is :00, :15, :30, or :45):
       --> TRADING + DATA COLLECTION MARK: We check prices, buy if between 25¢-45¢, set 75¢ auto-sell, and log to MongoDB.
   - If `minute % 15 != 0` (i.e. minute is :05, :10, :20, :25, :35, :40, :50, or :55):
       --> DATA COLLECTION ONLY MARK: We record prices into MongoDB for graphing/volatility engineering, but place NO trades.

2. Trading Strategy (25¢ - 45¢ Buy Rule):
   - For active 15-minute crypto markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE):
   - If YES price is between 25¢ and 45¢ (inclusive):
       1. Buy 1 share of YES.
       2. Place a resting Sell limit order at 75¢ (Take Profit auto-sell).
   - If NO price is between 25¢ and 45¢ (inclusive):
       1. Buy 1 share of NO.
       2. Place a resting Sell limit order at 75¢ (Take Profit auto-sell).

3. Dry-Run Mode (--dry-run):
   - Run `python main.py --dry-run` to preview strategy evaluation without placing any real orders.
"""

from __future__ import annotations

import logging
import sys
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


def extract_price_cents(market: dict, side: str) -> int | None:
    """
    Extracts the price in cents (1-99) for either 'yes' or 'no' from a Kalshi market dict.
    Supports both dollar string fields (e.g. '0.3500') and integer cent fields.
    """
    side_clean = side.lower()

    if side_clean == "yes":
        dollar_str = market.get("yes_ask_dollars") or market.get("yes_bid_dollars")
        cent_val = market.get("yes_ask") or market.get("yes_bid") or market.get("last_price")
    else:
        dollar_str = market.get("no_ask_dollars") or market.get("no_bid_dollars")
        cent_val = market.get("no_ask") or market.get("no_bid")

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
    timestamp_iso: str,
    minute_past_hour: int,
    dry_run: bool = False,
) -> dict:
    """
    Processes a single 15-minute crypto market:
    1. Reads YES and NO prices.
    2. Evaluates 25¢-45¢ buy condition if it is a 15-minute trading mark (:00, :15, :30, :45).
    3. Returns a clean snapshot dictionary to be saved in MongoDB.
    """
    ticker = market.get("ticker", "UNKNOWN")
    title = market.get("title", "15-Minute Crypto Market")
    series_ticker = market.get("series_ticker", "UNKNOWN")

    # Extract prices in cents
    yes_price_cents = extract_price_cents(market, "yes")
    no_price_cents = extract_price_cents(market, "no")

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
                trade_details = {
                    "dry_run": True,
                    "action": "would_buy",
                    "side": "yes",
                    "buy_price_cents": yes_price_cents,
                    "take_profit_cents": 75,
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
                    trade_details = result
                except Exception as error:
                    log.error(f"Failed to execute YES trade on {ticker}: {error}")

        # Check NO price condition: between 25¢ and 45¢ (inclusive)
        elif no_price_cents is not None and 25 <= no_price_cents <= 45:
            if dry_run:
                log.info(
                    f"🎯 [DRY-RUN MATCH] {ticker} -> NO price is {no_price_cents}¢ "
                    f"(between 25¢ and 45¢). WOULD BUY 1 SHARE OF [NO] + SET 75¢ AUTO-SELL."
                )
                trade_details = {
                    "dry_run": True,
                    "action": "would_buy",
                    "side": "no",
                    "buy_price_cents": no_price_cents,
                    "take_profit_cents": 75,
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
                    trade_details = result
                except Exception as error:
                    log.error(f"Failed to execute NO trade on {ticker}: {error}")

    # Step 3: Build structured MongoDB snapshot document
    snapshot_document = {
        "timestamp": timestamp_iso,
        "minute_past_hour": minute_past_hour,
        "ticker": ticker,
        "title": title,
        "series_ticker": series_ticker,
        "yes_bid_dollars": market.get("yes_bid_dollars"),
        "yes_ask_dollars": market.get("yes_ask_dollars"),
        "no_bid_dollars": market.get("no_bid_dollars"),
        "no_ask_dollars": market.get("no_ask_dollars"),
        "yes_price_cents": yes_price_cents,
        "no_price_cents": no_price_cents,
        "volume": market.get("volume"),
        "open_interest": market.get("open_interest"),
        "is_trading_mark": is_trading_interval,
        "trade_executed": trade_executed,
        "trade_details": trade_details,
    }

    return snapshot_document


def main():
    """
    Main bot execution loop. Supports --dry-run / --force-trade flags for testing.
    """
    args = sys.argv[1:]
    dry_run = "--dry-run" in args or "-d" in args
    force_trade_mark = "--force-trade" in args or "-f" in args

    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute
    timestamp_iso = now_utc.isoformat()

    # Determine if current minute is a 15-minute trading mark (:00, :15, :30, :45)
    is_trading_interval = (current_minute % 15 == 0) or force_trade_mark

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {timestamp_iso}")
    log.info(f"Current Minute Past Hour: :{current_minute:02d}")
    
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
            timestamp_iso=timestamp_iso,
            minute_past_hour=current_minute,
            dry_run=dry_run,
        )
        snapshots_to_save.append(snapshot)
        
        ticker = snapshot["ticker"]
        y_cents = snapshot["yes_price_cents"]
        n_cents = snapshot["no_price_cents"]
        log.info(f"  • Market [{ticker}] -> YES: {y_cents}¢ | NO: {n_cents}¢")

    # Step 4: Save all collected snapshot documents to MongoDB Atlas
    log.info("\nSaving market snapshot data to MongoDB Atlas...")
    save_market_snapshots(snapshots_to_save)

    log.info("Execution complete.")


if __name__ == "__main__":
    main()
