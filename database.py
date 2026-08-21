r"""
MongoDB Atlas Database Connector & Persistent Trade State Pipeline
===================================================================

Collections Managed:
--------------------
1. `trading_bot_db.market_snapshots`:
   - Ultra-compact 5-minute pricing, execution, and counterfactual empirical snapshots (~50 bytes per document).
   - Tagged by timeframe (`tf: "15m"` or `tf: "1h"`) across Crypto, Commodities (Gold, Silver, WTI), and Equity Indices.
2. `trading_bot_db.portfolio_pnl`:
   - Real-time time-series portfolio value, cash balance, and realized PnL snapshots.
3. `trading_bot_db.settlements`:
   - Settled market payout records, cost basis, revenue, and net profit/loss.
4. `trading_bot_db.active_positions`:
   - Crash-proof persistent store for active trade cost bases (`entry_price_cents`).
   - Ensures restart/redeploy resilience on Render background workers without falling back to arbitrary values.

Dynamic Calibration & Exploration Architecture:
------------------------------------------------
- 6-Hour Dynamic Calibration: Volatility ($\sigma$), empirical win rates, and optimal buy/TP corridors are calibrated from MongoDB.
- 15-Minute Discovery Window (`05:00–05:15 UTC`): Allows brief broad corridor discovery with a strict trade cap (3 trades/session).
- Counterfactual Empirical Tracking: Non-traded mid-zone opportunities (37¢–55¢) are logged as dry-run counterfactuals for risk-free data collection.

Storage Optimization (MongoDB Atlas Free Tier - 512MB Cap):
------------------------------------------------------------
To ensure our database stays tiny and never hits storage limits over months of execution,
we store snapshots as ultra-compact JSON documents (~50 bytes per document).
With 17 multi-asset markets checked across intervals:
- Storage consumption is ~450 KB per day (~13 MB per month).
- A 512MB free tier database easily stores years of market data!
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
try:
    import zoneinfo
except ImportError:
    zoneinfo = None
from pathlib import Path

from dotenv import load_dotenv

# Load configuration from .env file if available
load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)


_GLOBAL_MONGO_CLIENT = None
_INDEXES_INITIALIZED = False


def ensure_database_indexes(db=None) -> bool:
    """
    Ensures optimal query performance and automated TTL retention indexes are configured.
    - market_snapshots: TTL index on 'created_at' (expiring records older than SNAPSHOT_RETENTION_HOURS, default: 24h).
    - market_snapshots: Compound index on [('ts', -1)] and [('sym', 1), ('tf', 1), ('ts', -1)].
    - settlements: Indexes on [('ts', -1)] and [('ticker', 1)].
    - portfolio_pnl: Index on [('ts', -1)].

    Also runs an initial prune of legacy unindexed snapshots.
    """
    global _INDEXES_INITIALIZED
    if _INDEXES_INITIALIZED:
        return True

    try:
        import pymongo

        if db is None:
            if _GLOBAL_MONGO_CLIENT is None:
                return False
            db = _GLOBAL_MONGO_CLIENT.get_database("trading_bot_db")

        retention_hours = int(os.getenv("SNAPSHOT_RETENTION_HOURS", "24"))
        retention_seconds = retention_hours * 3600

        # 1. market_snapshots TTL & Performance Indexes
        snap_col = db.get_collection("market_snapshots")
        try:
            snap_col.create_index(
                [("created_at", pymongo.ASCENDING)],
                expireAfterSeconds=retention_seconds,
                name="idx_market_snapshots_ttl_created_at",
                background=True,
            )
        except Exception as ttl_err:
            log.debug(f"TTL index setup notice on market_snapshots: {ttl_err}")

        try:
            snap_col.create_index([("ts", pymongo.DESCENDING)], name="idx_market_snapshots_ts", background=True)
            snap_col.create_index(
                [("sym", pymongo.ASCENDING), ("tf", pymongo.ASCENDING), ("ts", pymongo.DESCENDING)],
                name="idx_market_snapshots_sym_tf_ts",
                background=True,
            )
        except Exception as idx_err:
            log.debug(f"Query index notice on market_snapshots: {idx_err}")

        # 2. settlements Indexes
        settle_col = db.get_collection("settlements")
        try:
            settle_col.create_index([("ts", pymongo.DESCENDING)], name="idx_settlements_ts", background=True)
            settle_col.create_index([("ticker", pymongo.ASCENDING)], name="idx_settlements_ticker", background=True)
        except Exception as idx_err:
            log.debug(f"Query index notice on settlements: {idx_err}")

        # 3. portfolio_pnl Indexes
        pnl_col = db.get_collection("portfolio_pnl")
        try:
            pnl_col.create_index([("ts", pymongo.DESCENDING)], name="idx_portfolio_pnl_ts", background=True)
        except Exception as idx_err:
            log.debug(f"Query index notice on portfolio_pnl: {idx_err}")

        _INDEXES_INITIALIZED = True

        # Prune legacy records beyond retention window on initial startup
        prune_old_market_snapshots(retention_hours=retention_hours)
        return True
    except Exception as error:
        log.warning(f"Could not initialize database indexes: {error}")
        return False


def prune_old_market_snapshots(retention_hours: int | None = None) -> int:
    """
    Prunes snapshot documents from 'market_snapshots' older than the retention cutoff
    and cleans up any orphaned/legacy records missing a valid BSON Date 'created_at'.

    Args:
        retention_hours: Cutoff age in hours (defaults to SNAPSHOT_RETENTION_HOURS or 24h).

    Returns:
        int: Number of deleted documents.
    """
    if retention_hours is None:
        retention_hours = int(os.getenv("SNAPSHOT_RETENTION_HOURS", "24"))

    try:
        if _GLOBAL_MONGO_CLIENT is None:
            return 0
        db = _GLOBAL_MONGO_CLIENT.get_database("trading_bot_db")
        snap_col = db.get_collection("market_snapshots")

        now_ts = int(time.time())
        cutoff_ts = now_ts - (retention_hours * 3600)
        cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)

        res = snap_col.delete_many({
            "$or": [
                {"ts": {"$lt": cutoff_ts}},
                {"created_at": {"$lt": cutoff_dt}},
                {"created_at": None},
                {"created_at": {"$exists": False}},
            ]
        })
        if res.deleted_count > 0:
            log.info(
                f"🧹 Pruned {res.deleted_count:,} legacy market snapshot(s) older than {retention_hours}h / orphaned from MongoDB Atlas."
            )
        return res.deleted_count
    except Exception as error:
        log.error(f"Failed to prune old market snapshots: {error}")
        return 0


def get_mongo_collection(collection_name: str = "market_snapshots"):
    """
    Establishes or reuses a connection to MongoDB Atlas using MONGODB_URI.
    Handles TLS/SSL certificate verification safely across Linux containers (e.g. Render.com).

    Args:
        collection_name: Target MongoDB collection name ('market_snapshots', 'portfolio_pnl', 'settlements').

    Returns:
        pymongo.collection.Collection object if connected, or None if URI is missing/fails.
    """
    global _GLOBAL_MONGO_CLIENT

    mongo_uri = os.getenv("MONGODB_URI")

    if not mongo_uri:
        log.warning(
            "MONGODB_URI is not set in your .env file or Render environment settings. "
            "Data will be logged locally, but not saved to MongoDB."
        )
        return None

    try:
        import pymongo

        if _GLOBAL_MONGO_CLIENT is None:
            # Native MongoClient initialization using MongoDB SRV connection string
            try:
                client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                _GLOBAL_MONGO_CLIENT = client
            except Exception as err:
                log.debug(f"Standard SRV connection attempt failed, trying fallback: {err}")
                # Fallback for container SSL handshake compatibility
                client = pymongo.MongoClient(
                    mongo_uri,
                    serverSelectionTimeoutMS=5000,
                    tls=True,
                    tlsAllowInvalidCertificates=True,
                )
                _GLOBAL_MONGO_CLIENT = client

        # Access database 'trading_bot_db' and requested collection
        db = _GLOBAL_MONGO_CLIENT.get_database("trading_bot_db")
        ensure_database_indexes(db)
        return db.get_collection(collection_name)
    except Exception as error:
        log.error(f"Could not connect to MongoDB Atlas: {error}")
        return None


def detect_market_timeframe(ticker: str) -> str:
    """
    Determines market timeframe ('5m', '15m', or '1h') from ticker string.
    - 5-minute series contain '05M' or '5M' (e.g. 'GEMI-BTC05M-...', 'BTC05M')
    - 15-minute series contain '15M' (e.g. 'KXBTC15M-...', 'GEMI-BTC15M-...')
    - Hourly directional series end in 'D', 'H', 'U', or '1H' (e.g. 'KXBTCD-...', 'KXTONH-...', 'INXU-...')
    """
    tick_upper = (ticker or "").upper()
    if re.search(r"(?:05M|(?<![0-9])5M|05MIN|5MIN|05-MIN|5-MIN|5_MIN|05_MIN)", tick_upper):
        return "5m"
    return "15m" if "15M" in tick_upper else "1h"


def save_market_snapshots(snapshots: list[dict]) -> bool:
    """
    Saves a list of ultra-compact market snapshot documents into MongoDB 'market_snapshots' collection.
    Every document is explicitly tagged with 'tf' ('15m' or '1h') and 'created_at' BSON Date for TTL index.
    """
    if not snapshots:
        log.info("No snapshots to save.")
        return False

    collection = get_mongo_collection("market_snapshots")
    if collection is None:
        return False

    now_utc = datetime.now(timezone.utc)

    # Ensure tf and created_at are explicitly set on each snapshot document
    for s in snapshots:
        if "tf" not in s or not s["tf"]:
            s["tf"] = detect_market_timeframe(s.get("tick", ""))
        if not isinstance(s.get("created_at"), datetime):
            # Set BSON Date for automated MongoDB Atlas TTL index
            if "ts" in s and isinstance(s["ts"], (int, float)) and s["ts"] > 0:
                try:
                    s["created_at"] = datetime.fromtimestamp(s["ts"], tz=timezone.utc)
                except Exception:
                    s["created_at"] = now_utc
            else:
                s["created_at"] = now_utc

    try:
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

    ex_name = str(pnl_summary.get("exchange") or "kalshi").lower()

    try:
        doc = {
            "ts": int(pnl_summary.get("ts") or 0),
            "ex": ex_name,
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
        log.error(f"Failed to insert portfolio PnL to MongoDB: {error}")
        return False


def get_market_session(hour_utc: int) -> str:
    """Returns the primary global trading session for a given UTC hour."""
    if 0 <= hour_utc < 8:
        return "asia_session"
    elif 8 <= hour_utc < 13:
        return "london_session"
    elif 13 <= hour_utc < 20:
        return "us_nyse_session"
    else:
        return "us_after_hours"


def detect_asset_symbol(ticker: str) -> str:
    """Extracts the base asset symbol (e.g. 'BTC', 'ETH', 'GOLD', 'WTI', 'INX') from ticker string."""
    t = (ticker or "").upper()
    for s in ["BTC", "ETH", "SOL", "XRP", "GOLD", "WTI", "SILVER", "INX", "NASDAQ100", "NDX", "SPX"]:
        if s in t:
            return s
    return "UNKNOWN"


def save_settlements(settlements: list[dict]) -> int:
    """
    Saves individual settled market PnL records into MongoDB 'settlements' collection.
    Automatically enriches settlement records with entry-time metadata from 'active_positions'
    (e.g., entry price, spot at entry, strike distance %, minute of candle, order role, strategy tag)
    to enable permanent, high-resolution Expected Value (EV) and win-rate analysis across Price, Asset, and Time.
    Cleans up settled positions from 'active_positions' after enrichment.
    """
    if not settlements:
        return 0

    collection = get_mongo_collection("settlements")
    if collection is None:
        return 0

    active_col = get_mongo_collection("active_positions")
    saved_count = 0
    try:
        for s in settlements:
            ticker = s.get("ticker")
            settled_time = s.get("settled_time")
            ex_name = str(s.get("exchange") or s.get("ex") or "kalshi").lower()
            if not ticker:
                continue

            filter_query = {"ticker": ticker, "ex": ex_name}
            if settled_time:
                filter_query["settled_time"] = settled_time

            s_tf = s.get("tf") or detect_market_timeframe(ticker)
            s_sym = s.get("sym") or detect_asset_symbol(ticker)
            s_doc = {**s, "ex": ex_name, "tf": s_tf, "sym": s_sym}

            # Enrich from active_positions if trade was tracked
            active_doc = None
            if active_col is not None:
                try:
                    active_doc = active_col.find_one({"ticker": ticker, "ex": ex_name})
                except Exception:
                    pass

            if active_doc:
                # Merge entry-time metrics
                if "entry_price_cents" in active_doc and "entry_price_cents" not in s_doc:
                    s_doc["entry_price_cents"] = active_doc["entry_price_cents"]
                if "side" in active_doc and "entered_side" not in s_doc:
                    s_doc["entered_side"] = active_doc["side"]
                for fld in [
                    "spot_price_at_entry", "target_strike_price", "strike_distance_diff",
                    "strike_distance_pct", "minute_into_candle", "seconds_remaining",
                    "hour_utc", "day_of_week", "market_session", "strategy_tag",
                    "order_role", "spread_cents_at_entry", "spread_saved_cents",
                    "regime", "entry_ts"
                ]:
                    if fld in active_doc and fld not in s_doc:
                        s_doc[fld] = active_doc[fld]

            # Compute trade outcome alignment if entered side is known
            market_res = str(s_doc.get("market_result") or s_doc.get("result") or "").lower().strip()
            entered_side = str(s_doc.get("entered_side") or (
                "yes" if (s_doc.get("yes_count", 0) > 0 and s_doc.get("no_count", 0) == 0)
                else "no" if (s_doc.get("no_count", 0) > 0 and s_doc.get("yes_count", 0) == 0)
                else ""
            )).lower().strip()

            if entered_side in ("yes", "no") and market_res in ("yes", "no"):
                s_doc["entered_side"] = entered_side
                s_doc["trade_won"] = (entered_side == market_res)

            collection.update_one(
                filter_query,
                {"$set": s_doc},
                upsert=True,
            )
            saved_count += 1

            # Clean up settled position from active_positions
            if active_col is not None and active_doc is not None:
                try:
                    active_col.delete_one({"ticker": ticker, "ex": ex_name})
                except Exception:
                    pass

        if saved_count > 0:
            log.info(f"📊 Saved/updated {saved_count} enriched settlement PnL record(s) in MongoDB Atlas!")
        return saved_count
    except Exception as error:
        log.error(f"Failed to upsert settlements to MongoDB: {error}")
        return 0


# ===========================================================================
# Active Position Cost-Basis & Entry Context Persistence
# ===========================================================================

def save_active_position_cost(
    ticker: str,
    side: str,
    count: int,
    entry_price_cents: int,
    ex: str = "kalshi",
    tf: str | None = None,
    client_order_id: str | None = None,
    tp_order_id: str | None = None,
    target_tp_cents: int | None = None,
    strategy_tag: str | None = None,
    spread_saved_cents: int | None = None,
    order_role: str | None = None,
    spot_price_at_entry: float | None = None,
    target_strike_price: float | None = None,
    strike_distance_diff: float | None = None,
    strike_distance_pct: float | None = None,
    minute_into_candle: int | None = None,
    seconds_remaining: float | None = None,
    spread_cents_at_entry: int | None = None,
    sym: str | None = None,
    regime: str | None = None,
) -> bool:
    """
    Persists an active trade cost-basis and rich entry-time market context into MongoDB 'active_positions'.
    Captures exact entry price, spot price, distance to strike, candle minute, order role, and strategy tag.
    These metrics are merged into 'settlements' upon expiration to ensure permanent EV analytics.
    """
    if not ticker or entry_price_cents <= 0:
        return False

    col = get_mongo_collection("active_positions")
    if col is None:
        return False

    ex_name = str(ex or "kalshi").lower()
    timeframe = tf or detect_market_timeframe(ticker)
    symbol = sym or detect_asset_symbol(ticker)
    now_ts = int(time.time())
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    # Compute distance metrics if spot and strike are provided
    if spot_price_at_entry is not None and target_strike_price is not None and target_strike_price > 0:
        if strike_distance_diff is None:
            strike_distance_diff = round(spot_price_at_entry - target_strike_price, 4)
        if strike_distance_pct is None:
            strike_distance_pct = round(((spot_price_at_entry - target_strike_price) / target_strike_price) * 100.0, 4)

    try:
        set_doc = {
            "ticker": ticker,
            "ex": ex_name,
            "tf": timeframe,
            "sym": symbol,
            "side": side.lower(),
            "count": count,
            "entry_price_cents": entry_price_cents,
            "total_cost_dollars": round((entry_price_cents * count) / 100.0, 4),
            "updated_at": now_ts,
            "entry_ts": now_ts,
            "hour_utc": now_dt.hour,
            "day_of_week": now_dt.strftime("%A"),
            "market_session": get_market_session(now_dt.hour),
        }
        if client_order_id:
            set_doc["client_order_id"] = client_order_id
        if tp_order_id:
            set_doc["tp_order_id"] = tp_order_id
        if target_tp_cents is not None:
            set_doc["target_tp_cents"] = target_tp_cents
        if strategy_tag is not None:
            set_doc["strategy_tag"] = strategy_tag
        if spread_saved_cents is not None:
            set_doc["spread_saved_cents"] = spread_saved_cents
        if order_role is not None:
            set_doc["order_role"] = order_role
        if spot_price_at_entry is not None:
            set_doc["spot_price_at_entry"] = spot_price_at_entry
        if target_strike_price is not None:
            set_doc["target_strike_price"] = target_strike_price
        if strike_distance_diff is not None:
            set_doc["strike_distance_diff"] = strike_distance_diff
        if strike_distance_pct is not None:
            set_doc["strike_distance_pct"] = strike_distance_pct
        if minute_into_candle is not None:
            set_doc["minute_into_candle"] = minute_into_candle
        if seconds_remaining is not None:
            set_doc["seconds_remaining"] = seconds_remaining
        if spread_cents_at_entry is not None:
            set_doc["spread_cents_at_entry"] = spread_cents_at_entry
        if regime is not None:
            set_doc["regime"] = regime

        col.update_one(
            {"ticker": ticker, "ex": ex_name},
            {"$set": set_doc},
            upsert=True,
        )
        return True
    except Exception as err:
        log.debug(f"Could not persist active position cost for {ticker} to MongoDB: {err}")
        return False


def get_active_position_doc(ticker: str, ex: str = "kalshi") -> dict | None:
    """
    Retrieves the full persisted active position document for a ticker from MongoDB Atlas.
    Returns dict (containing side, count, entry_price_cents, strategy_tag, etc.) or None if not found / MongoDB unavailable.
    """
    if not ticker:
        return None

    col = get_mongo_collection("active_positions")
    if col is None:
        return None

    ex_name = str(ex or "kalshi").lower()
    try:
        doc = col.find_one({"ticker": ticker, "ex": ex_name})
        if doc and isinstance(doc, dict):
            doc_copy = dict(doc)
            if "_id" in doc_copy:
                doc_copy["_id"] = str(doc_copy["_id"])
            return doc_copy
    except Exception as err:
        log.debug(f"Could not retrieve active position document for {ticker} from MongoDB: {err}")

    return None


def get_active_position_cost(ticker: str, ex: str = "kalshi") -> int | None:
    """
    Retrieves the persisted entry price for a ticker from MongoDB Atlas upon worker restart.
    Returns integer cents (1-99) or None if not found / MongoDB unavailable.
    """
    doc = get_active_position_doc(ticker, ex=ex)
    if doc and "entry_price_cents" in doc and doc["entry_price_cents"] is not None:
        return int(doc["entry_price_cents"])
    return None


def get_active_position_strategy_tag(ticker: str, ex: str = "kalshi") -> str | None:
    """
    Retrieves the persisted strategy tag for a ticker from MongoDB Atlas upon worker restart.
    Returns string tag (e.g. 'endgame_favorite_maker') or None if not found / MongoDB unavailable.
    """
    doc = get_active_position_doc(ticker, ex=ex)
    if doc and "strategy_tag" in doc and doc["strategy_tag"] is not None:
        return str(doc["strategy_tag"])
    return None


def get_active_position_target_tp(ticker: str, ex: str = "kalshi") -> int | None:
    """
    Retrieves the persisted target take-profit price for a ticker from MongoDB Atlas upon worker restart.
    Returns integer cents (1-99) or None if not found / MongoDB unavailable.
    """
    doc = get_active_position_doc(ticker, ex=ex)
    if doc and "target_tp_cents" in doc and doc["target_tp_cents"] is not None:
        return int(doc["target_tp_cents"])
    return None


def remove_active_position_cost(ticker: str, ex: str = "kalshi") -> bool:
    """
    Deletes the position cost record from MongoDB Atlas when a position is liquidated or settled.
    """
    if not ticker:
        return False

    col = get_mongo_collection("active_positions")
    if col is None:
        return False

    ex_name = str(ex or "kalshi").lower()
    try:
        col.delete_one({"ticker": ticker, "ex": ex_name})
        return True
    except Exception as err:
        log.debug(f"Could not remove active position record for {ticker} from MongoDB: {err}")
        return False


def load_all_active_positions(ex: str = "kalshi") -> dict[str, int]:
    """
    Loads all active positions from MongoDB Atlas into memory on startup.
    Returns: dict mapping {ticker -> entry_price_cents}
    """
    col = get_mongo_collection("active_positions")
    if col is None:
        return {}

    ex_name = str(ex or "kalshi").lower()
    pos_map: dict[str, int] = {}
    try:
        docs = col.find({"ex": ex_name})
        for d in docs:
            tick = d.get("ticker")
            p = d.get("entry_price_cents")
            if tick and p is not None:
                pos_map[tick] = int(p)
    except Exception as err:
        log.debug(f"Could not load active positions map from MongoDB: {err}")

    return pos_map


# ===========================================================================
# Empirical Expected Value (EV) Entry Validation & Asset Loss Gating Engine
# ===========================================================================

def get_midnight_et_cutoff(now_utc: datetime | None = None) -> tuple[str, float]:
    """
    Computes midnight Eastern Time (00:00:00 US/Eastern) for the current day.
    Returns:
        tuple[str, float]: (midnight_iso_utc, midnight_timestamp_utc)
    """
    if zoneinfo is not None:
        try:
            et_tz = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            et_tz = timezone(timedelta(hours=-4))
    else:
        et_tz = timezone(timedelta(hours=-4))

    now = now_utc or datetime.now(timezone.utc)
    now_et = now.astimezone(et_tz)
    midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_et.astimezone(timezone.utc)
    midnight_iso = midnight_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    midnight_ts = midnight_utc.timestamp()
    return midnight_iso, midnight_ts


def get_asset_realized_pnl_dollars(
    symbol: str,
    timeframe: str | None = None,
    since_midnight_et: bool = True,
    since_iso: str | None = None,
    since_ts: float | None = None,
    now_utc: datetime | None = None,
    db=None,
) -> float:
    """
    Queries MongoDB Atlas settlements collection and sums the net realized PnL in dollars
    for a specific (symbol, timeframe) contract since midnight ET of the current day (or custom cutoff).

    If since_midnight_et is True (default), only trades settled on or after 00:00 ET of the current day
    are included, enforcing an automatic daily loss reset at midnight ET every day.
    """
    col = get_mongo_collection("settlements")
    if col is None:
        return 0.0
    sym_upper = str(symbol or "").upper()
    try:
        conditions = []

        # 1. Symbol match
        conditions.append({
            "$or": [
                {"sym": sym_upper},
                {"ticker": {"$regex": sym_upper, "$options": "i"}},
            ]
        })

        # 2. Timeframe match
        if timeframe:
            tf_lower = str(timeframe).lower()
            if tf_lower in ("15m", "5m"):
                conditions.append({
                    "$or": [
                        {"tf": {"$in": ["15m", "5m"]}},
                        {"ticker": {"$regex": "15M|05M", "$options": "i"}},
                    ]
                })
            else:  # 1h / hourly
                conditions.append({
                    "$or": [
                        {"tf": {"$in": ["1h", "hourly", "daily"]}},
                        {"ticker": {"$regex": "H-|D-|1H", "$options": "i"}},
                    ]
                })
                conditions.append({
                    "ticker": {"$not": {"$regex": "15M|05M", "$options": "i"}}
                })

        # 3. Time filter (since midnight ET by default, resetting daily at 00:00 ET)
        if since_midnight_et or since_iso or since_ts is not None:
            if not since_iso and since_ts is None:
                iso_cutoff, ts_cutoff = get_midnight_et_cutoff(now_utc=now_utc)
            else:
                iso_cutoff = since_iso or ""
                ts_cutoff = since_ts if since_ts is not None else 0.0

            time_or_clauses = []
            if iso_cutoff:
                time_or_clauses.append({"settled_time": {"$gte": iso_cutoff}})
                time_or_clauses.append({"settled_time": {"$gte": iso_cutoff.rstrip("Z")}})
            if ts_cutoff > 0:
                time_or_clauses.append({"ts": {"$gte": ts_cutoff}})
                time_or_clauses.append({"settled_time_ts": {"$gte": ts_cutoff}})

            if time_or_clauses:
                conditions.append({"$or": time_or_clauses})

        match_query = {"$and": conditions} if len(conditions) > 1 else conditions[0]

        pipeline = [
            {"$match": match_query},
            {
                "$group": {
                    "_id": None,
                    "total_pnl": {"$sum": "$pnl_dollars"},
                }
            }
        ]
        res = list(col.aggregate(pipeline))
        if res and "total_pnl" in res[0] and res[0]["total_pnl"] is not None:
            return float(res[0]["total_pnl"])
    except Exception as err:
        log.debug(f"Could not calculate realized PnL for asset {symbol} [{timeframe}]: {err}")
    return 0.0


def is_asset_loss_gated(
    symbol: str,
    timeframe: str | None = None,
    max_loss_dollars: float = 1.0,
    since_midnight_et: bool = True,
    now_utc: datetime | None = None,
    db=None,
) -> tuple[bool, float]:
    """
    Checks if a specific (symbol, timeframe) contract's realized losses since midnight ET exceed the threshold (default: $1.00).
    If daily realized PnL <= -$1.00, returns (True, pnl_dollars) indicating live trading is gated for that specific contract for today.
    Resets at midnight ET every day.
    """
    pnl = get_asset_realized_pnl_dollars(
        symbol,
        timeframe=timeframe,
        since_midnight_et=since_midnight_et,
        now_utc=now_utc,
        db=db,
    )
    is_gated = (pnl <= -abs(max_loss_dollars))
    return is_gated, pnl


def is_valid_empirical_entry(price_cents: int, minute: int, timeframe: str) -> tuple[bool, str]:
    """
    Validates trade entry against empirical Expected Value (EV) findings:
    
    15-Minute Markets:
    - m = 0, 1, 2 (Open): Accept 1¢ – 35¢ (Discount / Value)
    - m = 3, 4, 5 (Early): Accept 21¢ – 50¢ (Early Value Reversion)
    - m = 12, 13, 14 (Endgame): Accept 66¢ – 80¢ (Endgame Momentum)
    - All other (price, minute) combinations are rejected.
    
    1-Hour Markets:
    - m = 0 .. 10 (Open): Accept 1¢ – 35¢ (Discount / Asymmetric Value)
    - m = 26 .. 40 (Mid): Accept 36¢ – 50¢ (Mid-Hour Value) and 51¢ – 80¢ (Mid-Hour Momentum)
    - m = 53 .. 59 (Endgame): Accept 51¢ – 80¢ (Endgame Trend Momentum)
    - All other (price, minute) combinations are rejected.
    """
    if price_cents < 1 or price_cents > 99:
        return False, "invalid_price_bounds"
        
    tf = str(timeframe or "15m").lower()
    
    if tf == "15m":
        m_mod = minute % 15
        # Zero Capital Allocation to Death Valleys (Minute 0–8, 36¢–65¢)
        if 0 <= m_mod <= 8 and 36 <= price_cents <= 65:
            return False, f"15m_death_valley_trap(p={price_cents}c,m={m_mod})"
        # Peak 1: Asymmetric Value at Open (1¢–35¢)
        if 0 <= m_mod <= 2 and 1 <= price_cents <= 35:
            return True, "15m_discount_open"
        # Early Value Reversion (21¢–50¢)
        if 3 <= m_mod <= 5 and 21 <= price_cents <= 50:
            return True, "15m_early_value"
        # Peak 3: Endgame Momentum (60¢–80¢)
        if 6 <= m_mod <= 14 and 60 <= price_cents <= 80:
            return True, "15m_endgame_momentum"
        return False, f"15m_disallowed_zone(p={price_cents}c,m={m_mod})"
        
    elif tf == "1h":
        m_mod = minute % 60
        # Peak 1: 1H Open Value (1¢–35¢)
        if 0 <= m_mod <= 10 and 1 <= price_cents <= 35:
            return True, "1h_discount_open"
        # Mid-Hour Value (36¢–50¢)
        if 26 <= m_mod <= 40 and 36 <= price_cents <= 50:
            return True, "1h_mid_value"
        # Peak 2: Mid-to-Late Trend Expansion (51¢–80¢)
        if 26 <= m_mod <= 55 and 51 <= price_cents <= 80:
            return True, "1h_mid_momentum"
        # Endgame Trend Momentum (51¢–80¢)
        if 53 <= m_mod <= 59 and 51 <= price_cents <= 80:
            return True, "1h_endgame_trend"
        return False, f"1h_disallowed_zone(p={price_cents}c,m={m_mod})"
        
    return False, f"unsupported_timeframe({tf})"
# Intuitive Core Concept:
# ------------------------
# Imagine you are shopping for sneakers to resell online:
# 1. "min_buy" (e.g. 35¢): If a pair is selling for dirt cheap (like 10¢),
#    it's almost certainly broken or fake (a "cheap trap"). So we don't buy below min_buy.
# 2. "max_buy" (e.g. 45¢): You never want to overpay. If you buy above max_buy,
#    your profit margin is too small to justify the risk.
# 3. "tp" (Take-Profit Sell Price, e.g. 82¢): As soon as we buy a contract, we immediately
#    list it for sale at this target price to lock in a profit before the 15-minute game ends!
#
# Why make this dynamic?
# Every crypto asset behaves differently! Some coins (like ZEC and NEAR) bounce up and
# down wildly (high volatility), so they need deeper buy discounts and can target higher
# sell prices. Other coins (like BTC and BNB) move steadily.
#
# This engine looks at real historical trades in MongoDB Atlas every 6 hours
# ===========================================================================


def _make_default_mark(mark: str, timeframe: str = "15m", asset_symbol: str = "DEFAULT") -> dict[str, int | float | bool]:
    """
    Builds default parameter dictionary for a given checkpoint mark ('00' or '05')
    and market timeframe ('15m' vs '1h').

    Corridor Dynamics & Asset Restrictions:
    - 15-Minute Profile: Clamped discount ceiling (<=30¢ at :00, <=24¢ at :05), momentum (>=62¢, >=0.15% lead),
      middle dead-zone (31¢–61¢ rejected), and 1-contract sizing.
    - 1-Hour Core Profile: 18¢–44¢ discount, momentum (>=56¢, >=0.10% lead), 37¢–55¢ dead-zone, and 3-contract sizing.
    - WTI (Crude Oil) Restricted Profile: High volatility requires 1-contract sizing, tighter corridors (<=30¢ at open,
      <=24¢ compressed), and stricter momentum confirmation (>=0.20% spot lead, >=62¢ buy).
    """
    is_compressed = (mark in ("05", "20", "30"))
    tf_clean = str(timeframe).lower()
    sym_clean = str(asset_symbol or "").upper()
    is_wti = (sym_clean == "WTI")

    if tf_clean == "1h":
        if is_wti:
            # WTI Restricted 1-Hour Profile (Conservative sizing, tighter corridor, high spot lead)
            return {
                "min_buy": 18,
                "max_buy": 24 if is_compressed else 30,
                "tp": 58 if is_compressed else 62,
                "enable_favorite_maker": True,
                "favorite_maker_min_buy": 85,
                "favorite_maker_max_buy": 92,
                "max_crypto_directional_concurrent": 2,
                "max_lead_changes": 1,
                "enable_momentum_buys": True,
                "momentum_min_buy": 62,
                "momentum_max_buy": 65 if is_compressed else 68,
                "momentum_tp": 88,
                "momentum_min_pct_dist": 0.20,
                "momentum_sl_floor": 40,
                "sl_threshold": 20,
                "sl_floor": 15,
                "cut_5min": 15,
                "endgame_12_5_floor": 15,
                "crash_floor_cents": 35,
                "avoid_mid_zone": True,
                "mid_zone_min": 37,
                "mid_zone_max": 55,
                "contracts_per_trade": 1,
                "is_restricted": True,
            }
        # Standard 1-Hour Trend Profile (Wider runway, larger core capital sizing)
        return {
            "min_buy": 18,
            "max_buy": 32 if is_compressed else 44,
            "tp": 58 if is_compressed else 62,
            "enable_favorite_maker": True,
            "favorite_maker_min_buy": 80,
            "favorite_maker_max_buy": 93,
            "max_crypto_directional_concurrent": 3,
            "max_lead_changes": 2,
            "enable_momentum_buys": True,
            "momentum_min_buy": 56,
            "momentum_max_buy": 65 if is_compressed else 68,
            "momentum_tp": 88,
            "momentum_min_pct_dist": 0.10,
            "momentum_sl_floor": 40,
            "sl_threshold": 25,
            "sl_floor": 15,
            "cut_5min": 15,
            "endgame_12_5_floor": 15,
            "crash_floor_cents": 35,
            "avoid_mid_zone": True,
            "mid_zone_min": 37,
            "mid_zone_max": 55,
            "contracts_per_trade": 3,
            "is_restricted": False,
        }
    else:
        if is_wti:
            # WTI Restricted 15-Minute Profile
            return {
                "min_buy": 18,
                "max_buy": 22 if is_compressed else 28,
                "tp": 58 if is_compressed else 62,
                "enable_favorite_maker": True,
                "favorite_maker_min_buy": 85,
                "favorite_maker_max_buy": 92,
                "max_crypto_directional_concurrent": 2,
                "max_lead_changes": 1,
                "enable_momentum_buys": True,
                "momentum_min_buy": 62,
                "momentum_max_buy": 65 if is_compressed else 68,
                "momentum_tp": 88,
                "momentum_min_pct_dist": 0.20,
                "momentum_sl_floor": 40,
                "sl_threshold": 20,
                "sl_floor": 15,
                "cut_5min": 15,
                "endgame_12_5_floor": 15,
                "crash_floor_cents": 35,
                "avoid_mid_zone": True,
                "mid_zone_min": 31,
                "mid_zone_max": 61,
                "contracts_per_trade": 1,
                "is_restricted": True,
            }
        # Standard 15-Minute Sniper Profile (Prioritizes Endgame Favorite Maker 80¢–93¢, strict momentum, 1-contract sizing)
        return {
            "min_buy": 18,
            "max_buy": 24 if is_compressed else 30,
            "tp": 58 if is_compressed else 62,
            "enable_favorite_maker": True,
            "favorite_maker_min_buy": 80,
            "favorite_maker_max_buy": 93,
            "max_crypto_directional_concurrent": 3,
            "max_lead_changes": 2,
            "enable_momentum_buys": True,
            "momentum_min_buy": 62,
            "momentum_max_buy": 65 if is_compressed else 68,
            "momentum_tp": 88,
            "momentum_min_pct_dist": 0.15,
            "momentum_sl_floor": 40,
            "sl_threshold": 20,
            "sl_floor": 15,
            "cut_5min": 15,
            "endgame_12_5_floor": 15,
            "crash_floor_cents": 35,
            "avoid_mid_zone": True,
            "mid_zone_min": 31,
            "mid_zone_max": 61,
            "contracts_per_trade": 1,
            "is_restricted": False,
        }


def calculate_dynamic_contracts_count(
    balance_cents: int | float | None,
    buy_price_cents: int,
    base_count: int = 1,
    max_bankroll_pct: float = 0.20,
    is_restricted: bool = False,
    recent_win_rate: float | None = None,
) -> int:
    """
    Calculates safe, dynamic contract quantity based on available cash balance, contract price, and asset win rate.
    Prevents catastrophic drawdown while dynamically scaling position sizes on high-EV proven winners.

    Rules:
    - If is_restricted is True: Clamped strictly to 1 contract.
    - If balance is None or non-numeric (e.g. mock test / dry-run): Returns base_count (or 1 if restricted).
    - If balance cannot afford 1 contract (balance_cents < buy_price_cents): Returns 0.
    - Small bankroll protection: under $10.00 (< 1000 cents), size strictly at 1 contract.
    - High-Win-Rate Multiplier (>=85% win rate e.g. Gold, BTC, ETH):
      If account balance >= 2000 cents ($20.00), allows scaling up to 3 contracts (or 4 if >= 90% WR).
    - Fractional bankroll sizing: Allocates at most max_bankroll_pct (default 20%) of available cash.
    - Contract count is clamped to at most effective_max.
    """
    if buy_price_cents <= 0:
        return 0

    effective_max = 1 if is_restricted else max(1, base_count)
    if not is_restricted and recent_win_rate is not None:
        if recent_win_rate >= 0.90:
            effective_max = max(effective_max, 4)
        elif recent_win_rate >= 0.85:
            effective_max = max(effective_max, 3)
        elif recent_win_rate >= 0.75:
            effective_max = max(effective_max, 2)

    if balance_cents is None or not isinstance(balance_cents, (int, float)):
        return 1 if is_restricted else base_count

    balance_int = int(balance_cents)
    if balance_int < buy_price_cents:
        return 0

    # Small bankroll protection: under $10.00, size strictly at 1 contract
    if balance_int < 1000:
        return 1

    max_capital_cents = balance_int * max_bankroll_pct
    calculated_contracts = int(max_capital_cents // buy_price_cents)
    safe_contracts = max(1, calculated_contracts)

    return min(safe_contracts, effective_max)


DEFAULT_DYNAMIC_PRICE_MAP: dict[str, dict[str, dict[str, int | float | bool]]] = {
    sym: {"00": _make_default_mark("00", "15m", asset_symbol=sym), "05": _make_default_mark("05", "15m", asset_symbol=sym)}
    for sym in (
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ZEC", "NEAR", "HYPE", "TON", "ADA", "BCH",
        "GOLD", "SILVER", "WTI", "INX", "NASDAQ100",
        "DEFAULT"
    )
}

DEFAULT_DYNAMIC_PRICE_MAP_1H: dict[str, dict[str, dict[str, int | float | bool]]] = {
    sym: {"00": _make_default_mark("00", "1h", asset_symbol=sym), "05": _make_default_mark("05", "1h", asset_symbol=sym)}
    for sym in (
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ZEC", "NEAR", "HYPE", "TON", "ADA", "BCH",
        "GOLD", "SILVER", "WTI", "INX", "NASDAQ100",
        "DEFAULT"
    )
}

# In-memory global cache variables (so the bot doesn't slow down by querying Mongo every 5 seconds)
_DYNAMIC_PRICE_MAP_CACHE: dict[str, dict[str, dict[str, int | float | bool]]] | None = None
_LAST_MAP_REFRESH_TIME: float = 0.0
_CACHE_TTL_SECONDS: float = 1 * 3600.0  # 1 hour = 3,600 seconds


def is_default_exploration_window(now_utc: datetime | None = None) -> bool:
    """
    Returns True if current UTC time is within the shortened 15-minute discovery window:
    05:00 to 05:15 UTC (1:00 AM to 1:15 AM Eastern Time).
    During this single 15-minute cycle, the bot allows broad corridor exploration
    with a strict global trade cap to collect empirical orderbook data while protecting capital.
    """
    now = now_utc or datetime.now(timezone.utc)
    return (now.hour == 5 and now.minute < 15)


def is_mid_zone_exploration_window(now_utc: datetime | None = None) -> bool:
    """
    Returns True strictly during the 15-minute discovery window: 05:00 to 05:15 UTC (1:00 AM to 1:15 AM Eastern Time).
    During this brief 15-minute window, low-EV middle zone trades (45¢–55¢) may be sampled
    up to the global exploration trade cap for empirical data collection.
    Outside this 15-minute window, middle zone trades are strictly avoided.
    """
    now = now_utc or datetime.now(timezone.utc)
    return (now.hour == 5 and now.minute < 15)


def should_refresh_dynamic_map(
    now_utc: datetime | None = None,
    last_refresh_time: float | None = None,
    cache_is_empty: bool | None = None,
) -> bool:
    """
    Decides whether it's time to pull fresh trading data from MongoDB.

    Rule 1 (Cold Start): If memory is empty (bot just booted up), refresh immediately!
    Rule 2 (1-Hour Expiry): If more than 1 hour passed since last refresh, refresh!
    Rule 3 (05:15 UTC Post-Exploration Anchor): Immediately after exiting the 05:00-05:15 UTC discovery window,
            force a fresh recalibration at 05:15-05:25 UTC to lock in calibrated parameters.
    Rule 4 (Hourly Clock Markers): If haven't refreshed in the current hour and near top of the hour.
    """
    if cache_is_empty is None:
        cache_is_empty = (_DYNAMIC_PRICE_MAP_CACHE is None)
    if cache_is_empty:
        return True

    if last_refresh_time is None:
        last_refresh_time = _LAST_MAP_REFRESH_TIME
    if last_refresh_time <= 0:
        return True

    now = now_utc or datetime.now(timezone.utc)
    current_ts = now.timestamp()
    elapsed = current_ts - last_refresh_time

    # 1. Check if 1 full hour (3,600 seconds) has passed
    if elapsed >= _CACHE_TTL_SECONDS:
        return True

    # 2. Check if we just exited 05:00-05:15 UTC exploration window (anchor at 05:15-05:25 UTC)
    if now.hour == 5 and 15 <= now.minute < 25:
        window_end_ts = now.replace(minute=15, second=0, microsecond=0).timestamp()
        if last_refresh_time < window_end_ts:
            return True

    # 3. Check hourly top-of-the-hour mark (minute < 10) if not refreshed yet this hour
    if now.minute < 10:
        hour_start_ts = now.replace(minute=0, second=0, microsecond=0).timestamp()
        if last_refresh_time < hour_start_ts:
            return True

    return False


def calibrate_dynamic_price_map_from_db(
    max_history_hours: int = 48,
) -> dict[str, dict[str, dict[str, int | bool]]]:
    """
    Reads recent trade data from MongoDB and calculates the smartest buy, sell, and stop-loss parameters.

    Quantitative Calibration Pipeline:
    -----------------------------------
    Step 1 (Volatility & Spread Profiling):
      - Standard deviation of % moves from strike determines baseline pricing bands and TP targets.
      - Dual Corridor calibration:
        * Discount Corridor (18¢–44¢ at :00, 18¢–30¢ at :05) with 58¢–62¢ TP.
        * Momentum Breakout Corridor (56¢–68¢ at :00, 56¢–65¢ at :05) with 88¢ TP & >=0.10% spot lead.

    Step 2 (Runway Timing & Theta Optimization - Recommendation 1):
      - Minute :00 (15m runway) receives full allocation corridor (max_buy up to 44¢ discount, 68¢ momentum).
      - Minute :05 (10m runway) has theta compression; max_buy ceiling clamped to <= 30¢ discount, 65¢ momentum.

    Step 3 (Asset-Specific Stop-Loss Calibration - Recommendation 2):
      - Analyzes historical Stop-Loss accuracy per asset (Rightful Saves vs. False Alarms).
      - Deep discount corridor with wide stop loss (25¢ loss threshold, 15¢ floor, 15¢ 5-min cut).

    Step 4 (Middle Zone Avoidance - Recommendation 3):
      - Evaluates empirical EV in the 37¢-55¢ coin-flip band. Avoids 37¢-55¢ entries.
    """
    # Start with a clean copy of default fallback map
    calibrated_map: dict[str, dict[str, dict[str, int | bool]]] = {
        k: {m: dict(v) for m, v in marks.items()}
        for k, marks in DEFAULT_DYNAMIC_PRICE_MAP.items()
    }

    try:
        snap_col = get_mongo_collection("market_snapshots")
        settle_col = get_mongo_collection("settlements")

        if snap_col is None or settle_col is None:
            log.warning("MongoDB collections unavailable. Using default price filter map.")
            return calibrated_map

        # 1. Fetch recent snapshots and settlements (with projection & limits for sub-second execution)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        cutoff_ts = now_ts - (max_history_hours * 3600)

        recent_snapshots = list(
            snap_col.find(
                {"ts": {"$gte": cutoff_ts}},
                {"sym": 1, "pct": 1, "trd": 1, "m": 1, "tick": 1, "ts": 1, "_id": 0},
            )
            .sort("ts", -1)
            .limit(3000)
        )
        if not recent_snapshots:
            recent_snapshots = list(
                snap_col.find(
                    {},
                    {"sym": 1, "pct": 1, "trd": 1, "m": 1, "tick": 1, "ts": 1, "_id": 0},
                )
                .sort("ts", -1)
                .limit(3000)
            )

        recent_settlements = list(
            settle_col.find(
                {},
                {"ticker": 1, "market_result": 1, "pnl_cents": 1, "_id": 0},
            )
            .sort("ts", -1)
            .limit(1000)
        )
        settlement_map = {s.get("ticker"): s for s in recent_settlements if s.get("ticker")}

        if not recent_snapshots:
            log.info("No snapshots found in MongoDB. Using default price filter map.")
            return calibrated_map

        # 2. Group historical trades, SL events, volatility, and settlements by asset
        volatility_by_sym: dict[str, list[float]] = defaultdict(list)
        trades_by_sym_mark: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        sl_events_by_sym: dict[str, list[dict]] = defaultdict(list)
        mid_zone_trades_by_sym: dict[str, list[dict]] = defaultdict(list)
        settles_by_sym: dict[str, list[dict]] = defaultdict(list)

        for s in recent_settlements:
            tick = s.get("ticker") or ""
            sym = extract_symbol_from_ticker(tick)
            settles_by_sym[sym].append(s)

        for s in recent_snapshots:
            sym = s.get("sym")
            if not sym:
                continue

            pct = s.get("pct")
            if pct is not None:
                volatility_by_sym[sym].append(pct)

            trd = s.get("trd")
            m = s.get("m")
            tick = s.get("tick")
            if trd and m is not None and tick:
                action = trd.get("action")
                side = str(trd.get("side", "")).lower()
                buy_p = trd.get("buy")
                settle = settlement_map.get(tick)

                won = False
                if settle:
                    mkt_res = str(settle.get("market_result") or "").lower()
                    if mkt_res and side:
                        won = (side == mkt_res)
                    elif (settle.get("pnl_cents") or 0) > 0:
                        won = True

                timing_mark = "05" if (m % 15 >= 5) else "00"

                if action in ("buy_opened", "counterfactual_mid_zone", "simulated_exploration", "dry_run_buy") and buy_p:
                    trade_record = {
                        "buy": int(buy_p),
                        "won": won,
                        "side": side,
                        "m": m,
                    }
                    trades_by_sym_mark[sym][timing_mark].append(trade_record)
                    if 37 <= int(buy_p) <= 55:
                        mid_zone_trades_by_sym[sym].append(trade_record)

                elif action in ("early_loss_cut", "stop_loss_sold", "stop_loss_exit"):
                    sl_events_by_sym[sym].append({
                        "action": action,
                        "side": side,
                        "won": won,
                        "loss_cents": trd.get("loss_cents", 10),
                    })

        # 3. Calibrate parameters for each symbol
        all_symbols = set(volatility_by_sym.keys()).union(trades_by_sym_mark.keys()).union(settles_by_sym.keys())
        for sym in all_symbols:
            pct_samples = volatility_by_sym.get(sym, [])
            # Note on Session-Bounded Markets (e.g. S&P 500 / Nasdaq 10:00 AM - 6:00 PM ET):
            # Snapshots are ONLY recorded when Kalshi markets are open and active.
            # During closed hours, no snapshots are saved, eliminating artificial zero-movement distortion.
            # Volatility (standard deviation of % move from strike) is computed strictly over live market hours.
            # If fewer than 2 snapshots exist (e.g. cold start over weekend), it falls back safely to 0.08% baseline.
            vol_std = statistics.stdev(pct_samples) if len(pct_samples) > 1 else 0.08

            # Asset-level stop-loss analysis (Recommendation 2)
            sl_list = sl_events_by_sym.get(sym, [])
            total_sl = len(sl_list)
            false_alarms = sum(1 for e in sl_list if e["won"])
            false_alarm_rate = (false_alarms / total_sl) if total_sl > 0 else 0.0

            all_sym_trades = (
                trades_by_sym_mark.get(sym, {}).get("00", []) +
                trades_by_sym_mark.get(sym, {}).get("05", [])
            )
            total_sym_n = len(all_sym_trades)
            sym_wins = sum(1 for t in all_sym_trades if t["won"])
            sym_win_rate = (sym_wins / total_sym_n) if total_sym_n > 0 else 0.38

            # Settlement PnL analysis for dynamic loss restriction
            sym_settles = settles_by_sym.get(sym, [])
            settle_cnt = len(sym_settles)
            settle_wins = sum(1 for s in sym_settles if (s.get("pnl_cents") or 0) > 0 or (s.get("pnl_dollars") or 0) > 0)
            settle_pnl_cents = sum(s.get("pnl_cents") or int(round((s.get("pnl_dollars") or 0) * 100)) for s in sym_settles)
            settle_wr = (settle_wins / settle_cnt) if settle_cnt > 0 else sym_win_rate

            # Dynamic Loss Restriction Regime Detection from MongoDB:
            # 1. Heavy loss regime: >=3 settled trades and net PnL <= -100¢ or win rate < 60% -> strictly tightened corridors & 88¢+ favorite floor.
            # 2. Restricted loss regime: WTI or any asset with net negative PnL or win rate < 75% -> tightened corridors & 85¢+ favorite floor.
            # 3. Standard regime: Win rate >= 75% and positive PnL -> standard 80¢–93¢ favorite corridor.
            is_heavy_loss = (settle_cnt >= 3 and settle_pnl_cents <= -100) or (settle_cnt >= 4 and settle_wr < 0.60)
            is_restricted = (sym == "WTI") or is_heavy_loss or (settle_cnt >= 2 and settle_pnl_cents < 0) or (settle_cnt >= 3 and settle_wr < 0.75)

            # Dynamic Asset Classification & Stop-Loss Calibration
            # Deep discount corridor with wide stop loss (20¢ loss threshold for restricted, 25¢ for standard)
            asset_sl_threshold = 20 if is_restricted else 25
            asset_sl_floor = 15
            asset_cut_5min = 15
            asset_endgame_floor = 15

            # Middle Zone Analysis (Recommendation 3)
            mid_trades = mid_zone_trades_by_sym.get(sym, [])
            mid_wins = sum(1 for t in mid_trades if t["won"])
            mid_win_rate = (mid_wins / len(mid_trades)) if len(mid_trades) >= 3 else 0.33
            avoid_mid = (mid_win_rate < 0.40) or is_restricted  # Avoid middle band if win rate is under 40% or if restricted

            calibrated_map[sym] = {}
            for mark in ("00", "05"):
                trades = trades_by_sym_mark.get(sym, {}).get(mark, [])

                # Step 1: Base parameters based dynamically on deep discount strategy
                base_min_buy = 18
                base_max_buy = 44 if mark == "00" else 30
                base_tp = 62 if mark == "00" else 58

                # Step 2: Fine-tune using winning discount trade history
                min_buy = base_min_buy
                max_buy = base_max_buy
                tp_price = base_tp

                discount_wins = [t["buy"] for t in trades if t["won"] and t["buy"] <= 44]
                if len(discount_wins) >= 3:
                    calibrated_max = int(statistics.median(discount_wins) + 2)
                    max_buy = min(base_max_buy, calibrated_max)

                # Step 3: Fine-tune momentum corridor using winning momentum trades (56¢ - 75¢)
                base_mom_min = 62 if is_restricted else 56
                base_mom_max = 68 if mark == "00" else 65
                base_mom_tp = 88
                mom_min_pct_dist = 0.20 if is_restricted else 0.10
                mom_sl_floor = 40

                mom_wins = [t["buy"] for t in trades if t["won"] and t["buy"] >= 56]
                if len(mom_wins) >= 3:
                    med_mom_buy = int(statistics.median(mom_wins))
                    base_mom_max = max(base_mom_min + 2, min(72, med_mom_buy + 3))

                # Step 4: Runway Timing Enforcement & Dynamic Loss-Tightened Corridors
                min_buy = 18
                if is_heavy_loss:
                    max_buy = min(max_buy, 18 if mark == "05" else 22)
                    fav_min_buy = 90
                    fav_max_buy = 92
                    enable_fav_maker = False  # Heavy loss regime: disable favorite maker to halt drawdown
                    max_lead = 1
                    max_concurrent = 1
                elif is_restricted:
                    max_buy = max(min_buy + 2, min(max_buy, 20 if mark == "05" else 25))
                    fav_min_buy = 88
                    fav_max_buy = 92
                    # If win rate is under 65% on restricted asset with multiple settled trades, disable favorite maker
                    enable_fav_maker = False if (settle_cnt >= 2 and settle_wr < 0.65) else True
                    max_lead = 1
                    max_concurrent = 1
                else:
                    if mark == "05":
                        max_buy = max(min_buy + 2, min(max_buy, 30))
                    else:
                        max_buy = max(min_buy + 2, min(max_buy, 44))
                    fav_min_buy = 80
                    fav_max_buy = 93
                    enable_fav_maker = True
                    max_lead = 2
                    max_concurrent = 3

                tp_price = max(max_buy + 12, min(tp_price, 65))

                calibrated_map[sym][mark] = {
                    "min_buy": min_buy,
                    "max_buy": max_buy,
                    "tp": tp_price,
                    "enable_favorite_maker": enable_fav_maker,
                    "favorite_maker_min_buy": fav_min_buy,
                    "favorite_maker_max_buy": fav_max_buy,
                    "max_crypto_directional_concurrent": max_concurrent,
                    "max_lead_changes": max_lead,
                    "enable_momentum_buys": False if is_heavy_loss else True,
                    "momentum_min_buy": base_mom_min,
                    "momentum_max_buy": base_mom_max,
                    "momentum_tp": base_mom_tp,
                    "momentum_min_pct_dist": mom_min_pct_dist,
                    "momentum_sl_floor": mom_sl_floor,
                    "sl_threshold": asset_sl_threshold,
                    "sl_floor": asset_sl_floor,
                    "cut_5min": asset_cut_5min,
                    "endgame_12_5_floor": asset_endgame_floor,
                    "crash_floor_cents": 50,
                    "avoid_mid_zone": avoid_mid,
                    "mid_zone_min": 37,
                    "mid_zone_max": 55,
                    "contracts_per_trade": 1 if is_restricted else 3,
                    "is_restricted": is_restricted,
                    "recent_win_rate": round(settle_wr, 4) if settle_cnt > 0 else round(sym_win_rate, 4),
                    "recent_pnl_cents": settle_pnl_cents,
                }

        log.info(
            f"🧠 [DYNAMIC CALIBRATION] Calibrated dynamic price & SL filter map for {len(all_symbols)} assets from MongoDB!"
        )

        # Run automated Graveyard & EV Analytics on historical MongoDB records
        try:
            run_graveyard_analytics_report(max_history_hours=max_history_hours)
        except Exception as report_err:
            log.debug(f"Graveyard analytics report skipped: {report_err}")

        return calibrated_map

    except Exception as err:
        log.error(f"Error while calibrating dynamic price map from MongoDB: {err}")
        return calibrated_map


def run_graveyard_analytics_report(max_history_hours: int = 168) -> dict:
    """
    Analyzes historical settlements and market snapshots in MongoDB Atlas to replicate
    the empirical findings from the Kalshi trading experiments ('Graveyard vs Edge').

    Aggregates & Reports:
    1. Performance by Price Band:
       - Longshots (<40¢): Quantifies Favorite-Longshot Bias loss.
       - Middle Coin Flips (40¢–60¢): Quantifies random walk noise / 50-50 chop.
       - Moderate Favorites (60¢–74¢): Taker vs Maker win rate and spread cost.
       - Heavy Favorites (74¢–93¢): Proven Maker edge (+1¢ to +2¢ per fill).
       - Extreme Favorites (>93¢): High win rate with low margin.
    2. Order Execution Role (Maker vs Taker):
       - Taker (buying at Ask): Average fill slippage & spread cost (~ -6¢).
       - Maker (resting at Bid): Average spread saved (+1¢ to +3¢).
    3. Dipped Favorite Recovery Rate:
       - % of positions entered at >=70¢ that dipped < entry price during holding but settled at 100¢.
       - Validates why panic stop-losses destroy ~20¢ of EV vs holding through.
    4. Cross-Asset Co-Directional Settlement Matrix:
       - Measures empirical settlement direction alignment across BTC, ETH, and SOL (validating 74%–84% correlation).
    """
    report = {
        "price_bands": {},
        "maker_vs_taker": {},
        "dipped_favorite_recovery": {},
        "crypto_correlation_matrix": {},
        "total_analyzed_settlements": 0,
    }

    try:
        settle_col = get_mongo_collection("settlements")
        if settle_col is None:
            return report

        now_ts = int(datetime.now(timezone.utc).timestamp())
        cutoff_ts = now_ts - (max_history_hours * 3600)

        settlements = list(
            settle_col.find({"ts": {"$gte": cutoff_ts}}).sort("ts", -1).limit(5000)
        )
        if not settlements:
            settlements = list(settle_col.find({}).sort("ts", -1).limit(5000))

        report["total_analyzed_settlements"] = len(settlements)
        if not settlements:
            return report

        # 1. Price Bands Performance
        bands = {
            "longshots_<40c": {"count": 0, "wins": 0, "pnl_cents": 0, "costs": 0},
            "coinflip_40_60c": {"count": 0, "wins": 0, "pnl_cents": 0, "costs": 0},
            "moderate_fav_60_74c": {"count": 0, "wins": 0, "pnl_cents": 0, "costs": 0},
            "heavy_fav_74_93c": {"count": 0, "wins": 0, "pnl_cents": 0, "costs": 0},
            "extreme_fav_>93c": {"count": 0, "wins": 0, "pnl_cents": 0, "costs": 0},
        }

        # 2. Maker vs Taker Fills
        maker_taker = {
            "maker": {"count": 0, "wins": 0, "pnl_cents": 0, "spread_saved_total": 0},
            "taker": {"count": 0, "wins": 0, "pnl_cents": 0, "spread_cost_total": 0},
        }

        # 3. Dipped favorites recovery
        dipped_fav_total = 0
        dipped_fav_recovered = 0

        # Group settlements by 15m interval timestamps to compute co-directional settlement
        settlements_by_time: dict[int, dict[str, str]] = defaultdict(dict)

        for s in settlements:
            pnl_c = s.get("pnl_cents")
            if pnl_c is None:
                pnl_c = int(round((s.get("pnl_dollars") or 0.0) * 100))
            cost_d = float(s.get("cost_dollars") or 0.0)
            cost_c = int(round(cost_d * 100)) if cost_d > 0 else (s.get("cost_cents") or 0)
            won = (pnl_c > 0)
            entry_p = s.get("entry_price_cents") or cost_c

            if entry_p > 0:
                if entry_p < 40:
                    b_key = "longshots_<40c"
                elif entry_p <= 60:
                    b_key = "coinflip_40_60c"
                elif entry_p <= 74:
                    b_key = "moderate_fav_60_74c"
                elif entry_p <= 93:
                    b_key = "heavy_fav_74_93c"
                else:
                    b_key = "extreme_fav_>93c"

                bands[b_key]["count"] += 1
                if won:
                    bands[b_key]["wins"] += 1
                bands[b_key]["pnl_cents"] += pnl_c
                bands[b_key]["costs"] += cost_c

            role = str(s.get("order_role") or "maker").lower()
            if role in maker_taker:
                maker_taker[role]["count"] += 1
                if won:
                    maker_taker[role]["wins"] += 1
                maker_taker[role]["pnl_cents"] += pnl_c
                if role == "maker":
                    maker_taker[role]["spread_saved_total"] += int(s.get("spread_saved_cents") or 2)

            if entry_p >= 70:
                dipped = s.get("dipped_below_entry") or (s.get("lowest_price_during_hold", entry_p) < entry_p)
                if dipped:
                    dipped_fav_total += 1
                    if won:
                        dipped_fav_recovered += 1

            ts = s.get("settled_time_ts") or s.get("ts") or 0
            tick = s.get("ticker", "")
            res = str(s.get("market_result") or "").lower()
            sym = extract_symbol_from_ticker(tick)
            if sym in ("BTC", "ETH", "SOL") and res in ("yes", "no") and ts > 0:
                time_block = (ts // 900) * 900
                settlements_by_time[time_block][sym] = res

        for b_name, b_data in bands.items():
            n = b_data["count"]
            wr = (b_data["wins"] / n) if n > 0 else 0.0
            avg_pnl = (b_data["pnl_cents"] / n) if n > 0 else 0.0
            report["price_bands"][b_name] = {
                "trades": n,
                "win_rate_pct": round(wr * 100, 1),
                "total_pnl_cents": b_data["pnl_cents"],
                "avg_pnl_per_trade_cents": round(avg_pnl, 2),
            }

        for role, r_data in maker_taker.items():
            n = r_data["count"]
            wr = (r_data["wins"] / n) if n > 0 else 0.0
            avg_pnl = (r_data["pnl_cents"] / n) if n > 0 else 0.0
            report["maker_vs_taker"][role] = {
                "fills": n,
                "win_rate_pct": round(wr * 100, 1),
                "total_pnl_cents": r_data["pnl_cents"],
                "avg_pnl_per_fill_cents": round(avg_pnl, 2),
            }

        recovery_rate = (dipped_fav_recovered / dipped_fav_total) if dipped_fav_total > 0 else 0.70
        report["dipped_favorite_recovery"] = {
            "total_dipped_favorites": dipped_fav_total,
            "recovered_to_win": dipped_fav_recovered,
            "recovery_rate_pct": round(recovery_rate * 100, 1),
        }

        pairs = [("BTC", "ETH"), ("BTC", "SOL"), ("ETH", "SOL")]
        for p1, p2 in pairs:
            matching = 0
            same_dir = 0
            for block, assets in settlements_by_time.items():
                if p1 in assets and p2 in assets:
                    matching += 1
                    if assets[p1] == assets[p2]:
                        same_dir += 1
            corr_pct = (same_dir / matching) if matching > 0 else 0.78
            report["crypto_correlation_matrix"][f"{p1}_{p2}"] = {
                "matching_events": matching,
                "same_direction_settlements": same_dir,
                "co_directional_pct": round(corr_pct * 100, 1),
            }

        log.info(
            f"📊 [GRAVEYARD ANALYTICS] Evaluated {len(settlements)} settled trades from MongoDB Atlas:\n"
            f"   - Heavy Favorites (74¢–93¢): {report['price_bands']['heavy_fav_74_93c']['win_rate_pct']}% Win Rate | "
            f"Avg PnL: {report['price_bands']['heavy_fav_74_93c']['avg_pnl_per_trade_cents']:+.1f}¢/trade\n"
            f"   - Longshots (<40¢): {report['price_bands']['longshots_<40c']['win_rate_pct']}% Win Rate | "
            f"Avg PnL: {report['price_bands']['longshots_<40c']['avg_pnl_per_trade_cents']:+.1f}¢/trade (Favorite-Longshot Bias Loss)\n"
            f"   - Dipped Favorites Recovery Rate: {report['dipped_favorite_recovery']['recovery_rate_pct']}% (Holding is +EV vs Stop-Loss)\n"
            f"   - BTC-ETH Co-Directional Settlement: {report['crypto_correlation_matrix'].get('BTC_ETH', {}).get('co_directional_pct', 78.0)}%"
        )

        return report

    except Exception as err:
        log.error(f"Error generating Graveyard Analytics report from MongoDB: {err}")
        return report


def get_dynamic_price_filter_map(
    force_refresh: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, dict[str, dict[str, int | bool]]]:
    """
    Retrieves the in-memory dynamic price filter map.
    If the cache is empty or 6 hours have passed, automatically queries MongoDB to refresh.

    Returns:
        Dictionary map structured as: { Asset -> { "00": {...}, "05": {...} } }
    """
    global _DYNAMIC_PRICE_MAP_CACHE, _LAST_MAP_REFRESH_TIME

    now = now_utc or datetime.now(timezone.utc)
    needs_refresh = force_refresh or should_refresh_dynamic_map(
        now_utc=now,
        last_refresh_time=_LAST_MAP_REFRESH_TIME,
        cache_is_empty=(_DYNAMIC_PRICE_MAP_CACHE is None),
    )

    if needs_refresh:
        log.info("🔄 [CACHE REFRESH] Pulling fresh 6-hour trade & volatility data from MongoDB Atlas...")
        new_map = calibrate_dynamic_price_map_from_db(max_history_hours=48)
        _DYNAMIC_PRICE_MAP_CACHE = new_map
        _LAST_MAP_REFRESH_TIME = now.timestamp()
        log.info("✅ [CACHE REFRESH] In-memory dynamic price filter map updated successfully.")

    return _DYNAMIC_PRICE_MAP_CACHE or DEFAULT_DYNAMIC_PRICE_MAP


def extract_symbol_from_ticker(ticker: str) -> str:
    """Extracts asset symbol from market ticker."""
    t_clean = (ticker or "").upper()
    for sym in (
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ZEC", "NEAR", "HYPE", "TON", "ADA", "BCH",
        "GOLD", "SILVER", "WTI", "INX", "NASDAQ100",
    ):
        if sym in t_clean:
            return sym
    prefix = t_clean.split("-")[0].replace("KX", "").replace("15M", "")
    return prefix or "DEFAULT"


def get_dynamic_filter_for_market(
    asset_symbol: str,
    is_5min_mark: bool,
    timeframe: str = "15m",
    dynamic_map: dict[str, dict[str, dict[str, int | float | bool]]] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, int | float | bool]:
    """
    Convenience lookup to get exact dynamic filter parameters for a specific market, timing mark, and timeframe.
    Between 05:00 and 05:15 UTC (1:00 AM - 1:15 AM ET), falls back to DEFAULT_DYNAMIC_PRICE_MAP
    corridors for empirical signal harvesting up to the exploration trade cap.

    Timeframe & Risk Differentiations:
    - '15m': Tighter discount corridor (18¢–30¢ at :00, 18¢–24¢ at :05), stricter momentum (>=0.15% lead),
             expanded middle dead zone (31¢–61¢), and 1-contract sniper sizing.
    - '1h':  Broad discount corridor (18¢–44¢ at open, 18¢–32¢ at :20), standard momentum (>=0.10% lead),
             standard middle dead zone (37¢–55¢), and 3-contract core capital sizing.
    - Restricted Markets (e.g. WTI or loss-heavy assets): 1-contract sizing, tighter max_buy (<=28¢/22¢),
      and stricter momentum spot lead (>=0.20%).

    Args:
        asset_symbol: Asset ticker string (e.g. "BTC", "ETH", "SOL", "GOLD", "WTI").
        is_5min_mark: True if running at compressed timing mark (:05/:20/:30), False if at open (:00/:10).
        timeframe: Market timeframe ('15m' or '1h'). Controls corridor tightness and capital sizing.
        dynamic_map: Optional preloaded map. If None, uses cached in-memory map.
        now_utc: Optional current UTC datetime for timing/regime evaluation.

    Returns:
        Dictionary containing all dynamic parameters for both discount and momentum corridors.
    """
    now = now_utc or datetime.now(timezone.utc)
    is_exploration = is_default_exploration_window(now)
    is_mid_zone_exploration = is_mid_zone_exploration_window(now)
    tf_clean = str(timeframe or "15m").lower()

    default_map = DEFAULT_DYNAMIC_PRICE_MAP_1H if tf_clean == "1h" else DEFAULT_DYNAMIC_PRICE_MAP

    if is_exploration and dynamic_map is None:
        active_map = default_map
    else:
        active_map = dynamic_map or get_dynamic_price_filter_map(now_utc=now)

    mark = "05" if is_5min_mark else "00"

    sym_clean = (asset_symbol or "").upper()
    asset_filters = active_map.get(sym_clean) or active_map.get("DEFAULT") or default_map["DEFAULT"]
    mark_filter = asset_filters.get(mark) or asset_filters.get("00") or default_map["DEFAULT"]["00"]

    base_defaults = _make_default_mark(mark, timeframe=tf_clean, asset_symbol=sym_clean)

    return {
        "min_buy": int(mark_filter.get("min_buy", base_defaults["min_buy"])),
        "max_buy": int(mark_filter.get("max_buy", base_defaults["max_buy"])),
        "tp": int(mark_filter.get("tp", base_defaults["tp"])),
        "enable_momentum_buys": bool(mark_filter.get("enable_momentum_buys", base_defaults["enable_momentum_buys"])),
        "momentum_min_buy": int(mark_filter.get("momentum_min_buy", base_defaults["momentum_min_buy"])),
        "momentum_max_buy": int(mark_filter.get("momentum_max_buy", base_defaults["momentum_max_buy"])),
        "momentum_tp": int(mark_filter.get("momentum_tp", base_defaults["momentum_tp"])),
        "momentum_min_pct_dist": float(mark_filter.get("momentum_min_pct_dist", base_defaults["momentum_min_pct_dist"])),
        "momentum_sl_floor": int(mark_filter.get("momentum_sl_floor", base_defaults["momentum_sl_floor"])),
        "sl_threshold": int(mark_filter.get("sl_threshold", base_defaults["sl_threshold"])),
        "sl_floor": int(mark_filter.get("sl_floor", base_defaults["sl_floor"])),
        "cut_5min": int(mark_filter.get("cut_5min", base_defaults["cut_5min"])),
        "endgame_12_5_floor": int(mark_filter.get("endgame_12_5_floor", base_defaults["endgame_12_5_floor"])),
        "crash_floor_cents": int(mark_filter.get("crash_floor_cents", base_defaults.get("crash_floor_cents", 50))),
        "avoid_mid_zone": False if is_mid_zone_exploration else bool(mark_filter.get("avoid_mid_zone", base_defaults["avoid_mid_zone"])),
        "mid_zone_min": int(mark_filter.get("mid_zone_min", base_defaults["mid_zone_min"])),
        "mid_zone_max": int(mark_filter.get("mid_zone_max", base_defaults["mid_zone_max"])),
        "contracts_per_trade": int(mark_filter.get("contracts_per_trade", base_defaults["contracts_per_trade"])),
        "is_restricted": bool(mark_filter.get("is_restricted", base_defaults.get("is_restricted", False))),
        "recent_win_rate": mark_filter.get("recent_win_rate", base_defaults.get("recent_win_rate")),
        "recent_pnl_cents": mark_filter.get("recent_pnl_cents", base_defaults.get("recent_pnl_cents")),
        "is_exploration": is_exploration,
    }


# ===========================================================================
# Ensemble Consensus & Settlement Autocorrelation Streak Engine
# (Inspired by DeweyMarco/simple-kalshi-bot & Reddit r/Kalshi empirical findings)
# ===========================================================================

_SETTLEMENT_STREAK_CACHE: dict[str, dict] = {}
_LAST_STREAK_CACHE_REFRESH: float = 0.0


def get_asset_settlement_streak(
    symbol: str,
    timeframe: str = "15m",
    limit: int = 10,
    force_refresh: bool = False,
    now_utc: datetime | None = None,
) -> dict:
    """
    Queries MongoDB Atlas 'settlements' (and falls back to 'market_snapshots') to compute
    the recent consecutive settlement streak / autocorrelation regime for an asset.

    Streak & Autocorrelation Logic:
    - In momentum regimes, crypto and commodities exhibit multi-market settlement streaks
      (e.g., 3-5 consecutive YES or NO settlements).
    - Knowing the prevailing streak allows our ensemble to confirm trend-following entries
      while preventing low-conviction counter-trend entries from fading powerful runs.

    Metrics Returned:
    - symbol: str
    - timeframe: '15m' or '1h'
    - streak_side: 'yes', 'no', or None
    - streak_length: int (count of consecutive identical outcomes from latest backwards)
    - recent_outcomes: list[str] (e.g. ['yes', 'yes', 'yes', 'no'])
    - yes_win_rate_pct: float (percentage of YES outcomes in sample)
    - sample_size: int
    - regime: 'strong_yes_streak', 'strong_no_streak', 'moderate_yes_streak', 'moderate_no_streak', 'neutral_flow', or 'mixed_choppy'
    """
    global _SETTLEMENT_STREAK_CACHE, _LAST_STREAK_CACHE_REFRESH

    now = now_utc or datetime.now(timezone.utc)
    sym_upper = (symbol or "").upper()
    tf_clean = str(timeframe or "15m").lower()
    cache_key = f"{sym_upper}_{tf_clean}"

    # 60-second in-memory TTL caching to prevent database request storms
    if not force_refresh and (now.timestamp() - _LAST_STREAK_CACHE_REFRESH < 60.0):
        if cache_key in _SETTLEMENT_STREAK_CACHE:
            return _SETTLEMENT_STREAK_CACHE[cache_key]

    default_result = {
        "symbol": sym_upper,
        "timeframe": tf_clean,
        "streak_side": None,
        "streak_length": 0,
        "recent_outcomes": [],
        "yes_win_rate_pct": 50.0,
        "sample_size": 0,
        "regime": "neutral_flow",
    }

    try:
        coll = get_mongo_collection("settlements")
        outcomes: list[str] = []

        if coll is not None:
            # Query recent settlements matching asset symbol and timeframe
            query = {
                "$or": [
                    {"ticker": {"$regex": sym_upper, "$options": "i"}},
                    {"sym": sym_upper},
                ],
                "tf": tf_clean,
            }
            cursor = coll.find(query).sort([("settled_time", -1), ("ts", -1)]).limit(limit)
            for doc in cursor:
                res = str(doc.get("market_result") or doc.get("result") or "").lower().strip()
                if res in ("yes", "no"):
                    outcomes.append(res)

        # Fallback to market_snapshots if settlements collection has few records
        if len(outcomes) < 3:
            snap_coll = get_mongo_collection("market_snapshots")
            if snap_coll is not None:
                snap_query = {
                    "sym": sym_upper,
                    "tf": tf_clean,
                    "result": {"$in": ["yes", "no", "YES", "NO"]},
                }
                snap_cursor = snap_coll.find(snap_query).sort([("ts", -1)]).limit(limit)
                for s_doc in snap_cursor:
                    s_res = str(s_doc.get("result") or "").lower().strip()
                    if s_res in ("yes", "no") and len(outcomes) < limit:
                        outcomes.append(s_res)

        if not outcomes:
            _SETTLEMENT_STREAK_CACHE[cache_key] = default_result
            return default_result

        # Calculate consecutive streak from the most recent outcome backwards
        streak_side = outcomes[0]
        streak_len = 0
        for o in outcomes:
            if o == streak_side:
                streak_len += 1
            else:
                break

        yes_count = sum(1 for o in outcomes if o == "yes")
        yes_pct = round((yes_count / len(outcomes)) * 100.0, 1)

        if streak_len >= 3:
            regime = f"strong_{streak_side}_streak"
        elif streak_len >= 2:
            regime = f"moderate_{streak_side}_streak"
        elif len(outcomes) >= 4 and abs(yes_pct - 50.0) <= 10.0:
            regime = "mixed_choppy"
        else:
            regime = f"mild_{streak_side}_bias"

        streak_result = {
            "symbol": sym_upper,
            "timeframe": tf_clean,
            "streak_side": streak_side,
            "streak_length": streak_len,
            "recent_outcomes": outcomes,
            "yes_win_rate_pct": yes_pct,
            "sample_size": len(outcomes),
            "regime": regime,
        }

        _SETTLEMENT_STREAK_CACHE[cache_key] = streak_result
        _LAST_STREAK_CACHE_REFRESH = now.timestamp()
        return streak_result

    except Exception as err:
        log.debug(f"Could not calculate settlement streak for {sym_upper} from MongoDB: {err}")
        return default_result


def evaluate_ensemble_consensus(
    symbol: str,
    proposed_side: str,
    price_cents: int,
    spot_price: float | None,
    target_price: float | None,
    timeframe: str = "15m",
    dynamic_filters: dict | None = None,
    lead_changes: int = 0,
    streak_data: dict | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """
    Ensemble Consensus Signal Engine combining:
    1. Oracle Spot Direction & Momentum Lead (Coinbase / Pyth / Yahoo)
    2. Settlement Regime Persistence (Historical Consecutive Streak from MongoDB Atlas)
    3. Microstructure Price Corridor & EV
    4. Market Choppiness / Whipsaw Guard (Lead changes across 50¢)

    Volume-Preservation Design:
    - Never blocks baseline trades when historical data is fresh or neutral.
    - Confirmed streak (>= 2 matching proposed_side): Confirmed high conviction boost.
    - Opposing streak (>= 3 consecutive opposing settlements): Rejects counter-trend fades
      unless spot price is experiencing a massive breakout move (|pct_dist| >= 0.20%).
    - Choppy market guard: Rejects markets where YES crossed 50¢ > 2 times.

    Returns:
        dict with keys:
            - is_approved: bool (True if trade passes ensemble consensus gates)
            - confidence: float (0.0 to 1.0)
            - consensus_score: float (-1.0 to 1.0)
            - streak_side: str | None
            - streak_length: int
            - regime: str
            - reason: str
    """
    prop_side = str(proposed_side or "").lower()
    filters = dynamic_filters or {}
    tf = str(timeframe or "15m").lower()

    # 1. Fetch or utilize provided settlement streak
    s_data = streak_data or get_asset_settlement_streak(symbol, timeframe=tf, now_utc=now_utc)
    streak_side = s_data.get("streak_side")
    streak_len = int(s_data.get("streak_length") or 0)
    streak_regime = s_data.get("regime", "neutral_flow")

    # 2. Compute Spot Lead / Distance percentage
    pct_dist = 0.0
    if spot_price and target_price and target_price > 0:
        pct_dist = ((spot_price - target_price) / target_price) * 100.0

    spot_aligned = (pct_dist >= 0.0) if prop_side == "yes" else (pct_dist <= 0.0)
    abs_pct_dist = abs(pct_dist)

    # -----------------------------------------------------------------------
    # Multi-Factor Consensus Scoring Matrix
    # -----------------------------------------------------------------------
    score = 0.0

    # Factor 1: Spot Price Momentum Lead
    if spot_aligned:
        if abs_pct_dist >= (0.15 if tf == "15m" else 0.10):
            score += 0.35  # Strong spot lead
        elif abs_pct_dist > 0.0:
            score += 0.20  # Mild spot alignment
        else:
            score += 0.10
    else:
        # Slight counter-spot move
        score -= 0.15

    # Factor 2: Settlement Regime Streak Persistence (DeweyMarco Consensus Concept)
    if streak_side == prop_side:
        if streak_len >= 3:
            score += 0.35  # Strong consecutive streak alignment
        elif streak_len == 2:
            score += 0.25  # Moderate streak alignment
        elif streak_len == 1:
            score += 0.15
        else:
            score += 0.10
    elif streak_side is None or s_data.get("sample_size", 0) == 0:
        score += 0.15  # Neutral default (safe fallback preserving volume)
    else:
        # Counter-streak: Proposed side opposes the prevailing settlement streak
        if streak_len >= 3:
            if abs_pct_dist >= 0.20:
                # Strong breakout overcomes opposing historical streak
                score += 0.05
            else:
                # Severe counter-streak penalty: Protect against fading a runaway freight train
                score -= 0.40
        elif streak_len == 2:
            score -= 0.10
        else:
            score -= 0.05

    # Factor 3: Price Corridor & Expected Value
    min_buy = int(filters.get("min_buy", 18))
    max_buy = int(filters.get("max_buy", 30 if tf == "15m" else 44))
    if min_buy <= price_cents <= max_buy:
        score += 0.25  # High EV discount corridor
    elif 74 <= price_cents <= 93:
        score += 0.30  # Favorite maker edge
    elif 56 <= price_cents <= 68:
        score += 0.20  # Momentum corridor
    elif 31 <= price_cents <= 61:
        if bool(filters.get("is_exploration", False)) or not bool(filters.get("avoid_mid_zone", True)):
            score += 0.10  # Exploration sampling approved
        else:
            score -= 0.20  # Low EV coin-flip middle zone penalty

    # Factor 4: Market Choppiness / Whipsaw Guard (Reddit Pondletter finding)
    max_lead_changes = int(filters.get("max_lead_changes", 2))
    if lead_changes > max_lead_changes:
        score -= 0.35  # Severe penalty for choppy oscillation
    elif lead_changes == 0:
        score += 0.10  # Clean unidirectional book

    # -----------------------------------------------------------------------
    # Decision Gate Formulation
    # -----------------------------------------------------------------------
    # Hard Block 1: Market is actively choppy (> max_lead_changes flips across 50¢)
    if lead_changes > max_lead_changes:
        return {
            "is_approved": False,
            "confidence": 0.10,
            "consensus_score": round(score, 3),
            "streak_side": streak_side,
            "streak_length": streak_len,
            "regime": "choppy_oscillation_blocked",
            "reason": f"Market lead changes ({lead_changes}) exceeds maximum threshold ({max_lead_changes}).",
        }

    # Hard Block 2: Fighting a strong opposing streak (>=3) without strong breakout momentum
    if streak_side is not None and streak_side != prop_side and streak_len >= 3 and abs_pct_dist < 0.20:
        return {
            "is_approved": False,
            "confidence": 0.20,
            "consensus_score": round(score, 3),
            "streak_side": streak_side,
            "streak_length": streak_len,
            "regime": "counter_streak_fading_blocked",
            "reason": (
                f"Proposed {prop_side.upper()} opposes severe {streak_len}x {streak_side.upper()} settlement streak "
                f"without sufficient breakout momentum (|pct_dist| {abs_pct_dist:.2f}% < 0.20%)."
            ),
        }

    # Hard Block 3: High-Value Price (>= 80¢) Spot Direction Divergence Guard
    if price_cents >= 80 and spot_price and target_price and target_price > 0:
        if prop_side == "yes" and spot_price < target_price:
            return {
                "is_approved": False,
                "confidence": 0.15,
                "consensus_score": round(score, 3),
                "streak_side": streak_side,
                "streak_length": streak_len,
                "regime": "high_price_spot_divergence_blocked",
                "reason": (
                    f"High-value {prop_side.upper()} buy ({price_cents}¢ >= 80¢) rejected: "
                    f"Spot (${spot_price:.4f}) is below strike target (${target_price:.4f}) (Dipping/crashing)."
                ),
            }
        elif prop_side == "no" and spot_price > target_price:
            return {
                "is_approved": False,
                "confidence": 0.15,
                "consensus_score": round(score, 3),
                "streak_side": streak_side,
                "streak_length": streak_len,
                "regime": "high_price_spot_divergence_blocked",
                "reason": (
                    f"High-value {prop_side.upper()} buy ({price_cents}¢ >= 80¢) rejected: "
                    f"Spot (${spot_price:.4f}) is above strike target (${target_price:.4f}) (Surging against NO)."
                ),
            }

    # Confidence normalizer: maps score range [-0.5, 1.0] into [0.0, 1.0]
    confidence = max(0.1, min(1.0, (score + 0.3) / 1.3))
    is_approved = score >= 0.20  # Easily cleared by standard discount/momentum setups under neutral/trending conditions

    regime_desc = (
        f"streak_confirmed_{streak_side}"
        if (streak_side == prop_side and streak_len >= 2)
        else ("neutral_flow" if s_data.get("sample_size", 0) == 0 else streak_regime)
    )

    reason = (
        f"Ensemble consensus approved (score={score:.2f}, streak={streak_len}x {streak_side or 'none'}, "
        f"lead_chg={lead_changes}, spot_dist={pct_dist:+.2f}%)"
        if is_approved
        else f"Ensemble consensus score ({score:.2f}) below threshold (0.20)"
    )

    # High-Conviction Sizing Multiplier (scales position sizing on proven high-conviction streaks)
    if is_approved and score >= 0.65 and streak_side == prop_side and streak_len >= 2:
        recommended_contracts = 3  # High conviction: strong spot momentum & confirmed historical streak
    elif is_approved and score >= 0.50 and streak_side == prop_side and streak_len >= 1:
        recommended_contracts = 2  # Moderate conviction: solid alignment
    else:
        recommended_contracts = 1  # Standard base sizing

    return {
        "is_approved": is_approved,
        "confidence": round(confidence, 2),
        "consensus_score": round(score, 3),
        "streak_side": streak_side,
        "streak_length": streak_len,
        "recommended_contracts": recommended_contracts,
        "regime": regime_desc,
        "reason": reason,
    }


# ===========================================================================
# Price Ladder & Timing Matrix Modeling Integration
# ===========================================================================

def fetch_market_snapshot_series(
    timeframe: str = "15m",
    symbol: str | None = None,
    limit_markets: int = 1000,
    max_history_days: int = 30,
) -> list[dict]:
    """
    Fetches snapshot time series for distinct market lifecycles along with their settlement results.

    Returns:
        List of market dictionaries with 'tick', 'sym', 'tf', 'result', and 'snapshots' list.
    """
    snap_col = get_mongo_collection("market_snapshots")
    settle_col = get_mongo_collection("settlements")
    if snap_col is None:
        return []

    cutoff_ts = int(time.time()) - (max_history_days * 86400)
    query: dict[str, Any] = {"ts": {"$gte": cutoff_ts}}
    if timeframe:
        query["tf"] = timeframe
    if symbol:
        query["sym"] = symbol.upper()

    try:
        snapshots = list(
            snap_col.find(query)
            .sort("ts", 1)
            .limit(limit_markets * 20)
        )
    except Exception as err:
        log.error(f"Failed to fetch snapshots from MongoDB: {err}")
        return []

    # Fetch settlements
    settle_map: dict[str, str] = {}
    if settle_col is not None:
        try:
            settlements = list(settle_col.find({}, {"ticker": 1, "market_result": 1, "result": 1}))
            for s in settlements:
                t = s.get("ticker")
                res = s.get("market_result") or s.get("result")
                if t and res:
                    settle_map[t] = str(res).lower()
        except Exception as err:
            log.warning(f"Could not load settlements: {err}")

    # Group snapshots by ticker
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for s in snapshots:
        t = s.get("tick")
        if t:
            by_ticker[t].append(s)

    markets_data = []
    for ticker, snaps in by_ticker.items():
        first_snap = snaps[0]
        sym = first_snap.get("sym", "BTC")
        tf = first_snap.get("tf", timeframe)

        # Determine settlement result
        result = settle_map.get(ticker)
        if not result:
            # Fallback: check if last snapshot contains terminal pricing
            last_snap = snaps[-1]
            y = last_snap.get("y")
            n = last_snap.get("n")
            if y is not None and y >= 95:
                result = "yes"
            elif n is not None and n >= 95:
                result = "no"
            elif y is not None and y <= 5:
                result = "no"
            elif n is not None and n <= 5:
                result = "yes"

        if result:
            markets_data.append({
                "tick": ticker,
                "sym": sym,
                "tf": tf,
                "result": result,
                "snapshots": snaps,
            })

    return markets_data


def run_price_ladder_modeling_from_db(
    timeframe: str = "15m",
    symbol: str | None = None,
    min_price_cents: int = 1,
    max_price_cents: int = 99,
    price_step: int = 1,
    fill_mode: str = "maker",
    json_output_path: str | None = None,
    csv_output_path: str | None = None,
) -> dict:
    """
    Executes the empirical price ladder modeling engine against historical MongoDB Atlas market data.
    Simulates entering at every price X in [1¢, 99¢] at every checkpoint minute T.
    """
    from models.price_ladder_model import (
        export_matrix_to_csv,
        export_matrix_to_json,
        find_profitable_patterns,
        format_price_matrix_report,
        model_price_ladder_matrix,
    )

    markets = fetch_market_snapshot_series(timeframe=timeframe, symbol=symbol)
    if not markets:
        log.warning("No settled market snapshot series found in MongoDB Atlas.")
        return {"error": "no_data", "report": "No settled market data found in MongoDB Atlas."}

    matrix = model_price_ladder_matrix(
        markets_data=markets,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
        price_step=price_step,
        timeframe=timeframe,
        fill_mode=fill_mode,
    )

    patterns = find_profitable_patterns(matrix)
    report = format_price_matrix_report(matrix, timeframe=timeframe, symbol=symbol)

    if json_output_path:
        export_matrix_to_json(matrix, json_output_path)
    if csv_output_path:
        export_matrix_to_csv(matrix, csv_output_path)

    return {
        "matrix": matrix,
        "patterns": patterns,
        "report": report,
        "markets_analyzed": len(markets),
    }


# ===========================================================================
# ROADMAP & ARCHITECTURAL EXTENSIONS (TODO 2 & TODO 3)
# ===========================================================================

# TODO (Improvement 2): Intra-Market Synthetic Arbitrage & Hedging Dislocation Scanner
# ------------------------------------------------------------------------------------
# Concept from DeweyMarco/simple-kalshi-bot (notes/ARBITRAGE.md):
# 1. Dual-Sided Mispricing Scanner:
#    - In prediction markets where YES + NO must settle to exactly $1.00 (100¢), market makers or
#      abrupt orderbook dislocations occasionally result in:
#          YES_ask + NO_ask <= 96¢ (accounting for any potential exchange fees/slippage).
#    - When detected, submit simultaneous IOC limit buys on BOTH sides to lock in a guaranteed,
#      mathematically risk-free profit of:
#          Guaranteed Profit = $1.00 - (YES_cost + NO_cost).
# 2. Legged Opposite-Side Hedging:
#    - When holding an open winning position (e.g. bought YES @ 22¢, currently trading @ 78¢/22¢ NO),
#      evaluate buying the opposite NO contract at <= 65¢ to lock in guaranteed sub-dollar arbitrage:
#          Combined Cost = 22¢ + 65¢ = 87¢ -> Guaranteed Net Profit = +13¢ upon expiration!
# Implementation steps:
# - Add `scan_intra_market_arbitrage(quotes: dict) -> dict | None`
# - Add `execute_arbitrage_hedge_flow(ticker: str, client: BaseTradingClient)`


# TODO (Improvement 3): Portfolio R-Multiple Risk Budgeting & Rolling Break-Even Strategy Hibernation
# ----------------------------------------------------------------------------------------------------
# Concept from DeweyMarco/simple-kalshi-bot (CONSENSUS_IMPROVE.md) & reddit /r/Kalshi:
# 1. Dynamic Risk Budget Units (R):
#    - Define R = Portfolio_Equity * Risk_Pct (e.g. $500 * 1% = $5.00).
#    - Daily Loss Cap: If realized daily portfolio drawdown reaches -3R (-$15.00), trip a daily
#      circuit breaker to halt new trade submissions across all 17 assets until 00:00 UTC.
#    - Weekly Loss Cap: If realized weekly portfolio drawdown reaches -8R (-$40.00), trip a weekly
#      circuit breaker to halt trading until next ISO week reset.
# 2. Mathematical Rolling Break-Even Win Rate Gating:
#    - For each asset symbol, track a rolling window of the last 30 settled trades from MongoDB Atlas.
#    - Calculate empirical Average Win (W_bar) and Average Loss (L_bar).
#    - Compute theoretical Break-Even Win Rate:
#          BE_Win_Rate = L_bar / (W_bar + L_bar)
#    - If Rolling_Win_Rate < BE_Win_Rate over >= 20 trades, place that specific asset lane into
#      "HIBERNATION" mode (zero live capital risk) while continuing counterfactual paper snapshots
#      until empirical edge returns.
# Implementation steps:
# - Add `check_portfolio_r_circuit_breakers(daily_pnl_cents: int, weekly_pnl_cents: int, bankroll_cents: int) -> bool`
# - Add `evaluate_asset_break_even_hibernation(symbol: str, rolling_window: int = 30) -> dict`



