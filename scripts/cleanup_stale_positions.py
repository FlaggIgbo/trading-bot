#!/usr/bin/env python3
import time
import sqlite3
import os
import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Clean up stale 'held' positions and unresolved evaluations.")
    parser.add_argument("--hours", type=float, default=48.0, help="Age in hours to consider stale (default 48)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be cleaned up without modifying")
    args = parser.parse_args()

    db_path = Path("~/.trading-bot-data/trading_bot.db").expanduser()
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    cutoff_ts = time.time() - (args.hours * 3600)
    
    # 1. Predictions (Positions)
    stale_preds = conn.execute(
        "SELECT id, ticker, side, datetime(ts, 'unixepoch') as dt FROM predictions WHERE outcome IS NULL AND ts < ?",
        (cutoff_ts,)
    ).fetchall()
    
    print(f"Found {len(stale_preds)} stale predictions (older than {args.hours}h):")
    for p in stale_preds:
        print(f"  [{p['dt']}] {p['ticker']} {p['side']} (ID: {p['id']})")
        
    if not args.dry_run and stale_preds:
        conn.execute(
            "UPDATE predictions SET outcome='resolved_stale', resolved_ts=? WHERE outcome IS NULL AND ts < ?",
            (time.time(), cutoff_ts)
        )
        print(f"Updated {len(stale_preds)} predictions to 'resolved_stale'.")

    # 2. Position Evaluations
    stale_evals = conn.execute(
        "SELECT id, ticker, side, datetime(ts, 'unixepoch') as dt FROM position_evaluations WHERE outcome IS NULL AND ts < ?",
        (cutoff_ts,)
    ).fetchall()
    
    print(f"\nFound {len(stale_evals)} stale position evaluations:")
    for e in stale_evals:
        print(f"  [{e['dt']}] {e['ticker']} {e['side']} (ID: {e['id']})")
        
    if not args.dry_run and stale_evals:
        conn.execute(
            "UPDATE position_evaluations SET outcome='resolved_stale', resolved_ts=? WHERE outcome IS NULL AND ts < ?",
            (time.time(), cutoff_ts)
        )
        print(f"Updated {len(stale_evals)} evaluations to 'resolved_stale'.")

    conn.commit()
    conn.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
