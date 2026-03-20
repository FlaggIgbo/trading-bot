#!/usr/bin/env bash
# Validated Ollama → patch loop (no blind YOLO to production).
# launchd: runs this file from disk each StartInterval; git pull + kickstart picks up edits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${OLLAMA_REPO:-$SCRIPT_DIR}"
LOG_DIR="${OLLAMA_LOG_DIR:-$HOME/comparisons}"
LOG_FILE="$LOG_DIR/last_run.log"
METRICS="$LOG_DIR/metrics.csv"
HTML="$REPO/docs/ollama_runs.html"
JOURNAL="${OLLAMA_JOURNAL:-$REPO/docs/ollama_journal.jsonl}"
PROMPT_TPL="$REPO/ollama_codex_prompt.tpl"
TMP_PROMPT="/tmp/ollama_codex_prompt.txt"
TMP_TAIL="/tmp/ollama_tail.log"
TMP_OUTPUT="/tmp/ollama_codex_output.txt"
BASELINE_JSON="/tmp/ollama_backtest_baseline_$$.json"
PATCH_FILE="/tmp/ollama_codex_patch.diff"
# Backward compat typo
OLLAMA_PORT="${OLLAMA_PORT:-${OLLMAPORT:-11434}}"
OLLMAPATH="${OLLAMA_HOST:-http://127.0.0.1:${OLLAMA_PORT}}"
# Default: coding-specialized model (run once: ollama pull qwen2.5-coder:7b). Override: OLLAMA_MODEL=mistral, llama3.2, etc.
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b}"
OLLAMA_BT_DAYS="${OLLAMA_BT_DAYS:-30}"
OLLAMA_BT_MODE="${OLLAMA_BT_MODE:-synthetic}"
OLLAMA_AUTO_PUSH="${OLLAMA_AUTO_PUSH:-0}"
MAX_STRATEGY_CHARS="${OLLAMA_MAX_STRATEGY_CHARS:-28000}"

if [[ -x "$REPO/.venv/bin/python" ]]; then
  PYTHON="$REPO/.venv/bin/python"
else
  PYTHON="${OLLAMA_PYTHON:-python3}"
fi

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$METRICS")"
mkdir -p "$(dirname "$JOURNAL")"
touch "$JOURNAL"

# --- 1) Resolve latest compare_historical log ---
if [[ -f "$LOG_FILE" ]]; then
  :
else
  latest=$(ls -1t "$LOG_DIR"/*.log "$LOG_DIR"/last_run.log/*.log 2>/dev/null | grep -v "metrics" | head -n1 || true)
  [[ -n "${latest:-}" && -f "$latest" ]] && LOG_FILE="$latest"
fi

tail -n 200 "$LOG_FILE" > "$TMP_TAIL" 2>/dev/null || touch "$TMP_TAIL"
[[ ! -f "$LOG_FILE" ]] && LOG_FILE="$TMP_TAIL"

WIN_RATE=$(grep -iE 'win rate [0-9.]+%|overall win rate [0-9.]+%|trades, [0-9.]+% win rate' "$LOG_FILE" 2>/dev/null | tail -n 1 | grep -oE '[0-9.]+%' | head -1 | tr -d '%' || echo "0")
PNL_CENTS=$(grep -E 'PnL|P&L' "$LOG_FILE" 2>/dev/null | grep -E 'KEEP|SELL' | tail -n 1 | grep -oE '[+-]?[0-9]+¢' | tr -d '¢' | tail -1 || echo "0")
PNL=$(awk "BEGIN {printf \"%.2f\", ${PNL_CENTS:-0}/100}")

echo "$(date -u +%s),$WIN_RATE,$PNL" >> "$METRICS"

# --- 2) HTML metrics table ---
{
  echo "<html><head><style>body{font-family:system-ui,sans-serif;} table{border-collapse:collapse;} td,th{padding:8px; border:1px solid #ccc;}</style></head><body>"
  echo "<h1>Ollama optimization runs</h1><p>Validated loop: backtest baseline + smoke compare_historical before commit.</p><table><tr><th>Timestamp</th><th>Win rate</th><th>PnL</th></tr>"
  grep -E '^[0-9]+,[0-9.]+,' "$METRICS" 2>/dev/null | tail -n 10 | awk '{a[NR]=$0} END {for (i=NR; i>0; i--) print a[i]}' | while IFS=',' read -r ts wr pnl rest; do
    echo "<tr><td>$(date -r "$ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$ts")</td><td>${wr}%</td><td>$pnl</td></tr>"
  done
  echo "</table></body></html>"
} > "$HTML"

# --- 3) Backtest baseline (same DB before model patch; used for optional PnL gate) ---
set +e
"$PYTHON" "$REPO/scripts/backtest.py" --mode "$OLLAMA_BT_MODE" --days "$OLLAMA_BT_DAYS" --json > "$BASELINE_JSON" 2>/tmp/ollama_backtest_err.txt
BT_EC=$?
set -e
if [[ $BT_EC -ne 0 ]]; then
  echo "⚠️  backtest baseline failed (exit $BT_EC); continuing without baseline compare. stderr:" >&2
  cat /tmp/ollama_backtest_err.txt >&2 || true
  BASELINE_JSON=""
fi

# --- 4) Prompt: template + strategy context + logs + metrics + journal ---
_strategy_slice() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  local n
  n=$(wc -c <"$f" | tr -d ' ')
  if [[ "$n" -gt "$MAX_STRATEGY_CHARS" ]]; then
    echo "# Truncated to first ${MAX_STRATEGY_CHARS} bytes (file size ${n})"
    head -c "$MAX_STRATEGY_CHARS" "$f"
    echo ""
    echo "# … (truncated; edit OLLAMA_MAX_STRATEGY_CHARS to raise)"
  else
    cat "$f"
  fi
}

{
  cat "$PROMPT_TPL"
  printf "\n\n### CONFIG (from config.yaml: strategy block only)\n"
  if [[ -f "$REPO/config.yaml" ]]; then
    awk '/^strategy:/{p=1; next} /^storage:/{exit} /^# Local historical storage/{exit} p{print}' "$REPO/config.yaml" | head -n 200 || true
  fi
  printf "\n### MODELS / STRATEGY ENGINE (Python)\n"
  printf "\n# models/strategy_registry.py\n"
  _strategy_slice "$REPO/models/strategy_registry.py"
  printf "\n# models/strategies/vol_regime.py (default strategy_engine)\n"
  _strategy_slice "$REPO/models/strategies/vol_regime.py"
  printf "\n# models/strategies/base.py (interface)\n"
  _strategy_slice "$REPO/models/strategies/base.py"
  printf "\n### RECENT LOG TAIL\n"
  cat "$TMP_TAIL"
  printf "\n### METRICS CSV (last 10 rows)\n"
  grep -E '^[0-9]+,[0-9.]+,' "$METRICS" 2>/dev/null | tail -n 10 || true
  printf "\n### OPTIMIZATION JOURNAL (last 8 lines)\n"
  tail -n 8 "$JOURNAL" 2>/dev/null || true
} > "$TMP_PROMPT"

# --- 5) Ollama (override model: phi4, mistral, etc. if installed) ---
OLLAMA_HOST="$OLLMAPATH" ollama run "$OLLAMA_MODEL" "$(cat "$TMP_PROMPT")" > "$TMP_OUTPUT" || {
  echo "❌ ollama run failed (host=$OLLMAPATH model=$OLLAMA_MODEL)" >&2
  exit 1
}

PATCH=$(awk '/^```diff$/ {flag=1; next} /^```$/ && flag {flag=0} flag' "$TMP_OUTPUT")
MSG=$(grep -i '^Commit message:' "$TMP_OUTPUT" | head -n 1 | cut -d: -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

if [[ -z "$PATCH" ]]; then
  echo "ℹ️  No ```diff block in model output; nothing to apply."
  echo "{\"ts\":$(date +%s),\"event\":\"no_patch\",\"model\":\"$OLLAMA_MODEL\",\"win_rate\":$WIN_RATE}" >>"$JOURNAL"
  exit 0
fi

echo "$PATCH" > "$PATCH_FILE"

cd "$REPO"
if git apply --verbose --recount --whitespace=fix "$PATCH_FILE"; then
  echo "✅ Patch applied to working tree; running validator…"
else
  echo "❌ git apply failed (malformed diff)."
  echo "{\"ts\":$(date +%s),\"event\":\"apply_failed\",\"model\":\"$OLLAMA_MODEL\"}" >>"$JOURNAL"
  exit 1
fi

VALID_ARGS=(--repo "$REPO" --smoke-markets "${OLLAMA_SMOKE_MARKETS:-2}" --backtest-mode "$OLLAMA_BT_MODE" --backtest-days "$OLLAMA_BT_DAYS")
[[ -n "$BASELINE_JSON" && -f "$BASELINE_JSON" ]] && VALID_ARGS+=(--baseline-json "$BASELINE_JSON")

if "$PYTHON" "$REPO/scripts/ollama_patch_validate.py" "${VALID_ARGS[@]}"; then
  echo "✅ Validation passed."
  "$PYTHON" -m py_compile signals/yfinance_client.py 2>/dev/null || true
  git add "$HTML" "$JOURNAL" 2>/dev/null || true
  git add -A
  git commit -m "${MSG:-chore: ollama-optimized bot}" || {
    echo "ℹ️  Nothing to commit (empty diff vs HEAD?)."
  }
  if [[ "$OLLAMA_AUTO_PUSH" == "1" ]]; then
    git push origin HEAD
  fi
  echo "{\"ts\":$(date +%s),\"event\":\"committed\",\"model\":\"$OLLAMA_MODEL\"}" >>"$JOURNAL"
else
  echo "❌ Validation failed — reverting applied patch."
  git apply -R --whitespace=fix "$PATCH_FILE" 2>/dev/null || git checkout -- .
  echo "{\"ts\":$(date +%s),\"event\":\"reverted\",\"model\":\"$OLLAMA_MODEL\"}" >>"$JOURNAL"
  exit 1
fi
