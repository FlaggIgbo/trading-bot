#!/usr/bin/env bash
# Validated Ollama → patch loop (no blind YOLO to production).
# launchd: runs this file from disk on each StartCalendarInterval; git pull + kickstart picks up edits.
set -euo pipefail

_ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
_log() { echo "[$(_ts)] ollama_loop $*"; }

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
# Prefer OLLAMA_PORT; OLLMAPORT is legacy typo alias (deprecated).
if [[ -z "${OLLAMA_PORT:-}" && -n "${OLLMAPORT:-}" ]]; then
  OLLAMA_PORT="$OLLMAPORT"
fi
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
OLLMAPATH="${OLLAMA_HOST:-http://127.0.0.1:${OLLAMA_PORT}}"
# Default: coding-specialized model (run once: ollama pull qwen2.5-coder:7b). Override: OLLAMA_MODEL=mistral, llama3.2, etc.
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b}"
OLLAMA_BT_DAYS="${OLLAMA_BT_DAYS:-30}"
OLLAMA_BT_MODE="${OLLAMA_BT_MODE:-synthetic}"
# With baseline JSON: reject patches that worsen Sharpe or max drawdown vs pre-patch backtest (see scripts/ollama_patch_validate.py). Set 0 to disable.
export OLLAMA_VALIDATE_REGRESSION_METRICS="${OLLAMA_VALIDATE_REGRESSION_METRICS:-1}"
OLLAMA_AUTO_PUSH="${OLLAMA_AUTO_PUSH:-0}"
# Commit docs/ollama_runs.html, series_leaderboard.html, journal after each refresh — not only when Ollama ships a code patch
OLLAMA_AUTO_COMMIT_DOCS="${OLLAMA_AUTO_COMMIT_DOCS:-1}"
MAX_STRATEGY_CHARS="${OLLAMA_MAX_STRATEGY_CHARS:-28000}"
# Default off: flat / misparsed PnL in metrics.csv used to skip Ollama forever (see docs/PNL_INVESTIGATION.md). Set OLLAMA_METRICS_GATE=1 to only run when PnL drops ≥ OLLAMA_METRICS_GATE_DROP in the window.
OLLAMA_METRICS_GATE="${OLLAMA_METRICS_GATE:-0}"
OLLAMA_METRICS_GATE_HOURS="${OLLAMA_METRICS_GATE_HOURS:-4}"
OLLAMA_METRICS_GATE_DROP="${OLLAMA_METRICS_GATE_DROP:-0.20}"
OLLAMA_LEADERBOARD_HOURS="${OLLAMA_LEADERBOARD_HOURS:-336}"
OLLAMA_BLACKLIST_MIN_TRADES="${OLLAMA_BLACKLIST_MIN_TRADES:-80}"
OLLAMA_TEMPERATURE="${OLLAMA_TEMPERATURE:-0.2}"
# Max generate→apply→validate rounds per launchd tick (reduces one-shot bad patches; each fail reverts before retry)
OLLAMA_MAX_PATCH_ATTEMPTS="${OLLAMA_MAX_PATCH_ATTEMPTS:-3}"
OLLAMA_LOG_CONTEXT_MAX_CHARS="${OLLAMA_LOG_CONTEXT_MAX_CHARS:-24000}"
# Shorter window for the Ollama prompt (so the model sees recent bleeding assets)
OLLAMA_PROMPT_LB_HOURS="${OLLAMA_PROMPT_LB_HOURS:-72}"
# First metrics row when metrics.csv is empty: window length (hours) ending at now_ts
OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS="${OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS:-24}"
SERIES_SUMMARY_HTML="${SERIES_SUMMARY_HTML:-/tmp/series_summary.html}"
# Auto-blacklist: writes newline series file; compare_historical merges via strategy.blacklist_extra_file (no YAML corruption)
OLLAMA_AUTO_BLACKLIST="${OLLAMA_AUTO_BLACKLIST:-1}"
BLACKLIST_EXTRA_FILE="${BLACKLIST_EXTRA_FILE:-$HOME/.trading-bot-data/series_blacklist_auto.txt}"
LB_HTML="$REPO/docs/series_leaderboard.html"
SUGGEST_TMP="/tmp/ollama_blacklist_suggest.txt"

if [[ -x "$REPO/.venv/bin/python" ]]; then
  PYTHON="$REPO/.venv/bin/python"
else
  PYTHON="${OLLAMA_PYTHON:-python3}"
fi

# Stage + commit dashboard artifacts when they differ from HEAD (honors OLLAMA_AUTO_PUSH).
_commit_dashboard_if_changed() {
  [[ "${OLLAMA_AUTO_COMMIT_DOCS:-1}" == "1" ]] || return 0
  [[ -d "$REPO/.git" ]] || return 0
  local f
  for f in "$HTML" "$LB_HTML" "$JOURNAL"; do
    [[ -f "$f" ]] || continue
    git -C "$REPO" add -- "$f" 2>/dev/null || true
  done
  if git -C "$REPO" diff --cached --quiet 2>/dev/null; then
    return 0
  fi
  if git -C "$REPO" commit -m "chore: refresh Ollama dashboard (ollama_runs, series leaderboard, journal)"; then
    echo "✅ Committed dashboard files."
    if [[ "${OLLAMA_AUTO_PUSH:-0}" == "1" ]]; then
      if git -C "$REPO" push origin HEAD; then
        echo "✅ Pushed dashboard commit."
      else
        echo "⚠️  git push failed (credentials/network)." >&2
      fi
    fi
  else
    echo "ℹ️  Dashboard commit not created (pre-commit hook or empty author?)." >&2
  fi
}

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$METRICS")"
mkdir -p "$(dirname "$JOURNAL")"
touch "$JOURNAL"

_log "start repo=$REPO python=$PYTHON ollama_host=${OLLMAPATH} model=${OLLAMA_MODEL} log_dir=$LOG_DIR metrics_gate=${OLLAMA_METRICS_GATE}"

# --- 1) Resolve latest compare_historical log (prefer newest mtime among last_run.log + eval_*.log) ---
LOG_CANDIDATES=()
[[ -f "$LOG_DIR/last_run.log" ]] && LOG_CANDIDATES+=("$LOG_DIR/last_run.log")
shopt -s nullglob
for f in "$LOG_DIR"/eval_*.log; do
  LOG_CANDIDATES+=("$f")
done
shopt -u nullglob
if ((${#LOG_CANDIDATES[@]} > 0)); then
  LOG_FILE=$(ls -1t "${LOG_CANDIDATES[@]}" 2>/dev/null | head -n1)
fi
if [[ -z "${LOG_FILE:-}" || ! -f "$LOG_FILE" ]]; then
  latest=$(ls -1t "$LOG_DIR"/*.log "$LOG_DIR"/last_run.log/*.log 2>/dev/null | grep -vE 'metrics|ollama' | head -n1 || true)
  [[ -n "${latest:-}" && -f "$latest" ]] && LOG_FILE="$latest"
fi
[[ -z "${LOG_FILE:-}" ]] && LOG_FILE="$LOG_DIR/last_run.log"

# Path shown in ollama_runs.html (before we fall back to empty TMP_TAIL for parsing)
METRICS_LOG_LABEL="$LOG_FILE"
if [[ -f "$METRICS_LOG_LABEL" ]]; then
  _llines=$(wc -l <"$METRICS_LOG_LABEL" | tr -d ' ' || echo 0)
  _lsize=$(wc -c <"$METRICS_LOG_LABEL" | tr -d ' ' || echo 0)
  _log "compare_log path=$METRICS_LOG_LABEL lines=${_llines} bytes=${_lsize}"
else
  _log "compare_log missing path=$METRICS_LOG_LABEL (will use empty/fallback context)"
fi

# Token-efficient prompt context: errors, calibration/adaptive/risk lines, losses (+ short tail fallback)
if [[ -f "$LOG_FILE" ]]; then
  if ! "$PYTHON" "$REPO/scripts/extract_ollama_log_context.py" "$LOG_FILE" \
    --max-chars "$OLLAMA_LOG_CONTEXT_MAX_CHARS" >"$TMP_TAIL" 2>/tmp/ollama_extract_ctx.err; then
    _log "extract_ollama_log_context failed; stderr:" && cat /tmp/ollama_extract_ctx.err >&2 || true
    tail -n 200 "$LOG_FILE" >"$TMP_TAIL"
    _log "fallback tail -n 200 from log"
  fi
else
  touch "$TMP_TAIL"
fi
_ctx_chars=$(wc -c <"$TMP_TAIL" | tr -d ' ' || echo 0)
_log "prompt_log_excerpt chars=${_ctx_chars} max_chars=${OLLAMA_LOG_CONTEXT_MAX_CHARS}"
[[ ! -f "$LOG_FILE" ]] && LOG_FILE="$TMP_TAIL"

# Per-loop interval: position_evaluations resolved in (prev_csv_ts, now] (SQLite)
NOW_TS=$(date -u +%s)
read -r WIN_RATE PNL < <(
  OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS="$OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS" \
    "$PYTHON" "$REPO/scripts/metrics_interval_position_eval.py" \
    --metrics-csv "$METRICS" --now-ts "$NOW_TS" 2>/dev/null || echo "null null"
)
# If interval script produced nothing (empty DB window still prints "0 0.00"); detect parse failure
if [[ -z "${WIN_RATE:-}" ]]; then
  read -r WIN_RATE PNL_CENTS < <("$PYTHON" "$REPO/scripts/parse_compare_log_metrics.py" "$LOG_FILE" 2>/dev/null || echo "0 0")
  PNL=$(awk "BEGIN {printf \"%.2f\", ${PNL_CENTS:-0}/100}")
fi

echo "$NOW_TS,$WIN_RATE,$PNL" >> "$METRICS"
_log "metrics_row ts=$NOW_TS win_rate=${WIN_RATE} pnl_usd=${PNL} -> append $METRICS"

# --- 2) Per-series leaderboard + blacklist hints (SQLite; does not edit config) ---
if ! "$PYTHON" "$REPO/scripts/export_series_leaderboard.py" --hours "$OLLAMA_LEADERBOARD_HOURS" --out "$LB_HTML"; then
  echo "⚠️  export_series_leaderboard.py failed — $LB_HTML may be stale or missing." >&2
  _log "export_series_leaderboard FAILED hours=$OLLAMA_LEADERBOARD_HOURS out=$LB_HTML"
else
  _log "export_series_leaderboard ok hours=$OLLAMA_LEADERBOARD_HOURS out=$LB_HTML"
fi
"$PYTHON" "$REPO/scripts/suggest_series_blacklist.py" --hours "$OLLAMA_LEADERBOARD_HOURS" --min-trades "$OLLAMA_BLACKLIST_MIN_TRADES" >"$SUGGEST_TMP" 2>/dev/null || true
if ! "$PYTHON" "$REPO/scripts/export_series_leaderboard.py" --hours "$OLLAMA_PROMPT_LB_HOURS" --out "$SERIES_SUMMARY_HTML"; then
  echo "⚠️  export_series_leaderboard.py (prompt summary) failed — $SERIES_SUMMARY_HTML may be stale." >&2
  _log "export_series_leaderboard prompt summary FAILED hours=$OLLAMA_PROMPT_LB_HOURS"
else
  _log "export_series_leaderboard prompt summary ok -> $SERIES_SUMMARY_HTML"
fi
if [[ "$OLLAMA_AUTO_BLACKLIST" == "1" ]]; then
  mkdir -p "$(dirname "$BLACKLIST_EXTRA_FILE")"
  # No --merge: rewrite file each run so series recover off the list when stats improve
  "$PYTHON" "$REPO/scripts/apply_series_blacklist.py" --hours "${OLLAMA_AUTO_BLACKLIST_HOURS:-168}" \
    --min-trades "${OLLAMA_AUTO_BLACKLIST_MIN_TRADES:-40}" --max-win-rate "${OLLAMA_AUTO_BLACKLIST_MAX_WR:-0.20}" \
    --out "$BLACKLIST_EXTRA_FILE" 2>/dev/null || true
fi

# --- 3) HTML metrics table + embedded leaderboard (Python: CSV-safe, escaped, UTC timestamps) ---
if ! "$PYTHON" "$REPO/scripts/render_ollama_dashboard_html.py" \
  --metrics-csv "$METRICS" \
  --out "$HTML" \
  --leaderboard "$LB_HTML" \
  --blacklist-suggest "$SUGGEST_TMP" \
  --log-label "$METRICS_LOG_LABEL" \
  --leaderboard-hours "$OLLAMA_LEADERBOARD_HOURS" \
  --blacklist-min-trades "$OLLAMA_BLACKLIST_MIN_TRADES" \
  --max-metric-rows 10; then
  echo "⚠️  render_ollama_dashboard_html.py failed — $HTML may be stale." >&2
  _log "render_ollama_dashboard_html FAILED out=$HTML"
else
  _log "wrote dashboard HTML $HTML (metrics rows + leaderboard iframe)"
fi

# --- 4) Metrics gate: skip Ollama when recent PnL is not stressed (reduces over-tweaking) ---
# Python exit 0 = proceed (insufficient samples in window OR drop ≥ threshold). Exit 1 = skip run.
if [[ "$OLLAMA_METRICS_GATE" == "1" ]] && [[ -f "$METRICS" ]]; then
  if ! METRICS="$METRICS" OLLAMA_METRICS_GATE_HOURS="$OLLAMA_METRICS_GATE_HOURS" OLLAMA_METRICS_GATE_DROP="$OLLAMA_METRICS_GATE_DROP" \
    "$PYTHON" -c "
import os, sys, time
from pathlib import Path
m = Path(os.environ['METRICS'])
h = float(os.environ.get('OLLAMA_METRICS_GATE_HOURS', '4'))
drop = float(os.environ.get('OLLAMA_METRICS_GATE_DROP', '0.2'))
now = time.time()
cutoff = now - h * 3600
rows = []
for line in m.read_text(encoding='utf-8', errors='replace').splitlines():
    parts = [p.strip() for p in line.split(',')]
    if len(parts) < 3:
        continue
    try:
        ts = float(parts[0])
        pnl = float(parts[2])
    except ValueError:
        continue
    if ts >= cutoff:
        rows.append((ts, pnl))
rows.sort(key=lambda x: x[0])
if len(rows) < 2:
    sys.exit(0)
pnls = [r[1] for r in rows]
# Identical PnL every sample (e.g. grep miss → all 0.0): do not block Ollama indefinitely
if len({round(p, 6) for p in pnls}) == 1:
    sys.exit(0)
first, last = rows[0][1], rows[-1][1]
sys.exit(0 if (last - first) <= -drop else 1)
"; then
    echo "⏭️  Skipping Ollama/backtest: no PnL drop ≥ \$${OLLAMA_METRICS_GATE_DROP} in last ${OLLAMA_METRICS_GATE_HOURS}h (set OLLAMA_METRICS_GATE=0 to always run)."
    _log "metrics_gate SKIP drop>=${OLLAMA_METRICS_GATE_DROP} in ${OLLAMA_METRICS_GATE_HOURS}h not met — dashboard commit only"
    echo "{\"ts\":$(date +%s),\"event\":\"metrics_gate_skip\",\"model\":\"$OLLAMA_MODEL\",\"drop\":${OLLAMA_METRICS_GATE_DROP},\"hours\":${OLLAMA_METRICS_GATE_HOURS}}" >>"$JOURNAL"
    _commit_dashboard_if_changed || true
    exit 0
  fi
fi

# --- 5) Backtest baseline (same DB before model patch; used for optional PnL gate) ---
set +e
"$PYTHON" "$REPO/scripts/backtest.py" --mode "$OLLAMA_BT_MODE" --days "$OLLAMA_BT_DAYS" --json > "$BASELINE_JSON" 2>/tmp/ollama_backtest_err.txt
BT_EC=$?
set -e
if [[ $BT_EC -ne 0 ]]; then
  echo "⚠️  backtest baseline failed (exit $BT_EC); continuing without baseline compare. stderr:" >&2
  cat /tmp/ollama_backtest_err.txt >&2 || true
  _log "backtest baseline FAILED exit=$BT_EC mode=$OLLAMA_BT_MODE days=$OLLAMA_BT_DAYS"
  BASELINE_JSON=""
else
  _log "backtest baseline ok mode=$OLLAMA_BT_MODE days=$OLLAMA_BT_DAYS -> $BASELINE_JSON"
fi

# --- 6) Prompt: template + strategy context + logs + metrics + journal ---
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
  printf "\n### PERFORMANCE BY ASSET (last %sh, SQLite → leaderboard HTML as plain text)\n" "$OLLAMA_PROMPT_LB_HOURS"
  if [[ -f "$SERIES_SUMMARY_HTML" ]]; then
    sed -e 's/<[^>]*>//g' "$SERIES_SUMMARY_HTML" | grep -v '^[[:space:]]*$' || true
  else
    echo "(no leaderboard file; export_series_leaderboard may have failed)"
  fi
  printf "\n### RECENT LOG SIGNALS (errors, calibration, adaptive, risk, losses — not full tail)\n"
  cat "$TMP_TAIL"
  printf "\n### METRICS CSV (last 10 rows)\n"
  grep -E '^[0-9]+,.+,.+$' "$METRICS" 2>/dev/null | tail -n 10 || true
  printf "\n### OPTIMIZATION JOURNAL (last 8 lines)\n"
  tail -n 8 "$JOURNAL" 2>/dev/null || true
} > "$TMP_PROMPT"
_prompt_chars=$(wc -c <"$TMP_PROMPT" | tr -d ' ' || echo 0)
_log "assembled prompt chars=${_prompt_chars} -> $TMP_PROMPT"

# --- 7) Ollama: generate → apply → validate (up to OLLAMA_MAX_PATCH_ATTEMPTS per tick; revert between tries) ---
VALID_ARGS=(--repo "$REPO" --smoke-markets "${OLLAMA_SMOKE_MARKETS:-2}" --backtest-mode "$OLLAMA_BT_MODE" --backtest-days "$OLLAMA_BT_DAYS")
[[ -n "$BASELINE_JSON" && -f "$BASELINE_JSON" ]] && VALID_ARGS+=(--baseline-json "$BASELINE_JSON")

PATCH_ATTEMPT=1
VALIDATED_OK=0
HAD_ANY_PATCH=0
while (( PATCH_ATTEMPT <= OLLAMA_MAX_PATCH_ATTEMPTS )); do
  if (( PATCH_ATTEMPT > 1 )); then
    echo "↻ Ollama patch attempt ${PATCH_ATTEMPT}/${OLLAMA_MAX_PATCH_ATTEMPTS} (previous apply/validate failed)…"
  fi
  _log "ollama_codex_generate attempt ${PATCH_ATTEMPT}/${OLLAMA_MAX_PATCH_ATTEMPTS} model=$OLLAMA_MODEL temp=$OLLAMA_TEMPERATURE"
  if ! OLLAMA_HOST="$OLLMAPATH" OLLAMA_TEMPERATURE="$OLLAMA_TEMPERATURE" \
    "$PYTHON" "$REPO/scripts/ollama_codex_generate.py" "$OLLAMA_MODEL" "$TMP_PROMPT" > "$TMP_OUTPUT"; then
    echo "❌ ollama_codex_generate.py failed (host=$OLLMAPATH model=$OLLAMA_MODEL temp=$OLLAMA_TEMPERATURE)" >&2
    _log "ollama_codex_generate FAILED"
    _commit_dashboard_if_changed || true
    exit 1
  fi

  PATCH=$(awk '/^```diff$/ {flag=1; next} /^```$/ && flag {flag=0} flag' "$TMP_OUTPUT")
  MSG=$(grep -i '^Commit message:' "$TMP_OUTPUT" | head -n 1 | cut -d: -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

  if [[ -z "$PATCH" ]]; then
    if (( PATCH_ATTEMPT == 1 )); then
      echo "ℹ️  No fenced diff block (markdown \`\`\`diff) in model output; nothing to apply."
      _log "no_patch (no fenced diff in model output)"
      _wr_json=$([[ "${WIN_RATE:-}" == "null" ]] && echo null || echo "$WIN_RATE")
      echo "{\"ts\":$(date +%s),\"event\":\"no_patch\",\"model\":\"$OLLAMA_MODEL\",\"win_rate\":${_wr_json}}" >>"$JOURNAL"
      _commit_dashboard_if_changed || true
      exit 0
    fi
    echo "ℹ️  No patch on retry ${PATCH_ATTEMPT}; stopping attempts."
    break
  fi

  HAD_ANY_PATCH=1
  echo "$PATCH" > "$PATCH_FILE"
  cd "$REPO"
  if ! git apply --verbose --recount --whitespace=fix "$PATCH_FILE"; then
    echo "❌ git apply failed (malformed diff), attempt ${PATCH_ATTEMPT}."
    echo "{\"ts\":$(date +%s),\"event\":\"apply_failed\",\"model\":\"$OLLAMA_MODEL\",\"attempt\":${PATCH_ATTEMPT}}" >>"$JOURNAL"
    ((PATCH_ATTEMPT++)) || true
    continue
  fi
  echo "✅ Patch applied to working tree; running validator…"

  if "$PYTHON" "$REPO/scripts/ollama_patch_validate.py" "${VALID_ARGS[@]}"; then
    echo "✅ Validation passed."
    _log "validate OK attempt ${PATCH_ATTEMPT}"
    VALIDATED_OK=1
    "$PYTHON" -m py_compile signals/yfinance_client.py 2>/dev/null || true
    git add "$HTML" "$LB_HTML" "$JOURNAL" 2>/dev/null || true
    git add -A
    git commit -m "${MSG:-chore: ollama-optimized bot}" || {
      echo "ℹ️  Nothing to commit (empty diff vs HEAD?)."
    }
    if [[ "$OLLAMA_AUTO_PUSH" == "1" ]]; then
      git push origin HEAD
    fi
    echo "{\"ts\":$(date +%s),\"event\":\"committed\",\"model\":\"$OLLAMA_MODEL\",\"attempt\":${PATCH_ATTEMPT}}" >>"$JOURNAL"
    break
  fi

  echo "❌ Validation failed — reverting applied patch (attempt ${PATCH_ATTEMPT})."
  _log "validate FAILED revert attempt ${PATCH_ATTEMPT}"
  git apply -R --whitespace=fix "$PATCH_FILE" 2>/dev/null || git checkout -- .
  echo "{\"ts\":$(date +%s),\"event\":\"reverted_retry\",\"model\":\"$OLLAMA_MODEL\",\"attempt\":${PATCH_ATTEMPT}}" >>"$JOURNAL"
  ((PATCH_ATTEMPT++)) || true
done

if [[ "$VALIDATED_OK" == "1" ]]; then
  _log "done success (patch committed)"
  exit 0
fi
if [[ "$HAD_ANY_PATCH" == "1" ]]; then
  echo "❌ Exhausted ${OLLAMA_MAX_PATCH_ATTEMPTS} patch attempt(s) without passing validation." >&2
  _log "done FAILURE exhausted_patch_attempts=${OLLAMA_MAX_PATCH_ATTEMPTS}"
  _commit_dashboard_if_changed || true
  exit 1
fi
_log "done (no patch to apply)"
exit 0
