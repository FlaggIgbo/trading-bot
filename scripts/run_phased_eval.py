"""
3-Hour Phased Strategy Evaluation Orchestrator
==============================================
Runs 4 strategies in parallel in the demo environment for 3 hours total,
rotating the single live-executing strategy each hour:

- Hour 1 (Phase 1): late_window_scalp (LIVE against demo.kalshi.co) | Others Simulated
- Hour 2 (Phase 2): panic_fade (LIVE against demo.kalshi.co) | Others Simulated
- Hour 3 (Phase 3): spot_lag (LIVE against demo.kalshi.co) | Others Simulated
- Random: Simulated (not live) across all 3 hours

Continuously writes status and telemetry to `.parallel_run_meta.json` and `log/parallel_run.log`,
which feeds real-time updates to `localhost.html` (served on port 8000).
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
os.chdir(ROOT_DIR)

from db import Database, Environment, PositionStatus
from risk_engine.settler import Settler
from ingestion.api.client import KalshiClient

PHASE_CONFIG = [
    {
        "phase": 1,
        "name": "Hour 1: late_window_scalp LIVE",
        "live_strat": "late_window_scalp",
    },
    {
        "phase": 2,
        "name": "Hour 2: panic_fade LIVE",
        "live_strat": "panic_fade",
    },
    {
        "phase": 3,
        "name": "Hour 3: spot_lag LIVE",
        "live_strat": "spot_lag",
    },
]

STRATEGIES_BASE = [
    {
        "strategy": "late_window_scalp",
        "env": "demo",
        "db_path": "db/trading_bot_late_window_scalp_demo.db",
        "log_path": "log/trading_bot_late_window_scalp_demo.log",
        "flags": ["--stop-loss", "--check-spot", "--taker-ioc"],
    },
    {
        "strategy": "panic_fade",
        "env": "demo",
        "db_path": "db/trading_bot_panic_fade_demo.db",
        "log_path": "log/trading_bot_panic_fade_demo.log",
        "flags": ["--stop-loss", "--check-spot"],
    },
    {
        "strategy": "spot_lag",
        "env": "demo",
        "db_path": "db/trading_bot_spot_lag_demo.db",
        "log_path": "log/trading_bot_spot_lag_demo.log",
        "flags": ["--stop-loss", "--check-spot", "--taker-ioc"],
    },
    {
        "strategy": "random",
        "env": "demo",
        "db_path": "db/trading_bot_random_demo.db",
        "log_path": "log/trading_bot_random_demo.log",
        "flags": [],
    },
]


def get_performance(db_path: str):
    p = Path(db_path)
    if not p.is_absolute():
        p = ROOT_DIR / p
    if not p.exists():
        return None
    try:
        db = Database(db_path=str(p))
        positions = db.get_all_positions(environment="*")
        db.close()
    except Exception as e:
        return {"error": str(e)}

    settled = [pos for pos in positions if pos.get("status") == PositionStatus.SETTLED]
    open_pos = [pos for pos in positions if pos.get("status") == PositionStatus.OPEN]
    wins = [pos for pos in settled if (pos.get("pnl_cents") or 0) > 0]
    losses = [pos for pos in settled if (pos.get("pnl_cents") or 0) < 0]
    total_pnl = sum(pos.get("pnl_cents") or 0 for pos in settled)
    win_rate = (len(wins) / len(settled) * 100.0) if settled else 0.0

    return {
        "total": len(positions),
        "open": len(open_pos),
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "pnl_cents": total_pnl,
        "pnl_dollars": total_pnl / 100.0,
        "win_rate": win_rate,
    }


def print_report(instances: list[dict]):
    print("\n" + "=" * 76, flush=True)
    print("3-HOUR PHASED RUN: CUMULATIVE STRATEGY PERFORMANCE", flush=True)
    print("=" * 76, flush=True)
    header = f"{'Strategy Instance':<30} | {'Trades':<8} | {'Open':<6} | {'Settled':<8} | {'W/L':<8} | {'Win Rate':<10} | {'PnL ($)':<10}"
    print(header, flush=True)
    print("-" * len(header), flush=True)

    for inst in instances:
        tag = inst["tag"]
        perf = get_performance(inst["db_path"])
        if not perf or "error" in perf:
            print(f"{tag:<30} | {'0':<8} | {'0':<6} | {'0':<8} | {'0/0':<8} | {'0.0%':<10} | {'$0.00':<10}", flush=True)
            continue
        wl = f"{perf['wins']}/{perf['losses']}"
        win_str = f"{perf['win_rate']:.1f}%"
        pnl_str = f"${perf['pnl_dollars']:+.2f}"
        print(f"{tag:<30} | {perf['total']:<8} | {perf['open']:<6} | {perf['settled']:<8} | {wl:<8} | {win_str:<10} | {pnl_str:<10}", flush=True)
    print("=" * 76 + "\n", flush=True)


def stream_process_output(proc, tag: str, log_file, combined_file=None):
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
    except Exception:
        pass


def settle_open_positions(instances: list[dict]):
    print("\n" + "=" * 76, flush=True)
    print("SETTLING OPEN POSITIONS ACROSS DATABASES...", flush=True)
    print("=" * 76, flush=True)
    client = KalshiClient(base_url="https://demo-api.kalshi.co/trade-api/v2")

    for attempt in range(8):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        unresolved = 0
        for inst in instances:
            p = ROOT_DIR / inst["db_path"]
            if not p.exists():
                continue
            try:
                db = Database(db_path=str(p), environment=Environment.DEMO)
                open_pos = [pos for pos in db.get_all_positions(environment=Environment.DEMO) if pos.get("status") == PositionStatus.OPEN]
                if open_pos:
                    settler = Settler(client=client, db=db)
                    settled = settler.settle(now_iso)
                    if settled:
                        for s in settled:
                            print(f"  [{inst['strategy']}] Settled {s['ticker']} ({s['side']}): result={s.get('result')}, PnL={s.get('pnl_cents')}c", flush=True)
                    rem = len([pos for pos in db.get_all_positions(environment=Environment.DEMO) if pos.get("status") == PositionStatus.OPEN])
                    unresolved += rem
                db.close()
            except Exception:
                pass
        if unresolved == 0:
            print("All candidate open positions resolved!", flush=True)
            break
        if attempt < 7:
            time.sleep(5)


def run_phased_eval(phase_duration: int = 3600, poll_interval: int = 60, upload_logs: bool = True):
    os.makedirs(ROOT_DIR / "db", exist_ok=True)
    os.makedirs(ROOT_DIR / "log", exist_ok=True)

    total_phases = len(PHASE_CONFIG)
    total_duration = phase_duration * total_phases
    run_start_time = time.time()
    run_end_time = run_start_time + total_duration

    combined_log_path = ROOT_DIR / "log" / "parallel_run.log"
    combined_log = open(combined_log_path, "a", buffering=1)

    python_bin = sys.executable

    print("\n" + "=" * 76, flush=True)
    print("STARTING 3-HOUR PHASED TRADING BOT EVALUATION RUN", flush=True)
    print(f"Target Total Duration: {total_duration / 3600:.1f} hours ({total_phases} x {phase_duration / 60:.0f}m phases)", flush=True)
    print(f"Start Time (UTC): {datetime.fromtimestamp(run_start_time, tz=timezone.utc).isoformat()}", flush=True)
    print(f"Target End Time (UTC): {datetime.fromtimestamp(run_end_time, tz=timezone.utc).isoformat()}", flush=True)
    print("Live Strategy Rotation Schedule:")
    for cfg in PHASE_CONFIG:
        print(f"  - Phase {cfg['phase']}: {cfg['live_strat'].upper()} (LIVE against demo.kalshi.co), rest Simulated", flush=True)
    print("=" * 76 + "\n", flush=True)

    stop_requested = False

    def handle_signal(sig, frame):
        nonlocal stop_requested
        print("\n[STOP REQUESTED] Gracefully terminating 4-hour phased run...", flush=True)
        stop_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    for phase_idx, phase_info in enumerate(PHASE_CONFIG):
        if stop_requested:
            break

        phase_num = phase_info["phase"]
        live_strat = phase_info["live_strat"]
        phase_name = phase_info["name"]

        phase_start = time.time()
        phase_end = phase_start + phase_duration

        print("\n" + "#" * 76, flush=True)
        print(f"ENTER {phase_name.upper()} (Phase {phase_num}/{total_phases})", flush=True)
        print(f"Live Strategy: {live_strat} -> demo.kalshi.co", flush=True)
        print(f"Phase Duration: {phase_duration}s (ends at ~{datetime.fromtimestamp(phase_end, tz=timezone.utc).strftime('%H:%M:%S UTC')})", flush=True)
        print("#" * 76 + "\n", flush=True)

        current_instances = []
        for base in STRATEGIES_BASE:
            is_live = (base["strategy"] == live_strat)
            live_label = "LIVE" if is_live else "Sim"
            tag = f"{base['strategy']} ({base['env']}, {live_label})"
            current_instances.append({
                "strategy": base["strategy"],
                "env": base["env"],
                "live": is_live,
                "tag": tag,
                "db_path": base["db_path"],
                "log_path": base["log_path"],
                "flags": base["flags"],
            })

        meta_content = {
            "start_time": run_start_time,
            "end_time": run_end_time,
            "phase": phase_num,
            "total_phases": total_phases,
            "phase_name": phase_name,
            "phase_live_strategy": live_strat,
            "phase_end_time": phase_end,
            "instances": current_instances,
        }
        with open(ROOT_DIR / ".parallel_run_meta.json", "w") as mf:
            json.dump(meta_content, mf, indent=2)

        processes = {}
        log_handles = {}

        for i, inst in enumerate(current_instances):
            tag = inst["tag"]
            log_path = ROOT_DIR / inst["log_path"]
            log_f = open(log_path, "a", buffering=1)
            log_handles[tag] = log_f

            offset = (i * 12) % poll_interval
            cmd = [
                python_bin,
                "-u",
                "main.py",
                "--strategy", inst["strategy"],
                "--env", inst["env"],
                "--interval", str(poll_interval),
                "--offset", str(offset),
                "--db", inst["db_path"],
            ]
            if inst["live"]:
                cmd.append("--live")
            cmd.extend(inst["flags"])

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
                args=(p, tag, log_f, combined_log),
                daemon=True,
            )
            t.start()
            print(f"  [+] Spawned [{tag}] PID={p.pid} (offset=+{offset}s)", flush=True)

        # Loop until phase_duration elapses
        last_interim = time.time()
        while time.time() < phase_end and not stop_requested:
            time.sleep(2)
            if time.time() - last_interim >= 600:  # 10-minute progress print
                last_interim = time.time()
                rem = max(0, int(phase_end - time.time()))
                overall_rem = max(0, int(run_end_time - time.time()))
                print(f"\n--- Progress: Phase {phase_num}/{total_phases} ({rem // 60}m remaining in phase, {overall_rem // 3600}h {(overall_rem % 3600) // 60}m left overall) ---", flush=True)
                print_report(current_instances)

        # Terminate processes for this phase
        print(f"\nPhase {phase_num} finished. Terminating worker processes for phase transition...", flush=True)
        for tag, p in processes.items():
            if p.poll() is None:
                p.terminate()
        for tag, p in processes.items():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in log_handles.values():
            try:
                f.close()
            except Exception:
                pass

        # Interim settlement
        settle_open_positions(current_instances)
        print_report(current_instances)

    # Post-run full settlement and report
    print("\n" + "=" * 76, flush=True)
    print("ALL 4 PHASES COMPLETE! RUNNING FINAL SETTLEMENT & TELEMETRY SYNC...", flush=True)
    print("=" * 76, flush=True)
    final_instances = [
        {"strategy": b["strategy"], "tag": f"{b['strategy']} (demo)", "db_path": b["db_path"]}
        for b in STRATEGIES_BASE
    ]
    settle_open_positions(final_instances)
    print_report(final_instances)

    try:
        combined_log.close()
    except Exception:
        pass

    # Upload run logs to object storage
    if upload_logs:
        try:
            from storage import LogStorageManager, get_default_config
            mgr = LogStorageManager(get_default_config(env="demo"))
            if mgr.is_configured:
                print(f"Uploading run logs to {mgr.config.backend.upper()} object storage...", flush=True)
                run_tag = f"phased_run_{int(run_start_time)}"
                files_to_upload = [combined_log_path, ROOT_DIR / ".parallel_run_meta.json"]
                for b in STRATEGIES_BASE:
                    if (ROOT_DIR / b["db_path"]).exists():
                        files_to_upload.append(ROOT_DIR / b["db_path"])
                    if (ROOT_DIR / b["log_path"]).exists():
                        files_to_upload.append(ROOT_DIR / b["log_path"])
                uploaded = mgr.upload_run_logs(run_tag=run_tag, files=files_to_upload)
                if uploaded:
                    print(f"Successfully uploaded {len(uploaded)} files to {mgr.config.backend} object storage under run_tag '{run_tag}'.", flush=True)
        except Exception as e:
            print(f"Notice: Object storage upload skipped ({e}). All logs preserved locally.", flush=True)

    print("\n4-hour phased run orchestration finished cleanly.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="4-Hour Phased Strategy Evaluation Orchestrator")
    parser.add_argument("--phase-duration", type=int, default=3600, help="Duration of each phase in seconds (default: 3600 = 1 hour)")
    parser.add_argument("--interval", type=int, default=60, help="Strategy poll interval in seconds (default: 60)")
    parser.add_argument("--no-upload-logs", dest="upload_logs", action="store_false", help="Skip remote object storage upload")
    args = parser.parse_args()

    run_phased_eval(phase_duration=args.phase_duration, poll_interval=args.interval, upload_logs=args.upload_logs)


if __name__ == "__main__":
    main()
