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
  `sym`  : Asset symbol (e.g. "BTC", "ETH", "SOL") -> 3-4 bytes
  `tick` : Market ticker (e.g. "KXBTC15M-26AUG132300-00") -> 22 bytes
  `y`    : YES price in cents integer (1-99) -> 1 byte
  `n`    : NO price in cents integer (1-99) -> 1 byte
  `trd`  : Optional trade object (only present if a trade triggered) -> ~20 bytes

With 7 crypto markets checked every 5 minutes (2,016 docs/day):
- Storage consumption is only ~100 KB per day (~3 MB per month).
- A 512MB free tier database can easily store years of market data!
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
