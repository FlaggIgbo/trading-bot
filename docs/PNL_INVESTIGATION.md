# PnL Investigation: Why the Model Is Doing Terrible Trades

This document summarizes the investigation into poor PnL across Kalshi and Gemini, tracing the decision flow in `compare_historical.py` and identifying likely root causes and fixes.

**Note:** The log-path and plist items in §1 were **addressed** in the main branch (see §6). The loop now resolves the newest compare log under `~/comparisons`, uses **`parse_compare_log_metrics.py`**, and the repo **`launchd/com.user.compare_historical.plist`** logs with **`--log …/comparisons`** and **`--position-eval-analysis`**.

---

## 1. Log Path Mismatch (historical — fixed in tree)

**Original problem:** The `ollama_codex_loop.sh` read metrics from a path that did not always match where launchd wrote compare output, and position-eval lines were missing without **`--position-eval-analysis`**.

**Current state:** Plist logs to **`~/comparisons`** with **`--position-eval-analysis`**. **`ollama_codex_loop.sh`** picks the newest of `last_run.log` and `eval_*.log` and parses metrics with Python (see §6).

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
model_probability_shrinkage → pulls toward 0.5 (`strategy.model_probability_shrinkage` in config.yaml)
                    ↓
anti_hallucination pipeline (Bayesian, EWMA, vol Z-score, adaptive edge, regime filter)
                    ↓
recommendation engine (REC_BUY_YES / REC_BUY_NO / REC_SKIP)
                    ↓
barbell gate (if barbell_enabled): base | alpha only, else SKIP
                    ↓
min_edge + calibration + adaptive learning + vol regime scaling
                    ↓
pending_orders / pending_sells → execution
```

When **`strategy.simple_ev_decision.enabled`** is true, barbell and many stacked edge penalties do not apply as separate gates; sizing still uses calibration/disagreement multipliers — see `config.yaml` and `README.md`.

**Weak spots:**
- **Empirical P**: 90-day rolling window; if regime changed recently, empirical is stale.
- **Vol model**: Can be overconfident on intraday; `intraday_vol_cap` (repo default **0.85**) caps vol-model probability—tune if tails still look hot.
- **Anti-hallucination**: Bayesian update (`strategy.anti_hallucination.bayesian_market_strength`) pulls model toward market; if the market is wrong, we may over-correct.
- **YES bias**: `yes_overconfidence_extra_edge`, `model_probability_shrinkage`, `favor_yes_when_above_strike`, parity caps—structural bias may still leak; compare to current `config.yaml` and `README.md` defensive preset.
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

- Layered mode places limits below bid using dip stack (`limit_dip_pct`, etc.). If fills happen at worse prices (slippage, thin book), realized edge is lower than expected.
- **Stale resting bids:** If limits never fill, review **`execution.resting_cancel_if_market_bid_above_limit_by`** (cancels when the tape has moved up past your price) and **`execution.limit_buy_edge_aggression_enabled`** / cross vs near-market thresholds (cross should require **stronger** edge than near-market). **`limit_buy_passive_only_below_edge`** forces passive/layered quotes when net edge is thin so you do not overpay spread on marginal trades.
- **Stacking / concentration:** With **`filter_disjoint_underlying_buys: false`**, use **`strategy.max_bankroll_fraction_per_underlying`** and **`max_bankroll_fraction_per_direction`** to cap notionals per Yahoo underlying (and per YES/NO side) instead of rejecting valid multi-strike ladders.
- **Check:** `get_limit_order_analysis` and price-after-buy calibration. If "would have filled" is high but we're still losing, execution quality is an issue.

### D. Sell Logic (Holding Losers Too Long)

- Optional **edge floor:** `execution.position_exit_min_net_edge` queues a sell when held-side **net** edge (model − bid − fee) drops below that value — independent of entry `min_edge`, so you can exit fading trades without waiting for P(lose) to spike.
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
5. **Reduce position size:** lower `kelly_fraction` (repo default **0.18** in `config.yaml`).
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

The Ollama loop still benefits from **`--position-eval-analysis`** in compare logs for human-readable win-rate / PnL lines; numeric metrics also use **`parse_compare_log_metrics.py`** on the resolved log file.

---

## 6. Status

- ✅ Circuit breaker (`adaptive_loss_block_*`) holds buys **per venue**: Kalshi loss vs Kalshi balance and Gemini loss vs Gemini balance are evaluated separately; only that platform's pending BUYs are removed (not win-rate based).
- ✅ Config tuned over time: `min_edge`, `kelly_fraction`, `model_probability_shrinkage`, `intraday_vol_cap`, two-sided quotes, and **`strategy.barbell_enabled`** (base/alpha directional filter) — see `config.yaml` and `README.md`.
- ✅ `ollama_codex_loop.sh` picks the **newest** of `~/comparisons/last_run.log` and `~/comparisons/eval_*.log` for metrics; compare plist logs to `~/comparisons` with `--position-eval-analysis`.
- ✅ HTML outputs: **`docs/ollama_runs.html`** documents data provenance; **`export_series_leaderboard.py`** sorts by PnL with correct per-series time order for consecutive-loss counts.
- ✅ Metrics gate default is **off** (`OLLAMA_METRICS_GATE=0`); with gate **on**, **flat identical PnL** in the window no longer blocks Ollama forever (treated as insufficient signal).
- ✅ **Liquidity / PnL execution:** `max_pct_of_book_volume` caps clip size vs same-side depth; `near_expiry_aggressive_hours` (via `execution:`) forces dip-style quotes when wall-clock DTE **≤** that many hours (inclusive boundary so sub-1h books are not skipped by an off-by-one); Ollama loop uses **`OLLAMA_MAX_PATCH_ATTEMPTS`** (default 3) with revert-between-retries so bad patches are less likely to stick; codex prompt stresses **markout / $** over win-rate vanity.
