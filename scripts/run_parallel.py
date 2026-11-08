"""
Parallel Strategy Evaluation Runner
===================================
Runs multiple trading bot strategies in parallel across specified environments
(demo or production) for a specified duration (e.g. 1 hour), logs output to
separate files, monitors for errors/warnings in real-time with safety shutdown,
and prints a comparative performance report.
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


def print_report(instances: list[dict]):
    print("\n" + "=" * 70, flush=True)
    print("STRATEGY PERFORMANCE EVALUATION SUMMARY", flush=True)
    print("=" * 70, flush=True)
    header = f"{'Instance':<24} | {'Trades':<8} | {'Open':<6} | {'Settled':<8} | {'W/L':<8} | {'Win Rate':<10} | {'PnL ($)':<10}"
    print(header, flush=True)
    print("-" * len(header), flush=True)

    for inst in instances:
        tag = inst["tag"]
        db_path = inst["db_path"]
        perf = get_performance(db_path)
        if not perf or "error" in perf:
            print(f"{tag:<24} | {'0':<8} | {'0':<6} | {'0':<8} | {'0/0':<8} | {'0.0%':<10} | {'$0.00':<10}", flush=True)
            continue

        wl = f"{perf['wins']}/{perf['losses']}"
        win_str = f"{perf['win_rate']:.1f}%"
        pnl_str = f"${perf['pnl_dollars']:+.2f}"
        print(f"{tag:<24} | {perf['total']:<8} | {perf['open']:<6} | {perf['settled']:<8} | {wl:<8} | {win_str:<10} | {pnl_str:<10}", flush=True)

    print("=" * 70 + "\n", flush=True)


def stream_process_output(proc, tag: str, log_file, combined_file=None, stop_event=None, error_records=None, stop_on_error=True):
    """Read lines from child process, log to file, echo to stdout, and monitor for errors."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            log_file.write(line)
            log_file.flush()
            clean = line.rstrip()
            tagged = f"[{tag}] {clean}"
            if combined_file is not None:
                combined_file.write(tagged + "\n")
                combined_file.flush()
            print(tagged, flush=True)

            if stop_on_error and stop_event is not None and not stop_event.is_set():
                is_err = (
                    "[ERROR]" in line
                    or "[CRITICAL]" in line
                    or "Traceback (most recent call last)" in line
                )
                is_warn = "[WARNING]" in line
                if is_err or is_warn:
                    issue_type = "ERROR" if is_err else "WARNING"
                    print(f"\n🚨 [SAFETY TRIGGER] Detected {issue_type} in [{tag}]: {clean}", flush=True)
                    if error_records is not None:
                        error_records.append((tag, issue_type, clean))
                    stop_event.set()
    except Exception:
        pass


def parse_instances(spec_list: list[str]) -> list[dict]:
    instances = []
    for item in spec_list:
        parts = item.split(":")
        strat = parts[0]
        env = parts[1] if len(parts) > 1 else "demo"
        live = len(parts) > 2 and parts[2].lower() in ("live", "true", "1")
        stop_loss = (len(parts) > 3 and parts[3].lower() in ("sl", "stoploss", "stop_loss", "true", "1")) or ("_sl" in strat)
        key = f"{strat}_{env}"
        live_str = ", LIVE" if live else ""
        sl_str = ", SL" if stop_loss else ""
        tag = f"{strat} ({env}{live_str}{sl_str})"
        instances.append({
            "raw": item,
            "strategy": strat,
            "env": env,
            "live": live,
            "stop_loss": stop_loss,
            "key": key,
            "tag": tag,
            "db_path": f"trading_bot_{key}.db",
            "log_path": f"trading_bot_{key}.log",
        })
    return instances


def main():
    parser = argparse.ArgumentParser(description="Run trading bot strategies in parallel with safety monitoring")
    parser.add_argument("--duration", type=int, default=3600, help="Run duration in seconds (default: 3600 = 1 hour)")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["sell_1.5n_35p", "sell_1.5n_35p:prod:live", "sell_3n", "sell_2.5n"],
        help="List of strategies in format 'strat', 'strat:env', or 'strat:env:live' (e.g. sell_1.5n_35p sell_1.5n_35p:prod:live sell_3n sell_2.5n)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        default=True,
        help="Immediately abort run if any warning or error is detected (default: True)",
    )
    parser.add_argument(
        "--no-stop-on-error",
        dest="stop_on_error",
        action="store_false",
        help="Do not abort on warnings or errors",
    )
    args = parser.parse_args()

    instances = parse_instances(args.strategies)
    start_time = time.time()
    end_time = start_time + args.duration

    print(f"Starting parallel evaluation of {len(instances)} instances:", flush=True)
    for inst in instances:
        print(f"  - {inst['tag']} -> DB={inst['db_path']}, Log={inst['log_path']}", flush=True)
    print(f"Duration: {args.duration}s ({args.duration / 3600:.2f} hours)", flush=True)
    print(f"Safety Stop-On-Error: {'ENABLED' if args.stop_on_error else 'DISABLED'}", flush=True)
    print(f"Target completion time: {datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()}", flush=True)

    processes = {}
    log_files = {}
    combined_log_path = "parallel_run.log"
    combined_file = open(combined_log_path, "a")

    stop_event = threading.Event()
    error_records = []

    for i, inst in enumerate(instances):
        tag = inst["tag"]
        log_file = open(inst["log_path"], "a")
        log_files[tag] = log_file

        # Stagger offsets by 8s across interval to prevent Kalshi API rate limit collisions
        offset = (i * 8) % args.interval
        cmd = [
            sys.executable,
            "-u",
            "main.py",
            "--strategy", inst["strategy"],
            "--env", inst["env"],
            "--interval", str(args.interval),
            "--offset", str(offset),
            "--db", inst["db_path"],
        ]
        if inst.get("live"):
            cmd.append("--live")
        if inst.get("stop_loss"):
            cmd.append("--stop-loss")
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        processes[tag] = p
        t = threading.Thread(
            target=stream_process_output,
            args=(p, tag, log_file, combined_file, stop_event, error_records, args.stop_on_error),
            daemon=True,
        )
        t.start()
        print(f"  [+] Started '{tag}' PID={p.pid} (offset=+{offset}s, DB={inst['db_path']})", flush=True)

    def cleanup(signum=None, frame=None):
        print("\nStopping strategy processes...", flush=True)
        for tag, p in processes.items():
            if p.poll() is None:
                p.terminate()
        for tag, p in processes.items():
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
        print_report(instances)
        if error_records:
            print("Summary of safety triggers / errors:", flush=True)
            for t, lvl, msg in error_records:
                print(f"  [{lvl}] [{t}] {msg}", flush=True)
        if signum is not None:
            sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    last_interim_report = time.time()
    try:
        while time.time() < end_time and not stop_event.is_set():
            time.sleep(min(2, max(1, int(end_time - time.time()))))

            # Periodic interim status report every 15 minutes (900s)
            if time.time() - last_interim_report >= 900:
                print("\n>>> [INTERIM 15-MINUTE PERFORMANCE UPDATE] <<<", flush=True)
                print_report(instances)
                last_interim_report = time.time()

            # Check if any child process died unexpectedly
            for inst in instances:
                p = processes.get(inst["tag"])
                if p and p.poll() is not None:
                    msg = f"Process '{inst['tag']}' exited prematurely with exit code {p.returncode}"
                    print(f"\n🚨 [SAFETY TRIGGER] {msg}", flush=True)
                    error_records.append((inst["tag"], "CRASH", msg))
                    stop_event.set()
                    break

        if stop_event.is_set():
            print("\n🚨 [SAFETY ABORT] Experiment halted immediately due to detected issue.", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
