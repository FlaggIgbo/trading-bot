#!/usr/bin/env python3
"""Build a compact, high-signal excerpt from compare/eval logs for LLM prompts.

Skips long runs of routine SUCCESS lines; keeps errors, risk/adaptive/calibration
blocks, large losses, and a short tail fallback.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

INTERESTING_PREFIXES = (
    "[Calibration]",
    "[Adaptive]",
    "[Empirical]",
    "[Risk]",
    "[Position Eval",
    "[Reconcile]",
    "[Fill Attribution]",
    "[Strategy]",
    "Traceback",
    "ERROR",
    "WARNING",
    "Exception",
)

LOSS_RE = re.compile(r"PnL\s*-\d+¢", re.I)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", type=Path)
    ap.add_argument("--max-chars", type=int, default=24000)
    ap.add_argument("--tail-lines", type=int, default=40, help="If excerpt is short, append last N raw lines")
    args = ap.parse_args()

    if not args.logfile.is_file():
        print("(no log file)")
        return

    lines = args.logfile.read_text(encoding="utf-8", errors="replace").splitlines()
    # Focus on recent portion for speed (older SUCCESS spam rarely helps)
    window = lines[-8000:] if len(lines) > 8000 else lines

    picked: list[str] = []
    for line in window:
        low = line.lower()
        if any(line.startswith(p) for p in INTERESTING_PREFIXES):
            picked.append(line)
            continue
        if LOSS_RE.search(line):
            picked.append(line)
            continue
        if "leg edges" in low or "edge trace" in low:
            picked.append(line)
            continue
        if any(
            x in low
            for x in (
                "error",
                "exception",
                "traceback",
                "failed",
                "circuit breaker",
                "blocking ",
                "want_better_price",
                "market_bid_lifted",
                "edge_exit",
                "insufficient book depth",
                "thin book for",
            )
        ):
            picked.append(line)
            continue

    # De-dup while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for ln in picked:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)

    if len(out) < 8:
        out.append("--- last log lines (fallback) ---")
        out.extend(window[-args.tail_lines :])

    text = "\n".join(out)
    if len(text) > args.max_chars:
        text = text[: args.max_chars] + "\n# … (truncated; extract_ollama_log_context --max-chars)"

    print(text)


if __name__ == "__main__":
    main()
