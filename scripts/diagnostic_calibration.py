import sys
import time
from pathlib import Path
from collections import defaultdict

# Add the project root to the path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config
import sqlite3

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
    if not conn:
        print("Database not found.")
        return

    # 1. Fetch last 300 resolved trades
    rows = conn.execute("""
        SELECT *, COALESCE(realized_pnl_cents, pnl_cents, 0) AS pnl
        FROM predictions
        WHERE outcome IN ('win', 'lose', 'sold')
        ORDER BY COALESCE(resolved_ts, ts) DESC
        LIMIT 300
    """).fetchall()

    if not rows:
        print("No resolved trades found.")
        return

    print(f"Analyzing last {len(rows)} resolved trades...")

    # 2. Calibration by Bucket
    buckets = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    bucket_stats = defaultdict(lambda: {"count": 0, "wins": 0, "expected_p": 0.0})

    for r in rows:
        p_win = float(r["hist_p"])
        if r["side"].lower() == "no":
            p_win = 1.0 - p_win
        
        # Find bucket
        b_idx = 0
        for i in range(len(buckets) - 1):
            if buckets[i] <= p_win < buckets[i+1]:
                b_idx = i
                break
        else:
            b_idx = len(buckets) - 2 # 0.9-1.0
            
        bucket_stats[b_idx]["count"] += 1
        if r["outcome"] == "win":
            bucket_stats[b_idx]["wins"] += 1
        bucket_stats[b_idx]["expected_p"] += p_win

    print("\n[Calibration Table]")
    print(f"{'Bucket':<12} | {'Count':<6} | {'Actual WR':<10} | {'Expected WR':<11} | {'Gap':<6}")
    print("-" * 60)
    for i in range(len(buckets) - 1):
        s = bucket_stats[i]
        if s["count"] == 0: continue
        actual = s["wins"] / s["count"]
        expected = s["expected_p"] / s["count"]
        gap = actual - expected
        print(f"{buckets[i]:.1f}-{buckets[i+1]:.1f}      | {s['count']:<6} | {actual:10.1%} | {expected:11.1%} | {gap:+.1%}")

    # 3. Payoff Analysis
    total_pnl = sum(r["pnl"] for r in rows)
    win_pnl = sum(r["pnl"] for r in rows if r["outcome"] == "win")
    lose_pnl = sum(r["pnl"] for r in rows if r["outcome"] in ("lose", "sold"))
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] in ("lose", "sold"))

    avg_win = (win_pnl / wins) if wins > 0 else 0
    avg_loss = (lose_pnl / losses) if losses > 0 else 0
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    print("\n[Payoff Analysis]")
    print(f"  Avg Win:      ${avg_win/100:.2f}")
    print(f"  Avg Loss:     ${avg_loss/100:.2f}")
    print(f"  Payoff Ratio: {payoff_ratio:.2f}")
    print(f"  Total PnL:    ${total_pnl/100:.2f}")

    # 4. Fat Pitch vs Normal Trades
    # Note: we need to identify fat pitch trades. In compare_historical they are marked by reason containing 'fat pitch' or based on net edge
    # For now let's use net edge > 0.35 as proxy if not stored
    
    fat_rows = []
    normal_rows = []
    for r in rows:
        # Re-derive net edge approx
        p_win = float(r["hist_p"])
        if r["side"].lower() == "no":
            p_win = 1.0 - p_win
        net_edge = p_win - r["entry_price"]
        
        if net_edge > 0.35:
            fat_rows.append(r)
        else:
            normal_rows.append(r)

    def print_group(name, group):
        if not group:
            print(f"\n[{name}] No trades.")
            return
        wins = sum(1 for r in group if r["outcome"] == "win")
        pnl = sum(r["pnl"] for group_r in group # wait
                  for r in [group_r]) # just to fix scope in list comp if needed, but it's not
        pnl = sum(r["pnl"] for r in group)
        wr = wins / len(group)
        print(f"\n[{name}]")
        print(f"  Count:    {len(group)}")
        print(f"  Win Rate: {wr:.1%}")
        print(f"  PnL:      ${pnl/100:.2f}")
        print(f"  Avg PnL:  ${(pnl/len(group))/100:.2f}")

    print_group("Fat Pitch Proxy (Edge > 35%)", fat_rows)
    print_group("Normal Trades", normal_rows)

    conn.close()

if __name__ == "__main__":
    main()
