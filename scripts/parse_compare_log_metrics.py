#!/usr/bin/env python3
"""Extract win rate and position-eval PnL (¢) from compare_historical / eval logs.

Matches the spirit of `grep … | tail -1`: use the last matching line in the file
for each signal, so log format tweaks only break if all patterns miss.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


WR_LINE_RES = [
    re.compile(r"overall\s+win\s+rate\s+([0-9]+(?:\.[0-9]+)?)\s*%", re.I),
    re.compile(r"\btrades,\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*win\s+rate\b", re.I),
    re.compile(r"\bwin\s+rate\s+([0-9]+(?:\.[0-9]+)?)\s*%\b", re.I),
    re.compile(r"\bWR\s+([0-9]+(?:\.[0-9]+)?)\s*%\b", re.I),
]
PNL_LINE_RE = re.compile(
    r"^\s*(?:KEEP|SELL)\b[^\n]*\bPnL\s*([+-]?[0-9]+)\s*¢",
    re.I,
)


def _parse(text: str) -> tuple[float, int]:
    lines = text.splitlines()
    win_rate = 0.0
    for line in reversed(lines):
        for pat in WR_LINE_RES:
            m = pat.search(line)
            if m:
                try:
                    win_rate = float(m.group(1))
                except ValueError:
                    continue
                break
        else:
            continue
        break

    pnl_cents = 0
    for line in reversed(lines):
        m = PNL_LINE_RE.match(line)
        if m:
            try:
                pnl_cents = int(m.group(1))
            except ValueError:
                continue
            break

    return win_rate, pnl_cents


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: parse_compare_log_metrics.py <logfile>", file=sys.stderr)
        sys.exit(2)
    p = Path(sys.argv[1])
    if not p.is_file():
        print("0 0")
        return
    wr, cents = _parse(p.read_text(encoding="utf-8", errors="replace"))
    print(f"{wr:g} {cents}")


if __name__ == "__main__":
    main()
