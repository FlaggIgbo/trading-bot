#!/usr/bin/env bash
set -euo pipefail

REPO="$HOME/git/trading-bot"
LOG_DIR="$HOME/comparisons"
# FIX: Ensure LOG is treated as a file path, not just a directory prefix
LOG_FILE="$LOG_DIR/last_run.log"
METRICS="$HOME/comparisons/metrics.csv"
HTML="$REPO/docs/ollama_runs.html"
PROMPT_TPL="$REPO/ollama_codex_prompt.tpl"
TMP_PROMPT="/tmp/ollama_codex_prompt.txt"
TMP_TAIL="/tmp/ollama_tail.log"
TMP_OUTPUT="/tmp/ollama_codex_output.txt"
OLLMAPORT="${OLLMAPORT:-11435}"
OLLMAPATH="http://127.0.0.1:${OLLMAPORT}"

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$METRICS")"

# 1. Fix Log Logic: Find the latest actual log file
# compare_historical writes eval_YYYYMMDDHH.log to LOG_DIR (or LOG_DIR/last_run.log/ when legacy)
if [[ -f "$LOG_FILE" ]]; then
  # If last_run.log exists as file, use it (legacy)
  :
else
  # Find most recent .log: LOG_DIR/*.log and LOG_DIR/last_run.log/*.log (eval_*.log)
  latest=$(ls -1t "$LOG_DIR"/*.log "$LOG_DIR"/last_run.log/*.log 2>/dev/null | grep -v "metrics" | head -n1)
  [[ -n "$latest" && -f "$latest" ]] && LOG_FILE="$latest"
fi

# 2. Extract Metrics safely
# compare_historical prints "win rate" (lowercase) and "PnL" in position-eval; calibration has "overall win rate"
tail -n 200 "$LOG_FILE" > "$TMP_TAIL" 2>/dev/null || touch "$TMP_TAIL"
[[ ! -f "$LOG_FILE" ]] && LOG_FILE="$TMP_TAIL"  # fallback when no log found
# Win rate: position-eval (KEEP/SELL), calibration (overall), or adaptive (Recent Xh)
WIN_RATE=$(grep -iE 'win rate [0-9.]+%|overall win rate [0-9.]+%|trades, [0-9.]+% win rate' "$LOG_FILE" 2>/dev/null | tail -n 1 | grep -oE '[0-9.]+%' | head -1 | tr -d '%' || echo "0")
PNL_CENTS=$(grep -E 'PnL|P&L' "$LOG_FILE" 2>/dev/null | grep -E 'KEEP|SELL' | tail -n 1 | grep -oE '[+-]?[0-9]+¢' | tr -d '¢' | tail -1 || echo "0")
PNL=$(awk "BEGIN {printf \"%.2f\", $PNL_CENTS/100}")

echo "$(date -u +%s),$WIN_RATE,$PNL" >> "$METRICS"

# 3. Generate HTML Summary (only metric rows: timestamp,win_rate,pnl)
{
  echo "<html><head><style>body{font-family:sans-serif;} table{border-collapse:collapse;} td,th{padding:8px; border:1px solid #ccc;}</style></head><body>"
  echo "<h1>Ollama Runs</h1><table><tr><th>Timestamp</th><th>Win rate</th><th>PnL</th></tr>"
  # Only lines matching timestamp,win_rate,pnl (timestamp = numeric Unix)
  grep -E '^[0-9]+,[0-9.]+,' "$METRICS" 2>/dev/null | tail -n 10 | awk '{a[NR]=$0} END {for (i=NR; i>0; i--) print a[i]}' | while IFS=',' read -r ts wr pnl rest; do
    echo "<tr><td>$(date -r "$ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$ts")</td><td>$wr%</td><td>$pnl</td></tr>"
  done
  echo "</table></body></html>"
} > "$HTML"

# 4. Build Prompt and Run
cat "$PROMPT_TPL" > "$TMP_PROMPT"
{
  printf "\n### RECENT LOG DATA (Tail):\n"
  cat "$TMP_TAIL"
  printf "\n### PERFORMANCE METRICS (CSV):\n"
  grep -E '^[0-9]+,[0-9.]+,' "$METRICS" 2>/dev/null | tail -n 10
} >> "$TMP_PROMPT"

# Run Ollama (using llama3.2 since llama3.3 was failing your install)
OLLAMA_HOST="$OLLMAPATH" ollama run llama3.2 "$(cat "$TMP_PROMPT")" > "$TMP_OUTPUT"

# 5. Apply Changes
PATCH=$(awk '/^```diff$/ {flag=1; next} /^```$/ && flag {flag=0} flag' "$TMP_OUTPUT")
MSG=$(grep -i '^Commit message:' "$TMP_OUTPUT" | head -n 1 | cut -d: -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

if [[ -n "$PATCH" ]]; then
  echo "$PATCH" > /tmp/ollama_codex_patch.diff

  cd "$REPO"
  if git apply --verbose --recount --whitespace=fix /tmp/ollama_codex_patch.diff; then
    echo "✅ Patch applied."
    python3 -m py_compile signals/yfinance_client.py || true
    git add docs/ollama_runs.html
    git add .
    git commit -m "${MSG:-chore: auto-optimization}"
  else
    echo "❌ Patch failed. Model output was malformed."
  fi
fi