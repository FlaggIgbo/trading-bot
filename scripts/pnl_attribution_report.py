#!/usr/bin/env python3
"""
Live PnL Attribution Report (Bias-Corrected)

This script reads from both `signals` (all opportunities) and `predictions` (filled trades)
to calculate "Shadow PnL" (Market-Take) versus "Realized PnL" (Passive-Fill).
"""

import argparse
import sqlite3
import time
from pathlib import Path

def _get_db_path() -> Path:
    return Path.home() / ".trading-bot-data" / "trading_bot.db"

def main():
    parser = argparse.ArgumentParser("PnL Attribution Report")
    parser.add_argument("--days", type=float, default=30.0, help="Days of history to analyze")
    args = parser.parse_args()

    db_path = _get_db_path()
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cutoff_ts = time.time() - (args.days * 86400)
    
    # 1. Query Signals (All Opportunities)
    sig_query = """
        SELECT side, edge, outcome 
        FROM signals
        WHERE outcome IN ('win', 'lose')
          AND resolved_ts >= ?
    """
    signals = conn.execute(sig_query, (cutoff_ts,)).fetchall()
    
    # 2. Query Predictions (Filled Trades)
    pred_query = """
        SELECT side, hist_p, theoretical_price, bid_at_place, entry_price, 
               outcome, realized_pnl_cents
        FROM predictions
        WHERE outcome IN ('win', 'lose')
          AND resolved_ts >= ?
    """
    fills = conn.execute(pred_query, (cutoff_ts,)).fetchall()
    
    buckets = {
        "<8%":   {"signals": 0, "sig_wins": 0, "fills": 0, "fill_wins": 0, "pnl": 0},
        "8–10%": {"signals": 0, "sig_wins": 0, "fills": 0, "fill_wins": 0, "pnl": 0},
        "10–20%": {"signals": 0, "sig_wins": 0, "fills": 0, "fill_wins": 0, "pnl": 0},
        "20%+":  {"signals": 0, "sig_wins": 0, "fills": 0, "fill_wins": 0, "pnl": 0},
    }

    # Process Signals for Shadow Stats
    for s in signals:
        edge = float(s["edge"] or 0.0)
        bucket_key = None
        if edge < 0.08: bucket_key = "<8%"
        elif edge < 0.10: bucket_key = "8–10%"
        elif edge < 0.20: bucket_key = "10–20%"
        else: bucket_key = "20%+"
        
        b = buckets[bucket_key]
        b["signals"] += 1
        if s["outcome"] == "win":
            b["sig_wins"] += 1

    # Process Fills for Realized Stats
    for f in fills:
        # Re-derive expected edge to bucket correctly (matching signals logic)
        side = (f["side"] or "").lower()
        model_p = float(f["hist_p"] or 0.0) if side == "yes" else (1.0 - float(f["hist_p"] or 0.0))
        exp_price = f["theoretical_price"] or f["bid_at_place"] or f["entry_price"]
        edge = model_p - float(exp_price) - 0.01 # fee proxy
        
        bucket_key = None
        if edge < 0.08: bucket_key = "<8%"
        elif edge < 0.10: bucket_key = "8–10%"
        elif edge < 0.20: bucket_key = "10–20%"
        else: bucket_key = "20%+"
        
        b = buckets[bucket_key]
        b["fills"] += 1
        if f["outcome"] == "win":
            b["fill_wins"] += 1
        b["pnl"] += int(f["realized_pnl_cents"] or 0)
        
    print(f"\n==========================================================================")
    print(f" BIAS-CORRECTED PnL ATTRIBUTION (Last {args.days} days)")
    print(f"==========================================================================")
    print(f"{'Bucket':<10} | {'Signals':<8} | {'Fill Rate':<10} | {'Shadow WR':<10} | {'Real WR':<10} | {'PnL (¢)'}")
    print("-" * 74)
    
    for key in ["<8%", "8–10%", "10–20%", "20%+"]:
        b = buckets[key]
        if b["signals"] == 0 and b["fills"] == 0:
            print(f"{key:<10} | {0:<8} | {'-':<10} | {'-':<10} | {'-':<10} | {0}")
            continue
            
        rate = (b["fills"] / b["signals"] * 100.0) if b["signals"] > 0 else 0.0
        swr = (b["sig_wins"] / b["signals"] * 100.0) if b["signals"] > 0 else 0.0
        rwr = (b["fill_wins"] / b["fills"] * 100.0) if b["fills"] > 0 else 0.0
        
        # Format strings for 'Rate' and 'WR' to handle 0/None cases
        rate_s = f"{rate:>5.1f}%" if b["signals"] > 0 else "N/A"
        swr_s = f"{swr:>5.1f}%" if b["signals"] > 0 else "N/A"
        rwr_s = f"{rwr:>5.1f}%" if b["fills"] > 0 else "N/A"
        
        print(f"{key:<10} | {b['signals']:<8} | {rate_s:<10} | {swr_s:<10} | {rwr_s:<10} | {b['pnl']:>5}")
        
    print("==========================================================================\n")
    print("Shadow WR (Win Rate) assumes immediate Market-Take execution.")
    print("Real WR   (Win Rate) reflects our actual Passive-Limit execution.")
    print("==========================================================================\n")

if __name__ == "__main__":
    main()
