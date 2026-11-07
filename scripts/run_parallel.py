"""
Parallel Strategy Evaluation Runner
===================================
Runs multiple trading bot strategies in parallel against demo.kalshi.co for a specified
duration (e.g. 3 hours), logs output to separate files, and prints a comparative performance report.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
os.chdir(ROOT_DIR)

from db import Database, PositionStatus


def get_performance(db_path: str):
    if not Path(db_path).exists():
        return None
    try:
        db = Database(db_path=db_path)
        positions = db.get_all_positions(environment="*")
        db.close()
    except Exception as e:
        return {"error": str(e)}

    total = len(positions)
    settled = [p for p in positions if p["status"] == PositionStatus.SETTLED]
    open_pos = [p for p in positions if p["status"] == PositionStatus.OPEN]
    wins = [p for p in settled if (p.get("pnl_cents") or 0) > 0]
    losses = [p for p in settled if (p.get("pnl_cents") or 0) < 0]

    total_pnl_cents = sum(p.get("pnl_cents") or 0 for p in settled)
    win_rate = (len(wins) / len(settled) * 100) if settled else 0.0

    return {
        "total": total,
        "open": len(open_pos),
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "pnl_cents": total_pnl_cents,
        "pnl_dollars": total_pnl_cents / 100.0,
        "win_rate": win_rate,
    }


def print_report(strategies: list[str]):
    print("\n" + "=" * 70)
    print("STRATEGY PERFORMANCE EVALUATION SUMMARY")
    print("=" * 70)
    header = f"{'Strategy':<12} | {'Trades':<8} | {'Open':<6} | {'Settled':<8} | {'W/L':<8} | {'Win Rate':<10} | {'PnL ($)':<10}"
    print(header)
    print("-" * len(header))

    for strat in strategies:
        db_path = f"trading_bot_{strat}.db"
        perf = get_performance(db_path)
        if not perf or "error" in perf:
            print(f"{strat:<12} | {'N/A':<8} | {'N/A':<6} | {'N/A':<8} | {'N/A':<8} | {'N/A':<10} | {'$0.00':<10}")
            continue

        wl = f"{perf['wins']}/{perf['losses']}"
        win_str = f"{perf['win_rate']:.1f}%"
        pnl_str = f"${perf['pnl_dollars']:+.2f}"
        print(f"{strat:<12} | {perf['total']:<8} | {perf['open']:<6} | {perf['settled']:<8} | {wl:<8} | {win_str:<10} | {pnl_str:<10}")

    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Run trading bot strategies in parallel")
    parser.add_argument("--duration", type=int, default=10800, help="Run duration in seconds (default: 10800 = 3 hours)")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["random", "last_5", "sell_3n", "sell_2n", "sell_2n_5m"],
        help="List of strategies to run (default: random last_5 sell_3n sell_2n sell_2n_5m)",
    )
    args = parser.parse_args()

    start_time = time.time()
    end_time = start_time + args.duration

    print(f"Starting parallel evaluation of strategies: {', '.join(args.strategies)}")
    print(f"Duration: {args.duration}s ({args.duration / 3600:.2f} hours)")
    print(f"Target completion time: {datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()}")

    processes = {}
    log_files = {}

    for i, strat in enumerate(args.strategies):
        db_path = f"trading_bot_{strat}.db"
        log_path = f"trading_bot_{strat}.log"
        log_file = open(log_path, "a")
        log_files[strat] = log_file

        # Stagger offsets by 10s (:00, :10, :20) to prevent Kalshi API 429 rate limits
        offset = i * 10
        cmd = [
            sys.executable,
            "main.py",
            "--strategy", strat,
            "--interval", str(args.interval),
            "--offset", str(offset),
            "--db", db_path,
        ]
        p = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
        processes[strat] = p
        print(f"  [+] Started '{strat}' strategy PID={p.pid} (offset=+{offset}s, DB={db_path}, Log={log_path})")

    def cleanup(signum=None, frame=None):
        print("\nStopping strategy processes...")
        for strat, p in processes.items():
            if p.poll() is None:
                p.terminate()
        for strat, p in processes.items():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in log_files.values():
            try:
                f.close()
            except Exception:
                pass
        print_report(args.strategies)
        if signum is not None:
            sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        while time.time() < end_time:
            time.sleep(min(15, max(1, int(end_time - time.time()))))
            # Verify child processes are healthy
            for strat, p in processes.items():
                if p.poll() is not None:
                    print(f"WARNING: Process for '{strat}' exited unexpectedly with code {p.returncode}. Restarting...")
                    db_path = f"trading_bot_{strat}.db"
                    log_file = log_files[strat]
                    cmd = [
                        sys.executable,
                        "main.py",
                        "--strategy", strat,
                        "--interval", str(args.interval),
                        "--offset", "0",
                        "--db", db_path,
                    ]
                    processes[strat] = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
