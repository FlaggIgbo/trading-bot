"""
Detailed breakdown of 48h data from MongoDB
"""
import os
import certifi
import pymongo
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
db = client["trading_bot_db"]

now_utc = datetime.now(timezone.utc)
cutoff_48h = now_utc - timedelta(hours=48)
cutoff_48h_ts = cutoff_48h.timestamp()

# 1. SETTLEMENTS
settles = list(db["settlements"].find())
settles_48h = []
for s in settles:
    st = s.get("settled_time")
    ts = s.get("ts") or s.get("entry_ts")
    dt = None
    if ts and isinstance(ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass
    elif st:
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except Exception:
            pass
    if dt and dt >= cutoff_48h:
        s["_dt"] = dt
        settles_48h.append(s)

df_s = pd.DataFrame(settles_48h)

def get_sym(ticker):
    t = str(ticker).upper()
    for sym in ["BTC", "ETH", "SOL", "XRP", "GOLD", "WTI", "SILVER", "INX", "NASDAQ100", "NDX", "SPX", "HYPE"]:
        if sym in t:
            return sym
    return "UNKNOWN"

def get_tf(ticker):
    t = str(ticker).upper()
    if "15M" in t: return "15m"
    if "05M" in t or "5M" in t: return "5m"
    return "1h"

df_s["sym"] = df_s["ticker"].apply(get_sym)
df_s["tf"] = df_s["ticker"].apply(get_tf)
df_s["pnl_dollars"] = pd.to_numeric(df_s["pnl_dollars"], errors="coerce").fillna(0.0)
df_s["cost_dollars"] = pd.to_numeric(df_s["cost_dollars"], errors="coerce").fillna(0.0)
df_s["revenue_dollars"] = pd.to_numeric(df_s["revenue_dollars"], errors="coerce").fillna(0.0)
df_s["won"] = df_s["pnl_dollars"] > 0
df_s["strategy"] = df_s.get("strategy_tag", pd.Series([None]*len(df_s))).fillna("unassigned")
df_s["role"] = df_s.get("order_role", pd.Series([None]*len(df_s))).fillna("unspecified")

print("=" * 80)
print("SETTLEMENT PERFORMANCE METRICS (LAST 48H)")
print("=" * 80)
print(f"Total Trades: {len(df_s)}")
print(f"Wins: {df_s['won'].sum()} ({(df_s['won'].mean()):.1%}) | Losses: {(df_s['pnl_dollars'] < 0).sum()} ({((df_s['pnl_dollars'] < 0).mean()):.1%})")
print(f"Total Wagered: ${df_s['cost_dollars'].sum():.2f}")
print(f"Total Revenue: ${df_s['revenue_dollars'].sum():.2f}")
print(f"Net Realized PnL: ${df_s['pnl_dollars'].sum():+.2f}")
if df_s['cost_dollars'].sum() > 0:
    print(f"ROI: {(df_s['pnl_dollars'].sum() / df_s['cost_dollars'].sum()):+.2%}")

print("\n--- PERFORMANCE BY ASSET ---")
sym_summary = df_s.groupby("sym").agg(
    trades=("won", "count"),
    wins=("won", "sum"),
    losses=("pnl_dollars", lambda x: (x < 0).sum()),
    win_rate=("won", lambda x: f"{x.mean():.1%}"),
    cost=("cost_dollars", "sum"),
    rev=("revenue_dollars", "sum"),
    net_pnl=("pnl_dollars", "sum"),
    avg_pnl=("pnl_dollars", "mean")
)
print(sym_summary.to_string())

print("\n--- PERFORMANCE BY TIMEFRAME ---")
tf_summary = df_s.groupby("tf").agg(
    trades=("won", "count"),
    wins=("won", "sum"),
    losses=("pnl_dollars", lambda x: (x < 0).sum()),
    win_rate=("won", lambda x: f"{x.mean():.1%}"),
    cost=("cost_dollars", "sum"),
    rev=("revenue_dollars", "sum"),
    net_pnl=("pnl_dollars", "sum"),
    avg_pnl=("pnl_dollars", "mean")
)
print(tf_summary.to_string())

print("\n--- PERFORMANCE BY ORDER ROLE & STRATEGY ---")
role_strat = df_s.groupby(["role", "strategy"]).agg(
    trades=("won", "count"),
    wins=("won", "sum"),
    losses=("pnl_dollars", lambda x: (x < 0).sum()),
    win_rate=("won", lambda x: f"{x.mean():.1%}"),
    cost=("cost_dollars", "sum"),
    rev=("revenue_dollars", "sum"),
    net_pnl=("pnl_dollars", "sum"),
    avg_pnl=("pnl_dollars", "mean")
)
print(role_strat.to_string())

print("\n--- ALL 70 SETTLED TRADES IN 48H (SORTED BY DATE) ---")
cols = ["_dt", "ticker", "sym", "tf", "role", "strategy", "cost_dollars", "revenue_dollars", "pnl_dollars", "market_result"]
available_cols = [c for c in cols if c in df_s.columns]
df_sorted = df_s.sort_values(by="_dt", ascending=True)
for idx, row in df_sorted.iterrows():
    dt_str = row["_dt"].strftime("%m-%d %H:%M")
    tick = row["ticker"]
    res = row.get("market_result", "?")
    pnl = row["pnl_dollars"]
    cost = row["cost_dollars"]
    role = row["role"]
    strat = row["strategy"]
    print(f"{dt_str} | {tick:<28} | {row['sym']:<5} | {row['tf']:<3} | cost:${cost:4.2f} | pnl:${pnl:+5.2f} | res:{res:<3} | {role:<11} | {strat}")

# 2. ACTIVE POSITIONS INVESTIGATION
print("\n" + "=" * 80)
print("ACTIVE POSITIONS AUDIT")
print("=" * 80)
active_docs = list(db["active_positions"].find())
df_act = pd.DataFrame(active_docs)
print(f"Total active position docs in DB: {len(df_act)}")
if "updated_at" in df_act.columns:
    df_act["dt"] = pd.to_datetime(df_act["updated_at"], unit="s", utc=True, errors="coerce")
    recent_act = df_act[df_act["dt"] >= cutoff_48h]
    print(f"Positions updated in last 48h: {len(recent_act)}")
    print(f"Positions older than 48h: {len(df_act) - len(recent_act)}")
    print("\nRecent positions updated in last 48h (breakdown by sym):")
    recent_act["sym"] = recent_act["ticker"].apply(get_sym)
    print(recent_act.groupby("sym").agg(
        count=("ticker", "count"),
        total_cost=("total_cost_dollars", "sum"),
        avg_entry=("entry_price_cents", "mean")
    ).to_string())

# 3. COUNTERFACTUAL / REGIME / SNAPSHOT METRICS
print("\n" + "=" * 80)
print("MARKET SNAPSHOT REGIMES & 4-VECTOR SIGNALS (48H)")
print("=" * 80)
snaps = db["market_snapshots"]
pipeline = [
    {"$match": {"ts": {"$gte": cutoff_48h_ts}}},
    {"$group": {
        "_id": "$c_regime",
        "count": {"$sum": 1},
        "avg_c_score": {"$avg": "$c_score"},
    }},
    {"$sort": {"count": -1}}
]
regime_res = list(snaps.aggregate(pipeline))
print("Consensus Regimes Distribution across Snapshots:")
for r in regime_res:
    print(f"  Regime: {str(r['_id']):<25} | Count: {r['count']:,} | Avg Score: {round(r['avg_c_score'] or 0, 3)}")

