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
    """)


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
            "SELECT id, platform, ticker, side, hist_p, entry_price, count FROM predictions WHERE outcome IS NULL"
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
) -> bool:
    """Mark a prediction as resolved. outcome: win if (side==yes and yes_settled) or (side==no and not yes_settled)."""
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


def get_calibration_stats(config: dict | None = None) -> dict[str, Any]:
    """
    Return calibration stats from resolved predictions: win rate, total PnL, win rate by hist_p bucket.
    Used to detect overconfidence (said 70%, won 50%) or losing streaks.
    """
    conn = _get_conn(config)
    if not conn:
        return {"enabled": False, "resolved_count": 0}
    try:
        rows = conn.execute(
            """SELECT outcome, pnl_cents, hist_p FROM predictions WHERE outcome IS NOT NULL"""
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
            }
        wins = sum(1 for r in rows if r["outcome"] == "win")
        loses = sum(1 for r in rows if r["outcome"] == "lose")
        total_pnl = sum(r["pnl_cents"] or 0 for r in rows)
        # Bucket by hist_p (0.5-0.6, 0.6-0.7, ...)
        buckets: dict[tuple[float, float], list[str]] = {}
        for r in rows:
            h = r["hist_p"] or 0.5
            # For NO: P(win)=1-hist_p
            bucket = (round(h * 10) / 10, round(h * 10) / 10 + 0.1)
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(r["outcome"])
        bucket_stats = [
            {
                "hist_p_range": f"{lo:.1f}-{hi:.1f}",
                "count": len(outcomes),
                "win_rate": sum(1 for o in outcomes if o == "win") / len(outcomes) if outcomes else 0,
            }
            for (lo, hi), outcomes in sorted(buckets.items())
        ]
        return {
            "enabled": True,
            "resolved_count": len(rows),
            "win_count": wins,
            "lose_count": loses,
            "total_pnl_cents": total_pnl,
            "win_rate": wins / (wins + loses) if (wins + loses) > 0 else None,
            "buckets": bucket_stats,
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
