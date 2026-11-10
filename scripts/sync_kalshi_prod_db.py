#!/usr/bin/env python3
"""
Sync Kalshi Production Database
===============================
Reconciles SQLite database with the actual fills and settlements from the Kalshi live exchange.
Corrects phantom stop-loss writes, recovers positions falsely deleted as "unfilled",
and accurately records the true PnL.
"""

import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from ingestion.api.client import KalshiClient
from db import Database, Environment, PositionSide, PositionStatus, Strategy

def sync_prod_db():
    db_path = ROOT_DIR / "trading_bot_last_9_spot_prod_sl_tp85.db"
    client = KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2", live_orders=True)

    # Fetch all fills
    fills = []
    cursor = None
    while True:
        url = f"{client.base_url}/portfolio/fills?limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        headers = client._build_auth_headers("GET", url)
        res = client._request("GET", url, headers=headers).json()
        batch = res.get("fills", [])
        if not batch:
            break
        fills.extend(batch)
        cursor = res.get("cursor")
        if not cursor or len(batch) < 100:
            break

    today_fills = [f for f in fills if f.get("created_time", "").startswith("2026-11-10")]
    settlements = {s.get("ticker"): s for s in client.get_settlements(limit=100) if s.get("settled_time", "").startswith("2026-11-10")}

    # Group fills by market ticker
    by_ticker = {}
    for f in sorted(today_fills, key=lambda x: x.get("created_time")):
        t = f.get("ticker")
        if t not in by_ticker:
            by_ticker[t] = {"buys": [], "sells": []}
        action = f.get("action")
        if action == "buy":
            by_ticker[t]["buys"].append(f)
        else:
            by_ticker[t]["sells"].append(f)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("DELETE FROM positions WHERE environment = 'production'")

    total_net = 0.0
    wins = 0
    losses = 0

    for t, data in sorted(by_ticker.items()):
        total_buy_cost = sum(
            float(b.get("count_fp") or b.get("count") or 1.0) * (
                float(b.get("yes_price_dollars") or 0.0) if b.get("side") == "yes" else float(b.get("no_price_dollars") or (1.0 - float(b.get("yes_price_dollars") or 0.0)))
            ) + float(b.get("fee_cost") or 0.0)
            for b in data["buys"]
        )
        total_sell_rev = sum(
            float(s.get("count_fp") or s.get("count") or 1.0) * (
                float(s.get("yes_price_dollars") or 0.0) if s.get("side") == "yes" else float(s.get("no_price_dollars") or (1.0 - float(s.get("yes_price_dollars") or 0.0)))
            ) - float(s.get("fee_cost") or 0.0)
            for s in data["sells"]
        )

        s = settlements.get(t)
        settle_rev = (float(s.get("revenue", 0)) / 100.0) if s else 0.0

        net_dollars = (total_sell_rev + settle_rev) - total_buy_cost
        pnl_cents = int(round(net_dollars * 100))
        total_net += net_dollars
        if net_dollars > 0:
            wins += 1
        elif net_dollars < 0:
            losses += 1

        first_fill = (data["buys"] or data["sells"])[0]
        side_str = "YES" if first_fill.get("side") == "yes" else "NO"
        side_val = PositionSide.YES.value if side_str == "YES" else PositionSide.NO.value
        entry_price = int(round(total_buy_cost * 100)) if total_buy_cost > 0 else 50
        status_val = "SETTLED" if (s or data["sells"]) else "OPEN"
        cid = f"sync_{t}_{first_fill.get('order_id', '')[:20]}"

        cur.execute(
            """
            INSERT OR REPLACE INTO positions (
                client_order_id, ticker, side, count, price_cents, status, close_time, environment, strategy, pnl_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                t,
                side_val,
                1,
                entry_price,
                status_val,
                s.get("settled_time", "") if s else "",
                "production",
                "last_9_spot",
                pnl_cents,
            ),
        )

    conn.commit()
    conn.close()
    print(f"Synced {len(by_ticker)} markets into {db_path}. Wins: {wins}, Losses: {losses}, Net PnL: ${total_net:+.2f}")

if __name__ == "__main__":
    sync_prod_db()
