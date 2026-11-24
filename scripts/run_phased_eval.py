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
        "name": "Hour 1: spot_lag PROD (LIVE) & spot_lag DEMO (LIVE)",
        "live_demo_strat": "spot_lag",
    },
    {
        "phase": 2,
        "name": "Hour 2: spot_lag PROD (LIVE) & late_window_scalp DEMO (LIVE)",
        "live_demo_strat": "late_window_scalp",
    },
    {
        "phase": 3,
        "name": "Hour 3: spot_lag PROD (LIVE) & panic_fade DEMO (LIVE)",
        "live_demo_strat": "panic_fade",
    },
]

DEMO_ROTATION_STRATEGIES = [
    {
        "name": "spot_lag demo",
        "strategy": "spot_lag",
        "env": "demo",
        "db_path": "db/trading_bot_spot_lag_demo.db",
        "log_path": "log/trading_bot_spot_lag_demo.log",
        "flags": ["--stop-loss", "--check-spot", "--taker-ioc"],
    },
    {
        "name": "late_window_scalp",
        "strategy": "late_window_scalp",
        "env": "demo",
        "db_path": "db/trading_bot_late_window_scalp_demo.db",
        "log_path": "log/trading_bot_late_window_scalp_demo.log",
        "flags": ["--stop-loss", "--check-spot", "--taker-ioc"],
    },
    {
        "name": "panic_fade",
        "strategy": "panic_fade",
        "env": "demo",
        "db_path": "db/trading_bot_panic_fade_demo.db",
        "log_path": "log/trading_bot_panic_fade_demo.log",
        "flags": ["--stop-loss", "--check-spot"],
    },
]

def get_strategy_instances(phase_num: int) -> list[dict]:
    """
    strategyDetails(hour in (1, 2, 3)):
    - random: demo and not live
    - spot_lag prod: prod and LIVE
    - remaining strategies ['spot_lag demo', 'late_window_scalp', 'panic_fade']:
      for i in range(3):
        if i == hour - 1: return live demo
        else: return simulated demo
    """
    instances = []

    # 1. spot_lag prod (always prod and live across all hours)
    instances.append({
        "name": "spot_lag prod",
        "strategy": "spot_lag",
        "env": "prod",
        "live": True,
        "tag": "spot_lag (prod, LIVE)",
        "db_path": "db/trading_bot_spot_lag_prod.db",
        "log_path": "log/trading_bot_spot_lag_prod.log",
        "flags": ["--stop-loss", "--check-spot", "--taker-ioc"],
    })

    # 2. Remaining demo strategies: rotated live demo
    active_idx = phase_num - 1
    for i, base in enumerate(DEMO_ROTATION_STRATEGIES):
        is_live = (i == active_idx)
        live_label = "LIVE" if is_live else "Sim"
        instances.append({
            "name": base["name"],
            "strategy": base["strategy"],
            "env": base["env"],
            "live": is_live,
            "tag": f"{base['strategy']} (demo, {live_label})",
            "db_path": base["db_path"],
            "log_path": base["log_path"],
            "flags": base["flags"],
        })

    # 3. random (always prod and not live across all hours - simulated on real prod orderbook)
    instances.append({
        "name": "random",
        "strategy": "random",
        "env": "prod",
        "live": False,
        "tag": "random (prod, Sim)",
        "db_path": "db/trading_bot_random_prod.db",
        "log_path": "log/trading_bot_random_prod.log",
        "flags": [],
    })

    return instances

ALL_STRATEGIES = [
    {
        "name": "spot_lag prod",
        "strategy": "spot_lag",
        "env": "prod",
        "tag": "spot_lag (prod, LIVE)",
        "db_path": "db/trading_bot_spot_lag_prod.db",
        "log_path": "log/trading_bot_spot_lag_prod.log",
    },
    {
        "name": "spot_lag demo",
        "strategy": "spot_lag",
        "env": "demo",
        "tag": "spot_lag (demo)",
        "db_path": "db/trading_bot_spot_lag_demo.db",
        "log_path": "log/trading_bot_spot_lag_demo.log",
    },
    {
        "name": "late_window_scalp",
        "strategy": "late_window_scalp",
        "env": "demo",
        "tag": "late_window_scalp (demo)",
        "db_path": "db/trading_bot_late_window_scalp_demo.db",
        "log_path": "log/trading_bot_late_window_scalp_demo.log",
    },
    {
        "name": "panic_fade",
        "strategy": "panic_fade",
        "env": "demo",
        "tag": "panic_fade (demo)",
        "db_path": "db/trading_bot_panic_fade_demo.db",
        "log_path": "log/trading_bot_panic_fade_demo.log",
    },
    {
        "name": "random prod",
        "strategy": "random",
        "env": "prod",
        "tag": "random (prod, Sim)",
        "db_path": "db/trading_bot_random_prod.db",
        "log_path": "log/trading_bot_random_prod.log",
    },
    {
        "name": "random demo",
        "strategy": "random",
        "env": "demo",
        "tag": "random (demo, Sim)",
        "db_path": "db/trading_bot_random_demo.db",
        "log_path": "log/trading_bot_random_demo.log",
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
    client_demo = KalshiClient(base_url="https://demo-api.kalshi.co/trade-api/v2")
    client_prod = KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2", live_orders=True)

    for attempt in range(8):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        unresolved = 0
        for inst in instances:
            p = ROOT_DIR / inst["db_path"]
            if not p.exists():
                continue
            try:
                is_prod = str(inst.get("env", "")).lower() in ("prod", "production")
                env_val = Environment.PRODUCTION if is_prod else Environment.DEMO
                client = client_prod if is_prod else client_demo
                db = Database(db_path=str(p), environment=env_val)
                open_pos = [pos for pos in db.get_all_positions(environment=env_val) if pos.get("status") == PositionStatus.OPEN]
                if open_pos:
                    settler = Settler(client=client, db=db)
                    settled = settler.settle(now_iso)
                    if settled:
                        for s in settled:
                            print(f"  [{inst.get('tag', inst['strategy'])}] Settled {s['ticker']} ({s['side']}): result={s.get('result')}, PnL={s.get('pnl_cents')}c", flush=True)
                    rem = len([pos for pos in db.get_all_positions(environment=env_val) if pos.get("status") == PositionStatus.OPEN])
                    unresolved += rem
                db.close()
            except Exception:
                pass
        if unresolved == 0:
            print("All candidate open positions resolved!", flush=True)
            break
        if attempt < 7:
            time.sleep(5)


def run_phased_eval(
    phase_duration: int = 3600,
    poll_interval: int = 60,
    upload_logs: bool = True,
    start_phase: int = 1,
    resume: bool = False,
):
    os.makedirs(ROOT_DIR / "db", exist_ok=True)
    os.makedirs(ROOT_DIR / "log", exist_ok=True)

    total_phases = len(PHASE_CONFIG)
    phases_to_run = [cfg for cfg in PHASE_CONFIG if cfg["phase"] >= start_phase]
    total_remaining_duration = phase_duration * len(phases_to_run)
    run_start_time = time.time()
    old_meta = None
    if resume and (ROOT_DIR / ".parallel_run_meta.json").exists():
        try:
            with open(ROOT_DIR / ".parallel_run_meta.json", "r") as mf:
                old_meta = json.load(mf)
                run_start_time = old_meta.get("start_time", run_start_time)
        except Exception:
            pass
    run_end_time = (old_meta.get("end_time") if (resume and old_meta and old_meta.get("end_time")) else (time.time() + total_remaining_duration))

    combined_log_path = ROOT_DIR / "log" / "parallel_run.log"
    combined_log = open(combined_log_path, "a", buffering=1)

    python_bin = sys.executable

    print("\n" + "=" * 76, flush=True)
    if resume or start_phase > 1:
        print(f"RESUMING 3-HOUR PHASED TRADING BOT EVALUATION RUN (Starting at Phase {start_phase})", flush=True)
    else:
        print("STARTING 3-HOUR PHASED TRADING BOT EVALUATION RUN (PROD vs DEMO)", flush=True)
    print(f"Total Evaluation Duration: {total_remaining_duration / 3600:.2f} hours ({len(phases_to_run)} phase(s) x {phase_duration / 60:.0f}m)", flush=True)
    print(f"Start Time (UTC): {datetime.fromtimestamp(run_start_time, tz=timezone.utc).isoformat()}", flush=True)
    print(f"Target End Time (UTC): {datetime.fromtimestamp(run_end_time, tz=timezone.utc).isoformat()}", flush=True)
    print("Live Strategy Rotation Schedule:")
    print("  - Continuous: spot_lag (PROD, LIVE) & random (PROD, Sim)")
    for cfg in phases_to_run:
        print(f"  - Phase {cfg['phase']}: {cfg['name']}", flush=True)
    print("=" * 76 + "\n", flush=True)

    # Settle any lingering positions from prior runs
    if resume or start_phase > 1:
        settle_open_positions(ALL_STRATEGIES)

    stop_requested = False

    def handle_signal(sig, frame):
        nonlocal stop_requested
        print("\n[STOP REQUESTED] Gracefully terminating phased run...", flush=True)
        stop_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    for phase_info in phases_to_run:
        if stop_requested:
            break

        phase_num = phase_info["phase"]
        phase_name = phase_info["name"]

        phase_start = time.time()
        if resume and old_meta and old_meta.get("phase") == phase_num and old_meta.get("phase_end_time", 0) > phase_start:
            phase_end = old_meta["phase_end_time"]
            current_phase_duration = max(30, int(phase_end - phase_start))
        else:
            phase_end = phase_start + phase_duration
            current_phase_duration = phase_duration

        print("\n" + "#" * 76, flush=True)
        print(f"ENTER {phase_name.upper()} (Phase {phase_num}/{total_phases})", flush=True)
        print(f"Phase Duration: {current_phase_duration}s (ends at ~{datetime.fromtimestamp(phase_end, tz=timezone.utc).strftime('%H:%M:%S UTC')})", flush=True)
        print("#" * 76 + "\n", flush=True)

        current_instances = get_strategy_instances(phase_num)

        meta_content = {
            "start_time": run_start_time,
            "end_time": run_end_time,
            "phase": phase_num,
            "total_phases": total_phases,
            "phase_name": phase_name,
            "phase_live_strategy": f"spot_lag (prod) & {phase_info.get('live_demo_strat', '')} (demo)",
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
                print_report(ALL_STRATEGIES)

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
        print_report(ALL_STRATEGIES)

    # Post-run full settlement and report
    print("\n" + "=" * 76, flush=True)
    print("ALL 3 PHASES COMPLETE! RUNNING FINAL SETTLEMENT & TELEMETRY SYNC...", flush=True)
    print("=" * 76, flush=True)
    settle_open_positions(ALL_STRATEGIES)
    print_report(ALL_STRATEGIES)

    try:
        combined_log.close()
    except Exception:
        pass

    # Upload run logs to object storage
    if upload_logs:
        try:
            from storage import LogStorageManager, get_default_config
            mgr = LogStorageManager(get_default_config(env="prod"))
            if not mgr.is_configured:
                mgr = LogStorageManager(get_default_config(env="demo"))
            if mgr.is_configured:
                print(f"Uploading run logs to {mgr.config.backend.upper()} object storage...", flush=True)
                run_tag = f"phased_run_{int(run_start_time)}"
                files_to_upload = [combined_log_path, ROOT_DIR / ".parallel_run_meta.json"]
                for b in ALL_STRATEGIES:
                    if (ROOT_DIR / b["db_path"]).exists():
                        files_to_upload.append(ROOT_DIR / b["db_path"])
                    if (ROOT_DIR / b["log_path"]).exists():
                        files_to_upload.append(ROOT_DIR / b["log_path"])
                uploaded = mgr.upload_run_logs(run_tag=run_tag, files=files_to_upload)
                if uploaded:
                    print(f"Successfully uploaded {len(uploaded)} files to {mgr.config.backend} object storage under run_tag '{run_tag}'.", flush=True)
        except Exception as e:
            print(f"Notice: Object storage upload skipped ({e}). All logs preserved locally.", flush=True)

    print("\n3-hour phased run orchestration finished cleanly.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="3-Hour Phased Strategy Evaluation Orchestrator")
    parser.add_argument("--phase-duration", type=int, default=3600, help="Duration of each phase in seconds (default: 3600 = 1 hour)")
    parser.add_argument("--interval", type=int, default=60, help="Strategy poll interval in seconds (default: 60)")
    parser.add_argument("--no-upload-logs", dest="upload_logs", action="store_false", help="Skip remote object storage upload")
    parser.add_argument("--start-phase", type=int, default=1, choices=[1, 2, 3], help="Phase to start/resume from (default: 1)")
    parser.add_argument("--resume", action="store_true", help="Resume previous run preserving start_time telemetry")
    args = parser.parse_args()

    run_phased_eval(
        phase_duration=args.phase_duration,
        poll_interval=args.interval,
        upload_logs=args.upload_logs,
        start_phase=args.start_phase,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
