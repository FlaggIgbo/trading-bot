#!/usr/bin/env python3
import os
import certifi
import pymongo
import pandas as pd
import numpy as np

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=10000)
db = client["trading_bot_db"]

print("================================================================================")
print("COMPREHENSIVE EXCHANGE COMPARISON: KALSHI VS GEMINI")
print("================================================================================")

# 1. PORTFOLIO PNL
print("\n--- 1. PORTFOLIO PNL DATA ---")
pnl_docs = list(db["portfolio_pnl"].find())
df_pnl = pd.DataFrame(pnl_docs)
print(f"Total Portfolio PnL snapshots: {len(df_pnl):,}")
for ex in ["kalshi", "gemini"]:
    sub = df_pnl[df_pnl["ex"] == ex].sort_values("ts", ascending=False)
    if len(sub) > 0:
        latest = sub.iloc[0]
        first = sub.iloc[-1]
        print(f"\nExchange: {ex.upper()} (n={len(sub):,} records)")
        print(f"  First Record:  {pd.to_datetime(first['ts'], unit='s')} | Balance: ${first.get('balance_dollars', 0):.2f} | Realized PnL: ${first.get('total_realized_pnl_dollars', 0):.2f}")
        print(f"  Latest Record: {pd.to_datetime(latest['ts'], unit='s')} | Balance: ${latest.get('balance_dollars', 0):.2f} | Realized PnL: ${latest.get('total_realized_pnl_dollars', 0):.2f}")
        print(f"  Settled Markets Count: {latest.get('settlements_count', 0)}")
        print(f"  Total Settled Revenue: ${latest.get('total_settled_revenue_dollars', 0):.2f}")
        print(f"  Total Settled Cost:    ${latest.get('total_settled_cost_dollars', 0):.2f}")
        print(f"  Open Positions:        {latest.get('open_positions_count', 0)} (${latest.get('open_exposure_dollars', 0):.2f})")

# 2. ACTIVE POSITIONS
print("\n--- 2. ACTIVE POSITIONS DATA ---")
pos_docs = list(db["active_positions"].find())
df_p = pd.DataFrame(pos_docs)
print(f"Total Active Positions: {len(df_p):,}")
for ex in ["kalshi", "gemini"]:
    sub = df_p[df_p["ex"] == ex].copy()
    print(f"\nExchange: {ex.upper()} (n={len(sub):,} positions)")
    if len(sub) > 0:
        sub["entry_price_cents"] = pd.to_numeric(sub["entry_price_cents"], errors="coerce")
        sub["spread_saved_cents"] = pd.to_numeric(sub["spread_saved_cents"], errors="coerce")
        sub["cost_dollars"] = sub["entry_price_cents"] / 100.0
        print(f"  Total Capital Allocated: ${sub['cost_dollars'].sum():,.2f}")
        print(f"  Mean Entry Price:        {sub['entry_price_cents'].mean():.1f}¢ (std: {sub['entry_price_cents'].std():.1f}¢)")
        print(f"  Mean Spread Saved:       {sub['spread_saved_cents'].mean():.2f}¢")
        print(f"  Timeframes:              {sub['tf'].value_counts().to_dict()}")
        print(f"  Sides:                   {sub['side'].value_counts().to_dict()}")
        print(f"  Strategies:              {sub['strategy_tag'].fillna('unspecified').value_counts().to_dict()}")
        print(f"  Order Roles:             {sub['order_role'].fillna('unspecified').value_counts().to_dict()}")

# 3. MARKET SNAPSHOTS MICROSTRUCTURE
print("\n--- 3. MARKET SNAPSHOTS MICROSTRUCTURE (1,000 SAMPLE COMPARISON) ---")
k_snaps = list(db["market_snapshots"].find({"ex": "kalshi"}).limit(2000))
g_snaps = list(db["market_snapshots"].find({"ex": "gemini"}).limit(2000))
df_k_snap = pd.DataFrame(k_snaps)
df_g_snap = pd.DataFrame(g_snaps)

print(f"Kalshi Sample (n={len(df_k_snap):,}):")
print(f"  Mean Bid-Ask Spread: {df_k_snap['sprd'].astype(float).mean():.2f}¢ (Median: {df_k_snap['sprd'].astype(float).median():.1f}¢)")
print(f"  Mean Volume:         {df_k_snap['vol'].astype(float).mean():,.0f} (Median: {df_k_snap['vol'].astype(float).median():,.0f})")
print(f"  Zero Volume %:       {(df_k_snap['vol'].astype(float) == 0).mean()*100:.1f}%")

print(f"Gemini Sample (n={len(df_g_snap):,}):")
print(f"  Mean Bid-Ask Spread: {df_g_snap['sprd'].astype(float).mean():.2f}¢ (Median: {df_g_snap['sprd'].astype(float).median():.1f}¢)")
print(f"  Mean Volume:         {df_g_snap['vol'].astype(float).mean():,.0f} (Median: {df_g_snap['vol'].astype(float).median():,.0f})")
print(f"  Zero Volume %:       {(df_g_snap['vol'].astype(float) == 0).mean()*100:.1f}%")

# 4. SETTLEMENTS
print("\n--- 4. SETTLEMENTS (LIVE & HISTORICAL) ---")
s_docs = list(db["settlements"].find())
df_s = pd.DataFrame(s_docs)
print(f"Total Settlements in DB: {len(df_s):,}")
print("Settlement ex counts:\n", df_s["ex"].value_counts(dropna=False))
print(f"Total Cost: ${df_s['cost_dollars'].astype(float).sum():,.2f}")
print(f"Total Revenue: ${df_s['revenue_dollars'].astype(float).sum():,.2f}")
print(f"Total Realized PnL: ${df_s['pnl_dollars'].astype(float).sum():,.2f}")
print(f"Overall Win Rate: {(df_s['pnl_dollars'].astype(float) > 0).mean()*100:.1f}%")

print("\nDone analysis.")
