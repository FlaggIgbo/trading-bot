import os
import sys
from datetime import datetime, timezone, timedelta
import pymongo
import pandas as pd
import numpy as np

uri = os.getenv("MONGODB_URI", "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority")
client = pymongo.MongoClient(uri)
db = client['trading_bot_db']

latest_snap = db['portfolio_pnl'].find_one(sort=[('ts', -1)])
now_utc = datetime.now(timezone.utc)
latest_ts = latest_snap['ts'] if latest_snap else int(now_utc.timestamp())

start_ts_24h = latest_ts - 24 * 3600
start_ts_48h = latest_ts - 48 * 3600

print(f"================================================================================")
print(f"  QUANTITATIVE 24-HOUR MONGODB PERFORMANCE AUDIT & ROOT CAUSE ANALYSIS")
print(f"================================================================================")
print(f"Database: trading_bot_db (MongoDB Atlas)")
print(f"Audit Window: Last 24 Hours")
print(f"Start Time (UTC): {datetime.fromtimestamp(start_ts_24h, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"End Time (UTC):   {datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"================================================================================")

# 1. Portfolio PnL & Balance Trajectory
pnl_records = list(db['portfolio_pnl'].find({'ts': {'$gte': start_ts_24h}}))
df_pnl = pd.DataFrame(pnl_records)

commit_ts = datetime(2026, 8, 24, 22, 36, 55, tzinfo=timezone.utc).timestamp()

print("\n--- 1. WALLET BALANCE & PORTFOLIO TRAJECTORY ---")
if not df_pnl.empty:
    for ex in sorted(df_pnl['ex'].dropna().unique()):
        sub = df_pnl[df_pnl['ex'] == ex].sort_values('ts')
        first = sub.iloc[0]
        last = sub.iloc[-1]
        start_bal = first.get('balance_dollars', 0.0)
        end_bal = last.get('balance_dollars', 0.0)
        start_pv = first.get('portfolio_value_dollars', start_bal)
        end_pv = last.get('portfolio_value_dollars', end_bal)
        open_exp = last.get('open_exposure_dollars', 0.0)
        open_pos = last.get('open_positions_count', 0)
        
        print(f"Exchange: {ex.upper()}")
        print(f"  Cash Balance:    ${start_bal:.2f} -> ${end_bal:.2f} (Delta: ${end_bal - start_bal:+.2f})")
        print(f"  Portfolio Value: ${start_pv:.2f} -> ${end_pv:.2f} (Delta: ${end_pv - start_pv:+.2f})")
        print(f"  Current Open Positions: {open_pos} (Open Exposure: ${open_exp:.2f})")
        print(f"  Balance Range: [Min: ${sub['balance_dollars'].min():.2f}, Max: ${sub['balance_dollars'].max():.2f}]")

# 2. Fetch all Active Positions for Cross-Matching
all_active = list(db['active_positions'].find())
act_by_ticker = {}
for a in all_active:
    ticker = a.get('ticker')
    if ticker not in act_by_ticker:
        act_by_ticker[ticker] = []
    act_by_ticker[ticker].append(a)

# 3. Settlements Processing
all_settlements = list(db['settlements'].find())
parsed_settlements = []
for s in all_settlements:
    st = s.get('settled_time')
    ts = None
    if isinstance(st, str):
        try:
            dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
            ts = dt.timestamp()
        except:
            pass
    elif isinstance(st, (int, float)):
        ts = float(st)
    elif isinstance(st, datetime):
        ts = st.replace(tzinfo=timezone.utc).timestamp()
    elif '_id' in s:
        ts = s['_id'].generation_time.timestamp()
    
    s['parsed_ts'] = ts
    s['parsed_dt'] = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
    
    ticker = s.get('ticker')
    matched_acts = act_by_ticker.get(ticker, [])
    s['matched_act_count'] = len(matched_acts)
    s['strategy_tags'] = list(set([a.get('strategy_tag', 'unspecified') for a in matched_acts]))
    s['order_roles'] = list(set([a.get('order_role', 'unspecified') for a in matched_acts]))
    s['sides_entered'] = list(set([a.get('side', 'unknown') for a in matched_acts]))
    s['entry_prices'] = [a.get('entry_price_cents', 0) for a in matched_acts]
    s['spread_saved'] = [a.get('spread_saved_cents', 0) for a in matched_acts]

    parsed_settlements.append(s)

df_s = pd.DataFrame(parsed_settlements)
df_24h = df_s[df_s['parsed_ts'] >= start_ts_24h].copy()

print(f"\n--- 2. OVERALL SETTLED TRADES SUMMARY (LAST 24H) ---")
print(f"Total Settled Contracts: {len(df_24h)}")

if not df_24h.empty:
    wins = (df_24h['pnl_dollars'] > 0).sum()
    losses = (df_24h['pnl_dollars'] < 0).sum()
    ties = (df_24h['pnl_dollars'] == 0).sum()
    total_cost = df_24h['cost_dollars'].sum()
    total_rev = df_24h['revenue_dollars'].sum()
    total_pnl = df_24h['pnl_dollars'].sum()
    roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
    win_rate = (wins / len(df_24h) * 100) if len(df_24h) > 0 else 0.0
    avg_ev_cents = (total_pnl * 100) / len(df_24h)

    print(f"Win / Loss Record:     {wins} Wins / {losses} Losses / {ties} Ties")
    print(f"Win Rate:              {win_rate:.1f}%")
    print(f"Total Capital Wagered: ${total_cost:.2f}")
    print(f"Total Gross Revenue:   ${total_rev:.2f}")
    print(f"Net Realized PnL:      ${total_pnl:+.2f}")
    print(f"Realized ROI:          {roi:+.2f}%")
    print(f"Avg Realized EV:       {avg_ev_cents:+.2f}¢ per contract")

    # Inferred Asset from Ticker if UNKNOWN
    def infer_asset(ticker, sym):
        if sym and sym != 'UNKNOWN':
            return sym
        if not isinstance(ticker, str):
            return 'UNKNOWN'
        t = ticker.upper()
        if 'BTC' in t: return 'BTC'
        if 'ETH' in t: return 'ETH'
        if 'SOL' in t: return 'SOL'
        if 'GOLD' in t: return 'GOLD'
        if 'SILVER' in t: return 'SILVER'
        if 'WTI' in t or 'CRUDE' in t: return 'WTI'
        if 'DOGE' in t: return 'DOGE'
        if 'XRP' in t: return 'XRP'
        if 'NEAR' in t: return 'NEAR'
        if 'ZEC' in t: return 'ZEC'
        if 'BNB' in t: return 'BNB'
        if 'INX' in t or 'SPX' in t: return 'INX'
        return 'OTHER'

    df_24h['clean_sym'] = df_24h.apply(lambda r: infer_asset(r['ticker'], r['sym']), axis=1)

    print("\n--- 3. DETAILED PERFORMANCE BY ASSET / COMMODITY / CRYPTO ---")
    sym_group = df_24h.groupby('clean_sym').agg(
        trades=('pnl_dollars', 'count'),
        wins=('pnl_dollars', lambda x: (x > 0).sum()),
        losses=('pnl_dollars', lambda x: (x < 0).sum()),
        total_cost=('cost_dollars', 'sum'),
        total_pnl=('pnl_dollars', 'sum')
    ).reset_index()
    sym_group['win_rate_%'] = (sym_group['wins'] / sym_group['trades'] * 100).round(1)
    sym_group['roi_%'] = (sym_group['total_pnl'] / sym_group['total_cost'] * 100).round(1)
    sym_group['ev_cents'] = ((sym_group['total_pnl'] * 100) / sym_group['trades']).round(2)
    sym_group = sym_group.sort_values('total_pnl')
    print(sym_group.to_string(index=False))

    print("\n--- 4. PERFORMANCE BY STRATEGY TAG / EXECUTION ROLE ---")
    def categorize_strategy(tags):
        if not tags: return 'Unspecified / Legacy'
        t_str = ', '.join(tags)
        if 'endgame_favorite_maker' in t_str: return 'Peak 3 (Endgame Favorite Maker)'
        if 'macro_trend_momentum' in t_str: return 'Peak 2 (Macro Trend Momentum)'
        if 'asymmetric_discount' in t_str: return 'Peak 1 (Asymmetric Discount)'
        return f"Other ({t_str})"

    df_24h['strat_category'] = df_24h['strategy_tags'].apply(categorize_strategy)
    strat_group = df_24h.groupby('strat_category').agg(
        trades=('pnl_dollars', 'count'),
        wins=('pnl_dollars', lambda x: (x > 0).sum()),
        losses=('pnl_dollars', lambda x: (x < 0).sum()),
        total_cost=('cost_dollars', 'sum'),
        total_pnl=('pnl_dollars', 'sum')
    ).reset_index()
    strat_group['win_rate_%'] = (strat_group['wins'] / strat_group['trades'] * 100).round(1)
    strat_group['roi_%'] = (strat_group['total_pnl'] / strat_group['total_cost'] * 100).round(1)
    strat_group['ev_cents'] = ((strat_group['total_pnl'] * 100) / strat_group['trades']).round(2)
    strat_group = strat_group.sort_values('total_pnl')
    print(strat_group.to_string(index=False))

    print("\n--- 5. PERFORMANCE BY TIMEFRAME ---")
    tf_group = df_24h.groupby('tf').agg(
        trades=('pnl_dollars', 'count'),
        wins=('pnl_dollars', lambda x: (x > 0).sum()),
        losses=('pnl_dollars', lambda x: (x < 0).sum()),
        total_cost=('cost_dollars', 'sum'),
        total_pnl=('pnl_dollars', 'sum')
    ).reset_index()
    tf_group['win_rate_%'] = (tf_group['wins'] / tf_group['trades'] * 100).round(1)
    tf_group['roi_%'] = (tf_group['total_pnl'] / tf_group['total_cost'] * 100).round(1)
    tf_group['ev_cents'] = ((tf_group['total_pnl'] * 100) / tf_group['trades']).round(2)
    print(tf_group.to_string(index=False))

    print("\n--- 6. FORENSIC LOSS AUDIT: ALL 24 LOSING TRADES IN LAST 24H ---")
    losing = df_24h[df_24h['pnl_dollars'] < 0].sort_values('pnl_dollars')
    for idx, r in losing.iterrows():
        sides = ', '.join(r['sides_entered']) if r['sides_entered'] else 'n/a'
        strats = ', '.join(r['strategy_tags']) if r['strategy_tags'] else 'n/a'
        print(f"[{r['parsed_dt'].strftime('%m-%d %H:%M')}] Ticker: {r['ticker']:<32} | Sym: {r['clean_sym']:<6} | TF: {r['tf']:<3} | MarketRes: {r['market_result']:<3} | BoughtSide: {sides:<3} | Cost: ${r['cost_dollars']:.2f} | PnL: ${r['pnl_dollars']:+.2f} | Strat: {strats}")

    print("\n--- 7. WHY DID COSTS EXCEED $1.00 ON SOME TRADES? ---")
    multi_cost = df_24h[df_24h['cost_dollars'] > 1.0].sort_values('cost_dollars', ascending=False)
    print(f"Found {len(multi_cost)} settled trades where cost > $1.00 (Total PnL on these: ${multi_cost['pnl_dollars'].sum():+.2f})")
    for idx, r in multi_cost.iterrows():
        print(f"  Ticker: {r['ticker']} | Cost: ${r['cost_dollars']:.2f} | Rev: ${r['revenue_dollars']:.2f} | PnL: ${r['pnl_dollars']:+.2f} | YES Count: {r.get('yes_count')} | NO Count: {r.get('no_count')} | Market Res: {r.get('market_result')}")

    print("\n--- 8. CORRELATION & HOURLY BREAKDOWN OF LOSSES ---")
    df_24h['settle_hour'] = df_24h['parsed_dt'].dt.hour
    hourly = df_24h.groupby('settle_hour').agg(
        trades=('pnl_dollars', 'count'),
        wins=('pnl_dollars', lambda x: (x > 0).sum()),
        losses=('pnl_dollars', lambda x: (x < 0).sum()),
        total_pnl=('pnl_dollars', 'sum')
    ).reset_index()
    hourly['win_rate_%'] = (hourly['wins'] / hourly['trades'] * 100).round(1)
    hourly = hourly.sort_values('total_pnl')
    print(hourly.to_string(index=False))

    print("\n--- 9. PERFORMANCE BEFORE VS AFTER LATEST COMMIT (eae0525 @ Aug 24 22:36 UTC) ---")
    df_24h['commit_period'] = df_24h['parsed_ts'].apply(lambda t: 'AFTER_LATEST_COMMIT (Aug 24 22:36+)' if t >= commit_ts else 'BEFORE_LATEST_COMMIT (Aug 24 16:48 - 22:36)')
    commit_group = df_24h.groupby('commit_period').agg(
        trades=('pnl_dollars', 'count'),
        wins=('pnl_dollars', lambda x: (x > 0).sum()),
        losses=('pnl_dollars', lambda x: (x < 0).sum()),
        total_cost=('cost_dollars', 'sum'),
        total_pnl=('pnl_dollars', 'sum')
    ).reset_index()
    commit_group['win_rate_%'] = (commit_group['wins'] / commit_group['trades'] * 100).round(1)
    commit_group['roi_%'] = (commit_group['total_pnl'] / commit_group['total_cost'] * 100).round(1)
    commit_group['ev_cents'] = ((commit_group['total_pnl'] * 100) / commit_group['trades']).round(2)
    print(commit_group.to_string(index=False))

