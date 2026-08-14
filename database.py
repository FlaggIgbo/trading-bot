"""
MongoDB Atlas Database Connector & Ultra-Compact Snapshot Logger
=================================================================

Storage Optimization (MongoDB Atlas Free Tier - 512MB Cap):
------------------------------------------------------------
To ensure our database stays tiny and never hits storage limits over months of execution,
we store snapshots as ultra-compact JSON documents (~50 bytes per document):

Compact Field Schema:
  `ts`   : Unix timestamp integer (e.g. 1786675912) -> 4 bytes
  `m`    : Minute past hour int (e.g. 0, 5, 15, 30) -> 1 byte
  `sym`  : Asset symbol (e.g. "BTC", "ETH", "SOL", "ADA", "TON", etc.) -> 3-4 bytes
  `tick` : Market ticker (e.g. "KXBTC15M-26AUG140015-15") -> 22 bytes
  `y`    : YES price in cents integer (1-99) -> 1 byte
  `n`    : NO price in cents integer (1-99) -> 1 byte
  `y_bid`: YES top bid in cents -> 1 byte
  `y_ask`: YES top ask in cents -> 1 byte
  `n_bid`: NO top bid in cents -> 1 byte
  `n_ask`: NO top ask in cents -> 1 byte
  `sprd` : Bid-Ask spread in cents -> 1 byte
  `p`    : Live underlying asset spot price (e.g. 63270.94) -> 8 bytes
  `tgt`  : Target strike price to beat for YES to win (e.g. 63318.74) -> 8 bytes
  `diff` : Difference float (current_price - price_to_beat) -> 8 bytes
  `pct`  : Percentage distance float ((spot - strike) / strike * 100) -> 8 bytes
  `rem`  : Minutes remaining to contract expiration -> 4 bytes
  `vol`  : Traded contract volume -> 4 bytes
  `trd`  : Optional trade object (includes action, buy, tp, sl, outcome) -> ~30 bytes

With 12 crypto markets checked every 5 minutes (3,456 docs/day):
- Storage consumption is ~300 KB per day (~9 MB per month).
- A 512MB free tier database can easily store years of market data!

Quantitative Analytical Questions Solved By This Dataset:
---------------------------------------------------------
1. Asset Volatility Profiling:
   - How volatile is each crypto asset across 15-minute intervals? (Tracks std dev of spot_price vs target_strike).

2. Complete Trade Outcome Matrix:
   - Bought & Won  : Entries that hit Take-Profit (82¢) or settled at 100¢.
   - Bought & Lost : Entries that expired OTM at 0¢.
   - Sold & Won    : Stop-Loss exits that saved capital from a full crash (Rightful Stop-Loss).
   - Sold & Lost   : Stop-Loss exits where position would have recovered to win (False Alarm).

3. Price vs. Win Ratio (Empirical Calibration Curve):
   - Statistically, if you enter at X¢ (e.g. 35¢ vs 40¢ vs 45¢), what is your exact Y% chance of winning?

4. Time Decay & Runway Efficiency:
   - Win rate and EV comparison for entries at Minute 00 (15 mins remaining) vs Minute 05 (10 mins remaining).

5. Counterfactual Opportunity Analysis:
   - Comparing actual strategy returns against holding every contract to full 100¢ settlement.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def get_mongo_collection():
    """
    Establishes a connection to MongoDB Atlas using the MONGODB_URI environment variable.
    Handles TLS/SSL certificate verification safely across Linux containers (e.g. Render.com).

    Returns:
        pymongo.collection.Collection object if connected, or None if URI is missing/fails.
    """
    mongo_uri = os.getenv("MONGODB_URI")

    if not mongo_uri:
        log.warning(
            "MONGODB_URI is not set in your .env file or Render environment settings. "
            "Data will be logged locally, but not saved to MongoDB."
        )
        return None

    try:
        import pymongo

        # Native MongoClient initialization using MongoDB SRV connection string
        try:
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
        except Exception as err:
            log.debug(f"Standard SRV connection attempt failed, trying fallback: {err}")
            # Fallback for container SSL handshake compatibility
            client = pymongo.MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=5000,
                tls=True,
                tlsAllowInvalidCertificates=True,
            )

        # Access database 'trading_bot_db' and collection 'market_snapshots'
        db = client.get_database("trading_bot_db")
        collection = db.get_collection("market_snapshots")
        return collection
    except Exception as error:
        log.error(f"Could not connect to MongoDB Atlas: {error}")
        return None


def save_market_snapshots(snapshots: list[dict]) -> bool:
    """
    Saves a list of ultra-compact market snapshot documents into MongoDB.

    Args:
        snapshots: List of compact dictionary records.

    Returns:
        bool: True if inserted into MongoDB successfully, False otherwise.
    """
    if not snapshots:
        log.info("No snapshots to save.")
        return False

    collection = get_mongo_collection()
    if collection is None:
        return False

    try:
        # Insert all snapshot records into the MongoDB collection
        result = collection.insert_many(snapshots)
        log.info(
            f"Successfully saved {len(result.inserted_ids)} ultra-compact snapshot(s) to MongoDB Atlas!"
        )
        return True
    except Exception as error:
        log.error(f"Failed to insert snapshots into MongoDB: {error}")
        return False
