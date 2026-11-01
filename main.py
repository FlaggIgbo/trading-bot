"""
Main Trading Bot (One Process, One Polling Loop)
================================================
System design:
1. Scanner: Fetch + filter open binary markets from Kalshi -> upsert into SQLite.
2. Trader: Read candidates + open positions from SQLite -> place 1-contract random bet -> write OPEN to SQLite.
3. Settler: Read OPEN positions where close_time < now -> GET resolution from Kalshi -> write SETTLED + PnL to SQLite.
"""

import argparse
import time
from dotenv import load_dotenv

from db import Database
from ingestion.api.client import KalshiClient
from ingestion.scanner import Scanner
from risk_engine.engine import Trader
from risk_engine.settler import Settler

# Load environment variables from .env file
load_dotenv()


def main(single_cycle=False, sleep_seconds=10, db_path="trading_bot.db"):
    print("Starting the simplified trading bot...")
    print("Strategy: If market is open, randomly bet 1 contract on YES or NO. Never sell, hold and hope for the best.")

    # Initialize Kalshi REST API client and SQLite database (tagged by environment)
    client = KalshiClient()
    db = Database(db_path=db_path, environment=client.environment)
    print(f"Environment: {client.environment} ({client.base_url})")

    # Initialize the 3 pipeline steps
    scanner = Scanner(client=client, db=db)
    trader = Trader(client=client, db=db)
    settler = Settler(client=client, db=db)

    # Main polling loop
    try:
        while True:
            # 1. Scanner: fetch + filter markets and upsert into SQLite
            markets = scanner.scan()
            print(f"1. Scanner: Fetched and upserted {len(markets)} open binary markets.")

            # 2. Trader: risk checks + place orders and write OPEN to SQLite
            orders = trader.trade()
            print(f"2. Trader: Placed {len(orders)} new orders.")

            # 3. Settler: check resolutions + compute PnL and write SETTLED to SQLite
            settled = settler.settle()
            print(f"3. Settler: Settled {len(settled)} expired positions.")

            # If user passed --once, exit after one cycle
            if single_cycle:
                break

            # Wait before next polling cycle
            time.sleep(sleep_seconds)

    except KeyboardInterrupt:
        print("\nStopping bot. Bye!")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple prediction market trading bot")
    parser.add_argument("--once", action="store_true", help="Run once and stop")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between cycles")
    parser.add_argument("--db", type=str, default="trading_bot.db", help="Path to SQLite database file")
    args = parser.parse_args()

    main(single_cycle=args.once, sleep_seconds=args.interval, db_path=args.db)
