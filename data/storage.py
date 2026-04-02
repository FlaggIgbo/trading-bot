"""
Local storage for historical data: yfinance cache, market snapshots, resolved outcomes.

Data is stored in SQLite under ~/.trading-bot-data/ (configurable). Not committed to git.
Used to: speed runs (cached closes), build history for backtesting, calibrate predictions.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import math
from pathlib import Path
from typing import Any, Sequence

_log = logging.getLogger(__name__)

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
            yes_ask REAL,
            no_ask REAL,
            yes_bid_size REAL,
            no_bid_size REAL,
            yes_ask_size REAL,
            no_ask_size REAL,
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
            fill_id TEXT,
            side TEXT NOT NULL,
            hist_p REAL NOT NULL,
            vol_p REAL,
            entry_price REAL NOT NULL,
            count INTEGER NOT NULL,
            ts REAL NOT NULL,
            fill_ts REAL,
            source TEXT,
            yf_ticker TEXT,
            threshold REAL,
            direction TEXT,
            rec TEXT,
            outcome TEXT,
            pnl_cents INTEGER,
            realized_pnl_cents INTEGER,
            resolved_ts REAL,
            exit_price REAL,
            exit_count INTEGER,
            exit_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);
        CREATE INDEX IF NOT EXISTS idx_predictions_outcome ON predictions(outcome);
        CREATE INDEX IF NOT EXISTS idx_predictions_platform ON predictions(platform);
        CREATE INDEX IF NOT EXISTS idx_predictions_fill ON predictions(platform, fill_id);
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
            direction TEXT,
            theoretical_price REAL,
            spread_bps_at_place REAL,
            fill_slippage_bps REAL
        );
        CREATE INDEX IF NOT EXISTS idx_limit_orders_ticker ON limit_orders(ticker);
        CREATE INDEX IF NOT EXISTS idx_limit_orders_order_id ON limit_orders(platform, order_id);
        CREATE INDEX IF NOT EXISTS idx_limit_orders_status ON limit_orders(status);
        CREATE TABLE IF NOT EXISTS position_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            sell_reason TEXT,
            hist_p REAL,
            vol_p REAL,
            p_lose REAL,
            yes_bid REAL,
            no_bid REAL,
            threshold REAL,
            direction TEXT,
            yf_ticker TEXT,
            rec TEXT,
            position_count INTEGER,
            entry_price REAL,
            current_price REAL,
            ts REAL NOT NULL,
            outcome TEXT,
            yes_settled INTEGER,
            pnl_cents INTEGER,
            resolved_ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_pos_eval_ticker ON position_evaluations(ticker);
        CREATE INDEX IF NOT EXISTS idx_pos_eval_outcome ON position_evaluations(outcome);
        CREATE INDEX IF NOT EXISTS idx_pos_eval_ts ON position_evaluations(ts);
        CREATE INDEX IF NOT EXISTS idx_pos_eval_resolved_ts ON position_evaluations(resolved_ts);
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            hist_p REAL,
            theoretical_price REAL,
            edge REAL,
            ts REAL NOT NULL,
            outcome TEXT,
            resolved_ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome);
    """)
    _migrate_predictions_price_after_buy(conn)
    _migrate_predictions_fill_attribution(conn)
    _migrate_predictions_fill_identity(conn)
    _migrate_predictions_exit_columns(conn)
    _migrate_predictions_realized_pnl_column(conn)
    _migrate_market_snapshots_depth_columns(conn)
    _migrate_predictions_resolution_price(conn)
    _migrate_limit_orders_fill_attribution(conn)
    _migrate_markout_table(conn)
    _migrate_stop_loss_book_state(conn)


def _migrate_predictions_price_after_buy(conn: sqlite3.Connection) -> None:
    """Add min_price_after_buy, max_price_after_buy to predictions (price movement after we bought)."""
    for col in ["min_price_after_buy", "max_price_after_buy"]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists


def _migrate_predictions_fill_attribution(conn: sqlite3.Connection) -> None:
    """Add fill-attribution columns to predictions (actual vs theoretical execution)."""
    for col in [
        "theoretical_price",
        "bid_at_place",
        "spread_bps_at_place",
        "fill_slippage_bps",
    ]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _migrate_predictions_fill_identity(conn: sqlite3.Connection) -> None:
    """Add fill identity/source columns for reconciliation-driven inserts."""
    for col, typ in [
        ("fill_id", "TEXT"),
        ("fill_ts", "REAL"),
        ("source", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_fill ON predictions(platform, fill_id)")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_predictions_exit_columns(conn: sqlite3.Connection) -> None:
    """Add optional exit-tracking columns for early-sell/cancel lifecycle accounting."""
    for col, typ in [
        ("exit_price", "REAL"),
        ("exit_count", "INTEGER"),
        ("exit_reason", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _migrate_predictions_realized_pnl_column(conn: sqlite3.Connection) -> None:
    """Add explicit realized pnl column and backfill from legacy pnl_cents."""
    try:
        conn.execute("ALTER TABLE predictions ADD COLUMN realized_pnl_cents INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "UPDATE predictions SET realized_pnl_cents = COALESCE(realized_pnl_cents, pnl_cents)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_market_snapshots_depth_columns(conn: sqlite3.Connection) -> None:
    """Add market depth/top-of-book columns to market_snapshots for backtest realism."""
    for col in [
        "yes_ask",
        "no_ask",
        "yes_bid_size",
        "no_bid_size",
        "yes_ask_size",
        "no_ask_size",
    ]:
        try:
            conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _migrate_predictions_resolution_price(conn: sqlite3.Connection) -> None:
    """Add resolution_price column to predictions (actual underlying asset price at settlement)."""
    try:
        conn.execute("ALTER TABLE predictions ADD COLUMN resolution_price REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_limit_orders_fill_attribution(conn: sqlite3.Connection) -> None:
    """Add fill-attribution columns to limit_orders (match predictions schema)."""
    for col in ["theoretical_price", "spread_bps_at_place", "fill_slippage_bps"]:
        try:
            conn.execute(f"ALTER TABLE limit_orders ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _migrate_stop_loss_book_state(conn: sqlite3.Connection) -> None:
    """Track how long book-based stop-loss conditions have been continuously true (cross-run)."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stop_loss_book_weak_state (
                platform TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                first_weak_ts REAL NOT NULL,
                entry_anchor REAL NOT NULL,
                PRIMARY KEY (platform, ticker, side)
            )
            """
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE stop_loss_book_weak_state ADD COLUMN entry_anchor REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def update_stop_loss_book_weak_state(
    *,
    platform: str,
    ticker: str,
    side: str,
    book_stop_condition: bool,
    persistence_seconds: float,
    entry_anchor: float | None = None,
    config: dict | None = None,
    now_ts: float | None = None,
) -> bool:
    """
    Persist book stop weakness across cron invocations.

    When book_stop_condition is False, clears stored state for this position.
    When True, returns True only after weakness has persisted for >= persistence_seconds.
    persistence_seconds <= 0: return True immediately (no persistence / legacy behavior).
    entry_anchor: position entry price; if it changes vs stored row, the timer resets.
    """
    now = float(now_ts if now_ts is not None else time.time())
    persist = float(persistence_seconds)
    conn = _get_conn(config)
    if not conn:
        return bool(book_stop_condition)

    plat, tk, sd = str(platform), str(ticker), str(side)
    ep = float(entry_anchor) if entry_anchor is not None else 0.0
    try:
        if not book_stop_condition:
            conn.execute(
                "DELETE FROM stop_loss_book_weak_state WHERE platform=? AND ticker=? AND side=?",
                (plat, tk, sd),
            )
            conn.commit()
            return False
        if persist <= 0:
            return True
        if ep <= 0:
            return False
        row = conn.execute(
            "SELECT first_weak_ts, entry_anchor FROM stop_loss_book_weak_state WHERE platform=? AND ticker=? AND side=?",
            (plat, tk, sd),
        ).fetchone()
        if row is not None:
            prev_e = row["entry_anchor"]
            if prev_e is None or abs(float(prev_e) - ep) > 1e-6:
                conn.execute(
                    "DELETE FROM stop_loss_book_weak_state WHERE platform=? AND ticker=? AND side=?",
                    (plat, tk, sd),
                )
                row = None
        if row is None:
            conn.execute(
                "INSERT INTO stop_loss_book_weak_state (platform, ticker, side, first_weak_ts, entry_anchor) VALUES (?,?,?,?,?)",
                (plat, tk, sd, now, ep),
            )
            conn.commit()
            return False
        first = float(row["first_weak_ts"])
        elapsed = max(0.0, now - first)
        return elapsed >= persist
    except Exception:
        conn.rollback()
        return bool(book_stop_condition) if persist <= 0 else False
    finally:
        conn.close()


def _migrate_markout_table(conn: sqlite3.Connection) -> None:
    """Create the markout_snapshots table for toxic flow detection."""
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS markout_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                ticker TEXT NOT NULL,
                fill_id TEXT,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                window_minutes INTEGER NOT NULL,
                price_after REAL NOT NULL,
                markout_bps REAL NOT NULL,
                fill_ts REAL NOT NULL,
                snapshot_ts REAL NOT NULL,
                UNIQUE(platform, fill_id, window_minutes)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_markout_ticker ON markout_snapshots(platform, ticker)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_markout_fill ON markout_snapshots(platform, fill_id)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def record_markout_snapshot(
    *,
    platform: str,
    ticker: str,
    fill_id: str | None,
    side: str,
    entry_price: float,
    window_minutes: int,
    price_after: float,
    markout_bps: float,
    fill_ts: float,
    config: dict | None = None,
) -> None:
    """Insert a markout observation. Called after each markout window elapses."""
    conn = _get_conn(config)
    if not conn:
        return
    try:
        conn.execute(
            """INSERT OR IGNORE INTO markout_snapshots
               (platform, ticker, fill_id, side, entry_price,
                window_minutes, price_after, markout_bps, fill_ts, snapshot_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (platform, ticker, fill_id or "", side, entry_price,
             window_minutes, price_after, markout_bps, fill_ts, time.time()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def get_markout_history(
    platform: str,
    ticker: str,
    window_minutes: int = 5,
    lookback_hours: float = 48.0,
    config: dict | None = None,
) -> list[float]:
    """Return recent markout_bps values for a ticker/window, newest first."""
    conn = _get_conn(config)
    if not conn:
        return []
    try:
        cutoff = time.time() - lookback_hours * 3600
        rows = conn.execute(
            """SELECT markout_bps FROM markout_snapshots
               WHERE platform=? AND ticker=? AND window_minutes=? AND snapshot_ts>=?
               ORDER BY snapshot_ts DESC""",
            (platform, ticker, window_minutes, cutoff),
        ).fetchall()
        return [float(r["markout_bps"]) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


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
    yes_ask: float | None = None,
    no_ask: float | None = None,
    yes_bid_size: float | None = None,
    no_bid_size: float | None = None,
    yes_ask_size: float | None = None,
    no_ask_size: float | None = None,
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
               (platform, ticker, title, yf_ticker, ts, yes_bid, no_bid, yes_ask, no_ask,
                yes_bid_size, no_bid_size, yes_ask_size, no_ask_size,
                hist_p, vol_p, rec, threshold, direction, edge)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                ticker,
                (title or "")[:200],
                yf_ticker or "",
                ts,
                yes_bid,
                no_bid,
                yes_ask,
                no_ask,
                yes_bid_size,
                no_bid_size,
                yes_ask_size,
                no_ask_size,
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


def get_limit_order_hints_by_order_id(
    platform: str,
    order_id: str,
    config: dict | None = None,
) -> dict[str, Any] | None:
    """
    Latest limit_orders row for (platform, order_id). Used when exchange fill payloads omit
    event ticker / YES-NO leg so reconciliation can still join to our placed-order record.
    """
    if not order_id:
        return None
    conn = _get_conn(config)
    if not conn:
        return None
    try:
        row = conn.execute(
            """SELECT ticker, side, hist_p, vol_p, yf_ticker, threshold, direction, rec,
                      theoretical_price, bid_at_place, spread_bps_at_place, fill_slippage_bps, limit_price
               FROM limit_orders
               WHERE platform=? AND order_id=?
               ORDER BY ts DESC LIMIT 1""",
            (platform, str(order_id)),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
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
    Record a limit order we placed (status=placed). Used for fill attribution and unfilled analysis.
    Returns SQLite row id or None if storage disabled / insert failed.
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
    *,
    ticker: str | None = None,
    side: str | None = None,
    limit_price: float | None = None,
) -> None:
    """
    Mark a limit order as cancelled. cancel_reason classifications:
    - 'no_longer_rec': model no longer recommends this trade (rec=SKIP)
    - 'want_better_price': model still recommends but wants to reprice at a better level
    - 'stale_ttl': order exceeded limit_order_ttl_minutes
    - 'bid_chase_refresh': passive bid chase — resting buy older than passive_bid_chase_refresh_minutes while still recommended (next cycle re-quotes at fresh bid)
    - 'regime_shift_guard': cancelled during momentum/volatility regime shift
    - 'expired_unfilled': auto-cancelled by platform at resolution (never filled)
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
    status_filter: str | Sequence[str] | None = None,
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
    theoretical_price: float | None = None,
    bid_at_place: float | None = None,
    spread_bps_at_place: float | None = None,
    fill_slippage_bps: float | None = None,
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
               (platform, ticker, order_id, side, hist_p, vol_p, entry_price, count, ts, yf_ticker, threshold, direction, rec,
                theoretical_price, bid_at_place, spread_bps_at_place, fill_slippage_bps)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                theoretical_price,
                bid_at_place,
                spread_bps_at_place,
                fill_slippage_bps,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def record_prediction_from_fill(
    *,
    platform: str,
    ticker: str,
    side: str,
    fill_id: str,
    order_id: str | None,
    entry_price: float,
    count: int,
    fill_ts: float | None = None,
    source: str = "fill_reconciliation",
    config: dict | None = None,
) -> dict[str, Any]:
    """
    Insert prediction only from confirmed fills (idempotent by platform+fill_id).
    Pull contextual model metadata from limit_orders when available.
    """
    if not fill_id:
        return {"status": "error", "id": None}
    qty = int(count or 0)
    if qty <= 0:
        return {"status": "error", "id": None}
    price = float(entry_price or 0.0)
    if price <= 0:
        return {"status": "error", "id": None}

    conn = _get_conn(config)
    if not conn:
        return {"status": "error", "id": None}
    resolved_for_log = ticker
    try:
        # Idempotency: skip if this fill was already recorded.
        existing = conn.execute(
            "SELECT id FROM predictions WHERE platform=? AND fill_id=? LIMIT 1",
            (platform, str(fill_id)),
        ).fetchone()
        if existing:
            return {"status": "duplicate", "id": int(existing["id"])}

        ctx = None
        if order_id:
            try:
                ctx = conn.execute(
                    """SELECT ticker, side, hist_p, vol_p, yf_ticker, threshold, direction, rec,
                              theoretical_price, bid_at_place, spread_bps_at_place, fill_slippage_bps
                       FROM limit_orders
                       WHERE platform=? AND order_id=?
                       ORDER BY ts DESC LIMIT 1""",
                    (platform, str(order_id)),
                ).fetchone()
            except Exception:
                ctx = None

        row_ticker = (ticker or "").strip()
        if not row_ticker and ctx and ctx["ticker"]:
            row_ticker = str(ctx["ticker"]).strip()
        if not row_ticker:
            return {"status": "error", "id": None}

        side_l = str(side or "").lower().strip()
        if side_l not in ("yes", "no") and ctx and ctx["side"]:
            cts = str(ctx["side"] or "").lower().strip()
            if cts in ("yes", "no"):
                side_l = cts
        if side_l not in ("yes", "no"):
            return {"status": "error", "id": None}

        resolved_for_log = row_ticker
        hist_p = float(ctx["hist_p"]) if ctx and ctx["hist_p"] is not None else 0.5
        vol_p = float(ctx["vol_p"]) if ctx and ctx["vol_p"] is not None else None
        yf_ticker = str(ctx["yf_ticker"] or "") if ctx else ""
        threshold = float(ctx["threshold"]) if ctx and ctx["threshold"] is not None else None
        direction = str(ctx["direction"] or "") if ctx else ""
        rec = str(ctx["rec"] or "") if ctx else ""
        theoretical_price = float(ctx["theoretical_price"]) if ctx and ctx["theoretical_price"] is not None else None
        bid_at_place = float(ctx["bid_at_place"]) if ctx and ctx["bid_at_place"] is not None else None
        spread_bps_at_place = float(ctx["spread_bps_at_place"]) if ctx and ctx["spread_bps_at_place"] is not None else None
        fill_slippage_bps = float(ctx["fill_slippage_bps"]) if ctx and ctx["fill_slippage_bps"] is not None else None

        ts_val = float(fill_ts if fill_ts is not None else time.time())
        cur = conn.execute(
            """INSERT INTO predictions
               (platform, ticker, order_id, fill_id, side, hist_p, vol_p, entry_price, count, ts, fill_ts, source,
                yf_ticker, threshold, direction, rec, theoretical_price, bid_at_place, spread_bps_at_place, fill_slippage_bps)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                row_ticker,
                order_id or "",
                str(fill_id),
                side_l,
                hist_p,
                vol_p,
                price,
                qty,
                ts_val,
                ts_val,
                source,
                yf_ticker,
                threshold,
                direction,
                rec,
                theoretical_price,
                bid_at_place,
                spread_bps_at_place,
                fill_slippage_bps,
            ),
        )
        if order_id:
            conn.execute(
                "UPDATE limit_orders SET status='filled' WHERE platform=? AND order_id=? AND status='placed'",
                (platform, str(order_id)),
            )
        conn.commit()
        return {"status": "inserted", "id": int(cur.lastrowid)}
    except Exception as exc:
        conn.rollback()
        _log.warning(
            "record_prediction_from_fill failed platform=%s fill_id=%s ticker=%s: %s",
            platform,
            fill_id,
            resolved_for_log,
            exc,
            exc_info=True,
        )
        return {"status": "error", "id": None}
    finally:
        conn.close()


def count_ticker_entries(ticker: str, config: dict | None = None) -> int:
    """Count non-cancelled prediction entries for a ticker (all time)."""
    conn = _get_conn(config)
    if not conn:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE ticker = ? AND (outcome IS NULL OR outcome NOT IN ('cancelled'))",
            (ticker,),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def get_ticker_entry_counts(config: dict | None = None) -> dict[str, int]:
    """Bulk-load non-cancelled entry counts per ticker for the cap check."""
    conn = _get_conn(config)
    if not conn:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, COUNT(*) as n FROM predictions "
            "WHERE outcome IS NULL OR outcome NOT IN ('cancelled') "
            "GROUP BY ticker"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def get_unresolved_contract_sums_by_platform_yf(config: dict | None = None) -> dict[tuple[str, str], int]:
    """
    Sum prediction `count` for open positions grouped by (platform, yf_ticker).
    Used to cap total directional exposure per underlying (e.g. BTC-USD) per venue.
    Rows with empty yf_ticker are excluded from aggregation.
    """
    conn = _get_conn(config)
    if not conn:
        return {}
    try:
        rows = conn.execute(
            """SELECT platform, yf_ticker, SUM(count) as total
               FROM predictions
               WHERE (outcome IS NULL OR outcome NOT IN ('cancelled'))
                 AND yf_ticker IS NOT NULL AND TRIM(yf_ticker) != ''
               GROUP BY platform, yf_ticker"""
        ).fetchall()
        out: dict[tuple[str, str], int] = {}
        for r in rows:
            plat = str(r["platform"] or "")
            yf = str(r["yf_ticker"] or "").strip()
            if plat and yf:
                out[(plat, yf)] = int(r["total"] or 0)
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def get_unresolved_predictions(config: dict | None = None) -> list[dict]:
    """Return predictions that don't have an outcome yet (market may still be open)."""
    conn = _get_conn(config)
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, platform, ticker, order_id, side, hist_p, vol_p, entry_price, count, ts, yf_ticker, threshold, direction FROM predictions WHERE outcome IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _fee_per_contract(price: float, maker_rate: float, min_fee_per_order: float = 0.01) -> float:
    """Same fee model used by strategy evaluation: rounded-up maker fee with floor."""
    try:
        p = float(price)
        r = float(maker_rate)
        floor_fee = max(0.0, float(min_fee_per_order))
    except (TypeError, ValueError):
        return 0.0
    fee = r * p * (1.0 - p)
    if fee <= 0:
        return 0.0
    rounded = math.ceil(fee * 100) / 100
    return max(rounded, floor_fee)


def mark_prediction_cancelled(
    *,
    platform: str,
    order_id: str,
    cancel_reason: str,
    config: dict | None = None,
) -> int:
    """
    Mark unresolved prediction rows tied to a canceled order as cancelled.
    Returns number of rows updated.
    """
    if not order_id:
        return 0
    conn = _get_conn(config)
    if not conn:
        return 0
    try:
        cur = conn.execute(
            """UPDATE predictions
               SET outcome='cancelled', pnl_cents=0, realized_pnl_cents=0, resolved_ts=?, exit_reason=?
               WHERE outcome IS NULL AND platform=? AND order_id=?""",
            (time.time(), cancel_reason, platform, str(order_id)),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def record_prediction_early_exit(
    *,
    platform: str,
    ticker: str,
    side: str,
    sell_price: float,
    count_sold: int,
    maker_fee_rate: float = 0.0,
    min_fee_per_order: float = 0.01,
    exit_reason: str = "early_sell",
    config: dict | None = None,
) -> dict[str, int]:
    """
    Realize PnL for early sells using FIFO unresolved prediction lots.
    Includes entry and exit fee estimates.
    Returns {"closed_count": int, "pnl_cents": int}.
    """
    remaining = max(0, int(count_sold))
    if remaining <= 0:
        return {"closed_count": 0, "pnl_cents": 0}
    conn = _get_conn(config)
    if not conn:
        return {"closed_count": 0, "pnl_cents": 0}
    closed_total = 0
    pnl_total = 0
    now_ts = time.time()
    try:
        lots = conn.execute(
            """SELECT * FROM predictions
               WHERE outcome IS NULL AND platform=? AND ticker=? AND side=?
               ORDER BY ts ASC, id ASC""",
            (platform, ticker, side),
        ).fetchall()
        pred_cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()]
        for lot in lots:
            if remaining <= 0:
                break
            lot_count = int(lot["count"] or 0)
            if lot_count <= 0:
                continue
            qty = min(remaining, lot_count)
            entry = float(lot["entry_price"] or 0.0)
            entry_fee = _fee_per_contract(entry, maker_fee_rate, min_fee_per_order)
            exit_fee = _fee_per_contract(sell_price, maker_fee_rate, min_fee_per_order)
            pnl_cents = int(round(((float(sell_price) - entry) - entry_fee - exit_fee) * 100.0 * qty))

            if qty == lot_count:
                conn.execute(
                    """UPDATE predictions
                       SET outcome='sold', pnl_cents=?, realized_pnl_cents=?, resolved_ts=?, exit_price=?, exit_count=?, exit_reason=?
                       WHERE id=?""",
                    (pnl_cents, pnl_cents, now_ts, float(sell_price), qty, exit_reason, int(lot["id"])),
                )
            else:
                conn.execute(
                    "UPDATE predictions SET count=? WHERE id=?",
                    (lot_count - qty, int(lot["id"])),
                )
                clone = {k: lot[k] for k in lot.keys() if k in pred_cols and k != "id"}
                clone["count"] = qty
                clone["outcome"] = "sold"
                clone["pnl_cents"] = pnl_cents
                clone["realized_pnl_cents"] = pnl_cents
                clone["resolved_ts"] = now_ts
                if "exit_price" in pred_cols:
                    clone["exit_price"] = float(sell_price)
                if "exit_count" in pred_cols:
                    clone["exit_count"] = qty
                if "exit_reason" in pred_cols:
                    clone["exit_reason"] = exit_reason
                cols = list(clone.keys())
                vals = [clone[c] for c in cols]
                conn.execute(
                    f"INSERT INTO predictions ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})",
                    vals,
                )

            remaining -= qty
            closed_total += qty
            pnl_total += pnl_cents

        conn.commit()
        return {"closed_count": closed_total, "pnl_cents": pnl_total}
    except Exception:
        conn.rollback()
        return {"closed_count": 0, "pnl_cents": 0}
    finally:
        conn.close()


def record_signal(platform: str, ticker: str, side: str, hist_p: float, theoretical_price: float, edge: float, cfg: dict) -> None:
    """Record a recommendation signal (Opportunity-level accounting)."""
    db_path = _get_db_path(cfg)
    ts = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO signals (platform, ticker, side, hist_p, theoretical_price, edge, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (platform, ticker, side, hist_p, theoretical_price, edge, ts))
        conn.commit()

def resolve_signal_outcomes(ticker: str, yes_settled: bool, cfg: dict) -> None:
    """Update unresolved signals for a ticker with the settled outcome."""
    db_path = _get_db_path(cfg)
    resolved_ts = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            UPDATE signals
            SET outcome = CASE 
                WHEN (side = 'yes' AND ? = 1) OR (side = 'no' AND ? = 0) THEN 'win'
                ELSE 'lose'
            END,
            resolved_ts = ?
            WHERE ticker = ? AND outcome IS NULL
        """, (1 if yes_settled else 0, 1 if yes_settled else 0, resolved_ts, ticker))
        conn.commit()
def record_prediction_outcome(
    prediction_id: int,
    yes_settled: bool,
    pnl_cents: int,
    config: dict | None = None,
    *,
    min_price_after_buy: float | None = None,
    max_price_after_buy: float | None = None,
    resolution_price: float | None = None,
) -> bool:
    """Mark a prediction as resolved. outcome: win if (side==yes and yes_settled) or (side==no and not yes_settled).
    Optionally store min/max contract price after we bought (from market_snapshots) for calibration.
    resolution_price: the actual underlying asset price at settlement time (for accuracy tracking)."""
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
        now_ts = time.time()
        set_parts = [
            "outcome=?", "pnl_cents=?", "realized_pnl_cents=?", "resolved_ts=?",
        ]
        params: list[Any] = [outcome, pnl_cents, pnl_cents, now_ts]
        if min_price_after_buy is not None and max_price_after_buy is not None:
            set_parts += ["min_price_after_buy=?", "max_price_after_buy=?"]
            params += [min_price_after_buy, max_price_after_buy]
        if resolution_price is not None:
            set_parts.append("resolution_price=?")
            params.append(resolution_price)
        params.append(prediction_id)
        conn.execute(
            f"UPDATE predictions SET {', '.join(set_parts)} WHERE id=?",
            params,
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
        select_cols = "outcome, COALESCE(realized_pnl_cents, pnl_cents) AS pnl_cents, hist_p, direction, side, entry_price"
        if has_pab_cols:
            select_cols += ", min_price_after_buy, max_price_after_buy"
        rows = conn.execute(
            f"""SELECT {select_cols} FROM predictions WHERE outcome IN ('win','lose','sold')"""
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
        loses = sum(1 for r in rows if r["outcome"] in ("lose", "sold"))
        total_pnl = sum(r["pnl_cents"] or 0 for r in rows)
        loss_weight = float((config or {}).get("strategy", {}).get("calibration_loss_weight", 1.0))
        # Bucket by hist_p (0.5-0.6, 0.6-0.7, ...); weight losers higher for win_rate
        # Treat "sold" outcomes as losses for calibration purposes
        buckets: dict[tuple[float, float], list[str]] = {}
        for r in rows:
            h = r["hist_p"] or 0.5
            bucket = (round(h * 10) / 10, round(h * 10) / 10 + 0.1)
            if bucket not in buckets:
                buckets[bucket] = []
            outcome = r["outcome"] if r["outcome"] != "sold" else "lose"
            buckets[bucket].append(outcome)
        bucket_stats = []
        for (lo, hi), outcomes in sorted(buckets.items()):
            ww = sum(1 for o in outcomes if o == "win")
            wl = sum(loss_weight for o in outcomes if o == "lose")
            wr = ww / (ww + wl) if (ww + wl) > 0 else 0
            bucket_stats.append({"hist_p_range": f"{lo:.1f}-{hi:.1f}", "lo": lo, "hi": hi, "count": len(outcomes), "win_rate": wr})
        # Win rate by (direction, side); weight losers higher; "sold" counts as lose
        by_ds: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = (r["direction"] or "").strip().lower() or "unknown"
            s = (r["side"] or "").strip().lower() or "unknown"
            key = f"{d}+{s}"
            if key not in by_ds:
                by_ds[key] = {"wins": 0, "loses": 0, "entry_sum": 0.0, "entry_n": 0}
            if r["outcome"] == "win":
                by_ds[key]["wins"] += 1
            else:
                by_ds[key]["loses"] += 1
            try:
                ep = float(r["entry_price"] or 0)
                if ep > 0:
                    by_ds[key]["entry_sum"] += ep
                    by_ds[key]["entry_n"] += 1
            except (TypeError, ValueError):
                pass
        by_direction_side = {}
        for k, v in by_ds.items():
            ww, wl = v["wins"], v["loses"] * loss_weight
            wr = ww / (ww + wl) if (ww + wl) > 0 else 0
            en = int(v.get("entry_n", 0) or 0)
            avg_e = (float(v["entry_sum"]) / en) if en > 0 else None
            by_direction_side[k] = {
                "count": v["wins"] + v["loses"],
                "win_rate": wr,
                "avg_entry_price": avg_e,
            }
        # Price-after-buy: how much lower/higher did contract go after we bought "low"
        rows_with_price = []
        for r in rows:
            entry_price = r["entry_price"] if "entry_price" in r.keys() else None
            min_after = r["min_price_after_buy"] if "min_price_after_buy" in r.keys() else None
            max_after = r["max_price_after_buy"] if "max_price_after_buy" in r.keys() else None
            if entry_price is not None and min_after is not None and max_after is not None:
                rows_with_price.append(
                    {
                        "entry_price": float(entry_price),
                        "min_price_after_buy": float(min_after),
                        "max_price_after_buy": float(max_after),
                    }
                )
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


def get_bet_accuracy_stats(
    config: dict | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, Any]:
    """
    Compute bet-vs-resolution accuracy from resolved predictions that have
    threshold, direction, and resolution_price.  Returns aggregate distance
    metrics that show how close the bot's bets were to the actual outcome.

    Distance = (resolution_price - threshold) / threshold, signed by direction:
      - For "above" bets: positive distance means asset was above threshold (YES won)
      - For "below" bets: sign is flipped so positive = bet-favorable

    Returns dict with per-trade details and summary stats (mean, median, stddev,
    pct_correct_side, avg_distance_when_wrong, avg_distance_when_right).
    """
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False, "count": 0}
    try:
        q = """SELECT ticker, side, outcome, threshold, direction, resolution_price,
                      hist_p, entry_price, COALESCE(realized_pnl_cents, pnl_cents) AS pnl_cents,
                      resolved_ts, yf_ticker
               FROM predictions
               WHERE outcome IN ('win','lose')
                 AND resolution_price IS NOT NULL
                 AND threshold IS NOT NULL
                 AND threshold > 0"""
        params: list[Any] = []
        if start_ts is not None:
            q += " AND COALESCE(resolved_ts, ts) >= ?"
            params.append(float(start_ts))
        if end_ts is not None:
            q += " AND COALESCE(resolved_ts, ts) <= ?"
            params.append(float(end_ts))
        q += " ORDER BY resolved_ts ASC, ts ASC"
        rows = conn.execute(q, params).fetchall()
        if not rows:
            return {"enabled": True, "count": 0, "trades": [], "summary": {}}

        trades: list[dict[str, Any]] = []
        distances: list[float] = []
        correct_distances: list[float] = []
        wrong_distances: list[float] = []
        for r in rows:
            threshold = float(r["threshold"])
            res_price = float(r["resolution_price"])
            direction = (r["direction"] or "").lower().strip()
            raw_dist_pct = ((res_price - threshold) / threshold) * 100.0
            if direction == "below":
                raw_dist_pct = -raw_dist_pct
            side = (r["side"] or "").lower()
            outcome = r["outcome"]
            won = outcome == "win"
            trade = {
                "ticker": r["ticker"],
                "yf_ticker": r["yf_ticker"],
                "side": side,
                "direction": direction,
                "threshold": threshold,
                "resolution_price": res_price,
                "distance_pct": round(raw_dist_pct, 4),
                "abs_distance_pct": round(abs(raw_dist_pct), 4),
                "hist_p": r["hist_p"],
                "entry_price": r["entry_price"],
                "outcome": outcome,
                "pnl_cents": r["pnl_cents"],
                "resolved_ts": r["resolved_ts"],
            }
            trades.append(trade)
            distances.append(raw_dist_pct)
            if won:
                correct_distances.append(abs(raw_dist_pct))
            else:
                wrong_distances.append(abs(raw_dist_pct))

        abs_dists = [abs(d) for d in distances]
        sorted_abs = sorted(abs_dists)
        n = len(sorted_abs)
        median_abs = sorted_abs[n // 2] if n % 2 == 1 else (sorted_abs[n // 2 - 1] + sorted_abs[n // 2]) / 2.0
        mean_abs = sum(abs_dists) / n
        mean_signed = sum(distances) / n
        variance = sum((d - mean_signed) ** 2 for d in distances) / max(1, n - 1)
        stddev = variance ** 0.5

        summary = {
            "count": n,
            "mean_abs_distance_pct": round(mean_abs, 4),
            "median_abs_distance_pct": round(median_abs, 4),
            "mean_signed_distance_pct": round(mean_signed, 4),
            "stddev_distance_pct": round(stddev, 4),
            "pct_correct_side": round(len(correct_distances) / n * 100.0, 2) if n > 0 else None,
            "avg_distance_when_correct_pct": round(sum(correct_distances) / len(correct_distances), 4) if correct_distances else None,
            "avg_distance_when_wrong_pct": round(sum(wrong_distances) / len(wrong_distances), 4) if wrong_distances else None,
        }
        return {"enabled": True, "count": n, "trades": trades, "summary": summary}
    except Exception:
        return {"enabled": False, "count": 0}
    finally:
        conn.close()


def get_fill_attribution_stats(
    config: dict | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, Any]:
    """Summarize actual vs theoretical fill quality from predictions."""
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False}
    try:
        q = """
            SELECT entry_price, theoretical_price, bid_at_place, spread_bps_at_place, fill_slippage_bps
            FROM predictions
            WHERE entry_price IS NOT NULL
        """
        params: list[Any] = []
        if start_ts is not None:
            q += " AND ts >= ?"
            params.append(start_ts)
        if end_ts is not None:
            q += " AND ts <= ?"
            params.append(end_ts)
        rows = conn.execute(q, params).fetchall()
        if not rows:
            return {"enabled": True, "count": 0}
        slippages = [float(r["fill_slippage_bps"]) for r in rows if r["fill_slippage_bps"] is not None]
        spreads = [float(r["spread_bps_at_place"]) for r in rows if r["spread_bps_at_place"] is not None]
        with_theoretical = sum(1 for r in rows if r["theoretical_price"] is not None)
        return {
            "enabled": True,
            "count": len(rows),
            "with_theoretical": with_theoretical,
            "avg_fill_slippage_bps": (sum(slippages) / len(slippages)) if slippages else None,
            "avg_spread_bps_at_place": (sum(spreads) / len(spreads)) if spreads else None,
            "pct_positive_slippage": (
                (sum(1 for s in slippages if s > 0) / len(slippages) * 100.0) if slippages else None
            ),
        }
    except Exception:
        return {"enabled": False}
    finally:
        conn.close()


def get_limit_order_fill_rate_stats(
    config: dict | None = None,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """
    Placed vs filled limit orders from limit_orders (terminal statuses: filled, cancelled).
    fill_rate = filled / (filled + cancelled); open placed-only orders excluded from denominator.
    """
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False}
    try:
        since = time.time() - max(1, int(days)) * 86400
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) AS n_filled,
              SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS n_cancelled,
              SUM(CASE WHEN status = 'placed' THEN 1 ELSE 0 END) AS n_open
            FROM limit_orders
            WHERE ts >= ?
            """,
            (since,),
        ).fetchone()
        if not row:
            return {"enabled": True, "days": days, "n_filled": 0, "n_cancelled": 0, "n_open": 0, "fill_rate": None}
        nf = int(row["n_filled"] or 0)
        nc = int(row["n_cancelled"] or 0)
        no = int(row["n_open"] or 0)
        denom = nf + nc
        rate = (nf / float(denom)) if denom > 0 else None
        return {
            "enabled": True,
            "days": days,
            "n_filled": nf,
            "n_cancelled": nc,
            "n_open": no,
            "fill_rate": rate,
        }
    except Exception:
        return {"enabled": False}
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
        inner = (
            "SELECT ticker, platform, title, yf_ticker, ts, yes_bid, no_bid, yes_ask, no_ask, "
            "yes_bid_size, no_bid_size, yes_ask_size, no_ask_size, "
            "hist_p, vol_p, rec, threshold, direction, edge FROM market_snapshots WHERE 1=1"
        )
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


def get_realized_pnl_stats(
    *,
    since_ts: float | None = None,
    config: dict | None = None,
) -> dict[str, float | int]:
    """
    Aggregate realized prediction outcomes over a time window.
    Returns total_pnl_cents and counts for wins/losses/sold/resolved.
    """
    conn = _get_conn(config)
    if not conn:
        return {"resolved_count": 0, "wins": 0, "losses": 0, "sold": 0, "total_pnl_cents": 0}
    try:
        q = (
            "SELECT "
            "COUNT(*) AS resolved_count, "
            "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN outcome='lose' THEN 1 ELSE 0 END) AS losses, "
            "SUM(CASE WHEN outcome='sold' THEN 1 ELSE 0 END) AS sold, "
            "SUM(COALESCE(realized_pnl_cents, pnl_cents, 0)) AS total_pnl_cents "
            "FROM predictions WHERE outcome IN ('win','lose','sold')"
        )
        params: list[Any] = []
        if since_ts is not None:
            q += " AND COALESCE(resolved_ts, ts) >= ?"
            params.append(float(since_ts))
        row = conn.execute(q, params).fetchone()
        if not row:
            return {"resolved_count": 0, "wins": 0, "losses": 0, "sold": 0, "total_pnl_cents": 0}
        return {
            "resolved_count": int(row["resolved_count"] or 0),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "sold": int(row["sold"] or 0),
            "total_pnl_cents": int(row["total_pnl_cents"] or 0),
        }
    except Exception:
        return {"resolved_count": 0, "wins": 0, "losses": 0, "sold": 0, "total_pnl_cents": 0}
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
        q = "SELECT * FROM predictions WHERE outcome IN ('win','lose','sold')" if resolved_only else "SELECT * FROM predictions"
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


def expire_unfilled_orders(
    ticker: str,
    platform: str | None = None,
    config: dict | None = None,
) -> int:
    """
    When a market resolves, mark any still-resting limit orders for that ticker
    as expired_unfilled. These were auto-cancelled by the platform at resolution.
    Returns number of rows updated.
    """
    conn = _get_conn(config)
    if not conn:
        return 0
    now = time.time()
    try:
        if platform:
            cur = conn.execute(
                "UPDATE limit_orders SET status='cancelled', cancel_reason='expired_unfilled', cancel_ts=? WHERE ticker=? AND platform=? AND status='placed'",
                (now, ticker, platform),
            )
        else:
            cur = conn.execute(
                "UPDATE limit_orders SET status='cancelled', cancel_reason='expired_unfilled', cancel_ts=? WHERE ticker=? AND status='placed'",
                (now, ticker),
            )
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def record_position_evaluation(
    *,
    platform: str,
    ticker: str,
    side: str,
    action: str,
    hist_p: float | None = None,
    vol_p: float | None = None,
    p_lose: float | None = None,
    yes_bid: float | None = None,
    no_bid: float | None = None,
    threshold: float | None = None,
    direction: str | None = None,
    yf_ticker: str | None = None,
    rec: str | None = None,
    position_count: int | None = None,
    entry_price: float | None = None,
    current_price: float | None = None,
    sell_reason: str | None = None,
    config: dict | None = None,
) -> int | None:
    """
    Record a position evaluation (KEEP or SELL decision).
    Rate-limited: at most one row per (platform, ticker, side) per hour.
    Returns row id or None.
    """
    conn = _get_conn(config)
    if not conn:
        return None
    ts = time.time()
    try:
        last = conn.execute(
            "SELECT ts FROM position_evaluations WHERE platform=? AND ticker=? AND side=? ORDER BY ts DESC LIMIT 1",
            (platform, ticker, side),
        ).fetchone()
        if last and (ts - float(last["ts"])) < 3600:
            return None
        cur = conn.execute(
            """INSERT INTO position_evaluations
               (platform, ticker, side, action, sell_reason, hist_p, vol_p, p_lose, yes_bid, no_bid,
                threshold, direction, yf_ticker, rec, position_count, entry_price, current_price, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform, ticker, side, action, sell_reason,
                hist_p, vol_p, p_lose, yes_bid, no_bid,
                threshold, direction or "", yf_ticker or "", rec or "",
                position_count, entry_price, current_price, ts,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def resolve_position_evaluations(
    ticker: str,
    yes_settled: bool,
    config: dict | None = None,
    *,
    maker_fee_rate: float = 0.0,
    min_fee_per_order: float = 0.01,
) -> int:
    """
    When a market resolves, update all unresolved position evaluations for that ticker.
    Returns number of rows updated.
    """
    conn = _get_conn(config)
    if not conn:
        return 0
    now = time.time()
    try:
        rows = conn.execute(
            "SELECT id, side, entry_price, position_count FROM position_evaluations WHERE ticker=? AND outcome IS NULL",
            (ticker,),
        ).fetchall()
        updated = 0
        for r in rows:
            side = (r["side"] or "").lower()
            won = (side == "yes" and yes_settled) or (side == "no" and not yes_settled)
            outcome = "win" if won else "lose"
            entry = float(r["entry_price"] or 0)
            cnt = int(r["position_count"] or 0)
            if entry > 0 and cnt > 0:
                fee_cents = int(round(_fee_per_contract(entry, maker_fee_rate, min_fee_per_order) * 100 * cnt))
                gross = int(round(((1.0 - entry) * 100 * cnt) if won else (-entry * 100 * cnt)))
                pnl = gross - fee_cents
            else:
                pnl = 0
            conn.execute(
                "UPDATE position_evaluations SET outcome=?, yes_settled=?, pnl_cents=?, resolved_ts=? WHERE id=?",
                (outcome, 1 if yes_settled else 0, pnl, now, int(r["id"])),
            )
            updated += 1
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def get_position_evaluation_stats(
    config: dict | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """
    Analyze position evaluation outcomes: how well did KEEP vs SELL decisions perform?
    For KEEP decisions, uses the last evaluation per (platform, ticker, side) before resolution.
    """
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False}
    since = time.time() - (days * 86400)
    try:
        rows = conn.execute(
            """SELECT pe.*
               FROM position_evaluations pe
               INNER JOIN (
                   SELECT platform, ticker, side, MAX(ts) AS max_ts
                   FROM position_evaluations
                   WHERE ts >= ? AND outcome IS NOT NULL
                   GROUP BY platform, ticker, side
               ) latest ON pe.platform = latest.platform AND pe.ticker = latest.ticker
                       AND pe.side = latest.side AND pe.ts = latest.max_ts
               WHERE pe.outcome IS NOT NULL""",
            (since,),
        ).fetchall()
        if not rows:
            return {"enabled": True, "total": 0}
        keep_rows = [r for r in rows if r["action"] == "keep"]
        sell_rows = [r for r in rows if r["action"] == "sell"]
        keep_wins = sum(1 for r in keep_rows if r["outcome"] == "win")
        keep_losses = sum(1 for r in keep_rows if r["outcome"] == "lose")
        sell_wins = sum(1 for r in sell_rows if r["outcome"] == "win")
        sell_losses = sum(1 for r in sell_rows if r["outcome"] == "lose")
        keep_wr = keep_wins / (keep_wins + keep_losses) if (keep_wins + keep_losses) > 0 else None
        sell_wr = sell_wins / (sell_wins + sell_losses) if (sell_wins + sell_losses) > 0 else None
        keep_pnl = sum(int(r["pnl_cents"] or 0) for r in keep_rows)
        sell_pnl = sum(int(r["pnl_cents"] or 0) for r in sell_rows)
        buckets: dict[str, dict[str, int]] = {}
        for r in keep_rows:
            hp = float(r["hist_p"] or 0.5)
            key = f"{round(hp * 10) / 10:.1f}-{round(hp * 10) / 10 + 0.1:.1f}"
            if key not in buckets:
                buckets[key] = {"wins": 0, "losses": 0}
            if r["outcome"] == "win":
                buckets[key]["wins"] += 1
            else:
                buckets[key]["losses"] += 1
        bucket_stats = []
        for k in sorted(buckets):
            w, l = buckets[k]["wins"], buckets[k]["losses"]
            bucket_stats.append({"hist_p_range": k, "count": w + l, "win_rate": w / (w + l) if (w + l) > 0 else None})
        keep_preds = [float(r["hist_p"] or 0.5) for r in keep_rows]
        keep_outs = [1.0 if r["outcome"] == "win" else 0.0 for r in keep_rows]
        return {
            "enabled": True,
            "total": len(rows),
            "keep": {
                "count": len(keep_rows),
                "wins": keep_wins,
                "losses": keep_losses,
                "win_rate": keep_wr,
                "pnl_cents": keep_pnl,
                "predictions": keep_preds,
                "outcomes": keep_outs,
            },
            "sell": {"count": len(sell_rows), "wins": sell_wins, "losses": sell_losses, "win_rate": sell_wr, "pnl_cents": sell_pnl},
            "keep_buckets": bucket_stats,
        }
    except Exception:
        return {"enabled": False}
    finally:
        conn.close()


def get_position_evaluation_interval_stats(
    config: dict | None = None,
    *,
    start_ts: float,
    end_ts: float,
) -> dict[str, Any]:
    """
    Aggregate resolved position evaluations with resolved_ts in (start_ts, end_ts].
    Matches KEEP/SELL outcome semantics used by get_position_evaluation_stats (combined pool).

    Returns keys: n, wins, losses, win_rate (0..1 or None if n==0), pnl_cents, enabled.
    """
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False, "n": 0, "wins": 0, "losses": 0, "win_rate": None, "pnl_cents": 0}
    try:
        rows = conn.execute(
            """
            SELECT outcome, pnl_cents
            FROM position_evaluations
            WHERE outcome IS NOT NULL
              AND resolved_ts IS NOT NULL
              AND resolved_ts > ?
              AND resolved_ts <= ?
            """,
            (float(start_ts), float(end_ts)),
        ).fetchall()
        wins = sum(1 for r in rows if (r["outcome"] or "").lower() == "win")
        losses = sum(1 for r in rows if (r["outcome"] or "").lower() == "lose")
        n = len(rows)
        pnl = sum(int(r["pnl_cents"] or 0) for r in rows)
        wr = (wins / n) if n else None
        return {
            "enabled": True,
            "n": n,
            "wins": wins,
            "losses": losses,
            "win_rate": wr,
            "pnl_cents": pnl,
        }
    except Exception:
        return {"enabled": False, "n": 0, "wins": 0, "losses": 0, "win_rate": None, "pnl_cents": 0}
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
        pred_resolved = conn.execute("SELECT COUNT(*) FROM predictions WHERE outcome IN ('win','lose','sold')").fetchone()[0]
        try:
            pos_eval_count = conn.execute("SELECT COUNT(*) FROM position_evaluations").fetchone()[0]
            pos_eval_resolved = conn.execute("SELECT COUNT(*) FROM position_evaluations WHERE outcome IS NOT NULL").fetchone()[0]
        except Exception:
            pos_eval_count = pos_eval_resolved = 0
        return {
            "enabled": True,
            "data_dir": str(_get_data_dir(config)),
            "yf_cache_entries": cache_count,
            "market_snapshots": snap_count,
            "resolved_outcomes": resolved_count,
            "predictions": pred_count,
            "predictions_resolved": pred_resolved,
            "position_evaluations": pos_eval_count,
            "position_evaluations_resolved": pos_eval_resolved,
        }
    except Exception:
        return {"enabled": False}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Adaptive learning: recency-weighted calibration, loss streak detection,
# per-series recent performance.
# ---------------------------------------------------------------------------


def _extract_series_prefix(ticker: str) -> str:
    """Extract series prefix from a ticker like KXBTCD-25MAR19-B81250 -> KXBTCD."""
    if not ticker:
        return ""
    parts = ticker.split("-")
    return parts[0] if parts else ticker


def get_seconds_since_last_resolved_trade(*, config: dict | None = None) -> float | None:
    """
    Seconds since the most recent resolved prediction (win/lose/sold).
    None if there is no history. Used to decay adaptive edge bumps during quiet periods.
    """
    conn = _get_conn(config)
    if not conn:
        return None
    try:
        row = conn.execute(
            """
            SELECT MAX(COALESCE(resolved_ts, ts)) AS mx
            FROM predictions
            WHERE outcome IN ('win', 'lose', 'sold')
            """
        ).fetchone()
        if not row or row["mx"] is None:
            return None
        return max(0.0, time.time() - float(row["mx"]))
    except Exception:
        return None
    finally:
        conn.close()


def get_recent_performance(
    *,
    lookback_hours: float = 6.0,
    platform: str | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """
    Win rate and PnL for trades resolved in the last *lookback_hours*.
    Includes win/lose/sold so early-exit losses aren't hidden.
    """
    conn = _get_conn(config)
    if not conn:
        return {"total": 0}
    cutoff = time.time() - lookback_hours * 3600
    try:
        q = """
            SELECT outcome, COALESCE(realized_pnl_cents, pnl_cents, 0) AS pnl
            FROM predictions
            WHERE outcome IN ('win', 'lose', 'sold')
              AND COALESCE(resolved_ts, ts) >= ?
        """
        params: list[Any] = [cutoff]
        if platform:
            q += " AND platform = ?"
            params.append(platform)
        rows = conn.execute(q, params).fetchall()
        if not rows:
            return {"total": 0, "win_rate": None, "wins": 0, "losses": 0, "sold": 0, "pnl_cents": 0}
        wins = sum(1 for r in rows if r["outcome"] == "win")
        losses = sum(1 for r in rows if r["outcome"] == "lose")
        sold = sum(1 for r in rows if r["outcome"] == "sold")
        pnl = sum(int(r["pnl"]) for r in rows)
        total = len(rows)
        avg_pnl = pnl / total if total > 0 else 0
        
        # Standard deviation for confidence weighting
        std_pnl = 0.0
        if total > 1:
            variance = sum((int(r["pnl"]) - avg_pnl) ** 2 for r in rows) / (total - 1)
            std_pnl = math.sqrt(variance)

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "sold": sold,
            "win_rate": wins / total if total > 0 else None,
            "pnl_cents": pnl,
            "avg_pnl_cents": avg_pnl,
            "std_pnl_cents": std_pnl,
        }
    except Exception:
        return {"total": 0}
    finally:
        conn.close()


def get_recent_performance_by_series(
    *,
    lookback_hours: float = 12.0,
    config: dict | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Per-series (ticker prefix like KXBTCD, KXETHD) recent performance.
    Returns {series: {win_rate, wins, losses, total, pnl_cents, consecutive_losses}}.
    """
    conn = _get_conn(config)
    if not conn:
        return {}
    cutoff = time.time() - lookback_hours * 3600
    try:
        rows = conn.execute(
            """SELECT ticker, outcome, COALESCE(realized_pnl_cents, pnl_cents, 0) AS pnl,
                      COALESCE(resolved_ts, ts) AS rts
               FROM predictions
               WHERE outcome IN ('win', 'lose', 'sold')
                 AND COALESCE(resolved_ts, ts) >= ?
               ORDER BY rts DESC""",
            (cutoff,),
        ).fetchall()
        if not rows:
            return {}
        from collections import defaultdict
        by_series: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            ticker = r["ticker"] or ""
            series = _extract_series_prefix(ticker)
            rts = float(r["rts"] or 0)
            by_series[series].append(
                {"outcome": r["outcome"], "pnl": int(r["pnl"]), "rts": rts}
            )
        result = {}
        for series, trades in by_series.items():
            trades.sort(key=lambda t: t["rts"], reverse=True)
            wins = sum(1 for t in trades if t["outcome"] == "win")
            losses = sum(1 for t in trades if t["outcome"] in ("lose", "sold"))
            pnl = sum(t["pnl"] for t in trades)
            consec_losses = 0
            for t in trades:
                if t["outcome"] in ("lose", "sold"):
                    consec_losses += 1
                else:
                    break
            result[series] = {
                "total": len(trades),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(trades) if trades else None,
                "pnl_cents": pnl,
                "consecutive_losses": consec_losses,
            }
        return result
    except Exception:
        return {}
    finally:
        conn.close()


def get_loss_streak(
    *,
    platform: str | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """
    Count the current consecutive loss streak (most recent resolved trades).
    Includes 'sold' as a loss.
    """
    conn = _get_conn(config)
    if not conn:
        return {"streak": 0}
    try:
        q = """
            SELECT outcome, COALESCE(realized_pnl_cents, pnl_cents, 0) AS pnl
            FROM predictions
            WHERE outcome IN ('win', 'lose', 'sold')
        """
        params: list[Any] = []
        if platform:
            q += " AND platform = ?"
            params.append(platform)
        q += " ORDER BY COALESCE(resolved_ts, ts) DESC LIMIT 50"
        rows = conn.execute(q, params).fetchall()
        if not rows:
            return {"streak": 0, "last_n_outcomes": [], "pnl_last_streak": 0}
        streak = 0
        pnl_streak = 0
        outcomes = []
        for r in rows:
            outcomes.append(r["outcome"])
            if r["outcome"] in ("lose", "sold"):
                streak += 1
                pnl_streak += int(r["pnl"])
            else:
                break
        return {
            "streak": streak,
            "last_n_outcomes": outcomes[:20],
            "pnl_last_streak": pnl_streak,
        }
    except Exception:
        return {"streak": 0}
    finally:
        conn.close()


def get_ewma_win_rate(
    *,
    halflife_trades: int = 30,
    platform: str | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """
    Exponentially-weighted moving average win rate. Recent trades weigh more.
    halflife_trades: weight of a trade N positions ago is 0.5^(N/halflife).
    """
    conn = _get_conn(config)
    if not conn:
        return {"ewma_win_rate": None, "n_trades": 0}
    try:
        q = """
            SELECT outcome
            FROM predictions
            WHERE outcome IN ('win', 'lose', 'sold')
        """
        params: list[Any] = []
        if platform:
            q += " AND platform = ?"
            params.append(platform)
        q += " ORDER BY COALESCE(resolved_ts, ts) DESC LIMIT 200"
        rows = conn.execute(q, params).fetchall()
        if not rows:
            return {"ewma_win_rate": None, "n_trades": 0}
        alpha = 1.0 - 0.5 ** (1.0 / max(1, halflife_trades))
        ewma = 0.5
        for r in reversed(rows):
            val = 1.0 if r["outcome"] == "win" else 0.0
            ewma = alpha * val + (1.0 - alpha) * ewma
        return {
            "ewma_win_rate": ewma,
            "n_trades": len(rows),
            "effective_n": min(len(rows), halflife_trades * 3),
        }
    except Exception:
        return {"ewma_win_rate": None, "n_trades": 0}
    finally:
        conn.close()


def get_recent_stop_loss_cooldowns(hours: float = 4.0, config: dict | None = None) -> set[tuple[str, str, str]]:
    """
    Get (platform, ticker, side) of trades that hit STOP_LOSS within the last N hours.
    Used to prevent immediate re-entry into 'falling knife' markets.
    """
    conn = _get_conn(config)
    if not conn:
        return set()
    try:
        ts_cutoff = time.time() - (float(hours) * 3600)
        rows = conn.execute(
            """SELECT platform, ticker, side FROM predictions
               WHERE outcome='sold' AND exit_reason='STOP_LOSS' AND resolved_ts > ?""",
            (ts_cutoff,),
        ).fetchall()
        return {(r["platform"], r["ticker"], r["side"]) for r in rows}
    except Exception:
        return set()
    finally:
        conn.close()

def get_filled_predictions_for_markout(
    lookback_hours: float = 24.0,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Return all predictions with fills in the last N hours that might need markouts."""
    conn = _get_conn(config)
    if not conn: return []
    try:
        cutoff = time.time() - lookback_hours * 3600
        rows = conn.execute(
            """SELECT platform, ticker, fill_id, side, entry_price, fill_ts 
               FROM predictions 
               WHERE fill_ts >= ? AND fill_id IS NOT NULL 
               ORDER BY fill_ts DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()

def get_nearest_market_snapshot(
    platform: str,
    ticker: str,
    target_ts: float,
    tolerance_seconds: float = 300,
    config: dict | None = None,
) -> dict[str, Any] | None:
    """Find the market snapshot for a ticker closest to target_ts within tolerance."""
    conn = _get_conn(config)
    if not conn: return None
    try:
        # Find the snapshot where ABS(ts - target_ts) is minimized
        row = conn.execute(
            """SELECT yes_bid, no_bid, ts FROM market_snapshots
               WHERE platform=? AND ticker=? AND ABS(ts - ?) <= ?
               ORDER BY ABS(ts - ?) LIMIT 1""",
            (platform, ticker, target_ts, tolerance_seconds, target_ts),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()
