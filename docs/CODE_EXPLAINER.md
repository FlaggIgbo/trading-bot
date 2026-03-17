# Trading Bot — Code Explainer

> **Layman's summary:** The bot watches prediction markets (e.g. "Will Bitcoin be above $81,250 on March 16?"), checks how often that actually happened in the past year, and compares that "historical chance" to what the market is charging. If the market is wrong, it suggests BUY YES, BUY NO, or SKIP.

---

## Table of Contents

1. [High-Level: What the Bot Does](#high-level-what-the-bot-does)
2. [Step-by-Step Flow (Main Script)](#step-by-step-flow-main-script)
3. [Every File in the Repository](#every-file-in-the-repository)
4. [Script-by-Script Deep Dive](#script-by-script-deep-dive)
5. [Key Functions Reference](#key-functions-reference)
6. [Configuration Deep Dive](#configuration-deep-dive)
7. [Command-Line Arguments](#command-line-arguments-verbose)
8. [API Clients Deep Dive](#api-clients-deep-dive)
9. [Storage Module Deep Dive](#storage-module-deep-dive)
10. [Ollama Analysis Deep Dive](#ollama-analysis-deep-dive)
11. [Constants Reference](#constants-reference)
12. [Error Handling & Edge Cases](#error-handling--edge-cases)
13. [Example Walkthrough](#example-walkthrough)
14. [Data Flow Diagram](#data-flow-diagram)
15. [Local Storage Schema](#local-storage-schema)
16. [Key Concepts (Layman's Terms)](#key-concepts-laymans-terms)
17. [Quick Reference](#quick-reference)

---

## High-Level: What the Bot Does

**In plain English:**

1. **Load settings** — Reads which markets to watch (Bitcoin, Ethereum, etc.) and strategy rules from `config.yaml`.
2. **Fetch data** — Gets market prices from Kalshi and Gemini, and historical prices from Yahoo Finance.
3. **Estimate fair probability** — Uses past daily closes to estimate how often the asset ends above/below the bet's target ("strike").
4. **Compare to market** — If our estimate says 20% chance but the market charges 40¢, we see a bargain (BUY NO). If our estimate says 60% and the market charges 30¢, we see a bargain (BUY YES). Otherwise we SKIP.
5. **Check for arbitrage** — Sometimes buying YES and NO together costs less than $1 (minus fees). That's risk-free profit, so we do that first.
6. **Evaluate positions** — For bets we already hold, we decide whether to sell (e.g. we now think we'll lose).
7. **Execute (optional)** — If `--execute` and `dry_run: false`, the bot places real orders on Kalshi and/or Gemini.

### Production-Grade Upgrades (2026)

- **Modular strategy engine:** `compare_historical.py` now routes recommendation decisions through `strategies/` (`strategy_engine` in config), with `vol_regime` as the production default.
- **Regime-aware volatility safety:** EWMA/Student-t probability flow is paired with volatility-spike safety to increase required edge during stress regimes.
- **Adaptive limit pricing:** `limit_dip_pct` is now ARR-inspired (`max(base_dip, 0.5 * avg_relative_move)`) so entries tighten in calm markets and widen in volatile markets.
- **High-fidelity synthetic backtest:** Synthetic mode supports dwell-time fill checks, partial-fill scaling from persisted depth fields, and per-trade liquidity friction in addition to slippage/spread realism.
- **Circuit breaker safety:** New BUYs are blocked when 24h realized losses breach configured limits (`max_daily_loss_pct` / `max_daily_loss_cents`).

---

## Step-by-Step Flow (Main Script)

The main entry point is **`compare_historical.py`**. Here is the exact flow when you run it.

### Step 0: Entry Point

| Step                    | What happens                                                                                    | Code pointer                                 |
| ----------------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------- |
| You run the script      | `python scripts/compare_historical.py [options]`                                                | —                                            |
| Parse command-line args | `--days`, `--markets`, `--edge`, `--preview`, `--execute`, `--use-volatility`, `--no-llm`, etc. | `compare_historical.py:1107` → `main()`       |
| Load config             | Reads `config.yaml` via `config.get_config()`                                                   | `compare_historical.py:1108`                  |
| Invoke core logic       | Calls `_run_impl()` with args, cfg, markets, strategy                                           | `compare_historical.py:1188` → `_run_impl()` |

---

### Step 1: Initialize

| Step                     | What happens                                            | Code pointer                                                |
| ------------------------ | ------------------------------------------------------- | ----------------------------------------------------------- |
| Load config              | Markets (KXBTCD, KXETHD, …), strategy, yfinance mapping | `config.py:19` → `get_config()`                             |
| Create Kalshi client     | Authenticate with API keys (RSA-PSS), get balance       | `kalshi_client.py` → `KalshiClient()`                       |
| Sync settlements         | Update resolved outcomes in storage (for calibration)   | `compare_historical.py:1053` → `_sync_kalshi_settlements()`  |
| Load strategy params     | All strategy rules in one place                         | `compare_historical.py:115` → `_load_strategy_params()` |
| Fetch events in parallel | Gemini events + Kalshi markets via `ThreadPoolExecutor` | `compare_historical.py:1252`                           |

---

### Step 2: Evaluate Each Market (Kalshi)

For each series (e.g. KXBTCD → Bitcoin):

| Step                        | What happens                                                | Code pointer                                                  |
| --------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------- |
| Get historical closes       | Yahoo Finance (or CoinGecko) for last N days                | `yfinance_client.py:137` → `get_aggs()`                       |
| Get Kalshi open markets     | `kalshi.get_markets(series_ticker=series, status="open")`   | `kalshi_client.py` → `get_markets()`                          |
| Loop over each market       | For each market in the list                                 | `compare_historical.py:1290` → `for mkt in mkt_list`          |
| Skip unsupported resolution | Non 5pm crypto / 4pm S&P intraday → skip                    | `compare_historical.py:1182` → `_should_skip_by_resolution()` |
| Check intra-arb             | YES+NO < 1−fee on same market → BUY BOTH                    | `compare_historical.py:1169` → `_check_intra_arb_kalshi()`    |
| Check inter-arb             | Kalshi YES + Gemini NO < 1−fee → hedge                      | `compare_historical.py:1182` → `_check_inter_arb()`           |
| Extract strike              | Parse "above $81,250" or "below $72,000" from title or ticker (e.g. -T82.9999) | `compare_historical.py:389` → `_extract_strike()`             |
| Compute empirical P(YES)    | Recency-weighted/rolling empirical probability (NumPy path when installed) | `compare_historical.py` → `_empirical_probability()`      |
| Compute volatility P(YES)   | Log-normal model (EWMA/sample variance), optional OTM smile; S0 uses live+close blend | `compare_historical.py` → `_volatility_probability()`     |
| Blend + score signals       | Dynamic empirical/vol blend, optional sklearn calibration, then signal-model scoring | `compare_historical.py` → `_dynamic_empirical_weight()`, `_fit_probability_calibrator()`, `_score_signal_model()` |
| Recommend                   | Compare hist_p vs market price → BUY YES / BUY NO / SKIP    | `compare_historical.py:535` → `_recommend()`                  |
| Apply safety rules          | Skip BUY NO when price above strike; skip BUY YES when < 5¢ | `compare_historical.py:1232-1258`                             |
| Add limit order             | Price = bid × (1−dip) for "dip catching"                    | `compare_historical.py:1286-1316` → `pending_orders.append()` |

---

### Step 3: Evaluate Each Market (Gemini, Crypto Only)

Same logic for Gemini events (BTC, ETH, SOL, XRP):

| Step              | What happens                               | Code pointer                                       |
| ----------------- | ------------------------------------------ | -------------------------------------------------- |
| Get Gemini events | `gemini.get_crypto_events()` (crypto category; hourly skipped if API returns 400) | `gemini_client.py:147` → `get_crypto_events()`     |
| Map asset         | Title/ticker → BTC-USD, ETH-USD, etc.      | `gemini_client.py:74` → `_event_to_asset()`        |
| Extract strike    | Parse from event title                     | `gemini_client.py` → `extract_strike_from_event()` |
| Same flow         | Arb, P(YES), recommend, rules, limit order | `compare_historical.py:1324-1435`                  |

---

### Step 4: Evaluate Your Positions (Sell Logic)

Look at what you already hold (Kalshi + Gemini) and decide whether to sell:

| Step                     | What happens                                                              | Code pointer                                            |
| ------------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------- |
| Fetch Kalshi positions   | Paginated `kalshi.get_positions()`                                        | `compare_historical.py:1654-1665`                       |
| Fetch Gemini positions   | `gemini_trading.get_positions()`; map symbol→market via `gemini_symbol_to_market` | `compare_historical.py:1868-1888`                       |
| Parse position           | Kalshi: `position_fp` positive = YES, negative = NO; Gemini: `outcome`, `totalQuantity` | `compare_historical.py:1668-1676`, `1878-1886`     |
| Use cached or fresh data | Market data from earlier loop or fresh fetch (Kalshi); Gemini uses events  | `compare_historical.py:1672`, `1887`                    |
| Compute P(lose)          | Blends model (hist_p) with market odds (yes_bid/no_bid); market weight increases near resolution via `_compute_p_lose()` | `compare_historical.py` → `_compute_p_lose()`           |
| SELL triggers            | LIKELY_LOSE (P≥55%), rec flip, RULE_VIOLATION (price above strike on NO), unsupported resolution | `compare_historical.py` → `pending_sells.append()` |
| Re-entry (optional)      | If `enable_sell_and_rebuy`: when selling loser, add BUY at current price if edge ≥ min_edge; bypasses min 5¢ | `_maybe_add_reentry_order()`                      |
| Add to pending_sells     | List of orders to place (platform=KALSHI or GEMINI)                       | `pending_sells.append()`                               |

---

### Step 5: Execute or Preview

| Mode               | What happens                                    | Code pointer                               |
| ------------------ | ----------------------------------------------- | ------------------------------------------ |
| Filter contradictory | Skip BUYs that conflict with existing or other pending BUYs (same underlying + resolution); Kalshi + Gemini; arb YES+NO allowed | `compare_historical.py:951` → `_filter_contradictory_buys()` |
| Budget per platform | Kalshi balance for Kalshi orders; Gemini balance for Gemini orders (separate `_allocate_budget_to_orders` calls) | `compare_historical.py:1861` → per-platform allocation |
| `--preview`        | Print planned orders, no API calls              | `compare_historical.py:1927-1975`          |
| `--execute`        | Review orders → cancel stale → SELL first → BUY | `compare_historical.py:1671-1925`          |
| Place Kalshi order | `create_order_safe()` with balance caps         | `kalshi_client.py` → `create_order_safe()` |
| Place Gemini order | `place_order_safe()`                            | `gemini_client.py` → `place_order_safe()`  |

---

### Step 6: Optional — Ollama LLM Analysis

| Step                             | What happens                             | Code pointer                                        |
| -------------------------------- | ---------------------------------------- | --------------------------------------------------- |
| If Ollama running, no `--no-llm` | Capture output, send to local LLM        | `ollama_analysis.py:78` → `_get_analysis()`         |
| Per-trade analysis               | Optional analysis of each proposed trade | `ollama_analysis.py:117` → `analyze_single_trade()` |

---

## Every File in the Repository

### Core Scripts

| File                                | Purpose                                             | Key code pointers                                                                                                                                                                    |
| ----------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **`scripts/compare_historical.py`** | Main evaluation and execution                       | `main()` L1107, `_run_impl()` L1195, `_load_strategy_params()` L115, `_empirical_probability()` L434, `_volatility_probability()` L463, `_recommend()` L535, `_extract_strike()` L388, `_filter_contradictory_buys()` L951, `_compute_p_lose()` L652, `_maybe_add_reentry_order()` L674 |
| **`scripts/backtest.py`**           | Backtest on historical data                         | `--mode real` (resolved predictions) or `synthetic` (market_snapshots + resolved_outcomes); includes slippage/spread realism, winner slippage tax, dwell-time fill modeling, and liquidity friction                                                                                                 |
| **`scripts/validate_config.py`**    | Config sanity validator                              | Runs pre-push checks for edge/fee consistency, Kelly bounds, EWMA ranges, exposure/loss caps, and macro/two-sided settings before backtest guard                                                                                                                         |
| **`scripts/compare_prices.py`**     | Live price snapshot (Kalshi, Gemini, yfinance)      | `_run()` L40, `main()` L119                                                                                                                                                          |
| **`scripts/test_connection.py`**    | Test API connections, balance, markets              | `_run()` L130, `debug_check()` L30, `inspect_output_types()` L86                                                                                                                     |
| **`scripts/debug_ollama.py`**       | Debug Ollama server, models, chat                   | `main()` L16                                                                                                                                                                         |
| **`strategies/`**                   | Pluggable recommendation engines                     | `base.py` interface, `default_threshold.py`, `vol_regime.py`                                                                                                                        |

### Configuration

| File              | Purpose                             | Key content                                                                        |
| ----------------- | ----------------------------------- | ---------------------------------------------------------------------------------- |
| **`config.yaml`** | Markets, strategy, storage, trading | `markets` L8, `yfinance_tickers` L21, `strategy` L29, `storage` L63, `trading` L67 |
| **`config.py`**   | Config loader with env overrides    | `get_config()` L19 — reads YAML, overrides via `MARKETS`, `POLL_INTERVAL_SECONDS`  |

### API Clients

| File                     | Purpose                            | Key functions                                                                                                                                                                                                                                            |
| ------------------------ | ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`kalshi_client.py`**   | Kalshi API with RSA-PSS auth       | `KalshiClient`: `get_balance()`, `get_markets()`, `get_positions()`, `get_market()`, `get_historical_market()`, `get_orderbook()`, `create_order_safe()`, `cancel_order_safe()`                                                                        |
| **`gemini_client.py`**   | Gemini Prediction Markets (crypto) | `GeminiPredictionClient`: `get_events()`, `get_crypto_events()` (fetches `crypto`; `hourly` category skipped if API returns 400); `GeminiTradingClient`: `get_balance()`, `place_order_safe()` (caps by balance like Kalshi), `cancel_order_safe()`, `get_positions()`, `get_active_orders()`; helpers: `_event_to_asset()`, `extract_strike_from_event()`, `get_yes_price()`, `get_no_price()`. Nonce: monotonically increasing per API key. |
| **`yfinance_client.py`** | Historical prices (no API key)     | `get_aggs()` L137 (daily closes), `get_intraday_aggs_at_hour()` (1h prices at 5pm/4pm ET for intraday empirical P), `get_aggs_as_of()` (point-in-time for backtest), `get_snapshot()` L52 (current price); CoinGecko fallback for crypto |

### Support Modules

| File                     | Purpose                | Key functions                                                                                                                                |
| ------------------------ | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **`constants.py`**       | Recommendation strings | `REC_SKIP`, `REC_BUY_YES`, `REC_BUY_NO`, `REC_ARB_INTRA`, `REC_ARB_INTER`, `REC_LIKELY_LOSE`, `REC_RULE_VIOLATION`                           |
| **`storage.py`**         | SQLite persistence     | `get_yf_closes()`, `set_yf_closes()`, `get_yf_intraday_at_hour()`, `set_yf_intraday_at_hour()`, `record_market_snapshot()`, `record_prediction()`, `record_prediction_outcome()`, `record_resolved()`, `record_limit_order()`, `record_order_cancel()`, `get_limit_order_analysis()`, `get_last_snapshot_for_ticker()`, `get_market_snapshots_for_backtest()`, `get_resolved_outcomes()`, `get_predictions_for_backtest()`, `_init_schema()` L58 |
| **`ollama_analysis.py`** | Local LLM analysis     | `capture_and_analyze()`, `run_analysis()`, `analyze_single_trade()` L117, `_get_analysis()` L78                                              |

### Scheduling & Dev Tools

| File                                            | Purpose                                                   |
| ----------------------------------------------- | --------------------------------------------------------- |
| **`launchd/com.user.compare_historical.plist`** | Run `compare_historical` every 10 min on macOS (configurable) |
| **`launchd/README.md`**                         | Setup: copy plist, `launchctl bootstrap`, caffeinate           |
| **`package.json`**                              | Prettier dev dependency for YAML/JSON/MD formatting       |
| **`.pre-commit-config.yaml`**                   | Ruff (Python linter/formatter) + Prettier on git push     |
| **`docs/IMPROVEMENTS.md`**                      | Ideas and backlog for future work                         |
| **`README.md`**                                 | Project overview, setup, usage, cron/launchd instructions |

---

## Script-by-Script Deep Dive

### `scripts/compare_historical.py` — Main Evaluation Script (VERBOSE)

**Purpose:** Evaluate prediction markets using historical price data, compare to market odds, and recommend BUY YES / BUY NO / SKIP. Optionally place real orders.

**Entry point:** `main()` (L1107) → parses args → `capture_and_analyze(_run, ...)` which runs `_run(args)` and optionally sends output to Ollama.

**`_run(args)` flow (L1011):**
1. Load config, markets list, yf_mapping, strategy, trading settings.
2. Apply whitelist/blacklist filters to markets.
3. Open optional log file if `--log` (tee stdout to file).
4. Call `_run_impl()` with all params.

**`_run_impl()` flow (L1195) — the heart of the bot:**
1. **Setup:** Create KalshiClient, fetch balance, load `_StrategyParams` from config. Sync Kalshi settlements into storage (updates resolved predictions).
2. **Parallel fetch:** ThreadPoolExecutor fetches (a) Gemini crypto events and (b) Kalshi open markets per series. Results: `gemini_events_by_asset`, `kalshi_markets_by_series`.
3. **Calibration:** If storage enabled, call `get_calibration_stats()` — win rate by bucket, by direction+side (e.g. "above+yes" underperforming). Used to adjust min_edge and favor_yes logic.
4. **Main loop — for each series (KXBTCD, KXETHD, etc.):**
   - Resolve yf_ticker from config (e.g. KXBTCD → BTC-USD).
   - Fetch daily closes: `yf_get_aggs(yf_ticker, days_back=args.days)`. Uses storage cache if fresh.
   - For each Kalshi market in that series:
     - Skip if `_should_skip_by_resolution()` (non–crypto/non–S&P intraday).
     - Check intra-arb: `_check_intra_arb_kalshi()` — if yes_ask+no_ask < 1−fee, add BUY YES + BUY NO to pending_orders, continue.
     - Check inter-arb: `_find_matching_gemini_event()` + `_check_inter_arb()` — if Kalshi YES + Gemini NO < 1−fee, add hedge orders.
     - Extract strike: `_extract_strike()` from market floor/cap or title regex.
     - Get closes for empirical: `_get_closes_for_empirical()` — for intraday + use_intraday_empirical, fetches 1h prices at resolution hour; else daily closes.
     - Empirical P: `_empirical_probability(closes_for_emp, threshold, direction)` — fraction of days above/below threshold.
     - Volatility P (if --use-volatility): `_volatility_probability(closes, …)` — log-normal model, EWMA vol.
     - Blend: `hist_p = vol_blend_weight * emp + (1−w) * vol_p` when both; for intraday without use_intraday_empirical, hist_p = vol_p only.
     - Apply bucket penalty: `_apply_bucket_penalty()` — dampen hist_p if that bucket underperforms in calibration.
     - Recommend: `_recommend(hist_p, yes_bid, min_edge)` or `_recommend_strict()` for non-standard intraday.
     - Safety rules: skip BUY NO when price above strike; skip BUY YES when yes_bid < min_yes_price (unless vol override); skip BUY YES when strike far above current (unless vol ≥ 97%); skip BUY NO when no_bid < min_no_price; calibration downgrade for "above+yes" underperformers.
     - Compute limit price: bid × (1−dip); use larger dip when P(win)≥90% or when vol/decline regime.
     - Add to `pending_orders` and `market_data_cache`.
   - **Gemini loop:** Expand events to markets via `expand_gemini_events_to_markets()`. For each Gemini market (crypto only): same flow — arb, P(YES), recommend, safety rules. Add to pending_orders.
5. **Position evaluation:** Fetch Kalshi positions (`get_positions()`), Gemini positions (`GeminiTradingClient.get_positions()`). For each position:
   - If ticker in `market_data_cache`, use cached closes/market. Else fetch market + closes for orphan positions.
   - Compute P(lose) via `_compute_p_lose()` — blends model with market odds; more weight on market near resolution.
   - SELL triggers: LIKELY_LOSE (P≥55%), rec flip (we now say BUY NO but we hold YES), RULE_VIOLATION (hold NO on "above X" but price > X×(1+cushion)), INTRADAY_MARKET (unsupported resolution).
   - Optional re-entry: `_maybe_add_reentry_order()` — when selling loser, add BUY at current price if edge ≥ min_edge (bypasses min 5¢).
6. **Filter contradictory buys:** `_filter_contradictory_buys()` — skip BUYs that conflict with kept positions or other pending BUYs (same underlying + resolution). Arb YES+NO allowed.
7. **Budget allocation:** `_allocate_budget_to_orders()` per platform (Kalshi balance for Kalshi; Gemini balance for Gemini). Scale down order counts to fit budget.
8. **Execute (if --execute):** Review open orders via `execution_engine.sync_and_cancel_resting_orders()` → cancel stale or needing higher price → SELL first (Kalshi + Gemini) → BUY. Record predictions on fill.

**Key data structures:**
- `pending_orders`: list of dicts with platform, ticker, title, rec, side, price, edge, count, hist_p, vol_p, threshold, direction, etc.
- `pending_sells`: list of dicts with platform, ticker, side, action="sell", count, price, rec.
- `market_data_cache`: ticker → (series, yf_ticker, closes, market_dict, current_price). current_price is live when available.
- `existing_by_underlying`: (yf_ticker, res_key, platform) → [(threshold, direction, side), ...] for conflict check.

---

### `scripts/backtest.py` — Backtest Resolved or Synthetic Trades

**Purpose:** Evaluate historical performance — win rate, total PnL, Sharpe ratio, max drawdown.

**Modes:**
- **real:** Uses `predictions` table (rows with outcome = win/lose). Only trades we actually placed and that have resolved. Computes cumulative PnL, daily returns, Sharpe, max drawdown.
- **synthetic:** Uses `market_snapshots` + `resolved_outcomes`. Uses ask-side proxy + configurable slippage and spread-aware size multiplier; reports avg fill slippage and spread proxy metrics.

**Flow:** `main()` → parse --mode, --days → call `_run_real()` or `_run_synthetic()` → print results.

**Key functions:** `_compute_sharpe(returns)`, `_compute_max_drawdown(cumulative_pnl)`.

---

### `strategies/` — Pluggable Recommendation Engines

**Purpose:** Decouple recommendation logic from execution plumbing in `compare_historical.py`.

**Current engines:**
- `vol_regime` (default): production path.
- `default_threshold`: baseline threshold+fee engine for controlled comparisons.

**How selection works:** `config.yaml` → `strategy.strategy_engine`.

---

### `execution_engine.py` — Order Lifecycle Sync/Cancellation

**Purpose:** Isolate open-order management from probability/recommendation logic so execution safety can be unit-tested independently.

**Key responsibility:** `sync_and_cancel_resting_orders(...)`
- Builds pending buy/sell lookups for Kalshi + Gemini.
- Reads active resting orders from both venues.
- Keeps orders already at same/better price.
- Cancels orders for `stale_ttl`, `no_longer_rec`, `want_better_price`, and `regime_shift_guard`.
- Records cancel reason to storage through `record_order_cancel(...)`.

---

### `scripts/compare_prices.py` — Live Price Snapshot

**Purpose:** Quick view of Kalshi + Gemini market prices vs current yfinance price. No recommendations.

**Flow:** `_run()` → load config → create KalshiClient + GeminiPredictionClient → fetch Gemini events by asset → for each series: get yfinance snapshot → fetch Kalshi markets → print YES/NO bids for up to 5 markets; if crypto, print Gemini events too. Wrapped in `capture_and_analyze()` for optional Ollama analysis.

---

### `scripts/test_connection.py` — API Connectivity Test

**Purpose:** Verify Kalshi and Gemini API credentials and connectivity.

**Flow:** `_run()` → if `--debug`: `debug_check()` prints env var status (key ID format, key path, base URL) → create KalshiClient → if `--inspect`: `inspect_output_types()` dumps schema of balance, markets, orderbook → else: get balance (tests auth), get markets, get one Gemini event, print summary. Wrapped in `capture_and_analyze()` for Ollama.

---

### `scripts/debug_ollama.py` — Ollama Integration Debug

**Purpose:** Diagnose Ollama/LLM issues when scripts fail to get analysis.

**Flow:** Check Ollama server (GET /api/version) → list models (GET /api/tags) → import ollama package → run minimal chat with test data → run `ollama_analysis.run_analysis()` with same data. Surfaces exact errors (server down, no models, chat failure).

---

## Key Functions Reference (VERBOSE)

| Function | File | Signature | Purpose |
|----------|------|-----------|---------|
| `_empirical_probability` | compare_historical | `(closes, threshold, direction, recent_days, recent_weight, use_rolling_window) -> float` | Recency-weighted empirical probability; uses vectorized NumPy path when available. |
| `_volatility_probability` | compare_historical | `(closes, threshold, direction, days_to_expiry, use_ewma, ewma_lambda, current_price, ...) -> float \| None` | Regime-aware volatility P(S>K) or P(S<K); Student-t/Normal CDF path; optional OTM smile adjustment. |
| `_build_signal_features` | compare_historical | `(emp_p, vol_p, market_yes, threshold, current_price, dte_days, spread_bps, no_bid) -> _SignalFeatures` | Builds normalized model features for signal scoring. |
| `_score_signal_model` | compare_historical | `(features, sp) -> float` | Weighted logistic scorer over signals (configurable in `signal_model_weights`). |
| `_fit_probability_calibrator` | compare_historical | `(sp, cfg) -> callable \| None` | Fits isotonic/Platt calibration on resolved predictions, then maps raw model probabilities to calibrated values. |
| `_recommend` | compare_historical | `(hist_p, market_yes, min_edge, maker_fee, ...) -> str` | BUY YES if hist_p - market_yes - fee ≥ min_edge; BUY NO if (1-hist_p) - no_price - fee ≥ min_edge; else SKIP. Easy win: when model+market both ≥90%, uses min_edge_easy_win (current config 2¢) instead. Prefers larger edge when both qualify. |
| `_recommend_strict` | compare_historical | `(hist_p, market_yes, min_p_win, min_edge, fee) -> str` | Same as _recommend but also requires hist_p ≥ min_p_win for YES (or 1-hist_p ≥ min_p_win for NO). For non-standard intraday. |
| `_extract_strike` | compare_historical | `(market) -> (threshold, direction)` | Parse floor/cap from market API or title regex ("above $X", "below $Y"). |
| `_get_closes_for_empirical` | compare_historical | `(yf_ticker, days_back, closes_daily, market, sp, cfg) -> list[float]` | For intraday + use_intraday_empirical: return intraday prices at resolution hour; else daily closes. |
| `_compute_p_lose` | compare_historical | `(hist_p, yes_bid, no_bid, side, dte, sp) -> float` | P(our position loses). Blends model prob with market odds; market weight increases as dte→0 (half-life decay). |
| `_filter_contradictory_buys` | compare_historical | `(pending_orders, existing_by_underlying) -> None` | Removes BUYs that would conflict with kept positions or other pending BUYs (same underlying+resolution). |
| `get_aggs` | yfinance_client | `(ticker, days_back, config) -> list[float]` | Daily closes from yfinance (or CoinGecko for crypto). Uses storage cache. |
| `get_intraday_aggs_at_hour` | yfinance_client | `(ticker, days_back, resolution_hour_et, config) -> list[float]` | 1h prices at that ET hour per day. Falls back to daily if intraday fails. |
| `get_config` | config | `() -> dict` | Load config.yaml; override markets, poll_interval from env. |
| `record_prediction` | storage | `(platform, ticker, side, hist_p, entry_price, count, ...) -> int \| None` | Insert row into predictions when order fills. |
| `record_prediction_outcome` | storage | `(prediction_id, yes_settled, pnl_cents, ...) -> bool` | Update predictions with outcome=win/lose, pnl_cents, resolved_ts. |
| `capture_and_analyze` | ollama_analysis | `(run_fn, script_name, use_llm, per_trade_analysis) -> int` | Runs run_fn. If use_llm and per_trade_analysis, script does its own per-trade Ollama; else captures stdout and runs one batch analysis. |

---

## Configuration Deep Dive

**`config.yaml` structure:**
- **markets:** List of Kalshi series or tickers (e.g. KXBTCD, KXETHD). Env override: `MARKETS=KXBTCD,KXETHD`.
- **yfinance_tickers:** Map series → Yahoo ticker (KXBTCD→BTC-USD, KXINXU→^GSPC).
- **strategy:** min_edge, dynamic empirical/vol settings, smile/liquidity controls, signal-model and calibration keys, and risk rules. See README for full list.
- **storage:** enabled, data_dir.
- **trading:** dry_run, max_pct_of_balance, min_contracts.

**`_StrategyParams` dataclass:** All strategy fields in one object. Built by `_load_strategy_params(strategy_dict)`. Used everywhere in compare_historical to avoid passing 20+ args.

### Strategy Params Reference (Every Field)

| `_StrategyParams` field | config.yaml key | Default | Purpose |
|------------------------|-----------------|---------|---------|
| `days_to_expiry` | `days_to_expiry` | 30 | Days (or fraction) to resolution for vol model |
| `no_above_cushion` | `skip_buy_no_above_cushion` | 0.01 | Skip BUY NO when price > strike×(1+cushion) |
| `sell_losing_pct` | `sell_losing_pct` | 0.55 | Sell when P(lose) ≥ this |
| `limit_dip_pct` | `limit_dip_pct` | 0.05 | Place limit at bid×(1−dip) to catch drops |
| `limit_order_mode` | `limit_order_mode` | `fixed_pct` fallback | Limit pricing mode: `fixed_pct`, `dynamic_tick`, or `layered` |
| `tick_offset` | `tick_offset` | 1 | Tick offset used by dynamic tick quote placement |
| `max_inventory_skew_pct` | `max_inventory_skew_pct` | 0.10 | Inventory-aware quote skew cap (less aggressive when crowded same-side) |
| `min_stability_duration_seconds` | `min_stability_duration_seconds` | 0 fallback | Mid-price stability gate before posting new BUY limits |
| `min_order_book_depth` | `min_order_book_depth` | 0 fallback | Minimum same-side depth required before posting BUY limits |
| `limit_order_ttl_minutes` | `limit_order_ttl_minutes` | 30 fallback | Resting BUY order time-to-live before cancel+refresh |
| `cancel_all_on_regime_shift` | `cancel_all_on_regime_shift` | true fallback | Cancel resting BUY limits during momentum/volatility regime shifts |
| `limit_dip_pct_high_confidence` | `limit_dip_pct_high_confidence` | 0.15 | Larger dip when P(win)≥90% |
| `limit_dip_pct_price_decline` | `limit_dip_pct_price_decline` | 0.10 | Dip when vol model suggests price decline |
| `vol_blend_weight` | `vol_blend_weight` | 0.5 | Blend emp + vol: w×emp + (1−w)×vol |
| `use_ewma_vol` | `use_ewma_vol` | true | EWMA vs sample variance for vol |
| `ewma_lambda` | `ewma_lambda` | 0.94 | EWMA decay (RiskMetrics-style) |
| `intraday_ewma_lambda` | `intraday_ewma_lambda` | 0.94 fallback | Intraday-specific EWMA decay override (current config 0.90) |
| `intraday_other_min_p_win` | `intraday_other_min_p_win` | 0.90 | For non-5pm/4pm intraday: require P(win)≥90% |
| `intraday_other_min_p_win_with_vol` | `intraday_other_min_p_win_with_vol` | 0.80 | Same but when --use-volatility |
| `enable_easy_win` | `enable_easy_win` | true | Buy when model+market aligned (90%+) with relaxed min_edge |
| `min_edge_easy_win` | `min_edge_easy_win` | 0.01 fallback | Min profit when easy-win; current config uses 0.02 (2¢) |
| `easy_win_min_p_win` | `easy_win_min_p_win` | 0.90 | Model P(win) ≥ this for easy-win |
| `easy_win_min_market_p` | `easy_win_min_market_p` | 0.85 | Market price ≥ this (same ballpark) for easy-win |
| `min_arb_fee` | `min_arb_fee` | 0.02 | Min edge for arb (yes+no < 1−fee) |
| `min_yes_price` | `min_yes_price` | 0.05 | Skip BUY YES when yes_bid < 5¢ |
| `min_no_price` | `min_no_price` | 0.05 | Skip BUY NO when no_bid < 5¢ |
| `max_buy_yes_strike_above_pct` | `max_buy_yes_strike_above_pct` | 0.05 | Skip BUY YES when strike > current×(1+pct) |
| `max_buy_no_strike_above_pct` | `max_buy_no_strike_above_pct` | 0.05 | Similar for BUY NO on "below X" |
| `enable_intra_arb` | `enable_intra_arb` | true | Buy YES+NO when sum < 1−fee (same market) |
| `enable_inter_arb` | `enable_inter_arb` | true | Kalshi YES + Gemini NO hedge |
| `kalshi_maker_fee_rate` | `kalshi_maker_fee_rate` | 0.0175 | 1.75% maker fee |
| `gemini_maker_fee_rate` | `gemini_maker_fee_rate` | 0.0175 | 1.75% maker fee |
| `absolute_winner_min_p_win` | `absolute_winner_min_p_win` | 0.95 | P(win) for "slam dunk" contracts |
| `absolute_winner_max_price` | `absolute_winner_max_price` | 0.05 | Max price (e.g. 5¢) for slam dunk |
| `absolute_winner_max_pct_of_balance` | `absolute_winner_max_pct_of_balance` | 0.50 | Max % of balance for slam dunk |
| `absolute_winner_max_contracts` | `absolute_winner_max_contracts` | 500 | Cap contracts per slam dunk |
| `use_calibration` | `use_calibration` | true | Use historical outcomes to adjust |
| `favor_yes_when_above_strike` | `favor_yes_when_above_strike` | true | Upgrade SKIP→BUY YES when price above strike |
| `min_calibration_samples` | `min_calibration_samples` | 5 | Min "above+YES" trades before calibration penalty |
| `calibration_min_win_rate_above_yes` | `calibration_min_win_rate_above_yes` | 0.45 | If above+YES win rate < this, don't favor YES |
| `calibration_extra_edge_above_yes` | `calibration_extra_edge_above_yes` | 0.05 | Extra edge required when above+YES underperforms |
| `calibration_extra_edge_overall` | `calibration_extra_edge_overall` | 0.02 | Add to min_edge when overall win rate < 50% |
| `min_calibration_samples_overall` | `min_calibration_samples_overall` | 20 | Min trades for overall calibration bump |
| `use_bucket_penalty` | `use_bucket_penalty` | true | Dampen hist_p when bucket underperforms |
| `min_bucket_samples` | `min_bucket_samples` | 5 | Min trades in bucket for penalty |
| `min_vol_confidence_far_otm` | `min_vol_confidence_far_otm` | 0.97 | Require vol≥97% for far OTM BUY YES |
| `enable_sell_and_rebuy` | `enable_sell_and_rebuy` | false | Re-buy when selling loser if edge remains |
| `p_lose_blend_market_max_weight` | `p_lose_blend_market_max_weight` | 0.5 | Max market weight in P(lose) blend at expiry |
| `p_lose_blend_market_half_life_days` | `p_lose_blend_market_half_life_days` | 0.5 | Half-life for market-weight decay |
| `use_intraday_empirical` | `use_intraday_empirical` | true | Use 1h prices at resolution hour for intraday empirical |
| `use_kelly` | `use_kelly` | true | Kelly criterion for position sizing; reduces size for farther-OTM (lower P(win)) |
| `kelly_fraction` | `kelly_fraction` | 0.25 | Fractional Kelly (0.25 = quarter-Kelly; safer than full Kelly) |
| `use_live_price` | `use_live_price` | true | Blend live + closes[-1] for vol model & safety; reflects sharp moves |
| `live_price_weight` | `live_price_weight` | 0.6 | When both available: 60% live + 40% closes[-1]; slightly prioritize live |
| `empirical_use_rolling_window` | `empirical_use_rolling_window` | true | Use rolling window for empirical probability |
| `empirical_recent_days` | `empirical_recent_days` | 90 | Recent window length for empirical model |
| `empirical_recent_weight` | `empirical_recent_weight` | 3.0 | Extra weight applied to recent samples |
| `use_volatility_by_default` | `use_volatility_by_default` | true | Apply volatility model even without `--use-volatility` |
| `vol_dynamic_n0` | `vol_dynamic_n0` | 90 | Sample-size pivot for dynamic empirical/vol blend |
| `uncertainty_edge_mult` | `uncertainty_edge_mult` | 0.5 | Adds CI-based uncertainty buffer to min edge |
| `strategy_engine` | `strategy_engine` | `vol_regime` | Select recommendation engine (`vol_regime` default, `default_threshold` optional) |
| `stop_loss_pct` | `stop_loss_pct` | 0.50 fallback | Stop-loss threshold from recorded entry price (current config 0.20) |
| `enable_strike_validation` | `enable_strike_validation` | true | Validate extracted strike against current price and market odds |
| `strike_validation_tolerance_pct` | `strike_validation_tolerance_pct` | 0.05 | Tolerance band used for strike sanity checks |
| `enable_otm_smile_adjustment` | `enable_otm_smile_adjustment` | true | Apply OTM smile proxy in volatility probability |
| `otm_smile_strength` | `otm_smile_strength` | 0.35 | Smile intensity |
| `otm_smile_max_mult` | `otm_smile_max_mult` | 2.0 | Cap for smile volatility multiplier |
| `liquidity_spread_bps_soft` | `liquidity_spread_bps_soft` | 1500 | Start reducing size when spread proxy is wide |
| `liquidity_spread_bps_hard` | `liquidity_spread_bps_hard` | 4000 | Maximum spread threshold for size haircut |
| `liquidity_min_size_multiplier` | `liquidity_min_size_multiplier` | 0.20 | Minimum size multiplier under poor liquidity |
| `liquidity_missing_ask_multiplier` | `liquidity_missing_ask_multiplier` | 0.80 | Size multiplier when ask data is missing |
| `enable_signal_model` | `enable_signal_model` | true | Route features through weighted signal model for final P(YES) |
| `signal_model_weights` | `signal_model_weights` | `{...}` | Feature weights for logistic-style scorer |
| `use_sklearn_calibration` | `use_sklearn_calibration` | true | Enable sklearn calibration from resolved predictions |
| `sklearn_calibration_method` | `sklearn_calibration_method` | isotonic | Calibration method (`isotonic` or `platt`) |
| `sklearn_calibration_min_samples` | `sklearn_calibration_min_samples` | 50 | Minimum resolved samples required for calibration fit |

---

## Command-Line Arguments (VERBOSE)

### `scripts/compare_historical.py`

| Argument | Type | Default | Purpose |
|----------|------|---------|---------|
| `--days` | int | config `days_back` or 365 | Days of historical data for empirical P |
| `--markets` | int | 0 | Max markets per series to evaluate (0 = all) |
| `--edge` | float | 0.15 | Override min_edge (0–1); e.g. 0.15 = 15% edge required |
| `--no-llm` | flag | — | Skip Ollama analysis (useful for cron, CI) |
| `--verbose` / `-v` | flag | — | Show closes range vs threshold (explains hist_p) |
| `--preview` | flag | — | Show planned orders, no API calls |
| `--execute` | flag | — | Place real buy/sell orders (requires `dry_run: false` in config) |
| `--use-volatility` | flag | — | Force-enable volatility model (also on by default when `use_volatility_by_default` is true) |
| `--limit-analysis` | flag | — | Print analysis of unfilled limit orders (why they didn't hit; informs limit_dip_pct tuning) |
| `--log` [DIR] | optional | — | Log output to `~/Desktop/trading-bot-logs/` or specified dir (eval_YYYYMMDDHH.log per hour) |

**Example:** `python scripts/compare_historical.py --days 365 --preview` → evaluate all markets, no orders, 365 days of history.

### `scripts/backtest.py`

| Argument | Type | Default | Purpose |
|----------|------|---------|---------|
| `--mode` | choice | real | `real` = resolved predictions; `synthetic` = snapshots+resolved_outcomes |
| `--days` | int | 0 | Limit to last N days (0 = all) |

**Example:** `python scripts/backtest.py --mode real --days 90` → backtest last 90 days of real trades.

### `scripts/compare_prices.py`

| Argument | Type | Default | Purpose |
|----------|------|---------|---------|
| `--no-llm` | flag | — | Skip Ollama analysis |

### `scripts/test_connection.py`

| Argument | Type | Default | Purpose |
|----------|------|---------|---------|
| `--debug` | flag | — | Run env/key diagnostics before connecting |
| `--inspect` | flag | — | Show API response schema (balance, markets, orderbook) |
| `--no-llm` | flag | — | Skip Ollama analysis |
| `--test-apis` | flag | — | Read-only test: verify positions, orders APIs |

### `scripts/debug_ollama.py`

No arguments. Checks Ollama server, models, chat, and `ollama_analysis.run_analysis()`.

---

## API Clients Deep Dive

### `kalshi_client.py` — Kalshi API

**Auth:** RSA-PSS signing. Requires `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env`. `KALSHI_BASE_URL` selects demo vs production.

**Key methods:**
- `get_balance()` — Returns `{ balance: cents }`. Fails if keys invalid.
- `get_markets(series_ticker, status)` — Open markets for a series (e.g. KXBTCD).
- `get_market(ticker)` — Single market details (yes_bid, no_bid, floor, cap, title, etc.).
- `get_positions()` — Paginated; returns `market_positions` with `ticker`, `position_fp` (positive=YES, negative=NO), `contracts`.
- `get_orderbook(ticker)` — Bids/asks for depth.
- `create_order_safe(ticker, side, action, count, yes_price_dollars|no_price_dollars, ...)` — Places limit order; caps by `max_pct_of_balance`; retries on 429.
- `cancel_order_safe(order_id)` — Cancels by order ID.

### `gemini_client.py` — Gemini Prediction Markets (Crypto)

**Two clients:**
1. **GeminiPredictionClient** — Public API for events/contracts (no auth for read).
2. **GeminiTradingClient** — Place orders; needs `GEMINI_API_KEY` + `GEMINI_API_SECRET`.

**PredictionClient:**
- `get_crypto_events(limit)` — Fetches `crypto` category; skips `hourly` if API returns 400.
- `_event_to_asset(evt)` — Maps event title/ticker → BTC-USD, ETH-USD, etc.
- `extract_strike_from_event(evt)` — Parses strike from title.
- `get_yes_price(contract)`, `get_no_price(contract)` — From orderbook or last trade.

**TradingClient:**
- `get_balance()` — Balance in USD.
- `place_order_safe(...)` — Caps by balance; nonce monotonically increasing per key.
- `get_positions()` — Positions with `outcome` (yes/no), `totalQuantity`.
- `get_active_orders()`, `cancel_order_safe()`.

### `yfinance_client.py` — Historical Prices

**No API key.** Uses Yahoo Finance (or CoinGecko for crypto if yfinance fails).

**Key methods:**
- `get_aggs(ticker, days_back, config)` — Daily closes. Checks `storage.get_yf_closes()` first; if stale/missing, fetches and `storage.set_yf_closes()`.
- `get_intraday_aggs_at_hour(ticker, days_back, hour_et, config)` — 1h prices at that ET hour per day. Falls back to daily closes if intraday unavailable.
- `get_aggs_as_of(ticker, as_of_ts, days_back, config)` — Point-in-time for backtest (no future leakage).
- `get_snapshot(ticker)` — Current price (single point).

**Fallback:** CoinGecko used for crypto tickers when yfinance returns no data.

---

## Storage Module Deep Dive

**Purpose:** SQLite persistence for yfinance cache, market snapshots, resolved outcomes, and placed predictions. Reduces API calls and enables backtesting/calibration.

**Database location:** `~/.trading-bot-data/trading_bot.db` (or `TRADING_BOT_DATA_DIR`). Schema created on first use via `_init_schema()`.

**Key functions:**

| Function | When called | What it does |
|----------|-------------|--------------|
| `get_yf_closes(ticker, days_back)` | `yfinance_client.get_aggs()` | Returns cached daily closes if fresh (< 1h); else `None` |
| `set_yf_closes(ticker, days_back, closes)` | After yfinance fetch | Writes or updates `yf_cache` |
| `get_yf_intraday_at_hour(...)` | `get_intraday_aggs_at_hour()` | Cached 1h-at-resolution-hour prices |
| `set_yf_intraday_at_hour(...)` | After intraday fetch | Writes `yf_intraday_cache` |
| `record_market_snapshot(...)` | After each market eval in compare_historical | Appends `market_snapshots` (yes_bid, no_bid, hist_p, rec, etc.) |
| `record_prediction(...)` | When order fills (Kalshi/Gemini) | Inserts `predictions` with order_id, side, entry_price, hist_p |
| `record_prediction_outcome(...)` | `_sync_kalshi_settlements()` on resolve | Updates predictions with outcome, pnl_cents, resolved_ts |
| `get_fill_attribution_stats(...)` | compare_historical startup diagnostics | Summarizes actual-vs-theoretical fill quality (avg slippage/spread) |
| `record_resolved(...)` | `_sync_kalshi_settlements()` | Upserts `resolved_outcomes` (yes_settled, our_last_rec, etc.) |
| `record_limit_order(...)` | When placing BUY limit order | Inserts `limit_orders` (limit_price, bid_at_place, dip_pct_used, hist_p, etc.) |
| `record_order_cancel(...)` | When cancelling resting order | Updates `limit_orders` with status=cancelled, cancel_reason (no_longer_rec|want_better_price) |
| `get_limit_order_analysis(config, days)` | `--limit-analysis` | Returns unfilled orders + would_have_filled from market_snapshots (inform limit_dip_pct) |
| `get_last_snapshot_for_ticker(ticker)` | Position eval, orphan positions | Last snapshot for a ticker |
| `get_market_snapshots_for_backtest(...)` | backtest.py synthetic | Snapshots in time range with rec filter |
| `get_resolved_outcomes(config)` | Calibration, backtest synthetic | Dict ticker → {yes_settled, ...} |
| `get_predictions_for_backtest(...)` | backtest.py real | Resolved predictions in time range |
| `get_calibration_stats(config)` | compare_historical startup | Win rate by bucket (hist_p bin, direction+side) |
| `get_unresolved_predictions(config)` | compare_historical | Predictions with outcome=NULL |
| `get_last_snapshot_per_ticker(...)` | backtest synthetic | Last snapshot per ticker with rec filter |

**Cache TTL:** 3600 seconds (1 hour) for yf_cache and yf_intraday_cache. Older entries return `None`; caller re-fetches.

---

## Ollama Analysis Deep Dive

**Purpose:** Send script output (or per-trade summaries) to a local LLM (Ollama) for analysis. Completely optional; scripts run fine with `--no-llm`.

**Flow:**
1. Script calls `capture_and_analyze(run_fn, script_name, use_llm=..., per_trade_analysis=...)`.
2. `capture_and_analyze` runs `run_fn()`. If `per_trade_analysis` and script is compare_historical, the script itself calls `analyze_single_trade()` for each proposed trade.
3. Otherwise, stdout is captured (via `io.StringIO` or similar). If `use_llm` and Ollama is reachable, truncated output is sent to `run_analysis(text, script_name)`.
4. `_get_analysis(text)` truncates if too long, builds a prompt, calls `ollama.chat()`, prints the LLM response.

**When per_trade_analysis:** compare_historical can analyze each BUY/SELL recommendation individually (if enabled). Else one batch analysis of full output.

**Failures:** If Ollama is down or no model, analysis is skipped silently (or with a note). `debug_ollama.py` surfaces exact errors.

**Environment:** `OLLAMA_MODEL` (default llama3.2), `OLLAMA_HOST` (default localhost:11434), `OLLAMA_MAX_CHARS` (25000), `OLLAMA_HEAD_CHARS` (8000), `OLLAMA_TAIL_CHARS` (12000) — truncation keeps start + end of output; middle omitted.

---

## Constants Reference

All recommendation and platform identifiers live in `constants.py`:

| Constant | Value | Meaning |
|----------|-------|---------|
| `REC_SKIP` | `"SKIP"` | No buy recommended; hist_p vs market doesn't meet edge threshold |
| `REC_BUY_YES` | `"BUY YES"` | Buy YES contract; hist_p suggests market underprices YES |
| `REC_BUY_NO` | `"BUY NO"` | Buy NO contract; hist_p suggests market underprices NO |
| `REC_ARB_INTRA` | `"ARB INTRA"` | Intra-market arb: buy both YES and NO on same market (sum < 1−fee) |
| `REC_ARB_INTER` | `"ARB INTER"` | Inter-platform arb: buy YES on one exchange, NO on another |
| `REC_LIKELY_LOSE` | `"LIKELY_LOSE"` | Sell trigger: P(lose) ≥ sell_losing_pct |
| `REC_RULE_VIOLATION` | `"RULE_VIOLATION"` | Sell trigger: e.g. hold NO on "above X" but price > strike |
| `PLATFORM_KALSHI` | `"kalshi"` | Kalshi exchange |
| `PLATFORM_GEMINI` | `"gemini"` | Gemini Prediction Markets |

---

## Error Handling & Edge Cases

**When yfinance returns no data:** `get_aggs()` returns `[]`. Empirical P becomes 0. Market is skipped or falls back to volatility-only when available.

**When get_snapshot fails (proxy, API down):** Live price falls back to `closes[-1]` (last historical close). Model and safety rules still run; sharp recent moves are not reflected until the next successful snapshot.

**When Kalshi/Gemini API fails (429, 500, auth):** Clients log and re-raise or return empty. Script continues with other markets. `create_order_safe` / `place_order_safe` cap by balance and retry on transient errors.

**When storage is disabled:** `get_yf_closes` returns `None` always; yfinance fetches every run. No market_snapshots, predictions, or calibration.

**When strike cannot be extracted:** `_extract_strike()` returns `(None, None)`; market is skipped (no threshold to compare).

**When resolution time is unparseable:** `_get_days_to_expiry_from_market()` returns `default_days` (e.g. 30). Volatility model uses that for time scaling.

**When calibration has no data:** `get_calibration_stats()` returns empty buckets; no penalty or favor_yes adjustment applied.

**When Ollama is unreachable:** Analysis skipped; main script output still printed. Use `--no-llm` in cron to avoid blocking.

---

## Example Walkthrough

**Scenario:** "Bitcoin above $81,250 tomorrow at 5pm ET?" on March 15.

1. **Config:** KXBTCD → BTC-USD, days_back=365, min_edge=0.10.
2. **Fetch:** `yf_get_aggs(BTC-USD, 365)` → 365 daily closes. Cache hit if run within last hour.
3. **Market:** Kalshi returns market with yes_bid=0.40, no_bid=0.58, floor_strike=81250.
4. **Strike:** `_extract_strike()` → (81250, "above").
5. **Empirical:** 15 of 365 days closed above 81250 → emp_p = 15/365 ≈ 4.1%.
6. **Volatility (if --use-volatility):** Log-normal P(S>81250) with EWMA vol, dte≈1 → vol_p ≈ 5%.
7. **Blend:** hist_p = 0.5×0.041 + 0.5×0.05 ≈ 4.6% (or pure emp if no vol).
8. **Recommend:** yes_bid=0.40; hist_p - 0.40 - fee < 0 → no BUY YES. no_bid=0.58; (1-0.046) - 0.58 - fee ≈ 0.37 > 0.10 → BUY NO.
9. **Safety:** Price (e.g. $96k) > strike $81,250 → skip BUY NO (no_above_cushion; we'd be betting "no" on "above" while price is already above).
10. **Result:** SKIP (safety rule overrides). If price were below strike, we'd add a limit order at no_bid × (1−dip).

---

## Data Flow Diagram

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│ config.yaml │────▶│ config.py     │────▶│ All scripts     │
└─────────────┘     │ get_config() │     └────────┬────────┘
                    └──────────────┘              │
                                                  ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────────────────────────┐
│ .env        │────▶│ kalshi_      │     │ compare_historical.py           │
│ (API keys)  │     │ gemini_      │────▶│ 1. Fetch Gemini + Kalshi       │
└─────────────┘     │ client       │     │ 2. Fetch yfinance closes (cache) │
                    └──────────────┘     │ 3. Evaluate each market        │
                          │              │ 4. Evaluate positions → sells   │
                          │              │ 5. Execute or preview            │
                          ▼              └────────────────┬────────────────┘
                    ┌──────────────┐                       │
                    │ yfinance_   │                       ▼
                    │ client      │              ┌─────────────────┐
                    │ get_aggs()  │              │ storage.py      │
                    └─────────────┘              │ yf_cache,       │
                          │                     │ market_snapshots│
                          └─────────────────────▶ predictions     │
                                                 └─────────────────┘
```

---

## Local Storage Schema

All data is stored in SQLite at `~/.trading-bot-data/trading_bot.db` (configurable via `storage.data_dir` or `TRADING_BOT_DATA_DIR`). Not committed to git.

### Schema Overview

| Table                | Purpose                                           | TTL      | Primary Key                                   |
| -------------------- | ------------------------------------------------- | -------- | --------------------------------------------- |
| **yf_cache**         | Daily closes from yfinance (fewer API calls)      | 1 hour   | `(ticker, days_back)`                          |
| **yf_intraday_cache**| Intraday prices at resolution hour (5pm/4pm ET)    | 1 hour   | `(ticker, days_back, resolution_hour_et)`      |
| **market_snapshots**  | Per-evaluation snapshot (yes/no bids+asks+sizes, hist_p, rec)| None     | `id` (auto-increment)                          |
| **resolved_outcomes**| Contract settlements (for calibration)            | None     | `ticker`                                      |
| **predictions**       | Placed orders + outcomes (backtest, calibration)  | None     | `id` (auto-increment)                          |
| **limit_orders**      | Limit orders placed + cancel reasons (learn from unfilled) | None     | `id` (auto-increment)                          |

### Table Structures

```
yf_cache
├── ticker           TEXT      (e.g. BTC-USD, ^GSPC)
├── days_back        INTEGER   (e.g. 365)
├── closes_json      TEXT      (JSON array of close prices)
├── updated_at       REAL      (Unix timestamp)
└── PRIMARY KEY (ticker, days_back)

yf_intraday_cache
├── ticker           TEXT
├── days_back        INTEGER
├── resolution_hour_et INTEGER (0–23, e.g. 17 = 5pm ET)
├── closes_json      TEXT      (JSON array, one price per day at that hour)
├── updated_at       REAL
└── PRIMARY KEY (ticker, days_back, resolution_hour_et)

market_snapshots
├── id               INTEGER   (auto-increment)
├── platform         TEXT      (kalshi | gemini)
├── ticker           TEXT
├── title            TEXT
├── yf_ticker        TEXT
├── ts               REAL      (Unix timestamp)
├── yes_bid          REAL
├── no_bid           REAL
├── hist_p           REAL
├── vol_p            REAL
├── rec              TEXT      (BUY YES | BUY NO | SKIP)
├── threshold        REAL
├── direction        TEXT      (above | below)
└── edge             REAL

resolved_outcomes
├── ticker           TEXT      (PRIMARY KEY)
├── platform         TEXT
├── title            TEXT
├── resolution_ts    REAL
├── yes_settled      INTEGER   (1=YES won, 0=NO won)
├── our_last_rec     TEXT
├── our_last_hist_p  REAL
└── market_yes_at_last REAL

predictions
├── id               INTEGER   (auto-increment)
├── platform         TEXT
├── ticker           TEXT
├── order_id         TEXT
├── side             TEXT      (yes | no)
├── hist_p           REAL
├── vol_p            REAL
├── entry_price      REAL
├── count            INTEGER
├── ts               REAL
├── yf_ticker        TEXT
├── threshold        REAL
├── direction        TEXT
├── rec              TEXT
├── outcome          TEXT      (win | lose, NULL until resolved)
├── pnl_cents        INTEGER
├── resolved_ts      REAL
├── min_price_after_buy REAL
├── max_price_after_buy REAL
├── theoretical_price REAL
├── bid_at_place     REAL
├── spread_bps_at_place REAL
└── fill_slippage_bps REAL

limit_orders
├── id               INTEGER   (auto-increment)
├── platform         TEXT      (kalshi | gemini)
├── ticker           TEXT
├── order_id         TEXT
├── side             TEXT      (yes | no)
├── limit_price      REAL
├── bid_at_place     REAL      (market bid when order placed)
├── dip_pct_used     REAL      (e.g. 0.05)
├── hist_p           REAL
├── vol_p            REAL
├── ts               REAL
├── status           TEXT      (placed | filled | cancelled)
└── cancel_reason    TEXT      (no_longer_rec | want_better_price, NULL if not cancelled)
```

### How Data Flows

| Data                    | Written by                              | Read by                                        |
| ----------------------- | --------------------------------------- | ---------------------------------------------- |
| **yf_cache**            | `yfinance_client.get_aggs()` via `storage.set_yf_closes()` | `yfinance_client.get_aggs()` via `storage.get_yf_closes()` |
| **yf_intraday_cache**   | `yfinance_client.get_intraday_aggs_at_hour()` via `storage.set_yf_intraday_at_hour()` | `yfinance_client.get_intraday_aggs_at_hour()` via `storage.get_yf_intraday_at_hour()` |
| **market_snapshots**    | `compare_historical.py` → `record_market_snapshot()` | `backtest.py`, `get_last_snapshot_for_ticker()`, calibration |
| **resolved_outcomes**   | `_sync_kalshi_settlements()` → `record_resolved()` | Calibration, backtest                           |
| **predictions**         | `record_prediction()` on order fill; `record_prediction_outcome()` on settlement | `get_unresolved_predictions()`, `get_calibration_stats()`, `backtest.py` |
| **limit_orders**       | `record_limit_order()` on place; `record_order_cancel()` on cancel | `get_limit_order_analysis()` for `--limit-analysis` |

### TTL Behavior

- **yf_cache** and **yf_intraday_cache**: Entries older than 1 hour (`_CACHE_TTL_SECONDS = 3600`) are treated as stale. Callers receive `None` and re-fetch from yfinance.
- **market_snapshots**, **resolved_outcomes**, **predictions**, **limit_orders**: Append-only; no TTL. Old data retained for backtesting, calibration, and limit-order analysis.

---

## Key Concepts (Layman's Terms)

| Term                   | Meaning                                                                                      |
| ---------------------- | -------------------------------------------------------------------------------------------- |
| **Strike / threshold** | The price the bet is about, e.g. $81,250 in "Bitcoin above $81,250"                          |
| **hist_p**             | Our estimated probability of YES from history (and optionally volatility)                    |
| **Edge**               | How much we think the market misprices — e.g. we say 30%, market says 15¢ → edge for BUY YES |
| **Limit order**        | "Buy only if price ≤ X" — waits until someone matches                                        |
| **Dip**                | Order placed below current bid to try to get a better fill on temporary drops                |
| **Intra-arb**          | Buy YES and NO on same market when total cost < 1−fee                                        |
| **Inter-arb**          | Buy YES on Kalshi and NO on Gemini (or vice versa) when total cost < 1−fee                   |

---

## Quick Reference

| Question                             | Answer                                                                |
| ------------------------------------ | --------------------------------------------------------------------- |
| Where is the main evaluation?        | `scripts/compare_historical.py` → `_run_impl()` L1195                 |
| Where is the backtest?               | `scripts/backtest.py` — `--mode real` or `synthetic`                 |
| Where are strategy params defined?   | `config.yaml` → `strategy:`; loaded in `_load_strategy_params()` L115 |
| Where is P(YES) computed?            | `_empirical_probability()` L434 + `_volatility_probability()` L463    |
| Where is P(lose) computed?           | `_compute_p_lose()` L652 — blends model with market odds; more weight near expiry |
| Where are Kalshi orders placed?      | `kalshi_client.py` → `create_order_safe()`                            |
| Where are Gemini orders placed?      | `gemini_client.py` → `place_order_safe()`                             |
| Where is historical price data from? | `yfinance_client.py` → `get_aggs()` (Yahoo Finance / CoinGecko)       |
| Where is data cached?                | `storage.py` → SQLite in `~/.trading-bot-data/`                       |
| How to run every 15 min?             | `launchd/` plist; see `launchd/README.md`                             |
