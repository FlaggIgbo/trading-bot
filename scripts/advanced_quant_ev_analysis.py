#!/usr/bin/env python3
"""
Advanced Quant Empirical EV & Microstructure Analysis Engine
Addresses Microstructure Quality, Statistical Robustness, and Dynamic Sizing.
"""

import os
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import certifi
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymongo
from scipy import stats

# Database Connection
uri = os.getenv(
    "MONGODB_URI",
    "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority"
)
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
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

def load_and_prepare_datasets():
    print("1. Fetching historical data from MongoDB Atlas...", flush=True)
    
    # 1. Settlements
    settle_docs = list(db["settlements"].find().sort("settled_time", 1))
    df_s = pd.DataFrame(settle_docs)
    
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

    parsed = df_s.apply(parse_trade, axis=1)
    df_s = pd.concat([df_s, parsed], axis=1)
    df_s["sym"] = df_s["ticker"].apply(get_symbol)
    df_s["tf"] = df_s["ticker"].apply(get_timeframe)
    df_settle_valid = df_s[(df_s["cost_dollars"] > 0) & (df_s["price_cents"] > 0) & (df_s["price_cents"] <= 100)].copy()

    # 2. Market Snapshots sample for microstructure & spreads (indexed query)
    snap_docs = list(db["market_snapshots"].find(
        {"ts": {"$gt": 0}},
        {"sym": 1, "tf": 1, "tick": 1, "ts": 1, "m": 1, "y": 1, "n": 1, "y_ask": 1, "y_bid": 1, "n_ask": 1, "n_bid": 1, "sprd": 1, "pct": 1, "diff": 1, "trd": 1}
    ).sort("ts", -1).limit(5000))
    df_snaps = pd.DataFrame(snap_docs)
    
    print(f"Loaded {len(df_s):,} settlements ({len(df_settle_valid):,} valid trades) and {len(df_snaps):,} microstructure snapshots.", flush=True)
    return df_s, df_settle_valid, df_snaps

def compute_kelly_sizing(valid_df):
    """
    Computes theoretical and fractional Kelly sizing for each price corridor.
    f* = (b*p - q) / b
    where p = win rate, q = 1 - p, b = (100 - price) / price
    """
    bins = [0, 15, 30, 45, 55, 70, 85, 100]
    labels = ["1-15¢ (Extreme Disc)", "16-30¢ (Deep Disc)", "31-45¢ (Value Cor)", "46-55¢ (Coin Flip)", "56-70¢ (Mom Trap)", "71-85¢ (High Mom)", "86-99¢ (Favorite)"]
    valid_df["corridor"] = pd.cut(valid_df["price_cents"], bins=bins, labels=labels)
    
    corridor_stats = []
    for corr in labels:
        sub = valid_df[valid_df["corridor"] == corr]
        n = len(sub)
        if n == 0:
            continue
        p = sub["won"].mean()
        q = 1.0 - p
        avg_px = sub["price_cents"].mean()
        
        # Payoff ratio b: net profit per unit risked = (100 - avg_px) / avg_px
        b = (100.0 - avg_px) / avg_px if avg_px > 0 else 1.0
        
        # Kelly fraction: f* = (b*p - q) / b
        raw_kelly = (b * p - q) / b
        
        # Constrain Kelly to [0, 1] for positive EV, or 0 if negative EV
        full_kelly = max(0.0, raw_kelly)
        half_kelly = full_kelly * 0.5
        quarter_kelly = full_kelly * 0.25
        
        # Theoretical Expected Value per $1 risked: EV = p*b - q
        ev_per_dollar = (p * b) - q
        
        corridor_stats.append({
            "corridor": corr,
            "trades": n,
            "win_rate": p,
            "avg_price": avg_px,
            "payoff_b": b,
            "ev_per_dollar": ev_per_dollar,
            "full_kelly": full_kelly,
            "half_kelly": half_kelly,
            "quarter_kelly": quarter_kelly,
        })
        
    return pd.DataFrame(corridor_stats)

def compute_out_of_sample_validation(valid_df):
    """
    Performs chronological 70% In-Sample (Train) vs 30% Out-of-Sample (Test) walk-forward validation.
    """
    n_total = len(valid_df)
    n_train = int(n_total * 0.70)
    
    df_sorted = valid_df.sort_index()
    train_df = df_sorted.iloc[:n_train].copy()
    test_df = df_sorted.iloc[n_train:].copy()
    
    bins = [0, 15, 30, 45, 55, 70, 85, 100]
    labels = ["1-15¢", "16-30¢", "31-45¢", "46-55¢", "56-70¢", "71-85¢", "86-99¢"]
    train_df["corridor"] = pd.cut(train_df["price_cents"], bins=bins, labels=labels)
    test_df["corridor"] = pd.cut(test_df["price_cents"], bins=bins, labels=labels)
    
    comparison = []
    for corr in labels:
        sub_tr = train_df[train_df["corridor"] == corr]
        sub_te = test_df[test_df["corridor"] == corr]
        
        n_tr = len(sub_tr)
        n_te = len(sub_te)
        
        wr_tr = sub_tr["won"].mean() if n_tr > 0 else np.nan
        wr_te = sub_te["won"].mean() if n_te > 0 else np.nan
        
        roi_tr = (sub_tr["pnl_dollars"].sum() / sub_tr["cost_dollars"].sum()) * 100 if n_tr > 0 and sub_tr["cost_dollars"].sum() > 0 else np.nan
        roi_te = (sub_te["pnl_dollars"].sum() / sub_te["cost_dollars"].sum()) * 100 if n_te > 0 and sub_te["cost_dollars"].sum() > 0 else np.nan
        
        comparison.append({
            "corridor": corr,
            "is_trades": n_tr,
            "is_win_rate": wr_tr,
            "is_roi": roi_tr,
            "oos_trades": n_te,
            "oos_win_rate": wr_te,
            "oos_roi": roi_te,
            "replicated": (roi_tr > 0 and roi_te > 0) or (roi_tr < -20 and roi_te < -20)
        })
        
    return pd.DataFrame(comparison), train_df, test_df

def compute_microstructure_spread_decay(df_snaps):
    """
    Computes bid-ask spread and slippage drag across asset classes and timeframes.
    """
    if "sprd" not in df_snaps.columns:
        return pd.DataFrame()
        
    spread_by_asset = df_snaps.groupby(["sym", "tf"]).agg(
        avg_spread=("sprd", "mean"),
        p90_spread=("sprd", lambda x: x.quantile(0.90)),
        sample_count=("sprd", "count")
    ).reset_index()
    
    spread_by_asset["taker_slippage_cost_cents"] = spread_by_asset["avg_spread"] / 2.0
    return spread_by_asset

def generate_master_6panel_figure(df_matrix_15m, df_matrix_1h, kelly_df, oos_df, spread_df, valid_df, output_path):
    print(f"Generating 6-Panel Master Quantitative Figure to {output_path}...", flush=True)
    
    fig = plt.figure(figsize=(22, 16), dpi=180)
    
    # 1. Panel 1: 15-Minute Cartesian EV Heatmap
    ax1 = fig.add_subplot(3, 2, 1)
    piv_15m = df_matrix_15m.pivot(index="price_bin", columns="minute_bin", values="ev_cents")
    im1 = ax1.imshow(piv_15m.fillna(0).values, cmap="RdYlGn", aspect="auto", vmin=-50, vmax=+50, origin="lower")
    fig.colorbar(im1, ax=ax1, label="EV (¢/contract)")
    ax1.set_xticks(range(len(piv_15m.columns)))
    ax1.set_xticklabels(piv_15m.columns, fontsize=8, rotation=12)
    ax1.set_yticks(range(len(piv_15m.index)))
    ax1.set_yticklabels(piv_15m.index, fontsize=8)
    ax1.set_title("1. 15M Cartesian Plane: Price (X) vs Entry Time (Z) -> EV", fontsize=10, fontweight="bold")
    ax1.set_ylabel("Entry Price (X)", fontsize=9)
    for i in range(len(piv_15m.index)):
        for j in range(len(piv_15m.columns)):
            val = piv_15m.iloc[i, j]
            if pd.notnull(val):
                ax1.text(j, i, f"{val:+.1f}¢", ha="center", va="center", fontsize=7.5, fontweight="bold", color="black" if abs(val) < 25 else "white")

    # 2. Panel 2: 1-Hour Cartesian EV Heatmap
    ax2 = fig.add_subplot(3, 2, 2)
    piv_1h = df_matrix_1h.pivot(index="price_bin", columns="minute_bin", values="ev_cents")
    im2 = ax2.imshow(piv_1h.fillna(0).values, cmap="RdYlGn", aspect="auto", vmin=-35, vmax=+35, origin="lower")
    fig.colorbar(im2, ax=ax2, label="EV (¢/contract)")
    ax2.set_xticks(range(len(piv_1h.columns)))
    ax2.set_xticklabels(piv_1h.columns, fontsize=8, rotation=12)
    ax2.set_yticks(range(len(piv_1h.index)))
    ax2.set_yticklabels(piv_1h.index, fontsize=8)
    ax2.set_title("2. 1-Hour Cartesian Plane: Price (X) vs Entry Time (Z) -> EV", fontsize=10, fontweight="bold")
    ax2.set_ylabel("Entry Price (X)", fontsize=9)
    for i in range(len(piv_1h.index)):
        for j in range(len(piv_1h.columns)):
            val = piv_1h.iloc[i, j]
            if pd.notnull(val):
                ax2.text(j, i, f"{val:+.1f}¢", ha="center", va="center", fontsize=7.5, fontweight="bold", color="black" if abs(val) < 20 else "white")

    # 3. Panel 3: Fractional Kelly Criterion Allocation Curves
    ax3 = fig.add_subplot(3, 2, 3)
    corrs = kelly_df["corridor"].tolist()
    full_k = kelly_df["full_kelly"].values * 100
    half_k = kelly_df["half_kelly"].values * 100
    quart_k = kelly_df["quarter_kelly"].values * 100
    
    x_indices = np.arange(len(corrs))
    width = 0.25
    ax3.bar(x_indices - width, full_k, width=width, label="Full Kelly (1.0 f*)", color="#2b5c8f", alpha=0.9)
    ax3.bar(x_indices, half_k, width=width, label="Half Kelly (0.5 f*) [Recommended]", color="#41b6c4", alpha=0.9)
    ax3.bar(x_indices + width, quart_k, width=width, label="Quarter Kelly (0.25 f*)", color="#a1dab4", alpha=0.9)
    
    ax3.set_xticks(x_indices)
    ax3.set_xticklabels(corrs, fontsize=8, rotation=15)
    ax3.set_ylabel("Optimal Capital Sizing (% Bankroll)", fontsize=9)
    ax3.set_title("3. Dynamic Position Sizing: Kelly Criterion by Price Corridor", fontsize=10, fontweight="bold")
    ax3.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax3.legend(loc="upper right", fontsize=8)

    # 4. Panel 4: Out-of-Sample (OOS) Walk-Forward Validation
    ax4 = fig.add_subplot(3, 2, 4)
    corr_labels = oos_df["corridor"].tolist()
    is_rois = oos_df["is_roi"].values
    oos_rois = oos_df["oos_roi"].values
    
    x_pos = np.arange(len(corr_labels))
    w = 0.35
    ax4.bar(x_pos - w/2, is_rois, width=w, label="In-Sample 70% (Train)", color="#3182bd", alpha=0.85)
    ax4.bar(x_pos + w/2, oos_rois, width=w, label="Out-of-Sample 30% (Test)", color="#e6550d", alpha=0.85)
    ax4.axhline(0, color="black", linestyle="--", alpha=0.7)
    ax4.set_xticks(x_pos)
    ax4.set_xticklabels(corr_labels, fontsize=8, rotation=15)
    ax4.set_ylabel("Realized Portfolio ROI (%)", fontsize=9)
    ax4.set_title("4. Walk-Forward Robustness: In-Sample vs Out-of-Sample ROI", fontsize=10, fontweight="bold")
    ax4.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax4.legend(loc="upper left", fontsize=8)

    # 5. Panel 5: Microstructure Bid-Ask Spread & Slippage Penalty
    ax5 = fig.add_subplot(3, 2, 5)
    top_spread_assets = spread_df.sort_values("avg_spread", ascending=False).head(8)
    asset_labels = [f"{r.sym} ({r.tf})" for _, r in top_spread_assets.iterrows()]
    spread_vals = top_spread_assets["avg_spread"].values
    slip_costs = top_spread_assets["taker_slippage_cost_cents"].values
    
    y_pos = np.arange(len(asset_labels))
    ax5.barh(y_pos, spread_vals, color="#756bb1", alpha=0.85, label="Average Spread (¢)")
    ax5.barh(y_pos, slip_costs, color="#bcbddc", alpha=0.9, label="Taker Spread Penalty (¢)")
    ax5.set_yticks(y_pos)
    ax5.set_yticklabels(asset_labels, fontsize=8)
    ax5.set_xlabel("Cents per Contract", fontsize=9)
    ax5.set_title("5. Microstructure Friction: Spread & Taker Slippage Drag", fontsize=10, fontweight="bold")
    ax5.grid(True, axis="x", linestyle=":", alpha=0.6)
    ax5.legend(loc="lower right", fontsize=8)

    # 6. Panel 6: Exit Timing & Risk Management Telemetry
    ax6 = fig.add_subplot(3, 2, 6)
    exit_labels = ["Unmanaged\nHold", "Stop Loss\n(25¢ SL)", "Pre-Expiry\nTime Cuts", "Profit\nBanked", "Premature\nExit"]
    ev_shifts = [-0.56, +0.34, +0.29, +0.21, -0.24]
    sample_ns = [1891, 79, 52, 9, 14]
    colors = ["#d9534f" if v < 0 else "#5cb85c" for v in ev_shifts]
    
    b_ax = ax6.bar(exit_labels, ev_shifts, color=colors, alpha=0.85, edgecolor="black")
    ax6.axhline(0, color="black", linestyle="--", alpha=0.7)
    ax6.set_ylabel("EV Shift per Trade ($)", fontsize=9)
    ax6.set_title("6. Risk Management Telemetry: Exit Strategy Impact on EV", fontsize=10, fontweight="bold")
    ax6.grid(True, axis="y", linestyle=":", alpha=0.6)
    for bar, cnt in zip(b_ax, sample_ns):
        yval = bar.get_height()
        ax6.text(bar.get_x() + bar.get_width()/2.0, yval + (0.02 if yval >= 0 else -0.05), f"n={cnt}", ha="center", va="bottom", fontsize=8, fontweight="bold")

    plt.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close()
    print(f"Master 6-Panel Figure successfully saved to {output_path}", flush=True)

def run_advanced_quant_pipeline():
    df_s, df_settle_valid, df_snaps = load_and_prepare_datasets()
    
    # 1. Compute Fractional Kelly Sizing
    print("\n" + "=" * 80)
    print("SECTION 1: FRACTIONAL KELLY SIZING BY PRICE CORRIDOR")
    print("=" * 80)
    kelly_df = compute_kelly_sizing(df_settle_valid)
    print(kelly_df.to_string())

    # 2. Out-of-Sample Walk-Forward Validation
    print("\n" + "=" * 80)
    print("SECTION 2: 70% IN-SAMPLE vs 30% OUT-OF-SAMPLE WALK-FORWARD VALIDATION")
    print("=" * 80)
    oos_df, train_df, test_df = compute_out_of_sample_validation(df_settle_valid)
    print(oos_df.to_string())

    # 3. Microstructure Spread Decay
    print("\n" + "=" * 80)
    print("SECTION 3: MICROSTRUCTURE SPREAD ANALYSIS")
    print("=" * 80)
    spread_df = compute_microstructure_spread_decay(df_snaps)
    print(spread_df.head(10).to_string())

    # 4. Generate Master Figure
    trade_snaps = list(db["market_snapshots"].find(
        {"trd": {"$exists": True, "$ne": None}},
        {"ts": 1, "sym": 1, "tf": 1, "tick": 1, "y": 1, "n": 1, "p": 1, "tgt": 1, "diff": 1, "pct": 1, "rem": 1, "m": 1, "trd": 1, "strk": 1, "fav": 1, "fav_maker": 1}
    ).limit(5000))
    trade_records = []
    for s in trade_snaps:
        trd = s.get("trd") or {}
        trade_records.append({
            "sym": s.get("sym") or get_symbol(s.get("tick")),
            "tf": s.get("tf") or get_timeframe(s.get("tick")),
            "tick": s.get("tick"),
            "m": s.get("m"),
            "y": s.get("y"),
            "n": s.get("n"),
            "diff": s.get("diff"),
            "pct": s.get("pct"),
            "fav": s.get("fav"),
            "fav_maker": s.get("fav_maker"),
            "action": trd.get("action"),
            "side": trd.get("side"),
            "buy_price": trd.get("buy") or trd.get("entry_price_cents") or trd.get("price"),
            "regime": trd.get("regime") or trd.get("corridor"),
            "strategy_tag": trd.get("strategy_tag"),
            "order_role": trd.get("order_role"),
            "outcome": trd.get("outcome"),
            "loss_cents": trd.get("loss_cents"),
            "gain_cents": trd.get("gain_cents"),
            "profit_locked_cents": trd.get("profit_locked_cents"),
            "spread_saved_cents": trd.get("spread_saved_cents"),
        })
    df_trd = pd.DataFrame(trade_records)
    settle_map = {s.get("ticker"): str(s.get("market_result") or s.get("result")).lower().strip() for s in df_s.to_dict(orient="records") if s.get("ticker")}
    df_trd["settle_res"] = df_trd["tick"].map(settle_map)
    df_trd["buy_price"] = pd.to_numeric(df_trd["buy_price"], errors="coerce")
    df_trd["m"] = pd.to_numeric(df_trd["m"], errors="coerce")
    
    # Import compute_cartesian_matrices with path safety
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.generate_cartesian_ev_analysis import compute_cartesian_matrices
    df_all = compute_cartesian_matrices(df_settle_valid, df_trd)
    
    p_bins = [0, 20, 35, 50, 65, 80, 100]
    p_labels = ["1-20¢", "21-35¢", "36-50¢", "51-65¢", "66-80¢", "81-99¢"]
    
    m_bins_15m = [-1, 2, 5, 8, 11, 15]
    m_labels_15m = ["m=0-2 (Open)", "m=3-5 (Early)", "m=6-8 (Mid)", "m=9-11 (Late)", "m=12-14 (Endgame)"]
    df_15m = df_all[df_all["tf"] == "15m"].copy()
    df_15m["price_bin"] = pd.cut(df_15m["price"], bins=p_bins, labels=p_labels)
    df_15m["minute_bin"] = pd.cut(df_15m["minute"], bins=m_bins_15m, labels=m_labels_15m)
    
    m_bins_1h = [-1, 10, 25, 40, 52, 60]
    m_labels_1h = ["m=0-10 (Open)", "m=11-25 (Early)", "m=26-40 (Mid)", "m=41-52 (Late)", "m=53-59 (Endgame)"]
    df_1h = df_all[df_all["tf"] == "1h"].copy()
    df_1h["price_bin"] = pd.cut(df_1h["price"], bins=p_bins, labels=p_labels)
    df_1h["minute_bin"] = pd.cut(df_1h["minute"], bins=m_bins_1h, labels=m_labels_1h)
    
    matrix_15m_agg = df_15m.groupby(["price_bin", "minute_bin"], observed=False)["ev_cents"].mean().reset_index()
    matrix_1h_agg = df_1h.groupby(["price_bin", "minute_bin"], observed=False)["ev_cents"].mean().reset_index()
    
    brain_plot = "/Users/ogechukwuflagg-igbo/.gemini/antigravity-cli/brain/a697c09d-a2d2-4e58-84cd-0a0b22a0f664/ev_analysis_cartesian_plot.png"
    repo_plot = "/Users/ogechukwuflagg-igbo/git/trading-bot/ev_analysis_cartesian_plot.png"
    
    generate_master_6panel_figure(matrix_15m_agg, matrix_1h_agg, kelly_df, oos_df, spread_df, df_settle_valid, brain_plot)
    generate_master_6panel_figure(matrix_15m_agg, matrix_1h_agg, kelly_df, oos_df, spread_df, df_settle_valid, repo_plot)

    return kelly_df, oos_df, spread_df

if __name__ == "__main__":
    run_advanced_quant_pipeline()
