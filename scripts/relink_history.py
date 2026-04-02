#!/usr/bin/env python3
"""
Relink Historical Predictions to Reconstructed Signals

This script iterates over predictions with signal_id=NULL and matches them
to the signals table using (platform, ticker, side) and a 60s timestamp window.
"""

import sqlite3
import time
from pathlib import Path

def _get_db_path() -> Path:
    return Path.home() / ".trading-bot-data" / "trading_bot.db"

def main():
    db_path = _get_db_path()
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 1. Get all predictions needing linkage
    preds = conn.execute("""
        SELECT id, platform, ticker, side, ts 
        FROM predictions 
        WHERE signal_id IS NULL
    """).fetchall()

    print(f"Found {len(preds)} predictions needing linkage.")

    linked_count = 0
    for p in preds:
        # Find a signal that matches: same platform/ticker/side, within 60s
        # Sort by closest time just in case multiple matches
        match = conn.execute("""
            SELECT id FROM signals
            WHERE platform = ? AND ticker = ? AND side = ?
              AND ABS(ts - ?) < 120
            ORDER BY ABS(ts - ?) ASC
            LIMIT 1
        """, (p["platform"], p["ticker"], p["side"], p["ts"], p["ts"])).fetchone()

        if match:
            sid = match["id"]
            conn.execute("UPDATE predictions SET signal_id = ? WHERE id = ?", (sid, p["id"]))
            
            # Also try to update limit_orders if we have a match
            # limit_orders use the same window
            conn.execute("""
                UPDATE limit_orders 
                SET signal_id = ? 
                WHERE platform = ? AND ticker = ? AND side = ? 
                  AND ABS(ts - ?) < 120
                  AND signal_id IS NULL
            """, (sid, p["platform"], p["ticker"], p["side"], p["ts"]))
            
            linked_count += 1

    conn.commit()
    print(f"Successfully linked {linked_count} predictions to reconstructed signals.")
    conn.close()

if __name__ == "__main__":
    main()
