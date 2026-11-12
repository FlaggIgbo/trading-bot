#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

INSTANCES = [
    {"tag": "regime_aware (prod, LIVE, SL, TP85c)", "db": "trading_bot_regime_aware_prod_sl_tp85.db", "log": "trading_bot_regime_aware_prod_sl_tp85.log", "env": "prod", "live": True},
    {"tag": "regime_aware (demo, LIVE, SL, TP85c)", "db": "trading_bot_regime_aware_demo_sl_tp85.db", "log": "trading_bot_regime_aware_demo_sl_tp85.log", "env": "demo", "live": True},
    {"tag": "sell_1.5n (demo)", "db": "trading_bot_sell_1.5n_demo.db", "log": "trading_bot_sell_1.5n_demo.log", "env": "demo", "live": False},
    {"tag": "last_21_spot (demo, SL, TP85c)", "db": "trading_bot_last_21_spot_demo_sl_tp85.db", "log": "trading_bot_last_21_spot_demo_sl_tp85.log", "env": "demo", "live": False},
    {"tag": "random (demo)", "db": "trading_bot_random_demo.db", "log": "trading_bot_random_demo.log", "env": "demo", "live": False},
]

def get_stats():
    now_ts = time.time()
    remaining_sec = 0

    meta_file = ROOT_DIR / ".parallel_run_meta.json"
    instances = INSTANCES
    if meta_file.exists():
        try:
            with open(meta_file, "r") as f:
                meta = json.load(f)
            remaining_sec = max(0, int(meta.get("end_time", now_ts) - now_ts))
            if meta.get("instances"):
                instances = meta["instances"]
        except Exception:
            pass
    elif (ROOT_DIR / "parallel_run.log").exists():
        try:
            with open(ROOT_DIR / "parallel_run.log", "r", errors="ignore") as f:
                lines = f.readlines()
            for line in reversed(lines[-200:]):
                m = re.search(r"Target completion time:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", line)
                if m:
                    end_dt = datetime.fromisoformat(m.group(1))
                    remaining_sec = max(0, int(end_dt.timestamp() - now_ts))
                    break
        except Exception:
            pass

    hours = remaining_sec // 3600
    mins = (remaining_sec % 3600) // 60
    secs = remaining_sec % 60
    remaining_str = f"{hours:02d}h {mins:02d}m {secs:02d}s"

    overall_trades = 0
    overall_settled = 0
    overall_open = 0
    overall_wins = 0
    overall_losses = 0
    overall_pnl_cents = 0
    overall_sl_count = 0

    strategy_stats = []
    all_positions = []

    for inst in instances:
        db_name = inst.get("db") or inst.get("db_path")
        log_name = inst.get("log") or inst.get("log_path")
        db_path = ROOT_DIR / db_name
        log_path = ROOT_DIR / log_name
        
        positions = []
        if db_path.exists():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                c = conn.cursor()
                c.execute("SELECT client_order_id, ticker, strategy, side, count, price_cents, status, close_time, pnl_cents FROM positions WHERE status IN ('OPEN', 'SETTLED') ORDER BY rowid DESC")
                positions = c.fetchall()
                conn.close()
            except Exception:
                pass

        sl_count = 0
        if log_path.exists():
            try:
                with open(log_path, "r", errors="ignore") as f:
                    for line in f:
                        if "[order_stop_loss]" in line or "[STOP LOSS]" in line:
                            sl_count += 1
            except Exception:
                pass

        total = len(positions)
        settled = [p for p in positions if p[6] == "SETTLED"]
        open_pos = [p for p in positions if p[6] == "OPEN"]
        wins = [p for p in settled if (p[8] or 0) > 0]
        losses = [p for p in settled if (p[8] or 0) < 0]
        pnl = sum(p[8] or 0 for p in settled)
        win_rate = (len(wins) / len(settled) * 100) if settled else 0.0

        gross_win = sum(p[8] for p in wins) / 100.0
        gross_loss = abs(sum(p[8] for p in losses)) / 100.0
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

        # Exclude 'random' baseline strategy from aggregate metric boxes
        is_random = "random" in str(inst.get("tag", "")).lower() or "random" in str(inst.get("strategy", "")).lower()
        if not is_random:
            overall_trades += total
            overall_settled += len(settled)
            overall_open += len(open_pos)
            overall_wins += len(wins)
            overall_losses += len(losses)
            overall_pnl_cents += pnl
            overall_sl_count += sl_count

        strategy_stats.append({
            "tag": inst["tag"],
            "env": inst["env"],
            "live": inst["live"],
            "total": total,
            "settled": len(settled),
            "open": len(open_pos),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "pnl_cents": pnl,
            "pnl_dollars": pnl / 100.0,
            "profit_factor": profit_factor,
            "sl_triggers": sl_count,
        })

        for p in positions:
            all_positions.append({
                "strategy": inst["tag"],
                "env": inst["env"],
                "live": inst["live"],
                "ticker": p[1],
                "side": p[3],
                "price": p[5],
                "status": p[6],
                "close_time": p[7],
                "pnl_cents": p[8],
                "pnl_dollars": (p[8] / 100.0) if p[8] is not None else None,
            })

    # Read latest logs from parallel_run.log
    latest_logs = []
    combined_log = ROOT_DIR / "parallel_run.log"
    if combined_log.exists():
        try:
            with open(combined_log, "r", errors="ignore") as f:
                lines = f.readlines()
                latest_logs = [line.strip() for line in lines[-20:] if line.strip()]
        except Exception:
            pass

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "time_remaining_seconds": remaining_sec,
        "time_remaining_str": f"{remaining_sec // 3600:02d}h {(remaining_sec % 3600) // 60:02d}m {remaining_sec % 60:02d}s",
        "summary": {
            "total_trades": overall_trades,
            "settled_trades": overall_settled,
            "open_trades": overall_open,
            "wins": overall_wins,
            "losses": overall_losses,
            "win_rate": (overall_wins / overall_settled * 100) if overall_settled else 0.0,
            "pnl_dollars": overall_pnl_cents / 100.0,
            "sl_triggers": overall_sl_count,
        },
        "strategies": strategy_stats,
        "positions": all_positions[:200],
        "logs": latest_logs,
    }

def main():
    while True:
        try:
            data = get_stats()
            tmp_path = ROOT_DIR / "live_data.json.tmp"
            target_path = ROOT_DIR / "live_data.json"
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, target_path)
        except Exception as e:
            pass
        time.sleep(2)

if __name__ == "__main__":
    main()
