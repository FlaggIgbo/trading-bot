"""
Main Trading Bot
=================
System design:
1. Ingestion Layer: Fetches open markets from Kalshi.
2. Risk Engine: If market is open, randomly bet 1 contract on YES or NO. Never sell, hold and hope for the best.
3. Logging Layer: Marked as TODO (ClickHouse in the future).
"""

import argparse
import time
from dotenv import load_dotenv

from ingestion.client import KalshiClient
from risk_engine.engine import RiskEngine

# Load environment variables from .env file
load_dotenv()


def main(single_cycle=False, sleep_seconds=10):
    print("Starting the simplified trading bot...")
    print("Strategy: If market is open, randomly bet 1 contract on YES or NO. Never sell, hold and hope for the best.")

    # Initialize the ingestion client
    client = KalshiClient()

    # Initialize the risk engine
    engine = RiskEngine(client=client)

    # Main loop
    try:
        while True:
            # Step 1: Ingestion - fetch all open markets
            markets = client.get_open_markets()
            print(f"Fetched {len(markets)} open markets.")

            # Step 2: Risk Engine - randomly bet 1 contract on any newly opened market
            engine.evaluate_all(markets)

            # If user passed --once, exit after one cycle
            if single_cycle:
                break

            # Wait before checking again
            time.sleep(sleep_seconds)

    except KeyboardInterrupt:
        print("\nStopping bot. Bye!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple prediction market trading bot")
    parser.add_argument("--once", action="store_true", help="Run once and stop")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between cycles")
    args = parser.parse_args()

    main(single_cycle=args.once, sleep_seconds=args.interval)
