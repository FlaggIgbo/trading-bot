# Trading Bot — Code Explainer

> **Layman's summary:** The bot watches prediction markets (e.g. "Will Bitcoin be above $81,250 on March 16?"), checks how often that actually happened in the past year, and compares that "historical chance" to what the market is charging. If the market is wrong, it suggests BUY YES, BUY NO, or SKIP.

---

## Table of Contents

1. [High-Level: What the Bot Does](#high-level-what-the-bot-does)
2. [Step-by-Step Flow (Main Script)](#step-by-step-flow-main-script)
3. [Every File in the Repository](#every-file-in-the-repository)
4. [Data Flow Diagram](#data-flow-diagram)
5. [Key Concepts (Layman's Terms)](#key-concepts-laymans-terms)
6. [Quick Reference](#quick-reference)

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
| Compute empirical P(YES)    | Fraction of past days above/below threshold                 | `compare_historical.py:434` → `_empirical_probability()`      |
| Compute volatility P(YES)   | Log-normal model using EWMA vol, time-to-expiry             | `compare_historical.py:463` → `_volatility_probability()`     |
| Blend probabilities         | `vol_blend_weight * emp + (1−w) * vol` when both available  | `compare_historical.py:1220-1223`                             |
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
| **`scripts/backtest.py`**           | Backtest on historical data                         | `--mode real` (resolved predictions) or `synthetic` (market_snapshots + resolved_outcomes); Sharpe, win rate, max drawdown                                                                                                 |
| **`scripts/bot.py`**                | Simple polling bot (buy when price below threshold) | `main()` L34, `_check_market()` L101, `_resolve_markets()` L84                                                                                                                       |
| **`scripts/compare_prices.py`**     | Live price snapshot (Kalshi, Gemini, yfinance)      | `_run()` L40, `main()` L119                                                                                                                                                          |
| **`scripts/test_connection.py`**    | Test API connections, balance, markets              | `_run()` L130, `debug_check()` L30, `inspect_output_types()` L86                                                                                                                     |
| **`scripts/debug_ollama.py`**       | Debug Ollama server, models, chat                   | `main()` L16                                                                                                                                                                         |

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
| **`yfinance_client.py`** | Historical prices (no API key)     | `get_aggs()` L137 (daily closes), `get_aggs_as_of()` (point-in-time for backtest), `get_snapshot()` L52 (current price); CoinGecko fallback for crypto                                                                                                                                   |

### Support Modules

| File                     | Purpose                | Key functions                                                                                                                                |
| ------------------------ | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **`constants.py`**       | Recommendation strings | `REC_SKIP`, `REC_BUY_YES`, `REC_BUY_NO`, `REC_ARB_INTRA`, `REC_ARB_INTER`, `REC_LIKELY_LOSE`, `REC_RULE_VIOLATION`                           |
| **`storage.py`**         | SQLite persistence     | `get_yf_closes()`, `set_yf_closes()`, `record_market_snapshot()`, `record_prediction()`, `record_prediction_outcome()`, `record_resolved()`, `get_last_snapshot_for_ticker()` (platform filter), `get_market_snapshots_for_backtest()`, `get_resolved_outcomes()`, `get_predictions_for_backtest()`, `_init_schema()` L58 |
| **`ollama_analysis.py`** | Local LLM analysis     | `capture_and_analyze()`, `run_analysis()`, `analyze_single_trade()` L117, `_get_analysis()` L78                                              |

### Scheduling & Dev Tools

| File                                            | Purpose                                                   |
| ----------------------------------------------- | --------------------------------------------------------- |
| **`launchd/com.user.compare_historical.plist`** | Run `compare_historical` every 10 min on macOS (configurable) |
| **`launchd/README.md`**                         | Setup: copy plist, `launchctl load`, caffeinate           |
| **`package.json`**                              | Prettier dev dependency for YAML/JSON/MD formatting       |
| **`.pre-commit-config.yaml`**                   | Ruff (Python linter/formatter) + Prettier on git push     |
| **`docs/IMPROVEMENTS.md`**                      | Ideas and backlog for future work                         |
| **`README.md`**                                 | Project overview, setup, usage, cron/launchd instructions |

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
