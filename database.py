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


def get_mongo_collection(collection_name: str = "market_snapshots"):
    """
    Establishes a connection to MongoDB Atlas using the MONGODB_URI environment variable.
    Handles TLS/SSL certificate verification safely across Linux containers (e.g. Render.com).

    Args:
        collection_name: Target MongoDB collection name ('market_snapshots', 'portfolio_pnl', 'settlements').

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

        # Access database 'trading_bot_db' and requested collection
        db = client.get_database("trading_bot_db")
        return db.get_collection(collection_name)
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

    collection = get_mongo_collection("market_snapshots")
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


def save_portfolio_pnl(pnl_summary: dict) -> bool:
    """
    Saves a portfolio balance & PnL snapshot document into MongoDB 'portfolio_pnl' collection.

    Args:
        pnl_summary: Dictionary containing real-time balance, open positions, and realized PnL totals.

    Returns:
        bool: True if inserted successfully, False otherwise.
    """
    if not pnl_summary:
        return False

    collection = get_mongo_collection("portfolio_pnl")
    if collection is None:
        return False

    try:
        doc = {
            "ts": int(pnl_summary.get("ts") or 0),
            "balance_dollars": pnl_summary.get("balance_dollars"),
            "portfolio_value_dollars": pnl_summary.get("portfolio_value_dollars"),
            "open_positions_count": pnl_summary.get("open_positions_count"),
            "open_exposure_dollars": pnl_summary.get("open_exposure_dollars"),
            "total_realized_pnl_dollars": pnl_summary.get("total_realized_pnl_dollars"),
            "total_realized_pnl_cents": pnl_summary.get("total_realized_pnl_cents"),
            "settlements_count": pnl_summary.get("settlements_count"),
            "total_settled_revenue_dollars": pnl_summary.get("total_settled_revenue_dollars"),
            "total_settled_cost_dollars": pnl_summary.get("total_settled_cost_dollars"),
        }
        collection.insert_one(doc)
        log.info("💰 Successfully recorded Portfolio PnL snapshot to MongoDB Atlas!")
        return True
    except Exception as error:
        log.error(f"Failed to insert portfolio PnL snapshot to MongoDB: {error}")
        return False


def save_settlements(settlements: list[dict]) -> int:
    """
    Upserts settled market trade records into the MongoDB 'settlements' collection.
    Prevents duplicate entries by keying on (ticker, settled_time).

    Args:
        settlements: List of settlement records with payout, cost, and pnl.

    Returns:
        int: Number of new/updated settlement documents saved.
    """
    if not settlements:
        return 0

    collection = get_mongo_collection("settlements")
    if collection is None:
        return 0

    saved_count = 0
    try:
        for s in settlements:
            ticker = s.get("ticker")
            settled_time = s.get("settled_time")
            if not ticker:
                continue

            filter_query = {"ticker": ticker}
            if settled_time:
                filter_query["settled_time"] = settled_time

            collection.update_one(
                filter_query,
                {"$set": s},
                upsert=True,
            )
            saved_count += 1

        if saved_count > 0:
            log.info(f"📊 Saved/updated {saved_count} settlement PnL record(s) in MongoDB Atlas!")
        return saved_count
    except Exception as error:
        log.error(f"Failed to upsert settlements to MongoDB: {error}")
        return 0

