"""
MongoDB Database Connection & Snapshot Logger
==============================================

What is MongoDB?
----------------
MongoDB is a "NoSQL" database that stores data in flexible, JSON-like documents.
Unlike traditional SQL databases (like MySQL or Postgres) that require strict tables and columns,
MongoDB allows us to save dictionary objects directly as documents.

How it works with Render.com & MongoDB Atlas:
---------------------------------------------
1. Render.com: Hosts our Python script and runs it on a 5-minute schedule.
2. MongoDB Atlas: A free cloud database service provided by MongoDB.
3. We store the database connection string in an Environment Variable named `MONGODB_URI`
   (e.g., `mongodb+srv://user:password@cluster.mongodb.net/...`).
4. Every 5 minutes, our script connects to MongoDB Atlas and inserts market snapshot documents.

Time-Series Graphing (Data Engineering Goal):
----------------------------------------------
Each saved document contains:
- `timestamp`: The exact date and time the snapshot was recorded (ISO string).
- `minute_past_hour`: Minute integer (e.g. 0, 5, 10, 15...).
- `ticker`: Market identifier (e.g. 'KXBTC15M-26AUG132245-45').
- `asset`: Cryptocurrency symbol (e.g. 'BTC', 'ETH', 'SOL').
- `yes_price`: Current YES price in cents (1-99).
- `no_price`: Current NO price in cents (1-99).
- `trade_executed`: Boolean indicating if a buy trade was placed on this 15-min mark.
- `trade_details`: Dictionary containing trade execution details (side, buy price, 75¢ target).

You can later query this collection using Python (Pandas/Matplotlib/Streamlit) to plot:
- X-axis: `timestamp` (time)
- Y-axis: `yes_price` or `no_price` (volatility & probability tracking over time)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def get_mongo_collection():
    """
    Establishes a connection to MongoDB Atlas using the MONGODB_URI environment variable.

    Returns:
        pymongo.collection.Collection object if connected, or None if URI is missing/fails.
    """
    # Step 1: Read the connection string from environment variables
    mongo_uri = os.getenv("MONGODB_URI")

    if not mongo_uri:
        log.warning(
            "MONGODB_URI is not set in your .env file or Render environment settings. "
            "Data will be logged locally, but not saved to MongoDB."
        )
        return None

    # Step 2: Attempt to connect using pymongo
    try:
        import pymongo

        # Create client with a 5-second connection timeout
        client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)

        # Access the database named 'trading_bot_db'
        db = client.get_database("trading_bot_db")

        # Access the collection named 'market_snapshots'
        collection = db.get_collection("market_snapshots")
        return collection
    except Exception as error:
        log.error(f"Could not connect to MongoDB Atlas: {error}")
        return None


def save_market_snapshots(snapshots: list[dict]) -> bool:
    """
    Saves a list of market snapshot documents into MongoDB.

    Args:
        snapshots: List of dictionary records containing timestamp, prices, and trade details.

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
            f"Successfully saved {len(result.inserted_ids)} market snapshot document(s) to MongoDB Atlas!"
        )
        return True
    except Exception as error:
        log.error(f"Failed to insert snapshots into MongoDB: {error}")
        return False
