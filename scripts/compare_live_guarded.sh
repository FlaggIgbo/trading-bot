#!/usr/bin/env bash
# Optional two-phase runner: fast preview (no LLM) then full run with --execute if trades showed up.
# Repo launchd plist already passes --execute once; use this wrapper only if you want a safety gate.
#
# Usage:
#   COMPARE_TWO_PHASE=1 ./scripts/compare_live_guarded.sh --days 365 --use-volatility --execute ...
# Without COMPARE_TWO_PHASE=1, forwards all args to compare_historical.py unchanged.
#
# Env:
#   COMPARE_TWO_PHASE=1   Enable preview-then-execute gate
#   COMPARE_PYTHON        Python binary (default: repo .venv or python3)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PY="$ROOT/.venv/bin/python3"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${COMPARE_PYTHON:-python3}"
fi
SCRIPT="$ROOT/scripts/compare_historical.py"

run_phased() {
  local -a base=()
  local a
  for a in "$@"; do
    [[ "$a" == "--execute" ]] && continue
    [[ "$a" == "--preview" ]] && continue
    base+=("$a")
  done
  local log
  log="$(mktemp -t compare_preview.XXXXXX)"
  echo "[compare_live_guarded] Phase 1: ${PY} $SCRIPT ${base[*]} --preview --no-llm" >&2
  set +e
  "$PY" "$SCRIPT" "${base[@]}" --preview --no-llm >"$log" 2>&1
  local ec=$?
  set -e
  if [[ $ec -ne 0 ]]; then
    echo "[compare_live_guarded] Phase 1 failed (exit $ec); log: $log" >&2
    cat "$log" >&2
    return "$ec"
  fi
  if grep -qE '\[Lottery Limit\]|PROPOSED TRADES|--- BUYS \(|--- SELLS \(|Placed BUY|EXECUTE:' "$log"; then
    echo "[compare_live_guarded] Phase 1: trade activity detected → running full command with your args." >&2
    rm -f "$log"
    exec "$PY" "$SCRIPT" "$@"
  fi
  echo "[compare_live_guarded] Phase 1: no trade patterns in log; skipping --execute pass (saving API work)." >&2
  cat "$log"
  rm -f "$log"
}

if [[ "${COMPARE_TWO_PHASE:-0}" == "1" || "${COMPARE_TWO_PHASE:-0}" == "true" ]]; then
  run_phased "$@"
else
  exec "$PY" "$SCRIPT" "$@"
fi
