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
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
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

            if "insufficient_balance" in line or "insufficient_funds" in line:
                print(f"\n🚨 [OUT OF MONEY] Kalshi prod balance depleted in [{tag}]: {clean}", flush=True)
                if error_records is not None:
                    error_records.append((tag, "OUT_OF_MONEY", clean))
                if stop_event is not None:
                    stop_event.set()

            if stop_on_error and stop_event is not None and not stop_event.is_set():
                is_err = (
                    ("[ERROR]" in line and "order_already_exists" not in line)
                    or "[CRITICAL]" in line
                    or "Traceback (most recent call last)" in line
                )
                is_warn = (
                    "[WARNING]" in line
                    and "UNFILLED" not in line
                    and "did not execute" not in line
                )
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
        explicit_sl = any(p.lower() in ("sl", "stoploss", "stop_loss") for p in parts[2:]) or (len(parts) > 3 and parts[3].lower() in ("true", "1", "sl"))
        explicit_nosl = any(p.lower() in ("nosl", "no_sl") for p in parts[2:]) or (len(parts) > 3 and parts[3].lower() in ("false", "0", "nosl"))
        stop_loss = (
            explicit_sl
            or ("_sl" in strat)
            or (live and "5m" in strat and not explicit_nosl)
        )

        take_profit = None
        entry_timeout = None
        for p in parts[3:]:
            m_tp = re.search(r"tp_?(\d+)", p.lower())
            if m_tp:
                take_profit = int(m_tp.group(1))
            m_to = re.search(r"(?:to|timeout)_?(\d+)", p.lower())
            if m_to:
                entry_timeout = float(m_to.group(1))

        explicit_ioc = any(p.lower() in ("ioc", "taker_ioc", "taker") for p in parts[2:])
        taker_ioc = explicit_ioc or ("regime" in strat.lower())

        explicit_regime = any(p.lower() in ("regime", "regime_filter") for p in parts[2:])
        regime_filter = explicit_regime or ("regime" in strat.lower())

        key = f"{strat}_{env}{'_sl' if stop_loss else ''}{f'_tp{take_profit}' if take_profit else ''}"
        live_str = ", LIVE" if live else ""
        sl_str = ", SL" if stop_loss else ""
        tp_str = f", TP{take_profit}c" if take_profit else ""
        tag = f"{strat} ({env}{live_str}{sl_str}{tp_str})"
        instances.append({
            "raw": item,
            "strategy": strat,
            "env": env,
            "live": live,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_timeout": entry_timeout,
            "taker_ioc": taker_ioc,
            "regime_filter": regime_filter,
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
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="Target end time in local/ET time (e.g. '3pm', '15:00', '15:00:00')",
    )
    args = parser.parse_args()

    if args.until:
        target_str = args.until.strip().lower()
        now_local = datetime.now().astimezone()
        target_hour = 15
        target_min = 0
        m = re.search(r"(\d+)(?::(\d+))?\s*(am|pm)?", target_str)
        if m:
            h = int(m.group(1))
            target_min = int(m.group(2)) if m.group(2) else 0
            ampm = m.group(3)
            if ampm == "pm" and h < 12:
                h += 12
            elif ampm == "am" and h == 12:
                h = 0
            target_hour = h
        target_dt = now_local.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if target_dt <= now_local:
            target_dt += timedelta(days=1)
        calc_dur = int((target_dt - now_local).total_seconds())
        if calc_dur > 0:
            args.duration = calc_dur

    instances = parse_instances(args.strategies)
    start_time = time.time()
    end_time = start_time + args.duration
    os.environ["PARALLEL_RUN_END_TIME"] = str(end_time)

    meta_file = ROOT_DIR / ".parallel_run_meta.json"
    try:
        import json
        with open(meta_file, "w") as f:
            json.dump({
                "start_time": start_time,
                "end_time": end_time,
                "duration": args.duration,
                "instances": instances,
            }, f, indent=2)
    except Exception:
        pass

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

    venv_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python")
    python_bin = venv_py if os.path.exists(venv_py) else sys.executable

    for i, inst in enumerate(instances):
        tag = inst["tag"]
        log_file = open(inst["log_path"], "a")
        log_files[tag] = log_file

        # Stagger offsets by 8s across interval to prevent Kalshi API rate limit collisions
        offset = (i * 8) % args.interval
        cmd = [
            python_bin,
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
        if inst.get("take_profit"):
            cmd.extend(["--take-profit", str(inst["take_profit"])])
        if inst.get("entry_timeout"):
            cmd.extend(["--entry-timeout", str(inst["entry_timeout"])])
        if inst.get("taker_ioc"):
            cmd.append("--taker-ioc")
        if inst.get("regime_filter"):
            cmd.append("--regime-filter")
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

        # Post-run settlement: resolve any remaining open positions
        print("\n" + "=" * 70, flush=True)
        print("POST-RUN SETTLEMENT: Resolving any open positions across databases...", flush=True)
        print("=" * 70, flush=True)
        from risk_engine.settler import Settler
        from ingestion.api.client import KalshiClient
        from db import Environment, PositionStatus
        for attempt in range(12):  # Poll up to 2 minutes for market closures
            now_utc = datetime.now(timezone.utc)
            now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            unresolved = 0
            for inst in instances:
                db_path = inst["db_path"]
                if not Path(db_path).exists():
                    continue
                try:
                    env = Environment.PRODUCTION if str(inst["env"]).lower() in ("prod", "production") else Environment.DEMO
                    base_url = "https://api.elections.kalshi.com/trade-api/v2" if env == Environment.PRODUCTION else "https://demo-api.kalshi.co/trade-api/v2"
                    client = KalshiClient(base_url=base_url)
                    db = Database(db_path=db_path, environment=env)
                    open_pos = [p for p in db.get_all_positions(environment=env) if p.get("status") == PositionStatus.OPEN]
                    if open_pos:
                        settler = Settler(client=client, db=db)
                        settled = settler.settle(now_iso)
                        if settled:
                            for s in settled:
                                print(f"  [{inst['tag']}] Settled {s['ticker']} ({s['side']}): result={s.get('result')}, PnL={s.get('pnl_cents')}c", flush=True)
                        remaining_open = len([p for p in db.get_all_positions(environment=env) if p.get("status") == PositionStatus.OPEN])
                        unresolved += remaining_open
                    db.close()
                except Exception as e:
                    pass
            if unresolved == 0:
                print("All open positions are fully resolved and settled!", flush=True)
                break
            if attempt < 11:
                time.sleep(10)

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
    last_balance_check = 0.0
    has_live_prod = any(inst.get("live") and inst.get("env") == "prod" for inst in instances)

    try:
        while time.time() < end_time and not stop_event.is_set():
            time.sleep(min(2, max(1, int(end_time - time.time()))))

            # Periodic interim status report every 15 minutes (900s)
            if time.time() - last_interim_report >= 900:
                print("\n>>> [INTERIM 15-MINUTE PERFORMANCE UPDATE] <<<", flush=True)
                print_report(instances)
                last_interim_report = time.time()

            # Check prod cash balance every 30s: halt entire run if out of money
            if has_live_prod and time.time() - last_balance_check >= 30:
                last_balance_check = time.time()
                try:
                    from ingestion.api.client import KalshiClient
                    bal_client = KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2", live_orders=True)
                    url = f"{bal_client.base_url}/portfolio/balance"
                    headers = bal_client._build_auth_headers("GET", url)
                    res = bal_client._request("GET", url, headers=headers)
                    if res.status_code == 200:
                        bal_data = res.json()
                        breakdowns = bal_data.get("balance_breakdown", [])
                        crypto_bal = 0.0
                        for b in breakdowns:
                            if b.get("exchange_index") == 2:
                                crypto_bal = float(b.get("balance", 0.0))
                        port_val = float(bal_data.get("portfolio_value", 0)) / 100.0
                        total_cash = float(bal_data.get("balance_dollars", 0.0))
                        # If available cash is < $0.25 (cannot place minimum size order) and portfolio is < $0.25
                        if (crypto_bal < 0.25 or total_cash < 0.25) and port_val < 0.25:
                            msg = f"Kalshi prod balance depleted! (Crypto cash: ${crypto_bal:.2f}, Total cash: ${total_cash:.2f}, Portfolio: ${port_val:.2f})"
                            print(f"\n🚨 [OUT OF MONEY] {msg} Halting entire parallel run.", flush=True)
                            error_records.append(("prod", "OUT_OF_MONEY", msg))
                            stop_event.set()
                            break
                except Exception:
                    pass

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
