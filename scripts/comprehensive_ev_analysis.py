#!/usr/bin/env python3
"""
Comprehensive MongoDB Empirical Trading Strategy Analysis
Analyzes Price (X), Asset (Y), and Timing of Entry / Exit (Z) on Expected Value (EV).
"""

import os
import sys
from datetime import datetime, timezone
from collections import defaultdict
import certifi
import pymongo
import pandas as pd
import numpy as np

# Database connection
uri = os.getenv(
    "MONGODB_URI",
    "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority"
)
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
db = client["trading_bot_db"]

def get_symbol(ticker):
    t = str(ticker).upper()
    for s in ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "NEAR", "HYPE", "ZEC", "GOLD", "WTI", "INX", "NASDAQ100", "SPX", "NDX"]:
        if s in t:
            return s
    return "OTHER"

def get_timeframe(ticker):
    t = str(ticker).upper()
    if "15M" in t:
        return "15m"
    if "05M" in t or "5M" in t:
        return "5m"
    return "1h"

def run_settlements_deep_dive():
    print("=" * 80)
    print("SECTION 1: SETTLEMENTS DATABASE DEEP DIVE (1,891 Trades)")
    print("=" * 80)

    settle_docs = list(db["settlements"].find())
    df = pd.DataFrame(settle_docs)
    
    # Parse trade details
    def parse_trade(row):
        yes_cnt = row.get("yes_count") or 0
        no_cnt = row.get("no_count") or 0
        cost = float(row.get("cost_dollars") or 0.0)
        pnl = float(row.get("pnl_dollars") or 0.0)
        res = str(row.get("market_result") or "").lower().strip()
        
        if yes_cnt > 0 and no_cnt == 0:
            side = "yes"
            count = yes_cnt
        elif no_cnt > 0 and yes_cnt == 0:
            side = "no"
            count = no_cnt
        elif yes_cnt > 0 and no_cnt > 0:
            side = "both"
            count = yes_cnt + no_cnt
        else:
            if cost > 0:
                if res == "yes":
                    side = "yes" if pnl > 0 else "no"
                elif res == "no":
                    side = "no" if pnl > 0 else "yes"
                else:
                    side = "unknown"
                count = 1
            else:
                side = "unknown"
                count = 0
                
        price_cents = (cost / count * 100) if count > 0 else 0
        won = (side == res) if (side in ("yes", "no") and res in ("yes", "no")) else (pnl > 0)
        return pd.Series([side, count, price_cents, won], index=["side", "contracts", "price_cents", "won"])

    parsed = df.apply(parse_trade, axis=1)
    df = pd.concat([df, parsed], axis=1)
    df["sym"] = df["ticker"].apply(get_symbol)
    df["tf"] = df["ticker"].apply(get_timeframe)

    # Valid trades with known entry price
    valid = df[(df["cost_dollars"] > 0) & (df["price_cents"] > 0) & (df["price_cents"] <= 100)].copy()
    
    print(f"Total Settlement Records: {len(df):,}")
    print(f"Attributed Trades with Valid Price & Cost: {len(valid):,}")
    
    # 1. Price Corridors
    bins = [0, 15, 30, 45, 55, 70, 85, 100]
    labels = [
        "1-15¢ (Extreme Discount)",
        "16-30¢ (Deep Discount)",
        "31-45¢ (Value Corridor)",
        "46-55¢ (Coin Flip Zone)",
        "56-70¢ (Momentum Trap)",
        "71-85¢ (High Momentum)",
        "86-99¢ (Favorite Corridor)"
    ]
    valid["corridor"] = pd.cut(valid["price_cents"], bins=bins, labels=labels)
    
    print("\n--- 1A. OVERALL PERFORMANCE BY PRICE CORRIDOR (X) ---")
    p_summary = valid.groupby("corridor", observed=False).agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        win_rate=("won", "mean"),
        avg_price=("price_cents", "mean"),
        total_cost=("cost_dollars", "sum"),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
        roi=("pnl_dollars", lambda p: (p.sum() / valid.loc[p.index, "cost_dollars"].sum()) if valid.loc[p.index, "cost_dollars"].sum() > 0 else 0)
    )
    p_summary["win_rate"] = p_summary["win_rate"].map(lambda x: f"{x:.1%}")
    p_summary["avg_price"] = p_summary["avg_price"].map(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
    p_summary["total_cost"] = p_summary["total_cost"].map(lambda x: f"${x:,.2f}")
    p_summary["total_pnl"] = p_summary["total_pnl"].map(lambda x: f"${x:,.2f}")
    p_summary["avg_pnl"] = p_summary["avg_pnl"].map(lambda x: f"${x:+.2f}")
    p_summary["roi"] = p_summary["roi"].map(lambda x: f"{x:+.1%}")
    print(p_summary.to_string())

    # 1B. Asset Performance
    print("\n--- 1B. PERFORMANCE BY ASSET (Y) ---")
    asset_summary = df.groupby("sym").agg(
        trades=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda p: (p > 0).sum()),
        win_rate=("pnl_dollars", lambda p: (p > 0).mean()),
        total_cost=("cost_dollars", "sum"),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
        roi=("pnl_dollars", lambda p: (p.sum() / df.loc[p.index, "cost_dollars"].sum()) if df.loc[p.index, "cost_dollars"].sum() > 0 else 0)
    ).sort_values("trades", ascending=False)
    asset_summary["win_rate"] = asset_summary["win_rate"].map(lambda x: f"{x:.1%}")
    asset_summary["total_cost"] = asset_summary["total_cost"].map(lambda x: f"${x:,.2f}")
    asset_summary["total_pnl"] = asset_summary["total_pnl"].map(lambda x: f"${x:,.2f}")
    asset_summary["avg_pnl"] = asset_summary["avg_pnl"].map(lambda x: f"${x:+.2f}")
    asset_summary["roi"] = asset_summary["roi"].map(lambda x: f"{x:+.1%}")
    print(asset_summary.to_string())

    # 1C. Timeframe Performance
    print("\n--- 1C. PERFORMANCE BY TIMEFRAME (Z_timeframe) ---")
    tf_summary = df.groupby("tf").agg(
        trades=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda p: (p > 0).sum()),
        win_rate=("pnl_dollars", lambda p: (p > 0).mean()),
        total_cost=("cost_dollars", "sum"),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
        roi=("pnl_dollars", lambda p: (p.sum() / df.loc[p.index, "cost_dollars"].sum()) if df.loc[p.index, "cost_dollars"].sum() > 0 else 0)
    )
    tf_summary["win_rate"] = tf_summary["win_rate"].map(lambda x: f"{x:.1%}")
    tf_summary["total_cost"] = tf_summary["total_cost"].map(lambda x: f"${x:,.2f}")
    tf_summary["total_pnl"] = tf_summary["total_pnl"].map(lambda x: f"${x:,.2f}")
    tf_summary["avg_pnl"] = tf_summary["avg_pnl"].map(lambda x: f"${x:+.2f}")
    tf_summary["roi"] = tf_summary["roi"].map(lambda x: f"{x:+.1%}")
    print(tf_summary.to_string())

    # 1D. Timeframe x Price Corridor Matrix
    print("\n--- 1D. CROSS-TAB: TIMEFRAME x PRICE CORRIDOR (X x Z_tf) ---")
    tf_corr = valid.groupby(["tf", "corridor"], observed=False).agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        win_rate=("won", "mean"),
        avg_price=("price_cents", "mean"),
        total_cost=("cost_dollars", "sum"),
        total_pnl=("pnl_dollars", "sum"),
        roi=("pnl_dollars", lambda p: (p.sum() / valid.loc[p.index, "cost_dollars"].sum()) if valid.loc[p.index, "cost_dollars"].sum() > 0 else 0)
    )
    tf_corr["win_rate"] = tf_corr["win_rate"].map(lambda x: f"{x:.1%}")
    tf_corr["avg_price"] = tf_corr["avg_price"].map(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
    tf_corr["total_cost"] = tf_corr["total_cost"].map(lambda x: f"${x:,.2f}")
    tf_corr["total_pnl"] = tf_corr["total_pnl"].map(lambda x: f"${x:,.2f}")
    tf_corr["roi"] = tf_corr["roi"].map(lambda x: f"{x:+.1%}")
    print(tf_corr.to_string())

    return df, valid

if __name__ == "__main__":
    run_settlements_deep_dive()
