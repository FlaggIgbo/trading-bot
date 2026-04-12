import sys
import time
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

# Add the project root to the path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from config import get_config
from signals.kalshi_client import KalshiClient
from signals.gemini_client import GeminiTradingClient

def get_balances():
    balances = {}
    try:
        k_client = KalshiClient()
        k_bal = k_client.get_balance()
        balances['Kalshi'] = k_bal.get('balance', 0) / 100.0
    except Exception as e:
        balances['Kalshi Error'] = str(e)

    try:
        g_client = GeminiTradingClient()
        g_bal = g_client.get_balance()
        balances['Gemini'] = g_bal.get('balance', 0) / 100.0
    except Exception as e:
        balances['Gemini Error'] = str(e)
    
    return balances

def get_holdings(cfg):
    db_path = Path(cfg.get("storage", {}).get("data_dir", "~/.trading-bot-data")).expanduser() / "trading_bot.db"
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    # Query open positions from predictions
    rows = conn.execute("""
        SELECT platform, ticker, side, entry_price, count, datetime(ts, 'unixepoch') as dt
        FROM predictions
        WHERE outcome IS NULL
        ORDER BY ts DESC
    """).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]

def get_recent_trades(cfg, hours=12):
    db_path = Path(cfg.get("storage", {}).get("data_dir", "~/.trading-bot-data")).expanduser() / "trading_bot.db"
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    cutoff_ts = time.time() - (hours * 3600)
    
    # Use COALESCE(resolved_ts, ts) to capture trades resolved in the window
    rows = conn.execute("""
        SELECT platform, ticker, side, entry_price, count, outcome, COALESCE(realized_pnl_cents, pnl_cents, 0) as pnl_cents, 
               datetime(ts, 'unixepoch') as dt, datetime(COALESCE(resolved_ts, ts), 'unixepoch') as resolved_dt
        FROM predictions
        WHERE outcome IN ('win', 'lose', 'sold') AND COALESCE(resolved_ts, ts) >= ?
        ORDER BY COALESCE(resolved_ts, ts) DESC
    """, (cutoff_ts,)).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]

def main():
    cfg = get_config()
    
    print("=== TRADING BOT VERBOSE STATUS REPORT (Last 12 Hours) ===")
    
    print("\n[BALANCES]")
    balances = get_balances()
    for k, v in balances.items():
        if isinstance(v, float):
            print(f"  {k:<10}: ${v:,.2f}")
        else:
            print(f"  {k:<10}: {v}")
            
    print("\n[EXECUTIVE SUMMARY (Last 12h)]")
    trades = get_recent_trades(cfg, 12)
    if not trades:
        print("  No trades resolved in the last 12 hours.")
    else:
        total_trades = len(trades)
        wins = sum(1 for t in trades if t['outcome'] == 'win')
        losses = sum(1 for t in trades if t['outcome'] in ('lose', 'sold'))
        total_pnl = sum(t['pnl_cents'] for t in trades) / 100.0
        wr = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        
        print(f"  Total Trades:   {total_trades}")
        print(f"  PnL:            ${total_pnl:,.2f}")
        print(f"  Win Ratio:      {wr:.1f}% ({wins}W / {losses}L)")
        
        # Breakdown by platform
        for plat in ['kalshi', 'gemini']:
            p_trades = [t for t in trades if t['platform'].lower() == plat]
            if p_trades:
                p_pnl = sum(t['pnl_cents'] for t in p_trades) / 100.0
                p_wins = sum(1 for t in p_trades if t['outcome'] == 'win')
                p_wr = (p_wins / len(p_trades) * 100.0)
                print(f"  - {plat.capitalize():<7}: ${p_pnl:>7.2f} (WR {p_wr:>5.1f}%)")

    print("\n[CURRENT HOLDINGS (Inventory)]")
    holdings = get_holdings(cfg)
    if not holdings:
        print("  No open positions.")
    else:
        print(f"  {'DT (UTC)':<20} {'PLATFORM':<10} {'TICKER':<30} {'SIDE':<5} {'PRICE':<6} {'QTY':<5}")
        for h in holdings:
            print(f"  {h['dt']:<20} {h['platform']:<10} {h['ticker'][:30]:<30} {h['side']:<5} {h['entry_price']:>5.2f} {h['count']:>5}")

    print("\n[RECENT TRADES DETAIL (Last 12h)]")
    if not trades:
        print("  No trades found.")
    else:
        print(f"  {'RESOLVED (UTC)':<20} {'PLATFORM':<10} {'TICKER':<30} {'SIDE':<5} {'PRICE':<6} {'QTY':<5} {'RESULT':<8} {'PNL'}")
        for t in trades:
            pnl = f"${t['pnl_cents']/100.0:>7.2f}"
            print(f"  {t['resolved_dt']:<20} {t['platform']:<10} {t['ticker'][:30]:<30} {t['side']:<5} {t['entry_price']:>5.2f} {t['count']:>5} {str(t['outcome']):<8} {pnl}")

    print("\n" + "="*80)

if __name__ == "__main__":
    main()
