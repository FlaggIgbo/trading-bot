"""
Comprehensive 48-Hour MongoDB Trading Data Evaluator
Queries collections: settlements, portfolio_pnl, market_snapshots, active_positions
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

# Load .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
db = client["trading_bot_db"]

now_utc = datetime.now(timezone.utc)
cutoff_48h = now_utc - timedelta(hours=48)
cutoff_48h_iso = cutoff_48h.strftime("%Y-%m-%dT%H:%M:%SZ")
cutoff_48h_ts = cutoff_48h.timestamp()

print("=" * 80)
print(f"MONGODB TRADING DATA EVALUATION: LAST 48 HOURS")
print(f"Evaluation Window: {cutoff_48h.strftime('%Y-%m-%d %H:%M:%S UTC')} to {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
print("=" * 80)

# ==============================================================================
# 1. SETTLEMENTS ANALYSIS (LAST 48 HOURS)
# ==============================================================================
print("\n" + "=" * 80)
print("1. SETTLEMENTS & REALIZED PNL PERFORMANCE (LAST 48 HOURS)")
print("=" * 80)

settle_docs = list(db["settlements"].find())
settles_48h = []
for s in settle_docs:
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
        s_copy = dict(s)
        s_copy["_parsed_dt"] = dt
        settles_48h.append(s_copy)

print(f"Total historical settlements in DB: {len(settle_docs):,}")
print(f"Settlements in last 48 hours:        {len(settles_48h):,}")

if settles_48h:
    df_s = pd.DataFrame(settles_48h)
    
    def get_sym(ticker):
        t = str(ticker).upper()
        for sym in ["BTC", "ETH", "SOL", "XRP", "GOLD", "WTI", "SILVER", "INX", "NASDAQ100", "NDX", "SPX", "HYPE"]:
            if sym in t:
                return sym
        return "UNKNOWN"
    
    def get_tf(ticker):
        t = str(ticker).upper()
        if "15M" in t:
            return "15m"
        if "05M" in t or "5M" in t:
            return "5m"
        return "1h"

    if "sym" not in df_s.columns or df_s["sym"].isnull().any():
        df_s["sym"] = df_s["ticker"].apply(get_sym)
    if "tf" not in df_s.columns or df_s["tf"].isnull().any():
        df_s["tf"] = df_s["ticker"].apply(get_tf)
        
    total_trades = len(df_s)
    wins = (df_s["pnl_dollars"] > 0).sum()
    losses = (df_s["pnl_dollars"] < 0).sum()
    ties = (df_s["pnl_dollars"] == 0).sum()
    win_rate = wins / total_trades if total_trades > 0 else 0
    
    total_pnl = df_s["pnl_dollars"].sum()
    total_cost = df_s["cost_dollars"].sum() if "cost_dollars" in df_s.columns else 0.0
    total_rev = df_s["revenue_dollars"].sum() if "revenue_dollars" in df_s.columns else 0.0
    roi = (total_pnl / total_cost) if total_cost > 0 else 0.0
    
    gross_profit = df_s[df_s["pnl_dollars"] > 0]["pnl_dollars"].sum()
    gross_loss = abs(df_s[df_s["pnl_dollars"] < 0]["pnl_dollars"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    
    print(f"\n[Overall Settlement Key Metrics - 48h]")
    print(f"  Total Trades Settled:    {total_trades}")
    print(f"  Wins:                    {wins} ({win_rate:.1%})")
    print(f"  Losses:                  {losses} ({losses/total_trades:.1%})")
    print(f"  Breakevens:              {ties}")
    print(f"  Total Capital Wagered:   ${total_cost:,.2f}")
    print(f"  Total Revenue Returned:  ${total_rev:,.2f}")
    print(f"  Net Realized PnL:        ${total_pnl:+,.2f}")
    print(f"  Aggregate ROI:           {roi:+.2%}")
    print(f"  Profit Factor:           {profit_factor:.2f}")
    print(f"  Average PnL per Trade:   ${total_pnl/total_trades:+,.4f}")
    print(f"  Best Single Win:         ${df_s['pnl_dollars'].max():+,.2f}")
    print(f"  Worst Single Loss:       ${df_s['pnl_dollars'].min():+,.2f}")

    print("\n--- Performance by Asset (Symbol) ---")
    sym_perf = df_s.groupby("sym").agg(
        trades=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda x: (x > 0).sum()),
        losses=("pnl_dollars", lambda x: (x < 0).sum()),
        win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
        total_cost=("cost_dollars", "sum") if "cost_dollars" in df_s.columns else ("pnl_dollars", lambda x: 0),
        total_rev=("revenue_dollars", "sum") if "revenue_dollars" in df_s.columns else ("pnl_dollars", lambda x: 0),
    )
    if "cost_dollars" in df_s.columns:
        sym_perf["roi"] = (sym_perf["total_pnl"] / sym_perf["total_cost"].replace(0, np.nan)).apply(lambda x: f"{x:+.1%}" if pd.notnull(x) else "N/A")
    sym_perf["win_rate"] = sym_perf["win_rate"].apply(lambda x: f"{x:.1%}")
    sym_perf["total_pnl"] = sym_perf["total_pnl"].apply(lambda x: f"${x:+,.2f}")
    sym_perf["avg_pnl"] = sym_perf["avg_pnl"].apply(lambda x: f"${x:+,.4f}")
    sym_perf["total_cost"] = sym_perf["total_cost"].apply(lambda x: f"${x:,.2f}")
    sym_perf["total_rev"] = sym_perf["total_rev"].apply(lambda x: f"${x:,.2f}")
    print(sym_perf.to_string())

    print("\n--- Performance by Timeframe ---")
    tf_perf = df_s.groupby("tf").agg(
        trades=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda x: (x > 0).sum()),
        losses=("pnl_dollars", lambda x: (x < 0).sum()),
        win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
        total_cost=("cost_dollars", "sum") if "cost_dollars" in df_s.columns else ("pnl_dollars", lambda x: 0),
    )
    if "cost_dollars" in df_s.columns:
        tf_perf["roi"] = (tf_perf["total_pnl"] / tf_perf["total_cost"].replace(0, np.nan)).apply(lambda x: f"{x:+.1%}" if pd.notnull(x) else "N/A")
    tf_perf["win_rate"] = tf_perf["win_rate"].apply(lambda x: f"{x:.1%}")
    tf_perf["total_pnl"] = tf_perf["total_pnl"].apply(lambda x: f"${x:+,.2f}")
    tf_perf["avg_pnl"] = tf_perf["avg_pnl"].apply(lambda x: f"${x:+,.4f}")
    tf_perf["total_cost"] = tf_perf["total_cost"].apply(lambda x: f"${x:,.2f}")
    print(tf_perf.to_string())

    if "strategy_tag" in df_s.columns:
        print("\n--- Performance by Strategy Tag ---")
        strat_perf = df_s.groupby(df_s["strategy_tag"].fillna("unassigned")).agg(
            trades=("pnl_dollars", "count"),
            wins=("pnl_dollars", lambda x: (x > 0).sum()),
            win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
            total_pnl=("pnl_dollars", "sum"),
            avg_pnl=("pnl_dollars", "mean"),
        )
        strat_perf["win_rate"] = strat_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        strat_perf["total_pnl"] = strat_perf["total_pnl"].apply(lambda x: f"${x:+,.2f}")
        strat_perf["avg_pnl"] = strat_perf["avg_pnl"].apply(lambda x: f"${x:+,.4f}")
        print(strat_perf.to_string())

    if "entered_side" in df_s.columns:
        print("\n--- Performance by Entered Side ---")
        side_perf = df_s.groupby(df_s["entered_side"].fillna("unknown")).agg(
            trades=("pnl_dollars", "count"),
            wins=("pnl_dollars", lambda x: (x > 0).sum()),
            win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
            total_pnl=("pnl_dollars", "sum"),
            avg_pnl=("pnl_dollars", "mean"),
        )
        side_perf["win_rate"] = side_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        side_perf["total_pnl"] = side_perf["total_pnl"].apply(lambda x: f"${x:+,.2f}")
        side_perf["avg_pnl"] = side_perf["avg_pnl"].apply(lambda x: f"${x:+,.4f}")
        print(side_perf.to_string())

    if "order_role" in df_s.columns:
        print("\n--- Performance by Order Role (Maker vs Taker) ---")
        role_perf = df_s.groupby(df_s["order_role"].fillna("unspecified")).agg(
            trades=("pnl_dollars", "count"),
            wins=("pnl_dollars", lambda x: (x > 0).sum()),
            win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
            total_pnl=("pnl_dollars", "sum"),
            avg_pnl=("pnl_dollars", "mean"),
        )
        role_perf["win_rate"] = role_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        role_perf["total_pnl"] = role_perf["total_pnl"].apply(lambda x: f"${x:+,.2f}")
        role_perf["avg_pnl"] = role_perf["avg_pnl"].apply(lambda x: f"${x:+,.4f}")
        print(role_perf.to_string())

    # Daily breakdown (Yesterday vs Today)
    df_s["settle_date"] = df_s["_parsed_dt"].dt.strftime("%Y-%m-%d")
    print("\n--- Performance by Date (UTC) ---")
    date_perf = df_s.groupby("settle_date").agg(
        trades=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda x: (x > 0).sum()),
        losses=("pnl_dollars", lambda x: (x < 0).sum()),
        win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
        total_pnl=("pnl_dollars", "sum"),
        total_cost=("cost_dollars", "sum") if "cost_dollars" in df_s.columns else ("pnl_dollars", lambda x: 0),
    )
    date_perf["win_rate"] = date_perf["win_rate"].apply(lambda x: f"{x:.1%}")
    date_perf["total_pnl"] = date_perf["total_pnl"].apply(lambda x: f"${x:+,.2f}")
    date_perf["total_cost"] = date_perf["total_cost"].apply(lambda x: f"${x:,.2f}")
    print(date_perf.to_string())

# ==============================================================================
# 2. PORTFOLIO PNL & BALANCE PROGRESSION (LAST 48 HOURS)
# ==============================================================================
print("\n" + "=" * 80)
print("2. PORTFOLIO PNL & BALANCE PROGRESSION (LAST 48 HOURS)")
print("=" * 80)

pnl_docs = list(db["portfolio_pnl"].find({"ts": {"$gte": cutoff_48h_ts}}).sort([("ts", 1)]))
print(f"Portfolio PnL snapshots in last 48h: {len(pnl_docs):,}")

if pnl_docs:
    df_p = pd.DataFrame(pnl_docs)
    df_p["dt"] = pd.to_datetime(df_p["ts"], unit="s", utc=True)
    
    first_snap = df_p.iloc[0]
    last_snap = df_p.iloc[-1]
    
    start_bal = first_snap.get("balance_dollars")
    end_bal = last_snap.get("balance_dollars")
    start_port = first_snap.get("portfolio_value_dollars")
    end_port = last_snap.get("portfolio_value_dollars")
    
    min_bal = df_p["balance_dollars"].min()
    max_bal = df_p["balance_dollars"].max()
    min_port = df_p["portfolio_value_dollars"].min()
    max_port = df_p["portfolio_value_dollars"].max()
    
    start_realized = first_snap.get("total_realized_pnl_dollars")
    end_realized = last_snap.get("total_realized_pnl_dollars")
    realized_change = (end_realized - start_realized) if (end_realized is not None and start_realized is not None) else None
    
    print(f"Start Timestamp:             {first_snap['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"End Timestamp:               {last_snap['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Starting Cash Balance:       ${start_bal:,.2f}" if start_bal is not None else "N/A")
    print(f"Ending Cash Balance:         ${end_bal:,.2f}" if end_bal is not None else "N/A")
    print(f"Cash Balance Range (Min/Max):${min_bal:,.2f} / ${max_bal:,.2f}")
    print(f"Starting Total Portfolio:    ${start_port:,.2f}" if start_port is not None else "N/A")
    print(f"Ending Total Portfolio:      ${end_port:,.2f}" if end_port is not None else "N/A")
    print(f"Total Portfolio Range:       ${min_port:,.2f} / ${max_port:,.2f}")
    print(f"Cumulative Realized PnL:     ${end_realized:+,.2f} (Change over 48h: ${realized_change:+,.2f})" if realized_change is not None else f"${end_realized:+,.2f}")
    print(f"Current Open Positions Count:{last_snap.get('open_positions_count')}")
    print(f"Current Open Exposure:       ${last_snap.get('open_exposure_dollars'):,.2f}" if last_snap.get('open_exposure_dollars') is not None else "N/A")
    
    # Resample 6-hourly to see portfolio trajectory
    df_p.set_index("dt", inplace=True)
    resampled = df_p.resample("6h").agg({
        "balance_dollars": "last",
        "portfolio_value_dollars": "last",
        "total_realized_pnl_dollars": "last",
        "open_positions_count": "last",
        "open_exposure_dollars": "last"
    }).dropna()
    print("\n--- 6-Hour Portfolio Progression ---")
    print(resampled.to_string())

# ==============================================================================
# 3. MARKET SNAPSHOTS & SCANNER ACTIVITY (LAST 48 HOURS / RETENTION WINDOW)
# ==============================================================================
print("\n" + "=" * 80)
print("3. MARKET SNAPSHOTS & SCANNER COVERAGE")
print("=" * 80)

snap_col = db["market_snapshots"]
total_snaps_48h = snap_col.count_documents({"ts": {"$gte": cutoff_48h_ts}})
print(f"Total snapshots in last 48h / active retention: {total_snaps_48h:,}")

# Check breakdown by symbol and tf
pipeline = [
    {"$match": {"ts": {"$gte": cutoff_48h_ts}}},
    {"$group": {
        "_id": {"sym": "$sym", "tf": "$tf"},
        "count": {"$sum": 1},
        "avg_sprd": {"$avg": "$sprd"},
        "trade_signals": {"$sum": {"$cond": [{"$ne": ["$trd", None]}, 1, 0]}},
        "fav_maker_signals": {"$sum": {"$cond": [{"$ne": ["$fav_maker", None]}, 1, 0]}},
    }},
    {"$sort": {"_id.sym": 1, "_id.tf": 1}}
]
snap_summary = list(snap_col.aggregate(pipeline))

if snap_summary:
    rows = []
    for r in snap_summary:
        rows.append({
            "Asset": r["_id"].get("sym"),
            "TF": r["_id"].get("tf"),
            "Snapshots": r["count"],
            "Avg Spread (¢)": round(r["avg_sprd"], 2) if r["avg_sprd"] is not None else None,
            "Trade Signals": r["trade_signals"],
            "Fav Maker Sigs": r["fav_maker_signals"]
        })
    df_snaps = pd.DataFrame(rows)
    print(df_snaps.to_string(index=False))

# Trade signals logged in market_snapshots
trade_snaps = list(snap_col.find(
    {"ts": {"$gte": cutoff_48h_ts}, "trd": {"$exists": True, "$ne": None}},
    {"ts": 1, "sym": 1, "tf": 1, "tick": 1, "m": 1, "trd": 1, "y": 1, "n": 1, "sprd": 1, "cfb": 1, "fav": 1, "fav_maker": 1}
))
print(f"\nTrade signal events recorded in snapshots (48h): {len(trade_snaps):,}")
if trade_snaps:
    actions = [s.get("trd", {}).get("action") for s in trade_snaps]
    action_counts = pd.Series(actions).value_counts()
    print("Action distribution in snapshots:")
    print(action_counts.to_string())

# ==============================================================================
# 4. ACTIVE POSITIONS IN MONGODB
# ==============================================================================
print("\n" + "=" * 80)
print("4. ACTIVE POSITIONS STORED IN MONGODB")
print("=" * 80)

active_col = db["active_positions"]
all_active = list(active_col.find())
print(f"Total active position records currently stored: {len(all_active):,}")

if all_active:
    df_a = pd.DataFrame(all_active)
    if "_id" in df_a.columns:
        df_a.drop(columns=["_id"], inplace=True)
    
    total_active_cost = df_a["total_cost_dollars"].sum() if "total_cost_dollars" in df_a.columns else 0.0
    print(f"Total Active Cost Basis: ${total_active_cost:,.2f}")
    
    if "sym" not in df_a.columns or df_a["sym"].isnull().any():
        df_a["sym"] = df_a["ticker"].apply(get_sym)
    if "tf" not in df_a.columns or df_a["tf"].isnull().any():
        df_a["tf"] = df_a["ticker"].apply(get_tf)
        
    print("\n--- Active Positions by Asset ---")
    active_by_sym = df_a.groupby("sym").agg(
        positions=("count", "count"),
        total_contracts=("count", "sum") if "count" in df_a.columns else ("ticker", "count"),
        total_cost=("total_cost_dollars", "sum") if "total_cost_dollars" in df_a.columns else ("ticker", lambda x: 0),
        avg_entry=("entry_price_cents", "mean") if "entry_price_cents" in df_a.columns else ("ticker", lambda x: 0),
    )
    print(active_by_sym.to_string())
    
    print("\n--- Sample of Most Recent Active Positions ---")
    sample_active = df_a.sort_values(by="updated_at", ascending=False).head(10) if "updated_at" in df_a.columns else df_a.head(10)
    cols_to_show = [c for c in ["ticker", "sym", "tf", "side", "count", "entry_price_cents", "strategy_tag", "order_role", "updated_at"] if c in sample_active.columns]
    print(sample_active[cols_to_show].to_string(index=False))

print("\n" + "=" * 80)
print("EVALUATION COMPLETE")
print("=" * 80)
