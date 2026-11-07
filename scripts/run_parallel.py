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
import threading
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
    print("\n" + "=" * 70, flush=True)
    print("STRATEGY PERFORMANCE EVALUATION SUMMARY", flush=True)
    print("=" * 70, flush=True)
    header = f"{'Strategy':<12} | {'Trades':<8} | {'Open':<6} | {'Settled':<8} | {'W/L':<8} | {'Win Rate':<10} | {'PnL ($)':<10}"
    print(header, flush=True)
    print("-" * len(header), flush=True)

    for strat in strategies:
        db_path = f"trading_bot_{strat}.db"
        perf = get_performance(db_path)
        if not perf or "error" in perf:
            print(f"{strat:<12} | {'0':<8} | {'0':<6} | {'0':<8} | {'0/0':<8} | {'0.0%':<10} | {'$0.00':<10}", flush=True)
            continue

        wl = f"{perf['wins']}/{perf['losses']}"
        win_str = f"{perf['win_rate']:.1f}%"
        pnl_str = f"${perf['pnl_dollars']:+.2f}"
        print(f"{strat:<12} | {perf['total']:<8} | {perf['open']:<6} | {perf['settled']:<8} | {wl:<8} | {win_str:<10} | {pnl_str:<10}", flush=True)

    print("=" * 70 + "\n", flush=True)


def stream_process_output(proc, strat: str, log_file, combined_file=None):
    """Read lines from child process, log to file, and echo immediately to stdout."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            log_file.write(line)
            log_file.flush()
            clean = line.rstrip()
            tagged = f"[{strat}] {clean}"
            if combined_file is not None:
                combined_file.write(tagged + "\n")
                combined_file.flush()
            print(tagged, flush=True)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Run trading bot strategies in parallel")
    parser.add_argument("--duration", type=int, default=10800, help="Run duration in seconds (default: 10800 = 3 hours)")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["random", "last_5", "sell_3n", "sell_2n", "sell_2n_5m", "sell_1.5n", "sell_2.5n"],
        help="List of strategies to run (default: random last_5 sell_3n sell_2n sell_2n_5m sell_1.5n sell_2.5n)",
    )
    args = parser.parse_args()

    start_time = time.time()
    end_time = start_time + args.duration

    print(f"Starting parallel evaluation of strategies: {', '.join(args.strategies)}", flush=True)
    print(f"Duration: {args.duration}s ({args.duration / 3600:.2f} hours)", flush=True)
    print(f"Target completion time: {datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()}", flush=True)

    processes = {}
    log_files = {}
    combined_log_path = "parallel_run.log"
    combined_file = open(combined_log_path, "a")

    for i, strat in enumerate(args.strategies):
        db_path = f"trading_bot_{strat}.db"
        log_path = f"trading_bot_{strat}.log"
        log_file = open(log_path, "a")
        log_files[strat] = log_file

        # Stagger offsets by 8s across interval to prevent Kalshi API rate limit collisions
        offset = (i * 8) % args.interval
        cmd = [
            sys.executable,
            "-u",
            "main.py",
            "--strategy", strat,
            "--interval", str(args.interval),
            "--offset", str(offset),
            "--db", db_path,
        ]
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        processes[strat] = p
        t = threading.Thread(target=stream_process_output, args=(p, strat, log_file, combined_file), daemon=True)
        t.start()
        print(f"  [+] Started '{strat}' strategy PID={p.pid} (offset=+{offset}s, DB={db_path}, Log={log_path})", flush=True)

    def cleanup(signum=None, frame=None):
        print("\nStopping strategy processes...", flush=True)
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
        try:
            combined_file.close()
        except Exception:
            pass
        print_report(args.strategies)
        if signum is not None:
            sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    last_interim_report = time.time()
    try:
        while time.time() < end_time:
            time.sleep(min(5, max(1, int(end_time - time.time()))))

            # Periodic interim status report every 15 minutes (900s)
            if time.time() - last_interim_report >= 900:
                print("\n>>> [INTERIM 15-MINUTE PERFORMANCE UPDATE] <<<", flush=True)
                print_report(args.strategies)
                last_interim_report = time.time()

            # Verify child processes are healthy
            for i, strat in enumerate(args.strategies):
                p = processes.get(strat)
                if p and p.poll() is not None:
                    print(f"WARNING: Process for '{strat}' exited unexpectedly with code {p.returncode}. Restarting...", flush=True)
                    db_path = f"trading_bot_{strat}.db"
                    log_file = log_files[strat]
                    offset = (i * 8) % args.interval
                    cmd = [
                        sys.executable,
                        "-u",
                        "main.py",
                        "--strategy", strat,
                        "--interval", str(args.interval),
                        "--offset", str(offset),
                        "--db", db_path,
                    ]
                    p = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    processes[strat] = p
                    t = threading.Thread(target=stream_process_output, args=(p, strat, log_file), daemon=True)
                    t.start()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
