#!/usr/bin/env python3
"""
Trading Bot Performance & Audit Report CLI
==========================================
Generates a comprehensive summary of trading performance:
- Overall & per-strategy PnL (profit/loss)
- Win rate (%) and baseline comparison (e.g. last_9 vs random)
- Open risk exposure ($ locked in open contracts)
- Position status counts (PENDING, OPEN, SETTLED, FAILED)
- Recent lifecycle audit events from the SQLite events table

Usage:
    python report.py
    python report.py --db trading_bot.db --env demo
"""

import argparse
import os
import sqlite3
import sys
from typing import Any, Optional


def format_cents(cents: Optional[int]) -> str:
    """Format integer cents to dollars string (e.g. 150 -> '+$1.50', -75 -> '-$0.75')."""
    if cents is None:
        return "$0.00"
    dollars = cents / 100.0
    prefix = "+" if dollars > 0 else ""
    return f"{prefix}${dollars:.2f}"


def run_report(db_path: str = "trading_bot.db", environment: Optional[str] = None) -> None:
    if not os.path.exists(db_path):
        print(f"[!] Database file '{db_path}' not found. Run the bot first to generate data.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Filter by environment if specified
    env_clause = ""
    params: list[Any] = []
    if environment:
        env_clause = "WHERE environment = ?"
        params.append(environment)

    print("\n" + "=" * 65)
    print(f"TRADING BOT REPORT: {db_path} (env: {environment or 'all'})")
    print("=" * 65)

    # 1. Position Status Breakdown
    query_status = f"""
        SELECT status, COUNT(*) as count FROM positions
        {env_clause}
        GROUP BY status
    """
    cursor.execute(query_status, tuple(params))
    status_counts = {row["status"]: row["count"] for row in cursor.fetchall()}
    pending_count = status_counts.get("PENDING", 0)
    open_count = status_counts.get("OPEN", 0)
    settled_count = status_counts.get("SETTLED", 0)
    failed_count = status_counts.get("FAILED", 0)
    total_positions = sum(status_counts.values())

    print("\n[1] POSITION LIFECYCLE SUMMARY")
    print("-" * 65)
    print(f"  Total Bets Placed:    {total_positions}")
    print(f"  - PENDING (Intent):   {pending_count}")
    print(f"  - OPEN (Active):      {open_count}")
    print(f"  - SETTLED (Resolved): {settled_count}")
    print(f"  - FAILED (Rejected):  {failed_count}")

    # 2. Open Exposure
    query_exposure = f"""
        SELECT COUNT(*) as open_count, SUM(price_cents * count) as exposure_cents
        FROM positions
        {'WHERE status = "OPEN"' if not environment else 'WHERE status = "OPEN" AND environment = ?'}
    """
    exp_params = [environment] if environment else []
    cursor.execute(query_exposure, tuple(exp_params))
    row_exp = cursor.fetchone()
    active_open = row_exp["open_count"] if row_exp and row_exp["open_count"] else 0
    locked_cents = row_exp["exposure_cents"] if row_exp and row_exp["exposure_cents"] else 0

    print("\n[2] OPEN RISK EXPOSURE")
    print("-" * 65)
    print(f"  Active Contracts:     {active_open}")
    print(f"  Capital at Risk:      {format_cents(locked_cents)}")

    # 3. Strategy Performance Breakdown
    cursor.execute("PRAGMA table_info(positions)")
    cols = {row["name"] for row in cursor.fetchall()}
    has_fees = "fees_cents" in cols
    fees_select = "SUM(fees_cents) as total_fees_cents" if has_fees else "0 as total_fees_cents"

    query_strategies = f"""
        SELECT
            strategy,
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_cents < 0 THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN pnl_cents = 0 THEN 1 ELSE 0 END) as ties,
            SUM(pnl_cents) as total_pnl_cents,
            {fees_select}
        FROM positions
        {'WHERE status = "SETTLED"' if not environment else 'WHERE status = "SETTLED" AND environment = ?'}
        GROUP BY strategy
        ORDER BY total_pnl_cents DESC
    """
    cursor.execute(query_strategies, tuple(params))
    strat_rows = cursor.fetchall()

    print("\n[3] STRATEGY PERFORMANCE (SETTLED)")
    print("-" * 65)
    header = f"{'Strategy':<20} {'Trades':<8} {'Wins':<6} {'Losses':<8} {'Win Rate':<10} {'Net PnL':<10}"
    print(header)
    print("-" * len(header))

    strat_stats = {}
    total_pnl = 0

    for row in strat_rows:
        strat = row["strategy"]
        trades = row["total_trades"]
        wins = row["wins"]
        losses = row["losses"]
        win_rate = (wins / trades * 100.0) if trades > 0 else 0.0
        pnl_cents = row["total_pnl_cents"] or 0
        total_pnl += pnl_cents
        strat_stats[strat] = {"trades": trades, "win_rate": win_rate, "pnl_cents": pnl_cents}

        print(
            f"{strat:<20} {trades:<8} {wins:<6} {losses:<8} {win_rate:>6.1f}%    {format_cents(pnl_cents):>10}"
        )

    if not strat_rows:
        print("  No settled trades recorded yet.")
    else:
        print("-" * len(header))
        print(f"{'OVERALL TOTAL':<20} {settled_count:<8} {'':<6} {'':<8} {'':<10} {format_cents(total_pnl):>10}")

    # 4. Strategy Edge Comparison vs Random Baseline
    if "random" in strat_stats and len(strat_stats) > 1:
        print("\n[4] STRATEGY EDGE COMPARISON (vs Random Baseline)")
        print("-" * 65)
        random_wr = strat_stats["random"]["win_rate"]
        print(f"  Random Baseline Win Rate: {random_wr:.1f}% ({strat_stats['random']['trades']} trades)")
        for strat, data in strat_stats.items():
            if strat == "random":
                continue
            diff = data["win_rate"] - random_wr
            sign = "+" if diff >= 0 else ""
            status_text = "EDGE DETECTED" if diff > 0 else "NO EDGE DETECTED"
            print(
                f"  - {strat:<18}: {data['win_rate']:.1f}% ({sign}{diff:.1f}% vs random) -> {status_text}"
            )

    # 5. Recent Lifecycle & Audit Events
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
    if cursor.fetchone():
        query_events = f"""
            SELECT id, timestamp, level, stage, ticker, strategy, message
            FROM events
            ORDER BY id DESC LIMIT 10
        """
        cursor.execute(query_events)
        events = cursor.fetchall()
        print("\n[5] RECENT AUDIT EVENTS (Last 10)")
        print("-" * 65)
        for ev in events:
            ts = ev["timestamp"][:19].replace("T", " ")
            lvl = ev["level"]
            stg = ev["stage"]
            msg = ev["message"]
            print(f"  [{ts}] [{lvl:<5}] [{stg:<10}] {msg}")
        if not events:
            print("  No audit events logged yet.")

    print("\n" + "=" * 65 + "\n")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Trading Bot Reporting Tool")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "trading_bot.db"), help="Path to SQLite database")
    parser.add_argument("--env", default=None, choices=["demo", "prod", "production"], help="Filter by environment")
    args = parser.parse_args()

    run_report(db_path=args.db, environment=args.env)


if __name__ == "__main__":
    main()
