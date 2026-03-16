"""
Local storage for historical data: yfinance cache, market snapshots, resolved outcomes.

Data is stored in SQLite under ~/.trading-bot-data/ (configurable). Not committed to git.
Used to: speed runs (cached closes), build history for backtesting, calibrate predictions.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

_DEFAULT_DATA_DIR = Path.home() / ".trading-bot-data"
_CACHE_TTL_SECONDS = 3600  # 1 hour for yf closes (daily data; refresh periodically)


def _get_data_dir(config: dict | None = None) -> Path:
    """Get storage directory from config or env, else default."""
    if config:
        storage = config.get("storage") or {}
        if p := storage.get("data_dir"):
            return Path(p).expanduser()
    if p := os.getenv("TRADING_BOT_DATA_DIR"):
        return Path(p).expanduser()
    return _DEFAULT_DATA_DIR


def _get_db_path(config: dict | None = None) -> Path:
    data_dir = _get_data_dir(config)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "trading_bot.db"


def _is_storage_enabled(config: dict | None = None) -> bool:
    if config:
        storage = config.get("storage") or {}
        if "enabled" in storage:
            return bool(storage["enabled"])
    return os.getenv("TRADING_BOT_STORAGE", "true").lower() in ("1", "true", "yes")


def _get_conn(config: dict | None = None) -> sqlite3.Connection | None:
    if not _is_storage_enabled(config):
        return None
    try:
        path = _get_db_path(config)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        return conn
    except Exception:
        return None


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS yf_cache (
            ticker TEXT NOT NULL,
            days_back INTEGER NOT NULL,
            closes_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (ticker, days_back)
        );
        CREATE TABLE IF NOT EXISTS yf_intraday_cache (
            ticker TEXT NOT NULL,
            days_back INTEGER NOT NULL,
            resolution_hour_et INTEGER NOT NULL,
            closes_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (ticker, days_back, resolution_hour_et)
        );
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            ticker TEXT NOT NULL,
            title TEXT,
            yf_ticker TEXT,
            ts REAL NOT NULL,
            yes_bid REAL,
            no_bid REAL,
            hist_p REAL,
            vol_p REAL,
            rec TEXT,
            threshold REAL,
            direction TEXT,
            edge REAL
        );
        CREATE INDEX IF NOT EXISTS idx_market_snap_ticker ON market_snapshots(ticker);
        CREATE INDEX IF NOT EXISTS idx_market_snap_ts ON market_snapshots(ts);
        CREATE TABLE IF NOT EXISTS resolved_outcomes (
            ticker TEXT PRIMARY KEY,
            platform TEXT,
            title TEXT,
            resolution_ts REAL,
            yes_settled INTEGER,
            our_last_rec TEXT,
            our_last_hist_p REAL,
            market_yes_at_last REAL
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            ticker TEXT NOT NULL,
            order_id TEXT,
            side TEXT NOT NULL,
            hist_p REAL NOT NULL,
            vol_p REAL,
            entry_price REAL NOT NULL,
            count INTEGER NOT NULL,
            ts REAL NOT NULL,
            yf_ticker TEXT,
            threshold REAL,
            direction TEXT,
            rec TEXT,
            outcome TEXT,
            pnl_cents INTEGER,
            resolved_ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);
        CREATE INDEX IF NOT EXISTS idx_predictions_outcome ON predictions(outcome);
        CREATE INDEX IF NOT EXISTS idx_predictions_platform ON predictions(platform);
        CREATE TABLE IF NOT EXISTS limit_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            ticker TEXT NOT NULL,
            order_id TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            limit_price REAL NOT NULL,
            bid_at_place REAL,
            dip_pct_used REAL,
            hist_p REAL,
            vol_p REAL,
            edge REAL,
            rec TEXT,
            count INTEGER,
            ts REAL NOT NULL,
            status TEXT DEFAULT 'placed',
            cancel_reason TEXT,
            cancel_ts REAL,
            yf_ticker TEXT,
            threshold REAL,
            direction TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_limit_orders_ticker ON limit_orders(ticker);
        CREATE INDEX IF NOT EXISTS idx_limit_orders_order_id ON limit_orders(platform, order_id);
        CREATE INDEX IF NOT EXISTS idx_limit_orders_status ON limit_orders(status);
    """)
    _migrate_predictions_price_after_buy(conn)


def _migrate_predictions_price_after_buy(conn: sqlite3.Connection) -> None:
    """Add min_price_after_buy, max_price_after_buy to predictions (price movement after we bought)."""
    for col in ["min_price_after_buy", "max_price_after_buy"]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists


def get_price_after_buy_from_snapshots(
    ticker: str,
    buy_ts: float,
    side: str,
    platform: str,
    config: dict | None = None,
) -> tuple[float | None, float | None]:
    """
    From market_snapshots, get min/max contract price after we bought.
    For YES: use yes_bid. For NO: use no_bid. Returns (min_p, max_p) or (None, None) if no snapshots.
    """
    conn = _get_conn(config)
    if not conn:
        return (None, None)
    try:
        rows = conn.execute(
            """SELECT yes_bid, no_bid FROM market_snapshots
               WHERE ticker=? AND platform=? AND ts>? ORDER BY ts""",
            (ticker, platform, buy_ts),
        ).fetchall()
        if not rows:
            return (None, None)
        prices = [
            (r["yes_bid"] or 0) if (side or "").lower() == "yes" else (r["no_bid"] or (1.0 - (r["yes_bid"] or 0)))
            for r in rows
        ]
        prices = [p for p in prices if p > 0]
        if not prices:
            return (None, None)
        return (min(prices), max(prices))
    except Exception:
        return (None, None)
    finally:
        conn.close()


def get_yf_closes(ticker: str, days_back: int, config: dict | None = None) -> list[float] | None:
    """
    Return cached yfinance closes if fresh, else None.
    TTL: 1 hour. Caller should fetch from yfinance and call set_yf_closes on miss.
    """
    conn = _get_conn(config)
    if not conn:
        return None
    try:
        row = conn.execute(
            "SELECT closes_json, updated_at FROM yf_cache WHERE ticker=? AND days_back=?",
            (ticker, days_back),
        ).fetchone()
        if not row:
            return None
        updated = row["updated_at"]
        if time.time() - updated > _CACHE_TTL_SECONDS:
            return None
        data = json.loads(row["closes_json"])
        return [float(x) for x in data]
    except Exception:
        return None
    finally:
        conn.close()


def set_yf_closes(ticker: str, days_back: int, closes: list[float], config: dict | None = None) -> None:
    """Store yfinance closes in cache."""
    conn = _get_conn(config)
    if not conn:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO yf_cache (ticker, days_back, closes_json, updated_at) VALUES (?,?,?,?)",
            (ticker, days_back, json.dumps([round(c, 4) for c in closes]), time.time()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def get_yf_intraday_at_hour(
    ticker: str,
    days_back: int,
    resolution_hour_et: int,
    config: dict | None = None,
) -> list[float] | None:
    """
    Return cached yfinance intraday closes at resolution_hour_et if fresh, else None.
    TTL: 1 hour. Caller should fetch via get_intraday_aggs_at_hour and call set_yf_intraday_at_hour on miss.
    """
    conn = _get_conn(config)
    if not conn:
        return None
    try:
        row = conn.execute(
            """SELECT closes_json, updated_at FROM yf_intraday_cache
               WHERE ticker=? AND days_back=? AND resolution_hour_et=?""",
            (ticker, days_back, resolution_hour_et),
        ).fetchone()
        if not row:
            return None
        updated = row["updated_at"]
        if time.time() - updated > _CACHE_TTL_SECONDS:
            return None
        data = json.loads(row["closes_json"])
        return [float(x) for x in data]
    except Exception:
        return None
    finally:
        conn.close()


def set_yf_intraday_at_hour(
    ticker: str,
    days_back: int,
    resolution_hour_et: int,
    closes: list[float],
    config: dict | None = None,
) -> None:
    """Store yfinance intraday closes at resolution_hour_et in cache."""
    conn = _get_conn(config)
    if not conn:
        return
    try:
        conn.execute(
            """INSERT OR REPLACE INTO yf_intraday_cache
               (ticker, days_back, resolution_hour_et, closes_json, updated_at)
               VALUES (?,?,?,?,?)""",
            (ticker, days_back, resolution_hour_et, json.dumps([round(c, 4) for c in closes]), time.time()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_market_snapshot(
    platform: str,
    ticker: str,
    yes_bid: float,
    no_bid: float,
    hist_p: float,
    rec: str,
    threshold: float | None = None,
    direction: str | None = None,
    vol_p: float | None = None,
    edge: float | None = None,
    title: str | None = None,
    yf_ticker: str | None = None,
    config: dict | None = None,
) -> None:
    """
    Record a market snapshot for backtesting/calibration.
    Call after each evaluation. ts is auto-set.
    """
    conn = _get_conn(config)
    if not conn:
        return
    ts = time.time()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO market_snapshots
               (platform, ticker, title, yf_ticker, ts, yes_bid, no_bid, hist_p, vol_p, rec, threshold, direction, edge)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                ticker,
                (title or "")[:200],
                yf_ticker or "",
                ts,
                yes_bid,
                no_bid,
                hist_p,
                vol_p,
                rec or "",
                threshold,
                direction or "",
                edge,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_resolved(
    ticker: str,
    yes_settled: bool,
    platform: str | None = None,
    title: str | None = None,
    our_last_rec: str | None = None,
    our_last_hist_p: float | None = None,
    market_yes_at_last: float | None = None,
    config: dict | None = None,
) -> None:
    """
    Record resolved outcome when we detect a market has settled.
    Used for calibration and backtesting. Call when Kalshi/Gemini reports settled.
    """
    conn = _get_conn(config)
    if not conn:
        return
    try:
        conn.execute(
            """INSERT OR REPLACE INTO resolved_outcomes
               (ticker, platform, title, resolution_ts, yes_settled, our_last_rec, our_last_hist_p, market_yes_at_last)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                ticker,
                platform or "",
                (title or "")[:200],
                time.time(),
                1 if yes_settled else 0,
                our_last_rec or "",
                our_last_hist_p,
                market_yes_at_last,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_limit_order(
    platform: str,
    ticker: str,
    order_id: str,
    side: str,
    action: str,
    limit_price: float,
    bid_at_place: float | None = None,
    dip_pct_used: float | None = None,
    hist_p: float | None = None,
    vol_p: float | None = None,
    edge: float | None = None,
    rec: str | None = None,
    count: int | None = None,
    yf_ticker: str | None = None,
    threshold: float | None = None,
    direction: str | None = None,
    config: dict | None = None,
) -> None:
    """
    Record a limit order we placed. Used to learn from unfilled orders:
    "limit at 38¢, market never dipped → we could relax dip" or "limit at 38¢,
    we cancelled (no_longer_rec) but price dipped to 35¢ → we would have filled."
    """
    conn = _get_conn(config)
    if not conn:
        return
    ts = time.time()
    try:
        conn.execute(
            """INSERT INTO limit_orders
               (platform, ticker, order_id, side, action, limit_price, bid_at_place, dip_pct_used,
                hist_p, vol_p, edge, rec, count, ts, yf_ticker, threshold, direction, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'placed')""",
            (
                platform,
                ticker,
                order_id,
                side,
                action,
                limit_price,
                bid_at_place,
                dip_pct_used,
                hist_p,
                vol_p,
                edge,
                rec or "",
                count,
                ts,
                yf_ticker or "",
                threshold,
                direction or "",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_order_cancel(
    platform: str,
    order_id: str,
    cancel_reason: str,
    config: dict | None = None,
) -> bool:
    """
    Mark a limit order as cancelled when we cancel it. cancel_reason: no_longer_rec,
    want_better_price. Returns True if updated.
    """
    conn = _get_conn(config)
    if not conn:
        return False
    ts = time.time()
    try:
        cur = conn.execute(
            "UPDATE limit_orders SET status='cancelled', cancel_reason=?, cancel_ts=? WHERE platform=? AND order_id=? AND status='placed'",
            (cancel_reason, ts, platform, order_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def get_unfilled_limit_orders_for_analysis(
    config: dict | None = None,
    days: int = 90,
) -> list[dict]:
    """
    Return limit orders that were placed and either cancelled or still resting,
    for analysis. Joins with resolved_outcomes and market_snapshots to compute:
    - min_price_during_order: did price ever hit our limit? (would have filled)
    - yes_settled: outcome when market resolved (for learning)
    """
    conn = _get_conn(config)
    if not conn:
        return []
    cutoff = time.time() - days * 86400
    try:
        rows = conn.execute(
            """SELECT id, platform, ticker, order_id, side, action, limit_price, bid_at_place,
                      dip_pct_used, hist_p, edge, rec, ts, cancel_reason, status
               FROM limit_orders WHERE ts > ? AND status IN ('placed','cancelled')
               ORDER BY ts DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def update_limit_order_filled(platform: str, order_id: str, config: dict | None = None) -> bool:
    """Mark a limit order as filled when we detect it filled (e.g. via settlements)."""
    conn = _get_conn(config)
    if not conn:
        return False
    try:
        cur = conn.execute(
            "UPDATE limit_orders SET status='filled' WHERE platform=? AND order_id=? AND status='placed'",
            (platform, order_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def record_limit_order(
    platform: str,
    ticker: str,
    order_id: str,
    side: str,
    action: str,
    limit_price: float,
    bid_at_place: float | None = None,
    dip_pct_used: float | None = None,
    hist_p: float | None = None,
    vol_p: float | None = None,
    edge: float | None = None,
    rec: str | None = None,
    count: int | None = None,
    yf_ticker: str | None = None,
    threshold: float | None = None,
    direction: str | None = None,
    config: dict | None = None,
) -> int | None:
    """
    Record a placed limit order. Used to learn from unfilled orders (why they didn't hit).
    Returns row id or None.
    """
    conn = _get_conn(config)
    if not conn:
        return None
    ts = time.time()
    try:
        cur = conn.execute(
            """INSERT INTO limit_orders
               (platform, ticker, order_id, side, action, limit_price, bid_at_place, dip_pct_used,
                hist_p, vol_p, edge, rec, count, ts, yf_ticker, threshold, direction, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                ticker,
                order_id,
                side,
                action,
                limit_price,
                bid_at_place,
                dip_pct_used,
                hist_p,
                vol_p,
                edge,
                rec or "",
                count,
                ts,
                yf_ticker or "",
                threshold,
                direction or "",
                "placed",
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def record_order_cancel(
    platform: str,
    order_id: str,
    cancel_reason: str,
    config: dict | None = None,
) -> bool:
    """Mark a limit order as cancelled with reason. cancel_reason: no_longer_rec | want_better_price."""
    conn = _get_conn(config)
    if not conn:
        return False
    try:
        conn.execute(
            """UPDATE limit_orders SET status='cancelled', cancel_reason=?, cancel_ts=? 
               WHERE platform=? AND order_id=? AND status='placed'""",
            (cancel_reason, time.time(), platform, order_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def get_unfilled_limit_orders_for_analysis(
    config: dict | None = None,
    days: int = 90,
) -> list[dict]:
    """
    Return limit orders that were cancelled or never filled (status=cancelled or placed for resolved markets).
    Joins with resolved_outcomes to enable learning: "limit at X, market resolved; did price ever hit X?"
    """
    conn = _get_conn(config)
    if not conn:
        return []
    since_ts = time.time() - (days * 86400)
    try:
        rows = conn.execute(
            """SELECT l.id, l.platform, l.ticker, l.order_id, l.side, l.action, l.limit_price,
                      l.bid_at_place, l.dip_pct_used, l.hist_p, l.rec, l.ts, l.status, l.cancel_reason,
                      r.yes_settled, r.resolution_ts
               FROM limit_orders l
               LEFT JOIN resolved_outcomes r ON l.ticker = r.ticker AND l.platform = r.platform
               WHERE l.ts >= ? AND (l.status IN ('cancelled', 'placed') OR l.status IS NULL)""",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def get_limit_order_analysis(config: dict | None = None, days: int = 90) -> dict[str, Any]:
    """
    Analyze unfilled limit orders: for each cancelled/unfilled order on a resolved market,
    compute min/max contract price from market_snapshots during order lifetime.
    Returns: { "orders": [...], "summary": {...} } to inform limit_dip_pct tuning.
    """
    conn = _get_conn(config)
    if not conn:
        return {"orders": [], "summary": {}}
    since_ts = time.time() - (days * 86400)
    try:
        rows = conn.execute(
            """SELECT l.id, l.platform, l.ticker, l.side, l.limit_price, l.bid_at_place, l.dip_pct_used,
                      l.hist_p, l.rec, l.ts, l.status, l.cancel_reason, r.yes_settled
               FROM limit_orders l
               LEFT JOIN resolved_outcomes r ON l.ticker = r.ticker AND l.platform = r.platform
               WHERE l.ts >= ? AND l.action = 'buy' AND l.status IN ('cancelled', 'placed')""",
            (since_ts,),
        ).fetchall()
        orders = []
        for r in rows:
            o = dict(r)
            ticker = o["ticker"]
            platform = o["platform"]
            side = o["side"]
            place_ts = o["ts"]
            # Get min price from snapshots during order lifetime (place_ts → resolution or now)
            snaps = conn.execute(
                """SELECT yes_bid, no_bid FROM market_snapshots 
                   WHERE ticker=? AND platform=? AND ts>=? ORDER BY ts""",
                (ticker, platform, place_ts),
            ).fetchall()
            prices = []
            for s in snaps:
                p = (s["yes_bid"] or 0) if side == "yes" else (s["no_bid"] or (1.0 - (s["yes_bid"] or 0)))
                if p > 0:
                    prices.append(p)
            o["min_price_after_place"] = min(prices) if prices else None
            o["max_price_after_place"] = max(prices) if prices else None
            o["would_have_filled"] = min(prices) <= o["limit_price"] if prices else False
            orders.append(o)
        filled_count = sum(1 for o in orders if o.get("would_have_filled"))
        return {
            "orders": orders,
            "summary": {
                "total": len(orders),
                "would_have_filled": filled_count,
                "missed_fills": len(orders) - filled_count,
            },
        }
    except Exception:
        return {"orders": [], "summary": {}}
    finally:
        conn.close()


def record_limit_order(
    platform: str,
    ticker: str,
    order_id: str,
    side: str,
    action: str,
    limit_price: float,
    bid_at_place: float | None = None,
    dip_pct_used: float | None = None,
    hist_p: float | None = None,
    vol_p: float | None = None,
    edge: float | None = None,
    rec: str | None = None,
    count: int | None = None,
    yf_ticker: str | None = None,
    threshold: float | None = None,
    direction: str | None = None,
    config: dict | None = None,
) -> None:
    """
    Record a limit order we placed. Used to learn from unfilled orders:
    when we cancel or when market resolves unfilled, we can analyze whether
    our limit was too aggressive (never touched) or we cancelled too soon.
    """
    conn = _get_conn(config)
    if not conn:
        return
    ts = time.time()
    try:
        conn.execute(
            """INSERT INTO limit_orders
               (platform, ticker, order_id, side, action, limit_price, bid_at_place, dip_pct_used,
                hist_p, vol_p, edge, rec, count, ts, yf_ticker, threshold, direction)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                ticker,
                order_id,
                side,
                action,
                limit_price,
                bid_at_place,
                dip_pct_used,
                hist_p,
                vol_p,
                edge,
                rec or "",
                count,
                ts,
                yf_ticker or "",
                threshold,
                direction or "",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_order_cancel(
    platform: str,
    order_id: str,
    cancel_reason: str,
    config: dict | None = None,
    *,
    ticker: str | None = None,
    side: str | None = None,
    limit_price: float | None = None,
) -> None:
    """
    Mark a limit order as cancelled. cancel_reason: no_longer_rec | want_better_price.
    If we have a row (by platform+order_id), update it. Else insert a minimal row
    so we have a record of cancelled orders even from before limit-order tracking.
    """
    conn = _get_conn(config)
    if not conn:
        return
    ts = time.time()
    try:
        cur = conn.execute(
            "UPDATE limit_orders SET status='cancelled', cancel_reason=?, cancel_ts=? WHERE platform=? AND order_id=?",
            (cancel_reason, ts, platform, order_id),
        )
        if cur.rowcount == 0 and ticker and side and limit_price is not None:
            conn.execute(
                """INSERT INTO limit_orders
                   (platform, ticker, order_id, side, action, limit_price, ts, status, cancel_reason, cancel_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (platform, ticker, order_id, side, "buy", limit_price, ts, "cancelled", cancel_reason, ts),
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def get_unfilled_limit_orders_for_analysis(
    config: dict | None = None,
    days: int = 90,
) -> list[dict]:
    """
    Return limit orders that were cancelled or never filled, joined with
    resolved_outcomes when available. Enables learning: did our limit ever touch?
    Would a smaller dip have filled? Used to tune limit_dip_pct.
    """
    conn = _get_conn(config)
    if not conn:
        return []
    since = time.time() - (days * 86400)
    try:
        rows = conn.execute(
            """SELECT lo.*, ro.yes_settled, ro.resolution_ts
               FROM limit_orders lo
               LEFT JOIN resolved_outcomes ro ON lo.ticker = ro.ticker
               WHERE lo.status IN ('cancelled', 'placed') AND lo.ts >= ?
               ORDER BY lo.ts DESC""",
            (since,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Get min price during order lifetime from market_snapshots (for "would we have filled?")
            snap_rows = conn.execute(
                """SELECT yes_bid, no_bid FROM market_snapshots
                   WHERE ticker=? AND platform=? AND ts>=? AND ts<=?
                   ORDER BY ts""",
                (
                    d["ticker"],
                    d["platform"],
                    d["ts"],
                    d["resolution_ts"] or time.time(),
                ),
            ).fetchall()
            min_price = None
            if snap_rows and d["side"]:
                prices = [
                    (row["yes_bid"] or 0) if d["side"].lower() == "yes" else (row["no_bid"] or (1.0 - (row["yes_bid"] or 0)))
                    for row in snap_rows
                ]
                prices = [p for p in prices if p > 0]
                if prices:
                    min_price = min(prices)
            d["min_price_during_order"] = min_price
            d["would_have_filled"] = (
                min_price is not None
                and d.get("limit_price") is not None
                and min_price <= float(d["limit_price"]) + 0.001
            )
            result.append(d)
        return result
    except Exception:
        return []
    finally:
        conn.close()


def record_limit_order(
    platform: str,
    ticker: str,
    order_id: str,
    side: str,
    action: str,
    limit_price: float,
    bid_at_place: float | None = None,
    dip_pct_used: float | None = None,
    hist_p: float | None = None,
    vol_p: float | None = None,
    edge: float | None = None,
    rec: str | None = None,
    count: int | None = None,
    yf_ticker: str | None = None,
    threshold: float | None = None,
    direction: str | None = None,
    config: dict | None = None,
) -> None:
    """
    Record a limit order we placed. Used to learn from unfilled orders: why they didn't hit,
    whether our dip was too aggressive, etc. Status starts as 'placed'; update via record_order_cancel.
    """
    conn = _get_conn(config)
    if not conn:
        return
    ts = time.time()
    try:
        conn.execute(
            """INSERT INTO limit_orders
               (platform, ticker, order_id, side, action, limit_price, bid_at_place, dip_pct_used,
                hist_p, vol_p, edge, rec, count, ts, yf_ticker, threshold, direction, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'placed')""",
            (
                platform,
                ticker,
                order_id,
                side,
                action,
                limit_price,
                bid_at_place,
                dip_pct_used,
                hist_p,
                vol_p,
                edge,
                rec or "",
                count,
                ts,
                yf_ticker or "",
                threshold,
                direction or "",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_order_cancel(
    platform: str,
    order_id: str,
    cancel_reason: str,
    config: dict | None = None,
    *,
    ticker: str | None = None,
    side: str | None = None,
    limit_price: float | None = None,
) -> None:
    """
    Mark a limit order as cancelled. cancel_reason: 'no_longer_rec' | 'want_better_price'.
    If we have a row (we placed it), update it. Else insert minimal row for historical record.
    """
    conn = _get_conn(config)
    if not conn:
        return
    ts = time.time()
    try:
        cur = conn.execute(
            "UPDATE limit_orders SET status='cancelled', cancel_reason=?, cancel_ts=? WHERE platform=? AND order_id=?",
            (cancel_reason, ts, platform, order_id),
        )
        if cur.rowcount == 0 and ticker and side and limit_price is not None:
            conn.execute(
                """INSERT INTO limit_orders (platform, ticker, order_id, side, action, limit_price, ts, status, cancel_reason, cancel_ts)
                   VALUES (?,?,?,?,?,?,?,'cancelled',?,?)""",
                (platform, ticker, order_id, side, "buy", limit_price, ts, cancel_reason, ts),
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def get_unfilled_limit_orders_for_analysis(
    config: dict | None = None,
    status_filter: str | str | None = None,
) -> list[dict]:
    """
    Return limit orders that didn't fill (cancelled or placed and never filled) for calibration.
    Joins with resolved_outcomes when ticker has resolved. Use to learn: "limit at 38¢, market
    never dipped that low" vs "limit at 38¢, we cancelled but price hit 35¢ — we could have filled."
    """
    conn = _get_conn(config)
    if not conn:
        return []
    try:
        if status_filter is None:
            where = "status IN ('placed', 'cancelled')"
            params: tuple = ()
        elif isinstance(status_filter, str):
            where = "status = ?"
            params = (status_filter,)
        else:
            where = "status IN (" + ",".join("?" * len(status_filter)) + ")"
            params = tuple(status_filter)
        rows = conn.execute(
            f"""SELECT lo.*, ro.yes_settled, ro.resolution_ts
                FROM limit_orders lo
                LEFT JOIN resolved_outcomes ro ON lo.ticker = ro.ticker
                WHERE {where}
                ORDER BY lo.ts DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def record_prediction(
    platform: str,
    ticker: str,
    side: str,
    hist_p: float,
    entry_price: float,
    count: int,
    order_id: str | None = None,
    vol_p: float | None = None,
    yf_ticker: str | None = None,
    threshold: float | None = None,
    direction: str | None = None,
    rec: str | None = None,
    config: dict | None = None,
) -> int | None:
    """
    Record a placed prediction (our bet). Returns prediction id or None.
    Call when an order is successfully placed on Kalshi or Gemini.
    """
    conn = _get_conn(config)
    if not conn:
        return None
    ts = time.time()
    try:
        cur = conn.execute(
            """INSERT INTO predictions
               (platform, ticker, order_id, side, hist_p, vol_p, entry_price, count, ts, yf_ticker, threshold, direction, rec)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                ticker,
                order_id or "",
                side,
                hist_p,
                vol_p,
                entry_price,
                count,
                ts,
                yf_ticker or "",
                threshold,
                direction or "",
                rec or "",
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def get_unresolved_predictions(config: dict | None = None) -> list[dict]:
    """Return predictions that don't have an outcome yet (market may still be open)."""
    conn = _get_conn(config)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, platform, ticker, side, hist_p, entry_price, count, ts FROM predictions WHERE outcome IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def record_prediction_outcome(
    prediction_id: int,
    yes_settled: bool,
    pnl_cents: int,
    config: dict | None = None,
    *,
    min_price_after_buy: float | None = None,
    max_price_after_buy: float | None = None,
) -> bool:
    """Mark a prediction as resolved. outcome: win if (side==yes and yes_settled) or (side==no and not yes_settled).
    Optionally store min/max contract price after we bought (from market_snapshots) for calibration."""
    conn = _get_conn(config)
    if not conn:
        return False
    try:
        row = conn.execute(
            "SELECT side FROM predictions WHERE id=? AND outcome IS NULL",
            (prediction_id,),
        ).fetchone()
        if not row:
            conn.close()
            return False
        side = row["side"]
        won = (side == "yes" and yes_settled) or (side == "no" and not yes_settled)
        outcome = "win" if won else "lose"
        if min_price_after_buy is not None and max_price_after_buy is not None:
            conn.execute(
                "UPDATE predictions SET outcome=?, pnl_cents=?, resolved_ts=?, min_price_after_buy=?, max_price_after_buy=? WHERE id=?",
                (outcome, pnl_cents, time.time(), min_price_after_buy, max_price_after_buy, prediction_id),
            )
        else:
            conn.execute(
                "UPDATE predictions SET outcome=?, pnl_cents=?, resolved_ts=? WHERE id=?",
                (outcome, pnl_cents, time.time(), prediction_id),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def _predictions_has_price_after_buy_columns(conn: sqlite3.Connection) -> bool:
    """Check if predictions table has min/max_price_after_buy columns (migration may have failed)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()]
    return "min_price_after_buy" in cols and "max_price_after_buy" in cols


def get_calibration_stats(config: dict | None = None) -> dict[str, Any]:
    """
    Return calibration stats from resolved predictions: win rate, total PnL, win rate by hist_p bucket,
    and win rate by (direction, side) for BUY YES on "above X" vs BUY NO on "below X" etc.
    Used to detect overconfidence and YES/NO bias (e.g. BUY YES on "above X" underperforming).
    Includes price_after_buy stats when columns exist (min/max contract price after we bought).
    """
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False, "resolved_count": 0}
    try:
        has_pab_cols = _predictions_has_price_after_buy_columns(conn)
        select_cols = "outcome, pnl_cents, hist_p, direction, side, entry_price"
        if has_pab_cols:
            select_cols += ", min_price_after_buy, max_price_after_buy"
        rows = conn.execute(
            f"""SELECT {select_cols} FROM predictions WHERE outcome IS NOT NULL"""
        ).fetchall()
        if not rows:
            return {
                "enabled": True,
                "resolved_count": 0,
                "win_count": 0,
                "lose_count": 0,
                "total_pnl_cents": 0,
                "win_rate": None,
                "buckets": [],
                "by_direction_side": {},
                "price_after_buy": None,
            }
        wins = sum(1 for r in rows if r["outcome"] == "win")
        loses = sum(1 for r in rows if r["outcome"] == "lose")
        total_pnl = sum(r["pnl_cents"] or 0 for r in rows)
        loss_weight = float((config or {}).get("strategy", {}).get("calibration_loss_weight", 1.0))
        # Bucket by hist_p (0.5-0.6, 0.6-0.7, ...); weight losers higher for win_rate
        buckets: dict[tuple[float, float], list[str]] = {}
        for r in rows:
            h = r["hist_p"] or 0.5
            bucket = (round(h * 10) / 10, round(h * 10) / 10 + 0.1)
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(r["outcome"])
        bucket_stats = []
        for (lo, hi), outcomes in sorted(buckets.items()):
            ww = sum(1 for o in outcomes if o == "win")
            wl = sum(loss_weight for o in outcomes if o == "lose")
            wr = ww / (ww + wl) if (ww + wl) > 0 else 0
            bucket_stats.append({"hist_p_range": f"{lo:.1f}-{hi:.1f}", "lo": lo, "hi": hi, "count": len(outcomes), "win_rate": wr})
        # Win rate by (direction, side); weight losers higher
        by_ds: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = (r["direction"] or "").strip().lower() or "unknown"
            s = (r["side"] or "").strip().lower() or "unknown"
            key = f"{d}+{s}"
            if key not in by_ds:
                by_ds[key] = {"wins": 0, "loses": 0}
            if r["outcome"] == "win":
                by_ds[key]["wins"] += 1
            else:
                by_ds[key]["loses"] += 1
        by_direction_side = {}
        for k, v in by_ds.items():
            ww, wl = v["wins"], v["loses"] * loss_weight
            wr = ww / (ww + wl) if (ww + wl) > 0 else 0
            by_direction_side[k] = {"count": v["wins"] + v["loses"], "win_rate": wr}
        # Price-after-buy: how much lower/higher did contract go after we bought "low"
        rows_with_price = [
            r for r in rows
            if r.get("entry_price") and r.get("min_price_after_buy") is not None and r.get("max_price_after_buy") is not None
        ]
        price_after_buy = None
        if rows_with_price:
            entry_prices = [r["entry_price"] for r in rows_with_price]
            min_after = [r["min_price_after_buy"] for r in rows_with_price]
            max_after = [r["max_price_after_buy"] for r in rows_with_price]
            dips = [(e - m) / e for e, m in zip(entry_prices, min_after) if e > 0 and m < e]
            rises = [(m - e) / e for e, m in zip(entry_prices, max_after) if e > 0 and m > e]
            price_after_buy = {
                "count": len(rows_with_price),
                "avg_dip_pct": sum(dips) / len(dips) * 100 if dips else 0,
                "avg_rise_pct": sum(rises) / len(rises) * 100 if rises else 0,
                "pct_dipped": len(dips) / len(rows_with_price) * 100 if rows_with_price else 0,
                "pct_rose": len(rises) / len(rows_with_price) * 100 if rows_with_price else 0,
            }
        return {
            "enabled": True,
            "resolved_count": len(rows),
            "win_count": wins,
            "lose_count": loses,
            "total_pnl_cents": total_pnl,
            "win_rate": wins / (wins + loses) if (wins + loses) > 0 else None,
            "buckets": bucket_stats,
            "by_direction_side": by_direction_side,
            "price_after_buy": price_after_buy,
        }
    except Exception:
        return {"enabled": False, "resolved_count": 0}
    finally:
        conn.close()


def get_market_snapshots_for_backtest(
    start_ts: float | None = None,
    end_ts: float | None = None,
    platform: str | None = None,
    rec_filter: list[str] | None = None,
    config: dict | None = None,
) -> list[dict]:
    """
    Return market snapshots for backtesting. Optional filters by time range, platform, rec.
    rec_filter: e.g. ["BUY YES", "BUY NO"] to include only actionable recommendations.
    """
    conn = _get_conn(config)
    if not conn:
        return []
    try:
        q = "SELECT * FROM market_snapshots WHERE 1=1"
        params: list[Any] = []
        if start_ts is not None:
            q += " AND ts >= ?"
            params.append(start_ts)
        if end_ts is not None:
            q += " AND ts <= ?"
            params.append(end_ts)
        if platform:
            q += " AND platform = ?"
            params.append(platform)
        if rec_filter:
            placeholders = ",".join("?" * len(rec_filter))
            q += f" AND rec IN ({placeholders})"
            params.extend(rec_filter)
        q += " ORDER BY ticker, ts"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def get_last_snapshot_per_ticker(
    rec_filter: list[str] | None = None,
    platform: str | None = None,
    config: dict | None = None,
) -> dict[str, dict]:
    """
    Return the most recent snapshot per ticker. Used for synthetic backtest and
    populating resolved_outcomes with our_last_rec/hist_p/market_yes.
    Returns {ticker: snapshot_dict}.
    """
    conn = _get_conn(config)
    if not conn:
        return {}
    try:
        q = """
            SELECT * FROM market_snapshots m1
            WHERE m1.id = (
                SELECT id FROM market_snapshots m2
                WHERE m2.ticker = m1.ticker
                AND (1=1)
                ORDER BY m2.ts DESC LIMIT 1
            )
        """
        params: list[Any] = []
        if platform:
            q = q.replace("(1=1)", "m2.platform = ?")
            params.append(platform)
        if rec_filter:
            ph = ",".join("?" * len(rec_filter))
            q = q.replace("(1=1)", f"m2.rec IN ({ph})")
            params.extend(rec_filter)
        # Simplify: just get all, group by ticker and take max ts in Python
        inner = "SELECT ticker, platform, title, yf_ticker, ts, yes_bid, no_bid, hist_p, vol_p, rec, threshold, direction, edge FROM market_snapshots WHERE 1=1"
        if platform:
            inner += " AND platform = ?"
        if rec_filter:
            inner += f" AND rec IN ({','.join('?' * len(rec_filter))})"
        inner += " ORDER BY ticker, ts DESC"
        params_inner = []
        if platform:
            params_inner.append(platform)
        if rec_filter:
            params_inner.extend(rec_filter)
        rows = conn.execute(
            f"SELECT * FROM ({inner})",
            params_inner,
        ).fetchall()
        by_ticker: dict[str, dict] = {}
        for r in rows:
            t = r["ticker"]
            if t not in by_ticker:
                by_ticker[t] = dict(r)
        return by_ticker
    except Exception:
        return {}
    finally:
        conn.close()


def get_resolved_outcomes(
    tickers: list[str] | None = None,
    config: dict | None = None,
) -> dict[str, dict]:
    """Return resolved outcomes. If tickers given, filter to those. Returns {ticker: row_dict}."""
    conn = _get_conn(config)
    if not conn:
        return {}
    try:
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            rows = conn.execute(
                f"SELECT * FROM resolved_outcomes WHERE ticker IN ({placeholders})",
                tickers,
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM resolved_outcomes").fetchall()
        return {r["ticker"]: dict(r) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def get_predictions_for_backtest(
    start_ts: float | None = None,
    end_ts: float | None = None,
    resolved_only: bool = True,
    config: dict | None = None,
) -> list[dict]:
    """Return predictions for backtest. resolved_only: only rows with outcome."""
    conn = _get_conn(config)
    if not conn:
        return []
    try:
        q = "SELECT * FROM predictions WHERE outcome IS NOT NULL" if resolved_only else "SELECT * FROM predictions"
        params: list[Any] = []
        if start_ts is not None:
            ts_col = "resolved_ts" if resolved_only else "ts"
            q += f" AND {ts_col} >= ?"
            params.append(start_ts)
        if end_ts is not None:
            ts_col = "resolved_ts" if resolved_only else "ts"
            q += f" AND {ts_col} <= ?"
            params.append(end_ts)
        q += " ORDER BY resolved_ts ASC, ts ASC"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def get_last_snapshot_for_ticker(
    ticker: str, platform: str | None = None, config: dict | None = None
) -> dict | None:
    """Return the most recent market snapshot for a ticker. Optional platform filter. For settlement sync."""
    conn = _get_conn(config)
    if not conn:
        return None
    try:
        q = "SELECT * FROM market_snapshots WHERE ticker = ?"
        params: list = [ticker]
        if platform:
            q += " AND platform = ?"
            params.append(platform)
        q += " ORDER BY ts DESC LIMIT 1"
        row = conn.execute(q, tuple(params)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def get_storage_stats(config: dict | None = None) -> dict[str, Any]:
    """Return counts of cached/snapshot data. Useful for diagnostics."""
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False}
    try:
        cache_count = conn.execute("SELECT COUNT(*) FROM yf_cache").fetchone()[0]
        snap_count = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        resolved_count = conn.execute("SELECT COUNT(*) FROM resolved_outcomes").fetchone()[0]
        pred_count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        pred_resolved = conn.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL").fetchone()[0]
        return {
            "enabled": True,
            "data_dir": str(_get_data_dir(config)),
            "yf_cache_entries": cache_count,
            "market_snapshots": snap_count,
            "resolved_outcomes": resolved_count,
            "predictions": pred_count,
            "predictions_resolved": pred_resolved,
        }
    except Exception:
        return {"enabled": False}
    finally:
        conn.close()
