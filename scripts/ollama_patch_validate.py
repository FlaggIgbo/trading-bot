#!/usr/bin/env python3
"""
Safety gate for ollama_codex_loop.sh: prove the tree still runs after a model-generated patch.

What this can and cannot do:
- Runs a smoke compare_historical (small market sample, no LLM) — catches import/syntax/runtime breaks.
- Runs scripts/backtest.py --json on --backtest-days (default 30); snapshots store historical recs unless
  you use --re-evaluate inside backtest (not used for the primary run here).
- Optional baseline JSON + env gates (unchanged semantics on the primary run).
- For synthetic mode, optionally runs extra 7d/30d/90d windows with backtest --re-evaluate to catch
  trade-count collapse when strategy code changes (on by default; disable with --no-reeval-multiwindow).

Env:
- OLLAMA_REQUIRE_PNL_IMPROVE=1: with baseline-json, fail if post-patch pnl < baseline (minus tolerance).
- OLLAMA_PNL_TOLERANCE_CENTS=N: tolerance for the improve gate.
- OLLAMA_VALIDATE_MAX_BACKTEST_DROP_CENTS=N: with baseline-json, fail if pnl drops more than N vs baseline.
- OLLAMA_VALIDATE_REGRESSION_METRICS=1: with baseline-json, reject if Sharpe regresses or max drawdown worsens
  (requires baseline JSON from the same backtest --json shape). Tolerances:
  OLLAMA_SHARPE_TOLERANCE (default 0.05), OLLAMA_MAX_DD_TOLERANCE_CENTS (default 0).

Process note (LLM loop): prefer parameter-only or reviewed patches; full code mutation should stay rare.

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


def _backtest_json(
    repo: Path, py: Path, mode: str, days: int, timeout: int, *, re_evaluate: bool = False
) -> dict:
    cmd = [
        str(py),
        str(repo / "scripts" / "backtest.py"),
        "--mode",
        mode,
        "--days",
        str(days),
        "--json",
    ]
    if re_evaluate:
        cmd.append("--re-evaluate")
    r = _run(cmd, cwd=repo, timeout=timeout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or r.stdout or "")
        raise SystemExit(f"backtest.py failed (exit {r.returncode})")
    line = (r.stdout or "").strip().splitlines()
    if not line:
        raise SystemExit("backtest --json produced no output")
    return json.loads(line[-1])


def _compare_to_baseline(
    post: dict,
    baseline_path: Path,
    *,
    require_improve: bool,
    tol: int,
    max_drop: int | None,
) -> int:
    base = _load_json(baseline_path)
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
    elif max_drop is not None:
        floor = b_pnl - max_drop
        if p_pnl < floor:
            print(
                f"❌ OLLAMA_VALIDATE_MAX_BACKTEST_DROP_CENTS: pnl {p_pnl}¢ < floor {floor}¢ "
                f"(baseline {b_pnl}¢, max drop {max_drop}¢)",
                file=sys.stderr,
            )
            return 1
        print(f"✅ Backtest floor: {p_pnl}¢ >= {floor}¢ (baseline {b_pnl}¢, max drop {max_drop}¢)")
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


def _parse_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _regression_metrics_gate(post: dict, baseline_path: Path) -> int:
    """Reject patch if risk-adjusted metrics regress vs baseline (optional strict gate)."""
    base = _load_json(baseline_path)
    sharpe_tol = float(os.environ.get("OLLAMA_SHARPE_TOLERANCE", "0.05"))
    dd_tol = float(os.environ.get("OLLAMA_MAX_DD_TOLERANCE_CENTS", "0"))

    checked = False
    b_sh = _parse_float(base.get("sharpe"))
    p_sh = _parse_float(post.get("sharpe"))
    if b_sh is not None and p_sh is not None:
        checked = True
        if p_sh < b_sh - sharpe_tol:
            print(
                f"❌ OLLAMA_VALIDATE_REGRESSION_METRICS: sharpe {p_sh:.4f} < baseline {b_sh:.4f} - {sharpe_tol}",
                file=sys.stderr,
            )
            return 1

    b_dd = _parse_float(base.get("max_drawdown_cents"))
    p_dd = _parse_float(post.get("max_drawdown_cents"))
    if b_dd is not None and p_dd is not None:
        checked = True
        # Larger drawdown magnitude = worse (both typically positive in our JSON).
        if p_dd > b_dd + dd_tol:
            print(
                f"❌ OLLAMA_VALIDATE_REGRESSION_METRICS: max_drawdown_cents {p_dd:.1f} > "
                f"baseline {b_dd:.1f} + tol {dd_tol}",
                file=sys.stderr,
            )
            return 1

    if checked:
        print("✅ Regression metrics gate: Sharpe / max drawdown vs baseline OK")
    else:
        print(
            "ℹ️  OLLAMA_VALIDATE_REGRESSION_METRICS set but baseline/post missing sharpe or "
            "max_drawdown_cents — skipped metric comparison (use backtest.py --json output as baseline)"
        )
    return 0


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
    ap.add_argument(
        "--no-reeval-multiwindow",
        action="store_true",
        help="Skip extra synthetic 7/30/90d runs with --re-evaluate",
    )
    args = ap.parse_args()

    repo = args.repo.resolve()
    py = repo / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)

    require_improve = os.environ.get("OLLAMA_REQUIRE_PNL_IMPROVE", "").lower() in ("1", "true", "yes")
    tol = int(os.environ.get("OLLAMA_PNL_TOLERANCE_CENTS", "0"))
    max_drop_raw = os.environ.get("OLLAMA_VALIDATE_MAX_BACKTEST_DROP_CENTS", "").strip()
    max_drop = int(max_drop_raw) if max_drop_raw.isdigit() else None
    check_regression = os.environ.get("OLLAMA_VALIDATE_REGRESSION_METRICS", "").lower() in ("1", "true", "yes")

    _smoke(repo, py, args.smoke_markets, args.smoke_timeout)
    print(f"✅ Smoke OK ({py.name} compare_historical --markets {args.smoke_markets})")

    if args.smoke_only:
        return 0

    post = _backtest_json(
        repo, py, args.backtest_mode, args.backtest_days, args.backtest_timeout, re_evaluate=False
    )
    print(
        f"✅ Backtest OK: mode={post.get('mode')} trades={post.get('trades')} "
        f"pnl_cents={post.get('total_pnl_cents')}"
    )

    if args.baseline_json and args.baseline_json.is_file():
        rc = _compare_to_baseline(
            post, args.baseline_json, require_improve=require_improve, tol=tol, max_drop=max_drop
        )
        if rc != 0:
            return rc
        if check_regression:
            rc = _regression_metrics_gate(post, args.baseline_json)
            if rc != 0:
                return rc

    if args.backtest_mode == "synthetic" and not args.no_reeval_multiwindow:
        for days in (7, 30, 90):
            print(f"--- Re-eval window {days}d (synthetic, --re-evaluate) ---")
            w = _backtest_json(repo, py, "synthetic", days, args.backtest_timeout, re_evaluate=True)
            trades = int(w.get("trades") or 0)
            pnl = w.get("total_pnl_cents")
            sharpe = w.get("sharpe")
            print(f"  {days}d: trades={trades} pnl={pnl}¢ sharpe={sharpe}")
            if days >= 30 and trades < 5:
                print(
                    f"❌ Trade count collapse in {days}d re-eval window ({trades} trades)",
                    file=sys.stderr,
                )
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
