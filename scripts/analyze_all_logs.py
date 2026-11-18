#!/usr/bin/env python3
"""
Comprehensive Analysis of All Trading Bot Logs and SQLite Databases
"""
import glob
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

def analyze_databases():
    strat_data = defaultdict(lambda: {
        'total_trades': 0,
        'wins': 0,
        'losses': 0,
        'ties': 0,
        'pnl_cents': 0,
        'demo_trades': 0,
        'prod_trades': 0,
        'demo_pnl': 0,
        'prod_pnl': 0,
        'db_files': set()
    })

    db_files = sorted(glob.glob(str(ROOT_DIR / '**/*.db'), recursive=True))

    for path in db_files:
        if 'backtest.db' in path:
            continue
        try:
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='positions'")
            if not c.fetchone():
                conn.close()
                continue
            
            file_base = os.path.basename(path)
            is_prod = 'prod' in file_base.lower() or 'production' in file_base.lower()

            c.execute("SELECT strategy, status, pnl_cents, environment FROM positions WHERE status='SETTLED'")
            rows = c.fetchall()
            for s_name, status, pnl, env in rows:
                pnl = pnl or 0
                d = strat_data[s_name]
                d['total_trades'] += 1
                if pnl > 0:
                    d['wins'] += 1
                elif pnl < 0:
                    d['losses'] += 1
                else:
                    d['ties'] += 1
                d['pnl_cents'] += pnl
                d['db_files'].add(os.path.relpath(path, ROOT_DIR))

                env_str = str(env).lower() if env else ('prod' if is_prod else 'demo')
                if 'prod' in env_str:
                    d['prod_trades'] += 1
                    d['prod_pnl'] += pnl
                else:
                    d['demo_trades'] += 1
                    d['demo_pnl'] += pnl
            conn.close()
        except Exception:
            pass
    return strat_data


def analyze_log_files():
    """Extract settlement events and performance metrics directly from all .log files."""
    log_files = sorted(glob.glob(str(ROOT_DIR / '**/*.log'), recursive=True))
    log_strats = defaultdict(lambda: {
        'settled': 0,
        'wins': 0,
        'losses': 0,
        'ties': 0,
        'pnl_cents': 0,
        'stop_losses': 0,
        'take_profits': 0,
    })

    settle_pattern = re.compile(r"\[settlement\] Settled\s+([\w-]+)\s+\((YES|NO)\):\s+result=(YES|NO),\s+PnL=(-?\d+)\s+cents")
    post_settle_pattern = re.compile(r"\[(.*?)\]\s+Settled\s+([\w-]+)\s+\((YES|NO)\):\s+result=(YES|NO),\s+PnL=(-?\d+)c")
    sl_pattern = re.compile(r"\[order_stop_loss\]|\[STOP LOSS\]")
    tp_pattern = re.compile(r"\[order_take_profit\]|\[TAKE PROFIT\]")

    for path in log_files:
        rel = os.path.relpath(path, ROOT_DIR)
        strat_hint = os.path.basename(path).replace("trading_bot_", "").replace(".log", "")
        
        # Parallel run logs contain tags like [last_9 (demo, SL)]
        is_parallel = "parallel_run" in os.path.basename(path)

        try:
            with open(path, "r", errors="ignore") as f:
                for line in f:
                    tag = strat_hint
                    if is_parallel:
                        m_tag = re.match(r"^\[(.*?)\]", line)
                        if m_tag:
                            tag = m_tag.group(1)
                    
                    # Normalize tag to extract strategy name
                    strat_name = tag.split()[0].split(":")[0].replace("_demo", "").replace("_prod", "")
                    
                    m = settle_pattern.search(line)
                    if m:
                        pnl = int(m.group(4))
                        s = log_strats[strat_name]
                        s['settled'] += 1
                        if pnl > 0:
                            s['wins'] += 1
                        elif pnl < 0:
                            s['losses'] += 1
                        else:
                            s['ties'] += 1
                        s['pnl_cents'] += pnl
                    else:
                        m2 = post_settle_pattern.search(line)
                        if m2:
                            pnl = int(m2.group(5))
                            s = log_strats[strat_name]
                            s['settled'] += 1
                            if pnl > 0:
                                s['wins'] += 1
                            elif pnl < 0:
                                s['losses'] += 1
                            else:
                                s['ties'] += 1
                            s['pnl_cents'] += pnl

                    if sl_pattern.search(line):
                        log_strats[strat_name]['stop_losses'] += 1
                    if tp_pattern.search(line):
                        log_strats[strat_name]['take_profits'] += 1
        except Exception:
            pass

    return log_strats


if __name__ == "__main__":
    db_results = analyze_databases()
    log_results = analyze_log_files()

    print("=" * 95)
    print("ALL TIME STRATEGY PERFORMANCE FROM SQLITE DATABASES")
    print("=" * 95)
    header = f"{'Strategy':<20} | {'Trades':<8} | {'Wins':<6} | {'Losses':<6} | {'Win Rate':<10} | {'Net PnL':<10} | {'Avg/Trade':<10} | {'Prod Trades/PnL':<18}"
    print(header)
    print("-" * len(header))

    sorted_db = sorted(db_results.items(), key=lambda x: x[1]['pnl_cents'], reverse=True)
    for s, d in sorted_db:
        wr = (d['wins'] / d['total_trades'] * 100) if d['total_trades'] else 0
        avg = (d['pnl_cents'] / d['total_trades']) if d['total_trades'] else 0
        pnl_str = f"${d['pnl_cents'] / 100:+.2f}"
        avg_str = f"{avg:+.1f}c"
        prod_info = f"{d['prod_trades']} (${d['prod_pnl']/100:+.2f})"
        print(f"{s:<20} | {d['total_trades']:8d} | {d['wins']:6d} | {d['losses']:6d} | {wr:9.1f}% | {pnl_str:>10} | {avg_str:>10} | {prod_info:<18}")

    print("\n" + "=" * 95)
    print("ALL TIME STRATEGY PERFORMANCE LOGGED IN .LOG FILES (Log-Level Settlements & Events)")
    print("=" * 95)
    header2 = f"{'Strategy / Tag':<25} | {'Settled':<8} | {'Wins':<6} | {'Losses':<6} | {'Win Rate':<10} | {'Net PnL':<10} | {'StopLoss':<9} | {'TakeProfit':<10}"
    print(header2)
    print("-" * len(header2))

    sorted_logs = sorted(log_results.items(), key=lambda x: x[1]['pnl_cents'], reverse=True)
    for s, d in sorted_logs:
        if d['settled'] == 0 and d['stop_losses'] == 0:
            continue
        wr = (d['wins'] / d['settled'] * 100) if d['settled'] else 0
        pnl_str = f"${d['pnl_cents'] / 100:+.2f}"
        print(f"{s:<25} | {d['settled']:8d} | {d['wins']:6d} | {d['losses']:6d} | {wr:9.1f}% | {pnl_str:>10} | {d['stop_losses']:9d} | {d['take_profits']:10d}")
