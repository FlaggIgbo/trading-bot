# PnL Investigation: Why the Model Is Doing Terrible Trades

This document summarizes the investigation into poor PnL across Kalshi and Gemini, tracing the decision flow in `compare_historical.py` and identifying likely root causes and fixes.

---

## 1. Log Path Mismatch (Immediate Fix)

**Problem:** The `ollama_codex_loop.sh` reads metrics from `$HOME/comparisons/last_run.log`, but the launchd plist logs compare_historical output to `~/Desktop/trading-bot-logs/eval_YYYYMMDDHH.log` (via `--log` with no argument). The "Win rate" and "P&L" lines that the loop greps for only appear when `--position-eval-analysis` is passed—and that flag is **not** in the plist.

**Impact:** The loop may be reading stale/empty data or wrong files, so the Ollama prompt gets no real performance context.

**Fix:**
1. Update the plist to log to `~/comparisons` and add `--position-eval-analysis`:
   ```xml
   <string>--log</string>
   <string>/Users/ogechukwuflagg-igbo/comparisons</string>
   <string>--position-eval-analysis</string>
   ```
2. Ensure compare_historical writes `eval_YYYYMMDDHH.log` into that dir. The loop expects `last_run.log` or `last_run_*.log`—either add a symlink/copy step, or change the loop to read the latest `eval_*.log` in `~/comparisons`.

---

## 2. Decision Flow (Where Things Can Go Wrong)

```
yfinance/Gemini closes → empirical P(above/below)
                    ↓
vol model (log-normal / Monte Carlo) → vol_p
                    ↓
empirical + vol blend (DTE-adjusted) → hist_p
                    ↓
signal model (optional) + probability calibrator
                    ↓
model_probability_shrinkage (0.75) → pulls toward 0.5
                    ↓
anti_hallucination pipeline (Bayesian, EWMA, vol Z-score, adaptive edge, regime filter)
                    ↓
recommendation engine (REC_BUY_YES / REC_BUY_NO / REC_SKIP)
                    ↓
min_edge + calibration + adaptive learning + vol regime scaling
                    ↓
pending_orders / pending_sells → execution
```

**Weak spots:**
- **Empirical P**: 90-day rolling window; if regime changed recently, empirical is stale.
- **Vol model**: Can be overconfident on intraday; `intraday_vol_cap: 0.90` helps but may not be enough.
- **Anti-hallucination**: Bayesian update with `market_strength: 2.0` pulls model toward market; if market is wrong, we may over-correct.
- **YES bias**: `yes_overconfidence_extra_edge: 0.04`, `model_probability_shrinkage: 0.75`, `favor_yes_when_above_strike: false`—structural bias may still leak.
- **Calibration**: `use_bucket_penalty`, `_get_effective_min_edge`, `_should_downgrade_buy_yes_above`—only help when we have enough resolved samples.

---

## 3. Likely Root Causes

### A. Model Overconfidence (hist_p Too High)

- Empirical treats past closes as stationary; in a regime shift, P(above) can be wrong.
- Vol model assumes log-normal; fat tails (e.g. crypto) can make it overconfident.
- **Check:** Run `--position-eval-analysis` and inspect "KEEP win rate by hist_p bucket". If high hist_p buckets (e.g. 0.9–1.0) have low win rates, the model is overconfident.

**Mitigations:**
- Increase `model_probability_shrinkage` (e.g. 0.75 → 0.65).
- Increase `anti_hallucination.bayesian_market_strength` (e.g. 2.0 → 2.75).
- Lower `intraday_vol_cap` (e.g. 0.90 → 0.85).

### B. Edge Calculation / Fee Underestimation

- `min_edge` is net of maker fees. If fee floor or rate is wrong, we think we have edge when we don't.
- **Check:** `kalshi_maker_fee_rate: 0.0175`, `gemini_maker_fee_rate: 0.0175`, `min_fee_per_order: 0.01`. Verify these match actual exchange fees.

### C. Limit Order Execution

- Layered mode places limits below bid using `limit_dip_pct: 0.08`. If fills happen at worse prices (slippage, thin book), realized edge is lower than expected.
- **Check:** `get_limit_order_analysis` and price-after-buy calibration. If "would have filled" is high but we're still losing, execution quality is an issue.

### D. Sell Logic (Holding Losers Too Long)

- Dynamic EV-optimal sell threshold: `P(lose) > 1 - (bid - exit_fee)`. High bids → low threshold (sell sooner); low bids → high threshold (hold longer).
- **Check:** Position eval stats: if SELL win rate is much lower than KEEP, we may be selling winners and keeping losers.

### E. Fill Reconciliation / Missing Predictions

- Predictions are created from confirmed fills via `record_prediction_from_fill`. If `limit_orders` isn't populated when orders execute, we fall back to `hist_p=0.5` and lose calibration context.
- **Check:** `--reconcile-diagnostics` output: "seen / inserted / dup / skipped". If inserted is low relative to real fills, reconciliation is failing.

### F. Two-Sided Quotes Adding Bad Risk

- `enable_two_sided_quotes: true` infers fair level and places paired YES/NO. If inference is wrong, we can add losing positions.
- **Mitigation:** Try `enable_two_sided_quotes: false` or raise `two_sided_quote_min_edge` to 0.05.

---

## 4. Recommended Actions (Priority Order)

1. **Fix log path** (Section 1) so the loop and dashboards see real metrics.
2. **Run with `--position-eval-analysis`** at least once manually to inspect KEEP vs SELL win rates and hist_p buckets.
3. **Tighten model confidence:**
   - `model_probability_shrinkage: 0.65`
   - `anti_hallucination.bayesian_market_strength: 2.75`
4. **Raise min_edge further** if win rate stays &lt; 40%: e.g. `min_edge: 0.35`.
5. **Reduce position size:** `kelly_fraction: 0.15` (from 0.25).
6. **Disable two-sided quotes** temporarily: `enable_two_sided_quotes: false`.
7. **Add a circuit breaker:** When `get_recent_performance` win rate &lt; 35% over 6h, block new BUYs (adaptive learning already adds edge; consider hard block).
8. **Dry-run validation:** Set `dry_run: true` for 2–3 days and compare proposed trades vs. what would have happened (requires backtest or manual tracking).

---

## 5. Metrics Sources

| Metric        | Source                                      | When Printed                          |
|---------------|---------------------------------------------|---------------------------------------|
| Win rate      | `get_position_evaluation_stats` (KEEP/SELL) | `--position-eval-analysis`            |
| PnL (¢)      | Same                                       | `--position-eval-analysis`            |
| Calibration   | `get_calibration_stats`                     | Always (if enabled)                   |
| Recent perf   | `get_recent_performance`                    | When adaptive learning adds edge      |
| Daily report  | `daily_performance_report.py`               | Once daily (different script)         |

The ollama_codex_loop expects "Win rate" and "P&L" in the log; those come from position-eval-analysis. Without that flag, the loop gets no useful metrics.

---

## 6. Status

- ✅ Circuit breaker added (`adaptive_loss_block_*` + `pending_orders.clear()`) to hold buys when recent win rate < 35% over 6h.
- ✅ Config tuned: higher `min_edge`, lower `kelly_fraction`, stronger `model_probability_shrinkage`, tighter `intraday_vol_cap`, and two-sided quotes disabled.
- ✅ `ollama_codex_loop.sh` now reads the same `~/comparisons/eval_*.log` output and the plist passes `--position-eval-analysis` so metrics are logged reliably.
