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
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load configuration from .env file if available
load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)


_GLOBAL_MONGO_CLIENT = None


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
        return db.get_collection(collection_name)
    except Exception as error:
        log.error(f"Could not connect to MongoDB Atlas: {error}")
        return None


def detect_market_timeframe(ticker: str) -> str:
    """
    Determines market timeframe ('15m' or '1h') from ticker string.
    - 15-minute series contain '15M' (e.g. 'KXBTC15M-...')
    - Hourly directional series end in 'D' or 'H' (e.g. 'KXBTCD-...', 'KXTONH-...')
    """
    tick_upper = (ticker or "").upper()
    return "15m" if "15M" in tick_upper else "1h"


def save_market_snapshots(snapshots: list[dict]) -> bool:
    """
    Saves a list of ultra-compact market snapshot documents into MongoDB 'market_snapshots' collection.
    Every document is explicitly tagged with 'tf' ('15m' or '1h') to distinguish timeframes.
    """
    if not snapshots:
        log.info("No snapshots to save.")
        return False

    collection = get_mongo_collection("market_snapshots")
    if collection is None:
        return False

    # Ensure tf is explicitly set on each snapshot document
    for s in snapshots:
        if "tf" not in s or not s["tf"]:
            s["tf"] = detect_market_timeframe(s.get("tick", ""))

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


def save_settlements(settlements: list[dict]) -> int:
    """
    Saves individual settled market PnL records into MongoDB 'settlements' collection.
    Tagging with 'tf' ('15m' vs '1h') ensures distinct performance reporting per timeframe.
    Prevents duplicate entries by keying on (ticker, ex, settled_time).
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
            ex_name = str(s.get("exchange") or s.get("ex") or "kalshi").lower()
            if not ticker:
                continue

            filter_query = {"ticker": ticker, "ex": ex_name}
            if settled_time:
                filter_query["settled_time"] = settled_time

            s_tf = s.get("tf") or detect_market_timeframe(ticker)
            s_doc = {**s, "ex": ex_name, "tf": s_tf}
            collection.update_one(
                filter_query,
                {"$set": s_doc},
                upsert=True,
            )
            saved_count += 1

        if saved_count > 0:
            log.info(f"📊 Saved/updated {saved_count} settlement PnL record(s) in MongoDB Atlas!")
        return saved_count
    except Exception as error:
        log.error(f"Failed to upsert settlements to MongoDB: {error}")
        return 0


# ===========================================================================
# Active Position Cost-Basis Persistence (Crash & Restart Resilience)
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
) -> bool:
    """
    Persists an active trade cost-basis into MongoDB Atlas 'active_positions' collection.
    Tagged with 'tf' ('15m' or '1h') to distinguish timeframes.
    Ensures that if the Render worker restarts/redeploys, exact entry price and intended TP are preserved.
    """
    if not ticker or entry_price_cents <= 0:
        return False

    col = get_mongo_collection("active_positions")
    if col is None:
        return False

    ex_name = str(ex or "kalshi").lower()
    timeframe = tf or detect_market_timeframe(ticker)
    try:
        set_doc = {
            "ticker": ticker,
            "ex": ex_name,
            "tf": timeframe,
            "side": side.lower(),
            "count": count,
            "entry_price_cents": entry_price_cents,
            "total_cost_dollars": round((entry_price_cents * count) / 100.0, 4),
            "updated_at": int(time.time()),
        }
        if client_order_id:
            set_doc["client_order_id"] = client_order_id
        if tp_order_id:
            set_doc["tp_order_id"] = tp_order_id
        if target_tp_cents is not None:
            set_doc["target_tp_cents"] = target_tp_cents

        col.update_one(
            {"ticker": ticker, "ex": ex_name},
            {"$set": set_doc},
            upsert=True,
        )
        return True
    except Exception as err:
        log.debug(f"Could not persist active position cost for {ticker} to MongoDB: {err}")
        return False


def get_active_position_cost(ticker: str, ex: str = "kalshi") -> int | None:
    """
    Retrieves the persisted entry price for a ticker from MongoDB Atlas upon worker restart.
    Returns integer cents (1-99) or None if not found / MongoDB unavailable.
    """
    if not ticker:
        return None

    col = get_mongo_collection("active_positions")
    if col is None:
        return None

    ex_name = str(ex or "kalshi").lower()
    try:
        doc = col.find_one({"ticker": ticker, "ex": ex_name})
        if doc and "entry_price_cents" in doc:
            return int(doc["entry_price_cents"])
    except Exception as err:
        log.debug(f"Could not retrieve active position cost for {ticker} from MongoDB: {err}")

    return None


def get_active_position_target_tp(ticker: str, ex: str = "kalshi") -> int | None:
    """
    Retrieves the persisted target take-profit price for a ticker from MongoDB Atlas upon worker restart.
    Returns integer cents (1-99) or None if not found / MongoDB unavailable.
    """
    if not ticker:
        return None

    col = get_mongo_collection("active_positions")
    if col is None:
        return None

    ex_name = str(ex or "kalshi").lower()
    try:
        doc = col.find_one({"ticker": ticker, "ex": ex_name})
        if doc and "target_tp_cents" in doc:
            return int(doc["target_tp_cents"])
    except Exception as err:
        log.debug(f"Could not retrieve target TP for {ticker} from MongoDB: {err}")

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
# Dynamic Price Filters & 6-Hour In-Memory Calibration Engine
# ===========================================================================
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


def _make_default_mark(mark: str, timeframe: str = "15m") -> dict[str, int | float | bool]:
    """
    Builds default parameter dictionary for a given checkpoint mark ('00' or '05')
    and market timeframe ('15m' vs '1h').

    Tighter Corridor Dynamics for 15-Minute Noise Reduction:
    - Clamped discount ceiling: 30¢ at open, 24¢ at :05 (deep discount only; eliminates 31¢-44¢ chop).
    - Tighter momentum breakout: 62¢–68¢ with >=0.15% spot lead (avoids ambiguous 56¢–61¢ zone).
    - Expanded middle dead-zone: 31¢–61¢ rejected (except during empirical exploration).
    - Differentiated capital sizing: 1 contract for 15m sniper trades vs 3 contracts for 1h core trend trades.
    """
    is_compressed = (mark in ("05", "20", "30"))
    tf_clean = str(timeframe).lower()

    if tf_clean == "1h":
        # 1-Hour Trend Profile (Wider runway, larger core capital sizing)
        return {
            "min_buy": 18,
            "max_buy": 32 if is_compressed else 44,
            "tp": 58 if is_compressed else 62,
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
            "avoid_mid_zone": True,
            "mid_zone_min": 37,
            "mid_zone_max": 55,
            "contracts_per_trade": 3,
        }
    else:
        # 15-Minute Sniper Profile (Tighter buy corridors, strict momentum lead, light sizing)
        return {
            "min_buy": 18,
            "max_buy": 24 if is_compressed else 30,
            "tp": 58 if is_compressed else 62,
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
            "avoid_mid_zone": True,
            "mid_zone_min": 31,
            "mid_zone_max": 61,
            "contracts_per_trade": 1,
        }


DEFAULT_DYNAMIC_PRICE_MAP: dict[str, dict[str, dict[str, int | float | bool]]] = {
    sym: {"00": _make_default_mark("00", "15m"), "05": _make_default_mark("05", "15m")}
    for sym in (
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ZEC", "NEAR", "HYPE", "TON", "ADA", "BCH",
        "GOLD", "SILVER", "WTI", "INX", "NASDAQ100",
        "DEFAULT"
    )
}

DEFAULT_DYNAMIC_PRICE_MAP_1H: dict[str, dict[str, dict[str, int | float | bool]]] = {
    sym: {"00": _make_default_mark("00", "1h"), "05": _make_default_mark("05", "1h")}
    for sym in (
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ZEC", "NEAR", "HYPE", "TON", "ADA", "BCH",
        "GOLD", "SILVER", "WTI", "INX", "NASDAQ100",
        "DEFAULT"
    )
}

# In-memory global cache variables (so the bot doesn't slow down by querying Mongo every 5 seconds)
_DYNAMIC_PRICE_MAP_CACHE: dict[str, dict[str, dict[str, int | float | bool]]] | None = None
_LAST_MAP_REFRESH_TIME: float = 0.0
_CACHE_TTL_SECONDS: float = 6 * 3600.0  # 6 hours = 21,600 seconds


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
    Rule 2 (6-Hour Expiry): If more than 6 hours passed since last refresh, refresh!
    Rule 3 (05:15 UTC Post-Exploration Anchor): Immediately after exiting the 05:00-05:15 UTC discovery window,
            force a fresh recalibration at 05:15-05:25 UTC to lock in calibrated parameters.
    Rule 4 (Clock Markers): If current UTC time hits 00:00, 05:00, 07:00, 12:00, or 18:00 UTC and haven't refreshed this hour.
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

    # 1. Check if 6 full hours (21,600 seconds) have passed
    if elapsed >= _CACHE_TTL_SECONDS:
        return True

    # 2. Check if we just exited 05:00-05:15 UTC exploration window (anchor at 05:15-05:25 UTC)
    if now.hour == 5 and 15 <= now.minute < 25:
        window_end_ts = now.replace(minute=15, second=0, microsecond=0).timestamp()
        if last_refresh_time < window_end_ts:
            return True

    # 3. Check standard clock marks (00:00, 05:00, 07:00, 12:00, 18:00 UTC) and haven't refreshed this hour
    if now.hour in (0, 5, 7, 12, 18) and now.minute < 10:
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

        # 2. Group historical trades, SL events, and volatility by asset
        volatility_by_sym: dict[str, list[float]] = defaultdict(list)
        trades_by_sym_mark: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        sl_events_by_sym: dict[str, list[dict]] = defaultdict(list)
        mid_zone_trades_by_sym: dict[str, list[dict]] = defaultdict(list)

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
        all_symbols = set(volatility_by_sym.keys()).union(trades_by_sym_mark.keys())
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

            # Dynamic Asset Classification & Stop-Loss Calibration
            # Deep discount corridor with wide stop loss (25¢ loss threshold, 15¢ floor, 15¢ 5-min cut)
            asset_sl_threshold = 25
            asset_sl_floor = 15
            asset_cut_5min = 15
            asset_endgame_floor = 15

            # Middle Zone Analysis (Recommendation 3)
            mid_trades = mid_zone_trades_by_sym.get(sym, [])
            mid_wins = sum(1 for t in mid_trades if t["won"])
            mid_win_rate = (mid_wins / len(mid_trades)) if len(mid_trades) >= 3 else 0.33
            avoid_mid = (mid_win_rate < 0.40)  # Avoid middle band if win rate is under 40%

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
                base_mom_min = 56
                base_mom_max = 68 if mark == "00" else 65
                base_mom_tp = 88
                mom_min_pct_dist = 0.10
                mom_sl_floor = 40

                mom_wins = [t["buy"] for t in trades if t["won"] and t["buy"] >= 56]
                if len(mom_wins) >= 3:
                    med_mom_buy = int(statistics.median(mom_wins))
                    base_mom_max = max(base_mom_min + 2, min(72, med_mom_buy + 3))

                # Step 4: Runway Timing Enforcement (Bounds: 18¢–44¢ at :00, 18¢–30¢ at :05)
                min_buy = 18
                if mark == "05":
                    max_buy = max(min_buy + 2, min(max_buy, 30))
                else:
                    max_buy = max(min_buy + 2, min(max_buy, 44))
                tp_price = max(max_buy + 12, min(tp_price, 65))

                calibrated_map[sym][mark] = {
                    "min_buy": min_buy,
                    "max_buy": max_buy,
                    "tp": tp_price,
                    "enable_momentum_buys": True,
                    "momentum_min_buy": base_mom_min,
                    "momentum_max_buy": base_mom_max,
                    "momentum_tp": base_mom_tp,
                    "momentum_min_pct_dist": mom_min_pct_dist,
                    "momentum_sl_floor": mom_sl_floor,
                    "sl_threshold": asset_sl_threshold,
                    "sl_floor": asset_sl_floor,
                    "cut_5min": asset_cut_5min,
                    "endgame_12_5_floor": asset_endgame_floor,
                    "avoid_mid_zone": avoid_mid,
                    "mid_zone_min": 37,
                    "mid_zone_max": 55,
                }

        log.info(
            f"🧠 [DYNAMIC CALIBRATION] Calibrated dynamic price & SL filter map for {len(all_symbols)} assets from MongoDB!"
        )
        return calibrated_map

    except Exception as err:
        log.error(f"Error while calibrating dynamic price map from MongoDB: {err}")
        return calibrated_map


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

    Timeframe Differentiations:
    - '15m': Tighter discount corridor (18¢–30¢ at :00, 18¢–24¢ at :05), stricter momentum (>=0.15% lead),
             expanded middle dead zone (31¢–61¢), and 1-contract sniper sizing.
    - '1h':  Broad discount corridor (18¢–44¢ at open, 18¢–32¢ at :20), standard momentum (>=0.10% lead),
             standard middle dead zone (37¢–55¢), and 3-contract core capital sizing.

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

    base_defaults = _make_default_mark(mark, timeframe=tf_clean)

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
        "avoid_mid_zone": False if is_mid_zone_exploration else bool(mark_filter.get("avoid_mid_zone", base_defaults["avoid_mid_zone"])),
        "mid_zone_min": int(mark_filter.get("mid_zone_min", base_defaults["mid_zone_min"])),
        "mid_zone_max": int(mark_filter.get("mid_zone_max", base_defaults["mid_zone_max"])),
        "contracts_per_trade": int(mark_filter.get("contracts_per_trade", base_defaults["contracts_per_trade"])),
        "is_exploration": is_exploration,
    }



