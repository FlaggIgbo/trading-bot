#!/usr/bin/env python3
"""
Enhanced Historical Reconstruction Script

Restores:
1. Signal metadata (Denominator)
2. Fill linkage (Numerator)
3. Shadow Outcomes (Predictive Alpha)
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

    # 1. Fetch resolutions to map shadow outcomes
    res_rows = conn.execute("SELECT ticker, yes_settled FROM resolved_outcomes").fetchall()
    resolutions = {r["ticker"]: r["yes_settled"] for r in res_rows}
    print(f"Loaded {len(resolutions)} resolutions for Shadow WR reconstruction.")

    # 2. Process limit_orders to reconstruct signals
    orders = conn.execute("""
        SELECT id, platform, ticker, side, hist_p, edge, ts, order_id, theoretical_price
        FROM limit_orders 
        WHERE signal_id IS NULL
    """).fetchall()

    print(f"Processing {len(orders)} historical limit orders...")

    counts = {"signals": 0, "fills": 0, "shadow_outcomes": 0}
    
    for o in orders:
        # Determine shadow outcome if ticker has settled
        outcome = "settled"
        if o["ticker"] in resolutions:
            yes_won = bool(resolutions[o["ticker"]])
            won = (o["side"] == "yes" and yes_won) or (o["side"] == "no" and not yes_won)
            outcome = "win" if won else "lose"
            counts["shadow_outcomes"] += 1
        
        tp = o["theoretical_price"] or o["hist_p"]
        
        cursor = conn.execute("""
            INSERT INTO signals (platform, ticker, side, hist_p, theoretical_price, edge, ts, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (o["platform"], o["ticker"], o["side"], o["hist_p"], tp, o["edge"], o["ts"], outcome))
        
        sid = cursor.lastrowid
        conn.execute("UPDATE limit_orders SET signal_id = ? WHERE id = ?", (sid, o["id"]))
        counts["signals"] += 1
        
        if o["order_id"]:
            cur = conn.execute("UPDATE predictions SET signal_id = ? WHERE platform = ? AND order_id = ? AND signal_id IS NULL", 
                               (sid, o["platform"], o["order_id"]))
            counts["fills"] += cur.rowcount

    conn.commit()
    print(f"Total reconstructed: {counts['signals']} signals, {counts['fills']} fills, {counts['shadow_outcomes']} shadow outcomes.")
    conn.close()

if __name__ == "__main__":
    main()
