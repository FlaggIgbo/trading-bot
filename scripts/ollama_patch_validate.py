#!/usr/bin/env python3
"""
Safety gate for ollama_codex_loop.sh: prove the tree still runs after a model-generated patch.

What this can and cannot do:
- Runs a smoke compare_historical (small market sample, no LLM) — catches import/syntax/runtime breaks.
- Optionally runs scripts/backtest.py --json before/after; on the *same* SQLite DB, synthetic/real
  backtest numbers usually do NOT change when you only edit strategy *code* (snapshots store prior recs).
  Set OLLAMA_REQUIRE_PNL_IMPROVE=1 only when comparing against a baseline JSON from an *earlier* DB state.

Usage:
  python3 scripts/ollama_patch_validate.py --repo . --baseline-json /tmp/bt.json
  python3 scripts/ollama_patch_validate.py --repo . --smoke-only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _smoke(repo: Path, py: Path, markets: int, timeout: int) -> None:
    cmd = [
        str(py),
        str(repo / "scripts" / "compare_historical.py"),
        "--markets",
        str(markets),
        "--no-llm",
    ]
    r = _run(cmd, cwd=repo, timeout=timeout)
    if r.returncode != 0:
        sys.stderr.write(r.stdout or "")
        sys.stderr.write(r.stderr or "")
        raise SystemExit(
            f"Smoke compare_historical failed (exit {r.returncode}). Fix the patch or revert.\n"
        )


def _backtest_json(repo: Path, py: Path, mode: str, days: int, timeout: int) -> dict:
    cmd = [
        str(py),
        str(repo / "scripts" / "backtest.py"),
        "--mode",
        mode,
        "--days",
        str(days),
        "--json",
    ]
    r = _run(cmd, cwd=repo, timeout=timeout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or r.stdout or "")
        raise SystemExit(f"backtest.py failed (exit {r.returncode})")
    line = (r.stdout or "").strip().splitlines()
    if not line:
        raise SystemExit("backtest --json produced no output")
    return json.loads(line[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate repo after Ollama-suggested patch")
    ap.add_argument("--repo", type=Path, default=Path("."), help="Project root")
    ap.add_argument("--baseline-json", type=Path, default=None, help="JSON from backtest.py --json before patch")
    ap.add_argument("--smoke-only", action="store_true", help="Only run compare_historical smoke")
    ap.add_argument("--smoke-markets", type=int, default=2, help="--markets for smoke run")
    ap.add_argument("--smoke-timeout", type=int, default=900, help="Seconds for smoke run")
    ap.add_argument("--backtest-mode", choices=["real", "synthetic"], default="synthetic")
    ap.add_argument("--backtest-days", type=int, default=30)
    ap.add_argument("--backtest-timeout", type=int, default=300)
    args = ap.parse_args()

    repo = args.repo.resolve()
    py = repo / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)

    require_improve = os.environ.get("OLLAMA_REQUIRE_PNL_IMPROVE", "").lower() in ("1", "true", "yes")
    tol = int(os.environ.get("OLLAMA_PNL_TOLERANCE_CENTS", "0"))

    _smoke(repo, py, args.smoke_markets, args.smoke_timeout)
    print(f"✅ Smoke OK ({py.name} compare_historical --markets {args.smoke_markets})")

    if args.smoke_only:
        return 0

    post = _backtest_json(repo, py, args.backtest_mode, args.backtest_days, args.backtest_timeout)
    print(
        f"✅ Backtest OK: mode={post.get('mode')} trades={post.get('trades')} "
        f"pnl_cents={post.get('total_pnl_cents')}"
    )

    if args.baseline_json and args.baseline_json.is_file():
        base = _load_json(args.baseline_json)
        b_pnl = int(base.get("total_pnl_cents") or 0)
        p_pnl = int(post.get("total_pnl_cents") or 0)
        b_tr = int(base.get("trades") or 0)
        p_tr = int(post.get("trades") or 0)
        if require_improve:
            if p_pnl + tol < b_pnl:
                print(
                    f"❌ OLLAMA_REQUIRE_PNL_IMPROVE: pnl {p_pnl}¢ < baseline {b_pnl}¢ (tolerance {tol}¢)",
                    file=sys.stderr,
                )
                return 1
            print(f"✅ PnL gate: {p_pnl}¢ >= {b_pnl}¢ - {tol}¢")
        else:
            if b_pnl != p_pnl or b_tr != p_tr:
                print(
                    f"ℹ️  Backtest metrics changed vs baseline (trades {b_tr}→{p_tr}, pnl¢ {b_pnl}→{p_pnl}). "
                    "That can happen if the patch touched config/backtest inputs."
                )
            else:
                print(
                    "ℹ️  Backtest metrics unchanged (expected if only strategy *code* changed; "
                    "DB snapshots/predictions are historical)."
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
