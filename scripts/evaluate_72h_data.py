"""
Comprehensive 72-Hour MongoDB Trading Data Evaluator
High-performance quantitative evaluator using MongoDB aggregation and analytics.
"""
import os
import sys
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
cutoff_72h = now_utc - timedelta(hours=72)
cutoff_72h_iso = cutoff_72h.strftime("%Y-%m-%dT%H:%M:%SZ")
cutoff_72h_ts = int(cutoff_72h.timestamp())

def parse_doc_time(doc):
    for k in ["settled_time", "created_at", "open_time", "close_time"]:
        if k in doc and doc[k] and isinstance(doc[k], str):
            try:
                return datetime.fromisoformat(doc[k].replace("Z", "+00:00"))
            except Exception:
                pass
    for k in ["ts", "entry_ts", "updated_at"]:
        if k in doc and doc[k] and isinstance(doc[k], (int, float)):
            try:
                return datetime.fromtimestamp(doc[k], tz=timezone.utc)
            except Exception:
                pass
    return None

def get_sym(ticker):
    t = str(ticker).upper()
    for sym in ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "GOLD", "WTI", "SILVER", "INX", "NASDAQ100", "NDX", "SPX", "HYPE", "ZEC"]:
        if sym in t:
            return sym
    return "OTHER"

def get_tf(ticker):
    t = str(ticker).upper()
    if "15M" in t:
        return "15m"
    if "05M" in t or "5M" in t:
        return "5m"
    if "1H" in t or "H" in t:
        return "1h"
    return "1h"

print("=" * 90)
print(f"📊 MONGODB TRADING BOT EVALUATION: LAST 72 HOURS")
print(f"⏱  Evaluation Window: {cutoff_72h.strftime('%Y-%m-%d %H:%M:%S UTC')} to {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
print("=" * 90)

# ==============================================================================
# 1. SETTLEMENTS & REALIZED PERFORMANCE
# ==============================================================================
print("\n" + "=" * 90)
print("1. SETTLEMENTS & REALIZED PNL PERFORMANCE (LAST 72 HOURS)")
print("=" * 90)

all_settles = list(db["settlements"].find())
settles_72h = []
for s in all_settles:
    dt = parse_doc_time(s)
    if dt and dt >= cutoff_72h:
        s_copy = dict(s)
        s_copy["_parsed_dt"] = dt
        settles_72h.append(s_copy)

print(f"Total historical settlements in DB: {len(all_settles):,}")
print(f"Settlements in last 72 hours:        {len(settles_72h):,}")

if settles_72h:
    df_s = pd.DataFrame(settles_72h)
    
    # Normalize columns
    if "sym" not in df_s.columns or df_s["sym"].isnull().any():
        df_s["sym"] = df_s["ticker"].apply(get_sym)
    if "tf" not in df_s.columns or df_s["tf"].isnull().any():
        df_s["tf"] = df_s["ticker"].apply(get_tf)
    
    df_s["pnl_dollars"] = pd.to_numeric(df_s["pnl_dollars"], errors="coerce").fillna(0.0)
    df_s["cost_dollars"] = pd.to_numeric(df_s["cost_dollars"], errors="coerce").fillna(0.0)
    df_s["revenue_dollars"] = pd.to_numeric(df_s["revenue_dollars"], errors="coerce").fillna(0.0)
    df_s["entry_price_cents"] = pd.to_numeric(df_s.get("entry_price_cents", 0), errors="coerce").fillna(0)
    df_s["spread_saved_cents"] = pd.to_numeric(df_s.get("spread_saved_cents", 0), errors="coerce").fillna(0)
    df_s["won"] = df_s["pnl_dollars"] > 0
    df_s["lost"] = df_s["pnl_dollars"] < 0
    df_s["tied"] = df_s["pnl_dollars"] == 0
    df_s["strategy"] = df_s.get("strategy_tag", pd.Series(["unassigned"]*len(df_s))).fillna("unassigned")
    df_s["role"] = df_s.get("order_role", pd.Series(["unspecified"]*len(df_s))).fillna("unspecified")
    df_s["exchange"] = df_s.get("ex", pd.Series(["unknown"]*len(df_s))).fillna("unknown")

    total_trades = len(df_s)
    wins = df_s["won"].sum()
    losses = df_s["lost"].sum()
    ties = df_s["tied"].sum()
    win_rate = wins / total_trades if total_trades > 0 else 0
    
    total_pnl = df_s["pnl_dollars"].sum()
    total_cost = df_s["cost_dollars"].sum()
    total_rev = df_s["revenue_dollars"].sum()
    roi = (total_pnl / total_cost) if total_cost > 0 else 0.0
    
    gross_profit = df_s[df_s["pnl_dollars"] > 0]["pnl_dollars"].sum()
    gross_loss = abs(df_s[df_s["pnl_dollars"] < 0]["pnl_dollars"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    
    avg_win = df_s[df_s["pnl_dollars"] > 0]["pnl_dollars"].mean() if wins > 0 else 0.0
    avg_loss = df_s[df_s["pnl_dollars"] < 0]["pnl_dollars"].mean() if losses > 0 else 0.0
    avg_ev_cents = (total_pnl / total_trades) * 100 if total_trades > 0 else 0.0
    total_spread_saved_dollars = df_s["spread_saved_cents"].sum() / 100.0

    print(f"\n--- EXECUTIVE SUMMARY (72H) ---")
    print(f"  • Total Trades Settled:     {total_trades}")
    print(f"  • Wins / Losses / Ties:     {wins} Wins | {losses} Losses | {ties} Ties")
    print(f"  • Realized Win Rate:        {win_rate:.1%}")
    print(f"  • Capital Wagered (Cost):   ${total_cost:.2f}")
    print(f"  • Total Revenue:            ${total_rev:.2f}")
    print(f"  • Net Realized PnL:         ${total_pnl:+.2f}")
    print(f"  • Return on Investment:     {roi:+.2%}")
    print(f"  • Profit Factor:            {profit_factor:.2f}")
    print(f"  • Average EV / Trade:       {avg_ev_cents:+.2f}¢")
    print(f"  • Avg Win / Avg Loss:       +${avg_win:.2f} / ${avg_loss:.2f}")
    print(f"  • Total Spread Alpha Saved: ${total_spread_saved_dollars:.2f} ({df_s['spread_saved_cents'].mean():.1f}¢/trade)")

    # Breakdown by Asset
    print("\n" + "-" * 90)
    print("📈 PERFORMANCE BY ASSET (SYMBOL)")
    print("-" * 90)
    asset_agg = df_s.groupby("sym").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        losses=("lost", "sum"),
        win_rate=("won", lambda x: f"{x.mean():.1%}"),
        cost=("cost_dollars", "sum"),
        rev=("revenue_dollars", "sum"),
        net_pnl=("pnl_dollars", "sum"),
        roi=("pnl_dollars", lambda x: f"{(x.sum() / df_s.loc[x.index, 'cost_dollars'].sum()):+.1%}" if df_s.loc[x.index, 'cost_dollars'].sum() > 0 else "0.0%"),
        avg_ev=("pnl_dollars", lambda x: f"{x.mean()*100:+.1f}¢")
    ).sort_values(by="net_pnl", ascending=False)
    print(asset_agg.to_string())

    # Breakdown by Timeframe
    print("\n" + "-" * 90)
    print("⏱  PERFORMANCE BY TIMEFRAME")
    print("-" * 90)
    tf_agg = df_s.groupby("tf").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        losses=("lost", "sum"),
        win_rate=("won", lambda x: f"{x.mean():.1%}"),
        cost=("cost_dollars", "sum"),
        rev=("revenue_dollars", "sum"),
        net_pnl=("pnl_dollars", "sum"),
        roi=("pnl_dollars", lambda x: f"{(x.sum() / df_s.loc[x.index, 'cost_dollars'].sum()):+.1%}" if df_s.loc[x.index, 'cost_dollars'].sum() > 0 else "0.0%"),
        avg_ev=("pnl_dollars", lambda x: f"{x.mean()*100:+.1f}¢")
    ).sort_values(by="net_pnl", ascending=False)
    print(tf_agg.to_string())

    # Breakdown by Strategy & Role
    print("\n" + "-" * 90)
    print("🎯 PERFORMANCE BY STRATEGY & ORDER ROLE")
    print("-" * 90)
    strat_agg = df_s.groupby(["strategy", "role"]).agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        losses=("lost", "sum"),
        win_rate=("won", lambda x: f"{x.mean():.1%}"),
        cost=("cost_dollars", "sum"),
        net_pnl=("pnl_dollars", "sum"),
        roi=("pnl_dollars", lambda x: f"{(x.sum() / df_s.loc[x.index, 'cost_dollars'].sum()):+.1%}" if df_s.loc[x.index, 'cost_dollars'].sum() > 0 else "0.0%"),
        avg_ev=("pnl_dollars", lambda x: f"{x.mean()*100:+.1f}¢"),
        spread_saved=("spread_saved_cents", lambda x: f"{x.sum()/100:.2f}$")
    ).sort_values(by="net_pnl", ascending=False)
    print(strat_agg.to_string())

    # Breakdown by Exchange
    print("\n" + "-" * 90)
    print("🏛  PERFORMANCE BY EXCHANGE")
    print("-" * 90)
    ex_agg = df_s.groupby("exchange").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        losses=("lost", "sum"),
        win_rate=("won", lambda x: f"{x.mean():.1%}"),
        cost=("cost_dollars", "sum"),
        net_pnl=("pnl_dollars", "sum"),
        roi=("pnl_dollars", lambda x: f"{(x.sum() / df_s.loc[x.index, 'cost_dollars'].sum()):+.1%}" if df_s.loc[x.index, 'cost_dollars'].sum() > 0 else "0.0%"),
        avg_ev=("pnl_dollars", lambda x: f"{x.mean()*100:+.1f}¢")
    ).sort_values(by="net_pnl", ascending=False)
    print(ex_agg.to_string())

    # Breakdown by Entry Price Bucket & Breakeven Analysis
    print("\n" + "-" * 90)
    print("💰 ENTRY PRICE BUCKET & THEORETICAL BREAKEVEN GAP ANALYSIS")
    print("-" * 90)
    bins = [0, 50, 70, 80, 85, 90, 95, 100]
    labels = ["<50¢", "50-69¢", "70-79¢", "80-84¢", "85-89¢", "90-94¢", "95-99¢"]
    df_s["price_bucket"] = pd.cut(df_s["entry_price_cents"], bins=bins, labels=labels, right=False)
    
    price_bucket_agg = []
    for bucket in labels:
        subset = df_s[df_s["price_bucket"] == bucket]
        if len(subset) > 0:
            n = len(subset)
            w = subset["won"].sum()
            wr = w / n
            avg_entry = subset["entry_price_cents"].mean()
            breakeven_wr = avg_entry / 100.0
            pnl = subset["pnl_dollars"].sum()
            cost = subset["cost_dollars"].sum()
            ev_realized = (pnl / n) * 100
            price_bucket_agg.append({
                "Bucket": bucket,
                "Trades": n,
                "Wins": w,
                "Losses": (subset["lost"]).sum(),
                "Win Rate": f"{wr:.1%}",
                "Avg Entry": f"{avg_entry:.1f}¢",
                "Req Breakeven": f"{breakeven_wr:.1%}",
                "Alpha Gap": f"{(wr - breakeven_wr):+.1%}",
                "Net PnL": f"${pnl:+.2f}",
                "Realized EV": f"{ev_realized:+.1f}¢"
            })
    if price_bucket_agg:
        print(pd.DataFrame(price_bucket_agg).to_string(index=False))

    # All Losing Trades Forensic Audit
    print("\n" + "-" * 90)
    print("🔍 ALL LOSING TRADES IN THE LAST 72 HOURS (FORENSIC AUDIT)")
    print("-" * 90)
    loss_trades = df_s[df_s["pnl_dollars"] < 0].sort_values(by="_parsed_dt", ascending=True)
    if len(loss_trades) == 0:
        print("🎉 Zero losing trades recorded in the last 72 hours!")
    else:
        for idx, row in loss_trades.iterrows():
            dt_str = row["_parsed_dt"].strftime("%Y-%m-%d %H:%M UTC")
            tick = row["ticker"]
            res = row.get("market_result", "?")
            pnl = row["pnl_dollars"]
            cost = row["cost_dollars"]
            entry_p = row["entry_price_cents"]
            role = row["role"]
            strat = row["strategy"]
            ex = row["exchange"]
            print(f"[{dt_str}] ({ex}) {tick:<28} | {row['sym']:<5} | Entry:{entry_p:2.0f}¢ | Cost:${cost:4.2f} | PnL:${pnl:+5.2f} | Res:{res:<3} | {role} | {strat}")

    # Chronological Settlement Log
    print("\n" + "-" * 90)
    print("📜 CHRONOLOGICAL SETTLEMENT LOG (LAST 72 HOURS)")
    print("-" * 90)
    df_sorted = df_s.sort_values(by="_parsed_dt", ascending=True)
    for idx, row in df_sorted.iterrows():
        dt_str = row["_parsed_dt"].strftime("%m-%d %H:%M")
        tick = row["ticker"]
        res = row.get("market_result", "?")
        pnl = row["pnl_dollars"]
        cost = row["cost_dollars"]
        entry_p = row["entry_price_cents"]
        role = row["role"]
        strat = row["strategy"]
        outcome_symbol = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⚪️")
        print(f"{outcome_symbol} {dt_str} | {tick:<26} | {row['sym']:<5} | {row['tf']:<3} | Entry:{entry_p:2.0f}¢ | Cost:${cost:4.2f} | PnL:${pnl:+5.2f} | Res:{res:<3} | {role:<6} | {strat}")

# ==============================================================================
# 2. ACTIVE POSITIONS AUDIT
# ==============================================================================
print("\n" + "=" * 90)
print("2. ACTIVE POSITIONS AUDIT (LAST 72 HOURS)")
print("=" * 90)

all_active = list(db["active_positions"].find())
active_72h = []
for p in all_active:
    dt = parse_doc_time(p)
    if dt and dt >= cutoff_72h:
        p_copy = dict(p)
        p_copy["_parsed_dt"] = dt
        active_72h.append(p_copy)

print(f"Total active position records in DB: {len(all_active):,}")
print(f"Active positions in last 72 hours:   {len(active_72h):,}")

if active_72h:
    df_act = pd.DataFrame(active_72h)
    if "sym" not in df_act.columns or df_act["sym"].isnull().any():
        df_act["sym"] = df_act["ticker"].apply(get_sym)
    if "tf" not in df_act.columns or df_act["tf"].isnull().any():
        df_act["tf"] = df_act["ticker"].apply(get_tf)
    df_act["total_cost_dollars"] = pd.to_numeric(df_act.get("total_cost_dollars", 0), errors="coerce").fillna(0.0)
    df_act["entry_price_cents"] = pd.to_numeric(df_act.get("entry_price_cents", 0), errors="coerce").fillna(0)
    df_act["count"] = pd.to_numeric(df_act.get("count", 1), errors="coerce").fillna(1)
    df_act["role"] = df_act.get("order_role", pd.Series(["unspecified"]*len(df_act))).fillna("unspecified")
    df_act["strategy"] = df_act.get("strategy_tag", pd.Series(["unassigned"]*len(df_act))).fillna("unassigned")

    print("\n--- ACTIVE POSITIONS BY ASSET (72H) ---")
    act_sym = df_act.groupby("sym").agg(
        positions=("ticker", "count"),
        total_contracts=("count", "sum"),
        total_cost=("total_cost_dollars", "sum"),
        avg_entry=("entry_price_cents", "mean")
    ).sort_values(by="positions", ascending=False)
    print(act_sym.to_string())

    print("\n--- ACTIVE POSITIONS BY STRATEGY & ROLE ---")
    act_strat = df_act.groupby(["strategy", "role"]).agg(
        positions=("ticker", "count"),
        total_contracts=("count", "sum"),
        total_cost=("total_cost_dollars", "sum"),
        avg_entry=("entry_price_cents", "mean")
    ).sort_values(by="positions", ascending=False)
    print(act_strat.to_string())

    # Latest 10 active positions
    print("\n--- 10 MOST RECENT ACTIVE POSITIONS ---")
    recent_10 = df_act.sort_values(by="_parsed_dt", ascending=False).head(10)
    for idx, row in recent_10.iterrows():
        dt_str = row["_parsed_dt"].strftime("%Y-%m-%d %H:%M:%S UTC")
        tick = row["ticker"]
        side = row.get("side", "?")
        cost = row["total_cost_dollars"]
        entry_p = row["entry_price_cents"]
        cnt = row["count"]
        role = row["role"]
        strat = row["strategy"]
        print(f"[{dt_str}] {tick:<26} | Side:{side:<3} | Qty:{cnt:2.0f} | Entry:{entry_p:2.0f}¢ | Cost:${cost:4.2f} | {role} | {strat}")

# ==============================================================================
# 3. PORTFOLIO PNL & WALLET TRAJECTORY
# ==============================================================================
print("\n" + "=" * 90)
print("3. PORTFOLIO PNL & WALLET BALANCE TRAJECTORY (LAST 72 HOURS)")
print("=" * 90)

all_pnl = list(db["portfolio_pnl"].find({"ts": {"$gte": cutoff_72h_ts}}))
pnl_72h = []
for p in all_pnl:
    dt = parse_doc_time(p)
    if dt and dt >= cutoff_72h:
        p_copy = dict(p)
        p_copy["_parsed_dt"] = dt
        pnl_72h.append(p_copy)

print(f"Portfolio telemetry snapshots in last 72 hours: {len(pnl_72h):,}")

if pnl_72h:
    df_pnl = pd.DataFrame(pnl_72h)
    df_pnl["balance_dollars"] = pd.to_numeric(df_pnl.get("balance_dollars", 0), errors="coerce").fillna(0.0)
    df_pnl["portfolio_value_dollars"] = pd.to_numeric(df_pnl.get("portfolio_value_dollars", 0), errors="coerce").fillna(0.0)
    df_pnl["open_exposure_dollars"] = pd.to_numeric(df_pnl.get("open_exposure_dollars", 0), errors="coerce").fillna(0.0)
    df_pnl["total_realized_pnl_dollars"] = pd.to_numeric(df_pnl.get("total_realized_pnl_dollars", 0), errors="coerce").fillna(0.0)
    df_pnl["open_positions_count"] = pd.to_numeric(df_pnl.get("open_positions_count", 0), errors="coerce").fillna(0)
    df_pnl["exchange"] = df_pnl.get("ex", pd.Series(["unknown"]*len(df_pnl))).fillna("unknown")

    for ex, group in df_pnl.groupby("exchange"):
        group_sorted = group.sort_values(by="_parsed_dt", ascending=True)
        start_row = group_sorted.iloc[0]
        end_row = group_sorted.iloc[-1]
        
        start_bal = start_row["balance_dollars"]
        end_bal = end_row["balance_dollars"]
        bal_change = end_bal - start_bal
        
        start_port = start_row["portfolio_value_dollars"]
        end_port = end_row["portfolio_value_dollars"]
        port_change = end_port - start_port
        
        max_bal = group_sorted["balance_dollars"].max()
        min_bal = group_sorted["balance_dollars"].min()
        max_exp = group_sorted["open_exposure_dollars"].max()
        latest_open_pos = end_row["open_positions_count"]
        latest_exp = end_row["open_exposure_dollars"]

        print(f"\n🏛  EXCHANGE: {ex.upper()}")
        print(f"  • Start Time:        {start_row['_parsed_dt'].strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  • Latest Time:       {end_row['_parsed_dt'].strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  • Starting Balance:  ${start_bal:.2f}")
        print(f"  • Latest Balance:    ${end_bal:.2f} (Net: ${bal_change:+.2f})")
        print(f"  • Portfolio Value:   ${start_port:.2f} -> ${end_port:.2f} (Net: ${port_change:+.2f})")
        print(f"  • Balance Range:     Min: ${min_bal:.2f} | Max: ${max_bal:.2f}")
        print(f"  • Peak Open Exposure:${max_exp:.2f}")
        print(f"  • Current Open Pos:  {latest_open_pos:.0f} (Exposure: ${latest_exp:.2f})")

# ==============================================================================
# 4. MARKET SNAPSHOTS & TELEMETRY INTELLIGENCE (AGGREGATED)
# ==============================================================================
print("\n" + "=" * 90)
print("4. MARKET SNAPSHOTS & TELEMETRY AUDIT (LAST 72 HOURS)")
print("=" * 90)

snap_agg = list(db["market_snapshots"].aggregate([
    {"$match": {"ts": {"$gte": cutoff_72h_ts}}},
    {"$group": {
        "_id": None,
        "count": {"$sum": 1},
        "avg_sprd": {"$avg": "$sprd"},
        "avg_fav": {"$avg": "$fav"},
        "tickers": {"$addToSet": "$tick"},
        "syms": {"$addToSet": "$sym"}
    }}
]))

if snap_agg:
    tot_snaps = snap_agg[0]["count"]
    uniq_ticks = len(snap_agg[0]["tickers"])
    uniq_syms = len(snap_agg[0]["syms"])
    avg_sprd = snap_agg[0]["avg_sprd"]
    avg_fav = snap_agg[0]["avg_fav"]
    avg_fav_str = f"{avg_fav:.2f}¢" if avg_fav is not None else "N/A"
    print(f"  • Total Telemetry Snapshots (72H): {tot_snaps:,}")
    print(f"  • Unique Markets / Tickers Tracked: {uniq_ticks:,}")
    print(f"  • Unique Underlying Assets Tracked: {uniq_syms}")
    print(f"  • Average Market Spread:            {avg_sprd:.2f}¢")
    print(f"  • Average Favorite Price:           {avg_fav_str}")

# Regimes
regime_agg = list(db["market_snapshots"].aggregate([
    {"$match": {"ts": {"$gte": cutoff_72h_ts}}},
    {"$group": {
        "_id": "$c_regime",
        "count": {"$sum": 1}
    }},
    {"$sort": {"count": -1}}
]))
if regime_agg:
    print("\n--- MARKET REGIME BREAKDOWN (72H) ---")
    for r in regime_agg:
        reg_name = r["_id"] if r["_id"] else "unclassified"
        pct = (r["count"] / tot_snaps) * 100 if tot_snaps > 0 else 0
        print(f"  • {reg_name:<20}: {r['count']:,} snapshots ({pct:.1f}%)")

# Asset Snapshot Breakdown
asset_snap_agg = list(db["market_snapshots"].aggregate([
    {"$match": {"ts": {"$gte": cutoff_72h_ts}}},
    {"$group": {
        "_id": "$sym",
        "count": {"$sum": 1},
        "avg_sprd": {"$avg": "$sprd"},
        "avg_fav": {"$avg": "$fav"}
    }},
    {"$sort": {"count": -1}}
]))
if asset_snap_agg:
    print("\n--- TELEMETRY VOLUME & SPREAD BY ASSET (72H) ---")
    df_ast_snap = pd.DataFrame(asset_snap_agg).rename(columns={"_id": "sym", "count": "snapshots"})
    print(df_ast_snap.to_string(index=False))

print("\n" + "=" * 90)
print("✅ 72-HOUR EVALUATION COMPLETE")
print("=" * 90)
