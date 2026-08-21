#!/usr/bin/env python3
import os
import sys
import certifi
import pymongo
import pandas as pd
import numpy as np
from datetime import datetime, timezone

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=10000)
db = client["trading_bot_db"]

def get_symbol(ticker):
    t = str(ticker).upper()
    for s in ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "NEAR", "HYPE", "ZEC", "GOLD", "SILVER", "WTI", "INX", "NASDAQ100", "SPX", "NDX"]:
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

print("1. Loading settlements and positions...", flush=True)
s_docs = list(db["settlements"].find({}, {
    "ticker": 1, "market_result": 1, "result": 1, "cost_dollars": 1,
    "pnl_dollars": 1, "revenue_dollars": 1, "settled_time": 1, "yes_count": 1, "no_count": 1, "ex": 1
}))
df_s = pd.DataFrame(s_docs)
print(f"Settlements loaded: {len(df_s):,}", flush=True)

pos_docs = list(db["active_positions"].find())
df_p = pd.DataFrame(pos_docs)
print(f"Active positions loaded: {len(df_p):,}", flush=True)

def parse_settle_trade(row):
    yes_cnt = row.get("yes_count") or 0
    no_cnt = row.get("no_count") or 0
    cost = float(row.get("cost_dollars") or 0.0)
    pnl = float(row.get("pnl_dollars") or 0.0)
    res = str(row.get("market_result") or row.get("result") or "").lower().strip()
    
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

parsed = df_s.apply(parse_settle_trade, axis=1)
df_s = pd.concat([df_s, parsed], axis=1)
df_s["sym"] = df_s["ticker"].apply(get_symbol)
df_s["tf"] = df_s["ticker"].apply(get_timeframe)
df_s["settled_dt"] = pd.to_datetime(df_s["settled_time"], errors="coerce")
df_s["ex"] = df_s["ex"].fillna("kalshi")
df_settle_valid = df_s[(df_s["cost_dollars"] > 0) & (df_s["price_cents"] > 0) & (df_s["price_cents"] <= 100)].copy()

print(f"Valid settlements: {len(df_settle_valid):,}", flush=True)

df_p["sym"] = df_p["sym"].fillna(df_p["ticker"].apply(get_symbol))
df_p["tf"] = df_p["tf"].fillna(df_p["ticker"].apply(get_timeframe))
df_p["price_cents"] = pd.to_numeric(df_p["entry_price_cents"], errors="coerce")
df_p["cost_dollars"] = df_p["price_cents"] / 100.0
df_p["ex"] = df_p["ex"].fillna("kalshi")

settle_map = {}
for _, r in df_s.iterrows():
    tick = r.get("ticker")
    if tick:
        settle_map[tick] = {
            "res": str(r.get("market_result") or r.get("result") or "").lower().strip(),
            "pnl": float(r.get("pnl_dollars") or 0.0),
            "cost": float(r.get("cost_dollars") or 0.0),
            "rev": float(r.get("revenue_dollars") or 0.0),
            "won": bool(r.get("won")),
        }

df_p["settle_res"] = df_p["ticker"].apply(lambda t: settle_map.get(t, {}).get("res"))
df_p["settle_pnl"] = df_p["ticker"].apply(lambda t: settle_map.get(t, {}).get("pnl"))
df_p["settle_cost"] = df_p["ticker"].apply(lambda t: settle_map.get(t, {}).get("cost"))
df_p["settle_won"] = df_p["ticker"].apply(lambda t: settle_map.get(t, {}).get("won"))

def get_entry_minute(row):
    ts = row.get("entry_ts") or row.get("updated_at")
    tf = row.get("tf")
    if pd.notnull(ts):
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.minute % 15 if tf == "15m" else dt.minute
        except Exception:
            pass
    px = row.get("price_cents") or 50
    tag = str(row.get("strategy_tag") or "")
    if "endgame" in tag or px >= 80:
        return 13 if tf == "15m" else 56
    elif "momentum" in tag or px >= 55:
        return 2 if tf == "15m" else 35
    elif "discount" in tag or px <= 35:
        return 1 if tf == "15m" else 5
    return 6 if tf == "15m" else 20

df_p["minute"] = df_p.apply(get_entry_minute, axis=1)

print("\n--- Summary by Exchange in Active Positions ---", flush=True)
for ex in ["kalshi", "gemini"]:
    sub = df_p[df_p["ex"] == ex]
    fav = sub[sub["strategy_tag"] == "endgame_favorite_maker"]
    print(f"Exchange {ex.upper()}: total={len(sub):,}, fav_maker={len(fav):,}, mean_price={sub['price_cents'].mean():.2f}¢, fav_mean_price={fav['price_cents'].mean():.2f}¢", flush=True)

print("\nAll done testing.", flush=True)
