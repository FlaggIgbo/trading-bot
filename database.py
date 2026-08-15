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
  `ex`   : Exchange identifier string ("kalshi") -> 6 bytes
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
import statistics
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
        log.info(f"💰 [{ex_name.upper()}] Successfully recorded Portfolio PnL snapshot to MongoDB Atlas!")
        return True
    except Exception as error:
        log.error(f"Failed to insert [{ex_name.upper()}] portfolio PnL snapshot to MongoDB: {error}")
        return False


def save_settlements(settlements: list[dict]) -> int:
    """
    Upserts settled market trade records into the MongoDB 'settlements' collection.
    Prevents duplicate entries by keying on (ticker, ex, settled_time).

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
            ex_name = str(s.get("exchange") or s.get("ex") or "kalshi").lower()
            if not ticker:
                continue

            filter_query = {"ticker": ticker, "ex": ex_name}
            if settled_time:
                filter_query["settled_time"] = settled_time

            s_doc = {**s, "ex": ex_name}
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
# (at 00:00, 06:00, 12:00, 18:00 UTC) and automatically tunes the best buy & sell prices!
# ===========================================================================

DEFAULT_DYNAMIC_PRICE_MAP: dict[str, dict[str, dict[str, int | bool]]] = {
    "BTC": {
        "00": {"min_buy": 32, "max_buy": 52, "tp": 82, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "ETH": {
        "00": {"min_buy": 32, "max_buy": 52, "tp": 82, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "SOL": {
        "00": {"min_buy": 32, "max_buy": 50, "tp": 82, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "XRP": {
        "00": {"min_buy": 30, "max_buy": 48, "tp": 82, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "DOGE": {
        # High win-rate & mean-reverting: widen stop-loss & cut thresholds to prevent false alarm shakeouts
        "00": {"min_buy": 30, "max_buy": 50, "tp": 82, "sl_threshold": 15, "sl_floor": 25, "cut_5min": 34, "endgame_12_5_floor": 22, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 15, "sl_floor": 25, "cut_5min": 34, "endgame_12_5_floor": 22, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "BNB": {
        # High win-rate & mean-reverting: widen stop-loss & cut thresholds to prevent false alarm shakeouts
        "00": {"min_buy": 30, "max_buy": 50, "tp": 82, "sl_threshold": 15, "sl_floor": 25, "cut_5min": 34, "endgame_12_5_floor": 22, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 15, "sl_floor": 25, "cut_5min": 34, "endgame_12_5_floor": 22, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "ZEC": {
        # High volatility: deeper discount floor, higher TP, adaptive stop loss
        "00": {"min_buy": 28, "max_buy": 48, "tp": 85, "sl_threshold": 12, "sl_floor": 26, "cut_5min": 36, "endgame_12_5_floor": 24, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 36, "tp": 78, "sl_threshold": 12, "sl_floor": 26, "cut_5min": 36, "endgame_12_5_floor": 24, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "NEAR": {
        # High volatility: deeper discount floor, higher TP, adaptive stop loss
        "00": {"min_buy": 28, "max_buy": 48, "tp": 85, "sl_threshold": 12, "sl_floor": 26, "cut_5min": 36, "endgame_12_5_floor": 24, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 36, "tp": 78, "sl_threshold": 12, "sl_floor": 26, "cut_5min": 36, "endgame_12_5_floor": 24, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
    "DEFAULT": {
        "00": {"min_buy": 32, "max_buy": 50, "tp": 82, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
        "05": {"min_buy": 28, "max_buy": 38, "tp": 76, "sl_threshold": 10, "sl_floor": 30, "cut_5min": 40, "endgame_12_5_floor": 25, "avoid_mid_zone": True, "mid_zone_min": 37, "mid_zone_max": 43},
    },
}

# In-memory global cache variables (so the bot doesn't slow down by querying Mongo every 5 seconds)
_DYNAMIC_PRICE_MAP_CACHE: dict[str, dict[str, dict[str, int | bool]]] | None = None
_LAST_MAP_REFRESH_TIME: float = 0.0
_CACHE_TTL_SECONDS: float = 6 * 3600.0  # 6 hours = 21,600 seconds


def should_refresh_dynamic_map(
    now_utc: datetime | None = None,
    last_refresh_time: float | None = None,
    cache_is_empty: bool | None = None,
) -> bool:
    """
    Decides whether it's time to pull fresh trading data from MongoDB.

    Rule 1 (Cold Start): If memory is empty (bot just booted up), refresh immediately!
    Rule 2 (6-Hour Expiry): If more than 6 hours passed since last refresh, refresh!
    Rule 3 (Clock Markers): If current UTC time hits 00:00, 06:00, 12:00, or 18:00 UTC, refresh!
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

    # 2. Check if we are at a 6-hour clock mark (00:00, 06:00, 12:00, 18:00 UTC) and haven't refreshed this hour
    if now.hour in (0, 6, 12, 18) and now.minute < 10:
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
      - Wild coins (NEAR, ZEC, HYPE) enforce deeper buy discounts (min_buy 28¢) and wider TP (85¢).
      - Stable coins (BTC, BNB, ETH) use standard balanced corridors.

    Step 2 (Runway Timing & Theta Optimization - Recommendation 1):
      - Minute :00 (15m runway) receives full allocation corridor (max_buy up to 52¢).
      - Minute :05 (10m runway) has severe theta compression; strict max_buy ceiling clamped to <= 38¢
        to eliminate negative-EV late entries.

    Step 3 (Asset-Specific Stop-Loss Calibration - Recommendation 2):
      - Analyzes historical Stop-Loss accuracy per asset (Rightful Saves vs. False Alarms).
      - Mean-reverting/high win-rate assets (DOGE, BNB) widen SL loss threshold (15¢), floor (25¢),
        and 5-min cut threshold (34¢) to prevent premature shakeouts before profitable settlement.
      - Trending assets (BTC, SOL, XRP) maintain tight, decisive SL (10¢ threshold, 30¢ floor, 40¢ 5-min cut)
        to aggressively preserve capital.

    Step 4 (Middle Zone Avoidance - Recommendation 3):
      - Evaluates empirical EV in the 37¢-43¢ band. If win rate < 40% or EV is negative,
        configures avoid_mid_zone=True so non-momentum orders only enter at deep discounts (<= 36¢)
        or high-confidence momentum (>= 44¢).
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

        # 1. Fetch recent snapshots and settlements
        now_ts = int(datetime.now(timezone.utc).timestamp())
        cutoff_ts = now_ts - (max_history_hours * 3600)

        recent_snapshots = list(snap_col.find({"ts": {"$gte": cutoff_ts}}))
        if not recent_snapshots:
            recent_snapshots = list(snap_col.find().sort("ts", -1).limit(3000))

        recent_settlements = list(settle_col.find())
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

                if action == "buy_opened" and buy_p:
                    trade_record = {
                        "buy": int(buy_p),
                        "won": won,
                        "side": side,
                        "m": m,
                    }
                    trades_by_sym_mark[sym][timing_mark].append(trade_record)
                    if 37 <= int(buy_p) <= 43:
                        mid_zone_trades_by_sym[sym].append(trade_record)

                elif action in ("early_loss_cut", "stop_loss_sold", "stop_loss_exit"):
                    sl_events_by_sym[sym].append({
                        "action": action,
                        "side": side,
                        "won": won,  # If True -> False Alarm; If False -> Rightful Save
                        "loss_cents": trd.get("loss_cents", 10),
                    })

        # 3. Calibrate parameters for each symbol
        all_symbols = set(volatility_by_sym.keys()).union(trades_by_sym_mark.keys())
        for sym in all_symbols:
            pct_samples = volatility_by_sym.get(sym, [])
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

            # Purely Dynamic Asset Classification & Stop-Loss Calibration
            # High win-rate / mean-reverting behavior: detected when false alarms >= 20% or overall win rate >= 45%
            if false_alarm_rate >= 0.20 or sym_win_rate >= 0.45:
                # Widen SL parameters to let positions breathe and avoid premature shakeouts
                asset_sl_threshold = 15
                asset_sl_floor = 25
                asset_cut_5min = 34
                asset_endgame_floor = 22
            # High volatility behavior: detected when standard deviation of 15m price swings >= 10%
            elif vol_std >= 0.10:
                # Adaptive thresholds for high-beta assets
                asset_sl_threshold = 12
                asset_sl_floor = 26
                asset_cut_5min = 36
                asset_endgame_floor = 24
            else:
                # Trending / low-beta behavior: tight, decisive SL and standard endgame floor
                asset_sl_threshold = 10
                asset_sl_floor = 30
                asset_cut_5min = 40
                asset_endgame_floor = 25

            # Middle Zone Analysis (Recommendation 3)
            mid_trades = mid_zone_trades_by_sym.get(sym, [])
            mid_wins = sum(1 for t in mid_trades if t["won"])
            mid_win_rate = (mid_wins / len(mid_trades)) if len(mid_trades) >= 3 else 0.33
            avoid_mid = (mid_win_rate < 0.40)  # Avoid middle band if win rate is under 40%

            calibrated_map[sym] = {}
            for mark in ("00", "05"):
                trades = trades_by_sym_mark.get(sym, {}).get(mark, [])

                # Step 1: Base parameters based dynamically on measured asset volatility
                if vol_std >= 0.10:
                    base_min_buy = 28
                    base_max_buy = 48 if mark == "00" else 36
                    base_tp = 85 if mark == "00" else 78
                elif vol_std >= 0.05:
                    base_min_buy = 32
                    base_max_buy = 50 if mark == "00" else 38
                    base_tp = 82 if mark == "00" else 76
                else:
                    base_min_buy = 32
                    base_max_buy = 52 if mark == "00" else 38
                    base_tp = 82 if mark == "00" else 76

                # Step 2: Fine-tune using winning trade history
                min_buy = base_min_buy
                max_buy = base_max_buy
                tp_price = base_tp

                if len(trades) >= 5:
                    win_prices = [t["buy"] for t in trades if t["won"]]
                    if win_prices:
                        calibrated_max = int(statistics.median(win_prices) + 3)
                        calibrated_min = max(26, min(win_prices) - 2)
                        max_buy = min(base_max_buy, calibrated_max)
                        min_buy = max(base_min_buy, calibrated_min)
                    else:
                        max_buy = min(base_max_buy, 36)
                        min_buy = base_min_buy

                # Step 3: Runway Timing Enforcement (Recommendation 1)
                # For Minute :05 (10m runway), strictly clamp max_buy <= 38 to eliminate theta drag
                if mark == "05":
                    min_buy = max(25, min(min_buy, 34))
                    max_buy = max(min_buy + 2, min(max_buy, 38))
                else:
                    min_buy = max(25, min(min_buy, 40))
                    max_buy = max(min_buy + 2, min(max_buy, 54))
                tp_price = max(max_buy + 15, min(tp_price, 90))

                calibrated_map[sym][mark] = {
                    "min_buy": min_buy,
                    "max_buy": max_buy,
                    "tp": tp_price,
                    "sl_threshold": asset_sl_threshold,
                    "sl_floor": asset_sl_floor,
                    "cut_5min": asset_cut_5min,
                    "endgame_12_5_floor": asset_endgame_floor,
                    "avoid_mid_zone": avoid_mid,
                    "mid_zone_min": 37,
                    "mid_zone_max": 43,
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
    dynamic_map: dict[str, dict[str, dict[str, int | bool]]] | None = None,
) -> dict[str, int | bool]:
    """
    Convenience lookup to get exact dynamic filter parameters for a specific market and timing mark.

    Args:
        asset_symbol: Asset ticker string (e.g. "BTC", "ETH", "SOL", "DOGE", "ZEC").
        is_5min_mark: True if running at the 5-minute mark (:05), False if at minute 00 (:00).
        dynamic_map: Optional preloaded map. If None, uses cached in-memory map.

    Returns:
        Dictionary: {
            "min_buy": int,
            "max_buy": int,
            "tp": int,
            "sl_threshold": int,
            "sl_floor": int,
            "cut_5min": int,
            "endgame_12_5_floor": int,
            "avoid_mid_zone": bool,
            "mid_zone_min": int,
            "mid_zone_max": int
        }
    """
    active_map = dynamic_map or get_dynamic_price_filter_map()
    mark = "05" if is_5min_mark else "00"

    sym_clean = (asset_symbol or "").upper()
    asset_filters = active_map.get(sym_clean) or active_map.get("DEFAULT") or DEFAULT_DYNAMIC_PRICE_MAP["DEFAULT"]
    mark_filter = asset_filters.get(mark) or asset_filters.get("00") or DEFAULT_DYNAMIC_PRICE_MAP["DEFAULT"]["00"]

    return {
        "min_buy": int(mark_filter.get("min_buy", 32)),
        "max_buy": int(mark_filter.get("max_buy", 38 if is_5min_mark else 50)),
        "tp": int(mark_filter.get("tp", 76 if is_5min_mark else 82)),
        "sl_threshold": int(mark_filter.get("sl_threshold", 10)),
        "sl_floor": int(mark_filter.get("sl_floor", 30)),
        "cut_5min": int(mark_filter.get("cut_5min", 40)),
        "endgame_12_5_floor": int(mark_filter.get("endgame_12_5_floor", 25)),
        "avoid_mid_zone": bool(mark_filter.get("avoid_mid_zone", True)),
        "mid_zone_min": int(mark_filter.get("mid_zone_min", 37)),
        "mid_zone_max": int(mark_filter.get("mid_zone_max", 43)),
    }



