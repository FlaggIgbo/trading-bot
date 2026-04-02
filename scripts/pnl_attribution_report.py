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
    # Using 'id' as the signal_id
    sig_query = """
        SELECT id, side, edge, outcome 
        FROM signals
        WHERE ts >= ?
    """
    signals = conn.execute(sig_query, (cutoff_ts,)).fetchall()
    
    # 2. Query Predictions (Filled Trades)
    # We now group by signal_id to ensure we don't double-count fills for the same decision
    pred_query = """
        SELECT signal_id, outcome, realized_pnl_cents
        FROM predictions
        WHERE ts >= ? AND signal_id IS NOT NULL
    """
    fills = conn.execute(pred_query, (cutoff_ts,)).fetchall()
    
    # Build a lookup of which signals were filled
    filled_signal_ids = {}
    for f in fills:
        sid = f["signal_id"]
        if sid not in filled_signal_ids:
            filled_signal_ids[sid] = {"wins": 0, "res": 0, "pnl": 0}
        
        if f["outcome"] in ("win", "lose"):
            filled_signal_ids[sid]["res"] += 1
            if f["outcome"] == "win":
                filled_signal_ids[sid]["wins"] += 1
        filled_signal_ids[sid]["pnl"] += int(f["realized_pnl_cents"] or 0)

    buckets = {
        "<8%":   {"sig_total": 0, "sig_res": 0, "sig_wins": 0, "filled_sigs": 0, "fill_res": 0, "fill_wins": 0, "pnl": 0},
        "8–10%": {"sig_total": 0, "sig_res": 0, "sig_wins": 0, "filled_sigs": 0, "fill_res": 0, "fill_wins": 0, "pnl": 0},
        "10–20%": {"sig_total": 0, "sig_res": 0, "sig_wins": 0, "filled_sigs": 0, "fill_res": 0, "fill_wins": 0, "pnl": 0},
        "20%+":  {"sig_total": 0, "sig_res": 0, "sig_wins": 0, "filled_sigs": 0, "fill_res": 0, "fill_wins": 0, "pnl": 0},
    }

    # Process Signals for Shadow Stats & Join with Fills
    for s in signals:
        edge = float(s["edge"] or 0.0)
        bucket_key = None
        if edge < 0.08: bucket_key = "<8%"
        elif edge < 0.10: bucket_key = "8–10%"
        elif edge < 0.20: bucket_key = "10–20%"
        else: bucket_key = "20%+"
        
        b = buckets[bucket_key]
        b["sig_total"] += 1
        
        # Shadow Outcome
        if s["outcome"] in ("win", "lose"):
            b["sig_res"] += 1
            if s["outcome"] == "win":
                b["sig_wins"] += 1
        
        # Realized Outcome (Linked via signal_id)
        sid = s["id"]
        if sid in filled_signal_ids:
            fstats = filled_signal_ids[sid]
            b["filled_sigs"] += 1
            b["pnl"] += fstats["pnl"]
            if fstats["res"] > 0:
                b["fill_res"] += 1
                # A signal counts as a win if any of its fills (usually just 1) ended in a win
                if fstats["wins"] > 0:
                    b["fill_wins"] += 1

    print(f"\n==========================================================================")
    print(f" BIAS-CORRECTED PnL ATTRIBUTION (Last {args.days} days)")
    print(f"==========================================================================")
    print(f"{'Bucket':<10} | {'Signals':<8} | {'Fill Rate':<10} | {'Shadow WR':<10} | {'Real WR':<10} | {'PnL (¢)'}")
    print("-" * 74)
    
    for key in ["<8%", "8–10%", "10–20%", "20%+"]:
        b = buckets[key]
        if b["sig_total"] == 0 and b["filled_sigs"] == 0:
            print(f"{key:<10} | {0:<8} | {'-':<10} | {'-':<10} | {'-':<10} | {0}")
            continue
            
        rate = (b["filled_sigs"] / b["sig_total"] * 100.0) if b["sig_total"] > 0 else 0.0
        swr = (b["sig_wins"] / b["sig_res"] * 100.0) if b["sig_res"] > 0 else None
        rwr = (b["fill_wins"] / b["fill_res"] * 100.0) if b["fill_res"] > 0 else None
        
        # Format strings
        rate_s = f"{rate:>5.1f}%" if b["sig_total"] > 0 else "0.0%"
        swr_s = f"{swr:>5.1f}%" if swr is not None else "N/A"
        rwr_s = f"{rwr:>5.1f}%" if rwr is not None else "N/A"
        
        print(f"{key:<10} | {b['sig_total']:<8} | {rate_s:<10} | {swr_s:<10} | {rwr_s:<10} | {b['pnl']:>5}")
        
    print("==========================================================================\n")
    print("Shadow WR (Win Rate) reflects our model's accuracy on all opportunities.")
    print("Real WR   (Win Rate) reflects our performance only on filled trades.")
    print("Fill Rate counts % of signals that resulted in at least one fill.")
    print("==========================================================================\n")

if __name__ == "__main__":
    main()
