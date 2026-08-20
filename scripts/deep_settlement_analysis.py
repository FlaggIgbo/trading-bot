import os
import certifi
import pymongo
import pandas as pd
import numpy as np

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where())
db = client["trading_bot_db"]

settle_docs = list(db["settlements"].find())
df = pd.DataFrame(settle_docs)

# 1. Parse entered side, contract count, entry price
def parse_trade_details(row):
    yes_cnt = row.get("yes_count") or 0
    no_cnt = row.get("no_count") or 0
    cost = float(row.get("cost_dollars") or 0.0)
    pnl = float(row.get("pnl_dollars") or 0.0)
    result = str(row.get("market_result") or "").lower().strip()
    
    # Determine side
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
        # Infer from pnl and result if count is missing
        # if result is yes and pnl > 0 -> bought yes
        # if result is no and pnl > 0 -> bought no
        # if result is yes and pnl < 0 -> bought no
        # if result is no and pnl < 0 -> bought yes
        if cost > 0:
            if result == "yes":
                side = "yes" if pnl > 0 else "no"
            elif result == "no":
                side = "no" if pnl > 0 else "yes"
            else:
                side = "unknown"
            count = 1
        else:
            side = "unknown"
            count = 0
            
    avg_price_cents = (cost / count * 100) if count > 0 else 0
    won = (side == result) if (side in ("yes", "no") and result in ("yes", "no")) else (pnl > 0)
    
    return pd.Series([side, count, avg_price_cents, won], index=["entered_side", "contracts", "entry_price_cents", "won"])

details = df.apply(parse_trade_details, axis=1)
df = pd.concat([df, details], axis=1)

# Symbol and timeframe
def get_sym(ticker):
    t = str(ticker).upper()
    for s in ["BTC", "ETH", "SOL", "XRP", "GOLD", "WTI", "INX", "NASDAQ100"]:
        if s in t:
            return s
    return "OTHER"
df["sym"] = df["ticker"].apply(get_sym)

def get_tf(ticker):
    t = str(ticker).upper()
    if "15M" in t:
        return "15m"
    if "05M" in t or "5M" in t:
        return "5m"
    return "1h"
df["tf"] = df["ticker"].apply(get_tf)

print("=" * 80)
print(f"DEEP SETTLEMENTS ANALYSIS ({len(df):,} trades)")
print("=" * 80)

# Filter trades where we have valid entry price (> 0 and <= 100)
valid_trades = df[(df["cost_dollars"] > 0) & (df["entry_price_cents"] > 0) & (df["entry_price_cents"] <= 100)].copy()
print(f"Valid trades with parsed entry prices: {len(valid_trades):,}")

# 1. Price corridor breakdown
bins = [0, 15, 30, 45, 55, 70, 85, 100]
labels = ["1-15¢ (Extreme Discount)", "16-30¢ (Deep Discount)", "31-45¢ (Discount/Value)", "46-55¢ (Coin Flip)", "56-70¢ (Momentum Low)", "71-85¢ (Momentum High)", "86-99¢ (Favorite)"]
valid_trades["price_bin"] = pd.cut(valid_trades["entry_price_cents"], bins=bins, labels=labels)

print("\n--- PERFORMANCE BY ENTRY PRICE BUCKET ---")
bin_summary = valid_trades.groupby("price_bin", observed=False).agg(
    trades=("won", "count"),
    wins=("won", "sum"),
    win_rate=("won", "mean"),
    avg_price=("entry_price_cents", "mean"),
    total_cost=("cost_dollars", "sum"),
    total_pnl=("pnl_dollars", "sum"),
    avg_pnl_per_trade=("pnl_dollars", "mean"),
    roi=("pnl_dollars", lambda pnl: (pnl.sum() / valid_trades.loc[pnl.index, "cost_dollars"].sum()) if valid_trades.loc[pnl.index, "cost_dollars"].sum() > 0 else 0)
)
bin_summary["win_rate"] = bin_summary["win_rate"].apply(lambda x: f"{x:.1%}")
bin_summary["avg_price"] = bin_summary["avg_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
bin_summary["total_cost"] = bin_summary["total_cost"].apply(lambda x: f"${x:,.2f}")
bin_summary["total_pnl"] = bin_summary["total_pnl"].apply(lambda x: f"${x:,.2f}")
bin_summary["avg_pnl_per_trade"] = bin_summary["avg_pnl_per_trade"].apply(lambda x: f"${x:+.2f}")
bin_summary["roi"] = bin_summary["roi"].apply(lambda x: f"{x:+.1%}")
print(bin_summary.to_string())

# 2. Timeframe x Price Bin
print("\n--- 15-MINUTE MARKETS BY PRICE BUCKET ---")
df_15m = valid_trades[valid_trades["tf"] == "15m"]
m15_summary = df_15m.groupby("price_bin", observed=False).agg(
    trades=("won", "count"),
    wins=("won", "sum"),
    win_rate=("won", "mean"),
    avg_price=("entry_price_cents", "mean"),
    total_pnl=("pnl_dollars", "sum"),
    roi=("pnl_dollars", lambda pnl: (pnl.sum() / df_15m.loc[pnl.index, "cost_dollars"].sum()) if df_15m.loc[pnl.index, "cost_dollars"].sum() > 0 else 0)
)
m15_summary["win_rate"] = m15_summary["win_rate"].apply(lambda x: f"{x:.1%}")
m15_summary["avg_price"] = m15_summary["avg_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
m15_summary["total_pnl"] = m15_summary["total_pnl"].apply(lambda x: f"${x:,.2f}")
m15_summary["roi"] = m15_summary["roi"].apply(lambda x: f"{x:+.1%}")
print(m15_summary.to_string())

print("\n--- 1-HOUR MARKETS BY PRICE BUCKET ---")
df_1h = valid_trades[valid_trades["tf"] == "1h"]
h1_summary = df_1h.groupby("price_bin", observed=False).agg(
    trades=("won", "count"),
    wins=("won", "sum"),
    win_rate=("won", "mean"),
    avg_price=("entry_price_cents", "mean"),
    total_pnl=("pnl_dollars", "sum"),
    roi=("pnl_dollars", lambda pnl: (pnl.sum() / df_1h.loc[pnl.index, "cost_dollars"].sum()) if df_1h.loc[pnl.index, "cost_dollars"].sum() > 0 else 0)
)
h1_summary["win_rate"] = h1_summary["win_rate"].apply(lambda x: f"{x:.1%}")
h1_summary["avg_price"] = h1_summary["avg_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
h1_summary["total_pnl"] = h1_summary["total_pnl"].apply(lambda x: f"${x:,.2f}")
h1_summary["roi"] = h1_summary["roi"].apply(lambda x: f"{x:+.1%}")
print(h1_summary.to_string())

# 3. Entered side (YES vs NO) x Asset
print("\n--- ASSET x ENTERED SIDE PERFORMANCE ---")
asset_side = valid_trades.groupby(["sym", "entered_side"]).agg(
    trades=("won", "count"),
    win_rate=("won", "mean"),
    avg_price=("entry_price_cents", "mean"),
    total_pnl=("pnl_dollars", "sum"),
    roi=("pnl_dollars", lambda pnl: (pnl.sum() / valid_trades.loc[pnl.index, "cost_dollars"].sum()) if valid_trades.loc[pnl.index, "cost_dollars"].sum() > 0 else 0)
)
asset_side["win_rate"] = asset_side["win_rate"].apply(lambda x: f"{x:.1%}")
asset_side["avg_price"] = asset_side["avg_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
asset_side["total_pnl"] = asset_side["total_pnl"].apply(lambda x: f"${x:,.2f}")
asset_side["roi"] = asset_side["roi"].apply(lambda x: f"{x:+.1%}")
print(asset_side.to_string())
