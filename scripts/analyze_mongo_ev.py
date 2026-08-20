import os
import sys
import certifi
import pymongo
import pandas as pd
import numpy as np

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
db = client["trading_bot_db"]

print("=" * 70, flush=True)
print("1. SETTLEMENTS HISTORICAL PERFORMANCE & EV", flush=True)
print("=" * 70, flush=True)

settle_docs = list(db["settlements"].find())
print(f"Total settlements recorded: {len(settle_docs):,}", flush=True)

if settle_docs:
    df_s = pd.DataFrame(settle_docs)
    
    def get_sym(ticker):
        t = str(ticker).upper()
        for s in ["BTC", "ETH", "SOL", "XRP", "GOLD", "WTI", "INX", "NASDAQ100"]:
            if s in t:
                return s
        return "OTHER"
    df_s["sym"] = df_s["ticker"].apply(get_sym)
    
    def get_tf(ticker):
        t = str(ticker).upper()
        if "15M" in t:
            return "15m"
        if "05M" in t or "5M" in t:
            return "5m"
        return "1h"
    df_s["tf"] = df_s["ticker"].apply(get_tf)
    
    if "pnl_dollars" in df_s.columns:
        total_pnl = df_s["pnl_dollars"].sum()
        total_cost = df_s["cost_dollars"].sum() if "cost_dollars" in df_s.columns else 0
        total_rev = df_s["revenue_dollars"].sum() if "revenue_dollars" in df_s.columns else 0
        wins = (df_s["pnl_dollars"] > 0).sum()
        losses = (df_s["pnl_dollars"] < 0).sum()
        ties = (df_s["pnl_dollars"] == 0).sum()
        
        print(f"\n[Overall Settlement Stats]", flush=True)
        print(f"  Total Trades: {len(df_s):,}", flush=True)
        print(f"  Wins: {wins:,} ({wins/len(df_s):.1%}) | Losses: {losses:,} ({losses/len(df_s):.1%}) | Ties: {ties:,}", flush=True)
        print(f"  Net Realized PnL: ${total_pnl:,.2f}", flush=True)
        print(f"  Total Capital Wagered: ${total_cost:,.2f}", flush=True)
        print(f"  Total Revenue Returned: ${total_rev:,.2f}", flush=True)
        if total_cost > 0:
            print(f"  Aggregate ROI: {total_pnl/total_cost:.2%}", flush=True)
            print(f"  Average EV per Trade: ${total_pnl/len(df_s):.2f}", flush=True)
            
        print("\n--- PERFORMANCE BY ASSET (SYMBOL) ---", flush=True)
        sym_perf = df_s.groupby("sym").agg(
            trades=("pnl_dollars", "count"),
            wins=("pnl_dollars", lambda x: (x > 0).sum()),
            win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
            total_pnl=("pnl_dollars", "sum"),
            avg_pnl=("pnl_dollars", "mean"),
            total_cost=("cost_dollars", "sum") if "cost_dollars" in df_s.columns else ("pnl_dollars", lambda x: 0),
        )
        if "cost_dollars" in df_s.columns:
            sym_perf["roi"] = (sym_perf["total_pnl"] / sym_perf["total_cost"].replace(0, np.nan)).apply(lambda x: f"{x:.1%}")
        sym_perf["win_rate"] = sym_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        sym_perf["total_pnl"] = sym_perf["total_pnl"].apply(lambda x: f"${x:,.2f}")
        sym_perf["avg_pnl"] = sym_perf["avg_pnl"].apply(lambda x: f"${x:.2f}")
        print(sym_perf.to_string(), flush=True)

        print("\n--- PERFORMANCE BY TIMEFRAME ---", flush=True)
        tf_perf = df_s.groupby("tf").agg(
            trades=("pnl_dollars", "count"),
            wins=("pnl_dollars", lambda x: (x > 0).sum()),
            win_rate=("pnl_dollars", lambda x: (x > 0).mean()),
            total_pnl=("pnl_dollars", "sum"),
            avg_pnl=("pnl_dollars", "mean"),
            total_cost=("cost_dollars", "sum") if "cost_dollars" in df_s.columns else ("pnl_dollars", lambda x: 0),
        )
        if "cost_dollars" in df_s.columns:
            tf_perf["roi"] = (tf_perf["total_pnl"] / tf_perf["total_cost"].replace(0, np.nan)).apply(lambda x: f"{x:.1%}")
        tf_perf["win_rate"] = tf_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        tf_perf["total_pnl"] = tf_perf["total_pnl"].apply(lambda x: f"${x:,.2f}")
        tf_perf["avg_pnl"] = tf_perf["avg_pnl"].apply(lambda x: f"${x:.2f}")
        print(tf_perf.to_string(), flush=True)

print("\n" + "=" * 70, flush=True)
print("2. TRADE EXECUTION & STRATEGY ATTRIBUTION ANALYSIS", flush=True)
print("=" * 70, flush=True)

trade_snaps = list(db["market_snapshots"].find(
    {"trd": {"$exists": True, "$ne": None}},
    {"ts": 1, "sym": 1, "tf": 1, "tick": 1, "y": 1, "n": 1, "p": 1, "tgt": 1, "diff": 1, "pct": 1, "rem": 1, "m": 1, "trd": 1, "strk": 1, "fav": 1, "fav_maker": 1}
))
print(f"Total Trade execution snapshots found: {len(trade_snaps):,}", flush=True)

if trade_snaps:
    trade_records = []
    for s in trade_snaps:
        trd = s.get("trd") or {}
        rec = {
            "sym": s.get("sym"),
            "tf": s.get("tf"),
            "tick": s.get("tick"),
            "m": s.get("m"),
            "y": s.get("y"),
            "n": s.get("n"),
            "diff": s.get("diff"),
            "pct": s.get("pct"),
            "fav": s.get("fav"),
            "fav_maker": s.get("fav_maker"),
            "strk": s.get("strk"),
            "action": trd.get("action"),
            "side": trd.get("side"),
            "buy_price": trd.get("buy") or trd.get("entry_price_cents") or trd.get("price"),
            "regime": trd.get("regime") or trd.get("corridor"),
            "strategy_tag": trd.get("strategy_tag"),
            "order_role": trd.get("order_role"),
            "outcome": trd.get("outcome"),
            "loss_cents": trd.get("loss_cents"),
            "gain_cents": trd.get("gain_cents"),
        }
        trade_records.append(rec)
    
    df_t = pd.DataFrame(trade_records)
    
    print("\n--- ACTIONS BREAKDOWN ---", flush=True)
    print(df_t["action"].value_counts().to_string(), flush=True)
    
    print("\n--- STRATEGY TAGS ---", flush=True)
    print(df_t["strategy_tag"].fillna("unassigned").value_counts().to_string(), flush=True)

    print("\n--- REGIMES / CORRIDORS ---", flush=True)
    print(df_t["regime"].fillna("unassigned").value_counts().to_string(), flush=True)

    # Correlate trades with settlement outcomes
    settle_map = {}
    for s in settle_docs:
        tick = s.get("ticker")
        res = s.get("market_result") or s.get("result")
        pnl = s.get("pnl_dollars")
        if tick and res:
            settle_map[tick] = {
                "result": str(res).lower().strip(),
                "pnl": pnl,
                "cost": s.get("cost_dollars", 0),
                "rev": s.get("revenue_dollars", 0)
            }
            
    df_t["settle_result"] = df_t["tick"].apply(lambda t: settle_map.get(t, {}).get("result"))
    df_t["settle_pnl"] = df_t["tick"].apply(lambda t: settle_map.get(t, {}).get("pnl"))
    
    # Calculate trade win status: did the entered side match the settled outcome?
    df_buys = df_t[df_t["action"].str.contains("buy|open", case=False, na=False)].copy()
    df_buys = df_buys[df_buys["settle_result"].notnull()].copy()
    df_buys["won"] = (df_buys["side"].str.lower() == df_buys["settle_result"])
    df_buys["buy_price"] = pd.to_numeric(df_buys["buy_price"], errors="coerce")
    
    print(f"\nSettled Buy Entries with Attribution: {len(df_buys):,}", flush=True)
    if len(df_buys) > 0:
        # Calculate EV per contract
        df_buys["ev_cents"] = np.where(df_buys["won"], 100 - df_buys["buy_price"], -df_buys["buy_price"])
        
        print("\n--- EV BY ENTRY PRICE CORRIDOR ---", flush=True)
        bins = [0, 20, 35, 50, 65, 80, 100]
        labels = ["<20¢ (Deep Discount)", "20-35¢ (Discount)", "35-50¢ (Coin Flip Low)", "50-65¢ (Coin Flip High)", "65-80¢ (Momentum)", ">80¢ (Favorite)"]
        df_buys["price_corridor"] = pd.cut(df_buys["buy_price"], bins=bins, labels=labels)
        
        corr_perf = df_buys.groupby("price_corridor", observed=False).agg(
            trades=("won", "count"),
            wins=("won", "sum"),
            win_rate=("won", "mean"),
            avg_entry_price=("buy_price", "mean"),
            avg_ev_cents=("ev_cents", "mean"),
            total_ev_cents=("ev_cents", "sum"),
            roi=("ev_cents", lambda x: (x.sum() / df_buys.loc[x.index, "buy_price"].sum()) if df_buys.loc[x.index, "buy_price"].sum() > 0 else 0)
        )
        corr_perf["win_rate"] = corr_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        corr_perf["avg_entry_price"] = corr_perf["avg_entry_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
        corr_perf["avg_ev_cents"] = corr_perf["avg_ev_cents"].apply(lambda x: f"{x:+.1f}¢" if pd.notnull(x) else "N/A")
        corr_perf["roi"] = corr_perf["roi"].apply(lambda x: f"{x:+.1%}")
        print(corr_perf.to_string(), flush=True)

        print("\n--- EV BY MINUTE OF ENTRY (m) ---", flush=True)
        m_perf = df_buys.groupby("m", observed=False).agg(
            trades=("won", "count"),
            wins=("won", "sum"),
            win_rate=("won", "mean"),
            avg_entry_price=("buy_price", "mean"),
            avg_ev_cents=("ev_cents", "mean"),
            roi=("ev_cents", lambda x: (x.sum() / df_buys.loc[x.index, "buy_price"].sum()) if df_buys.loc[x.index, "buy_price"].sum() > 0 else 0)
        )
        m_perf["win_rate"] = m_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        m_perf["avg_entry_price"] = m_perf["avg_entry_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
        m_perf["avg_ev_cents"] = m_perf["avg_ev_cents"].apply(lambda x: f"{x:+.1f}¢" if pd.notnull(x) else "N/A")
        m_perf["roi"] = m_perf["roi"].apply(lambda x: f"{x:+.1%}")
        print(m_perf.to_string(), flush=True)

        print("\n--- EV BY SIDE (YES vs NO) ---", flush=True)
        side_perf = df_buys.groupby("side", observed=False).agg(
            trades=("won", "count"),
            wins=("won", "sum"),
            win_rate=("won", "mean"),
            avg_entry_price=("buy_price", "mean"),
            avg_ev_cents=("ev_cents", "mean"),
            roi=("ev_cents", lambda x: (x.sum() / df_buys.loc[x.index, "buy_price"].sum()) if df_buys.loc[x.index, "buy_price"].sum() > 0 else 0)
        )
        side_perf["win_rate"] = side_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        side_perf["avg_entry_price"] = side_perf["avg_entry_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
        side_perf["avg_ev_cents"] = side_perf["avg_ev_cents"].apply(lambda x: f"{x:+.1f}¢" if pd.notnull(x) else "N/A")
        side_perf["roi"] = side_perf["roi"].apply(lambda x: f"{x:+.1%}")
        print(side_perf.to_string(), flush=True)

        print("\n--- EV BY STRATEGY / REGIME ---", flush=True)
        strat_perf = df_buys.groupby("strategy_tag", observed=False).agg(
            trades=("won", "count"),
            wins=("won", "sum"),
            win_rate=("won", "mean"),
            avg_entry_price=("buy_price", "mean"),
            avg_ev_cents=("ev_cents", "mean"),
            roi=("ev_cents", lambda x: (x.sum() / df_buys.loc[x.index, "buy_price"].sum()) if df_buys.loc[x.index, "buy_price"].sum() > 0 else 0)
        )
        strat_perf["win_rate"] = strat_perf["win_rate"].apply(lambda x: f"{x:.1%}")
        strat_perf["avg_entry_price"] = strat_perf["avg_entry_price"].apply(lambda x: f"{x:.1f}¢" if pd.notnull(x) else "N/A")
        strat_perf["avg_ev_cents"] = strat_perf["avg_ev_cents"].apply(lambda x: f"{x:+.1f}¢" if pd.notnull(x) else "N/A")
        strat_perf["roi"] = strat_perf["roi"].apply(lambda x: f"{x:+.1%}")
        print(strat_perf.to_string(), flush=True)
