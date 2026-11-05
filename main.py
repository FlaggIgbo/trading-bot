"""
Main Trading Bot (One Process, Aligned 15-Minute Cycle)
=======================================================
System design:
1. Wait until the next :00:30, :15:30, :30:30, or :45:30 mark on the clock.
2. Scanner: Fetch + filter open binary 15-minute markets from Kalshi -> upsert into SQLite.
3. Settler: Read OPEN positions where close_time < now -> GET resolution from Kalshi -> write SETTLED + PnL to SQLite.
4. Trader: Read candidates + open positions from SQLite -> place 1-contract bet -> write OPEN to SQLite.
"""

import argparse
import time
from dotenv import load_dotenv

from db import Database
from ingestion.api.client import KalshiClient
from ingestion.scanner import Scanner
from logging.analytics import analytics
from risk_engine.engine import Trader
from risk_engine.settler import Settler

# Load environment variables from .env file
load_dotenv()


def seconds_until_next_slot(now=None, interval=900, offset=30):
    # Every 15 minutes (900s) is :00, :15, :30, :45.
    # Adding offset=30 targets :00:30, :15:30, :30:30, and :45:30.
    if now is None:
        now = time.time()
    elapsed = (now - offset) % interval
    wait = interval - elapsed
    return 0.0 if wait == interval else wait


def main(single_cycle=False, sleep_seconds=900, offset_seconds=30, db_path="trading_bot.db", strategy="last_3"):
    analytics.info(
        f"Starting trading bot (strategy={strategy}, schedule=every {sleep_seconds}s at +{offset_seconds}s)...",
        event="bot_start",
        strategy=strategy,
        sleep_seconds=sleep_seconds,
        offset_seconds=offset_seconds,
    )

    # Initialize Kalshi REST API client and SQLite database (tagged by environment)
    client = KalshiClient()
    db = Database(db_path=db_path, environment=client.environment)
    analytics.info(
        f"Environment: {client.environment} ({client.base_url})",
        event="environment_init",
        environment=client.environment,
        base_url=client.base_url,
    )

    # Initialize the 3 pipeline steps
    scanner = Scanner(client=client, db=db)
    settler = Settler(client=client, db=db)
    trader = Trader(client=client, db=db, strategy=strategy)

    # Main loop aligned to :00:30, :15:30, :30:30, :45:30
    try:
        while True:
            if not single_cycle and sleep_seconds > 0:
                wait_sec = seconds_until_next_slot(interval=sleep_seconds, offset=offset_seconds)
                analytics.info(
                    f"Waiting {wait_sec:.1f}s until next :{offset_seconds:02d}s slot...",
                    event="wait_slot",
                    wait_seconds=round(wait_sec, 1),
                )
                time.sleep(wait_sec)

            # 1. Scanner: fetch + filter open 15-minute binary markets and save into SQLite
            markets = scanner.scan()
            analytics.track(
                "markets_scanned",
                message=f"1. Scanner: Fetched and upserted {len(markets)} open binary markets.",
                count=len(markets),
            )

            # 2. Settler: settle the previous 15-minute positions that just closed
            settled = settler.settle()
            analytics.track(
                "positions_settled",
                message=f"2. Settler: Settled {len(settled)} expired positions.",
                count=len(settled),
            )

            # 3. Trader: place 1-contract bet on each new 15-minute market and let it ride
            orders = trader.trade()
            analytics.track(
                "orders_placed",
                message=f"3. Trader: Placed {len(orders)} new orders.",
                count=len(orders),
            )

            # If user passed --once, exit after one cycle
            if single_cycle:
                break

            # Pause 1s so we move past the current :30s boundary before computing the next slot
            time.sleep(1)

    except KeyboardInterrupt:
        analytics.info("Stopping bot. Bye!", event="bot_stop")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple 15-minute prediction market trading bot")
    parser.add_argument("--once", action="store_true", help="Run once immediately and stop")
    parser.add_argument("--interval", type=int, default=900, help="Window length in seconds (default: 900 = 15 mins)")
    parser.add_argument("--offset", type=int, default=30, help="Seconds past each window start (default: 30)")
    parser.add_argument("--db", type=str, default="trading_bot.db", help="Path to SQLite database file")
    parser.add_argument(
        "--strategy",
        type=str,
        default="last_3",
        choices=["last_3", "random"],
        help="Direction strategy: 'last_3' (majority of last 3 markets) or 'random'",
    )
    args = parser.parse_args()

    main(
        single_cycle=args.once,
        sleep_seconds=args.interval,
        offset_seconds=args.offset,
        db_path=args.db,
        strategy=args.strategy,
    )
