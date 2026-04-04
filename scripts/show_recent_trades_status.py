import sys
import time
from pathlib import Path
from collections import defaultdict
import sqlite3

# Add the project root to the path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config
from storage import get_recent_performance, get_position_evaluation_interval_stats, get_unresolved_predictions

def fmt_pnl(cents):
    if cents is None:
        return "N/A"
    dollars = cents / 100.0
    return f"${dollars:,.2f}"

def _get_conn(cfg):
    db_path = Path(cfg.get("storage", {}).get("data_dir", "~/.trading-bot-data")).expanduser() / "trading_bot.db"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

def main():
    cfg = get_config()
    conn = _get_conn(cfg)
    
    lookback_hours = 6.0
    now_ts = time.time()
    start_ts = now_ts - lookback_hours * 3600.0
    
    # 1. Performance Summary (Last 6h)
    perf = get_recent_performance(lookback_hours=lookback_hours, config=cfg)
    perf_k = get_recent_performance(lookback_hours=lookback_hours, config=cfg, platform="kalshi")
    perf_g = get_recent_performance(lookback_hours=lookback_hours, config=cfg, platform="gemini")
    
    # 2. Position evaluations
    pos_stats = get_position_evaluation_interval_stats(cfg, start_ts=start_ts, end_ts=now_ts)
    
    # 3. Unresolved
    unresolved = get_unresolved_predictions(cfg)
    
    print("=" * 75)
    print(f"TRADING STATUS REPORT (Lookback: {lookback_hours} hours)")
    print("=" * 75)
    
    print("\n[EXECUTIVE SUMMARY (Last 6h)]")
    if perf.get("total", 0) > 0:
        wr_pct = (perf["win_rate"] * 100.0) if perf.get("win_rate") is not None else 0.0
        print(f"  Total Trades:   {perf['total']}")
        print(f"  PnL:            {fmt_pnl(perf['pnl_cents'])}")
        print(f"  Win Ratio:      {wr_pct:.1f}% ({perf['wins']}W / {perf['losses']}L)")
        
        print("\n  - Kalshi: " + (f"{fmt_pnl(perf_k['pnl_cents'])} (WR {(perf_k['win_rate']*100 if perf_k['win_rate'] is not None else 0):.1f}%)" if perf_k['total'] else "0 trades"))
        print(f"  - Gemini: " + (f"{fmt_pnl(perf_g['pnl_cents'])} (WR {(perf_g['win_rate']*100 if perf_g['win_rate'] is not None else 0):.1f}%)" if perf_g['total'] else "0 trades"))
    else:
        print("  No trades resolved in this interval.")

    # 4. Calibration & Quality (Last 300 trades)
    if conn:
        rows_diag = conn.execute("""
            SELECT *, COALESCE(realized_pnl_cents, pnl_cents, 0) AS pnl
            FROM predictions
            WHERE outcome IN ('win', 'lose', 'sold')
            ORDER BY COALESCE(resolved_ts, ts) DESC
            LIMIT 300
        """).fetchall()
        
        if rows_diag:
            print("\n[DIAGNOSTIC (Last 300 trades)]")
            
            wins = sum(1 for r in rows_diag if r["outcome"] == "win")
            total = len(rows_diag)
            wr_300 = (wins / total) if total > 0 else 0
            
            # Payoff ratio
            win_pnl = sum(r["pnl"] for r in rows_diag if r["outcome"] == "win")
            lose_pnl = sum(r["pnl"] for r in rows_diag if r["outcome"] in ("lose", "sold"))
            losses = total - wins
            avg_win = (win_pnl / wins) if wins > 0 else 0
            avg_loss = (lose_pnl / losses) if losses > 0 else 0
            payoff = abs(avg_win / avg_loss) if avg_loss != 0 else 0
            
            # Fat pitch proxy performance
            fat_rows = []
            for r in rows_diag:
                p_win = float(r["hist_p"])
                if r["side"].lower() == "no": p_win = 1.0 - p_win
                if (p_win - r["entry_price"]) > 0.35: fat_rows.append(r)
            
            fat_pnl = sum(r["pnl"] for r in fat_rows)
            fat_wr = (sum(1 for r in fat_rows if r["outcome"] == "win") / len(fat_rows)) if fat_rows else 0
            
            print(f"  300-Trade WR:   {wr_300:.1%} (Current Calibration)")
            print(f"  Avg Win/Loss:   {fmt_pnl(avg_win)} / {fmt_pnl(avg_loss)}")
            print(f"  Payoff Ratio:   {payoff:.2f}")
            print(f"  Fat Pitch PnL:  {fmt_pnl(fat_pnl)} (WR {fat_wr:.1%} on {len(fat_rows)} trades)")

    print("\n[Signal Context]")
    if pos_stats.get("enabled") and pos_stats.get("n", 0) > 0:
        wr_pct_pos = (pos_stats["win_rate"] * 100.0) if pos_stats.get("win_rate") is not None else 0.0
        print(f"  Eval Count:     {pos_stats['n']}")
        print(f"  Eval Win Rate:  {wr_pct_pos:.1f}%")

    print("\n[Inventory]")
    if unresolved:
        print(f"  Open Positions: {len(unresolved)}")
        by_platform = defaultdict(int)
        for p in unresolved:
            by_platform[p.get("platform", "unknown")] += 1
        for plat, count in by_platform.items():
            print(f"  - {plat.capitalize()}: {count}")
    else:
        print("  No open positions.")
        
    print("\n" + "=" * 75)
    if conn: conn.close()

if __name__ == "__main__":
    main()
