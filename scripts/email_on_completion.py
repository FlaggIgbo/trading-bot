#!/usr/bin/env python3
"""
Watcher Daemon: Sends Run Report via Email When Parallel Run Completes
=====================================================================
Monitors the ongoing evaluation run until the specified completion time
(or until worker processes terminate), executes post-run settlement sync,
and dispatches the performance report to the designated recipient via SMTP.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

def main():
    parser = argparse.ArgumentParser(description="Watcher to email results on run completion")
    parser.add_argument("--until", type=str, default="2026-11-25T19:45:00Z", help="Target completion timestamp ISO")
    parser.add_argument("--recipient", type=str, default="theotherogeflagg@gmail.com", help="Email recipient")
    args = parser.parse_args()

    dt = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    target_end_ts = dt.timestamp()

    print(f"[{datetime.now(timezone.utc).isoformat()}] Completion email watcher started.", flush=True)
    print(f"Target completion time: {args.until} (~{int(target_end_ts - time.time())}s from now).", flush=True)
    print(f"Recipient: {args.recipient}", flush=True)

    while time.time() < target_end_ts:
        time.sleep(10)

    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Target end time reached! Waiting 15s for database & settler sync...", flush=True)
    time.sleep(15)

    print(f"[{datetime.now(timezone.utc).isoformat()}] Dispatching email report to {args.recipient}...", flush=True)
    cmd = [
        sys.executable,
        str(ROOT_DIR / "scripts" / "send_run_report_email.py"),
        "--recipient", args.recipient,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print("STDOUT:", res.stdout)
    if res.stderr:
        print("STDERR:", res.stderr)
    print(f"[{datetime.now(timezone.utc).isoformat()}] Completion watcher finished.", flush=True)


if __name__ == "__main__":
    main()
