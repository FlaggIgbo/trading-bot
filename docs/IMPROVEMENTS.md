# Trading Bot Improvement Roadmap

Inspired by top open-source trading repositories: [freqtrade](https://github.com/freqtrade/freqtrade), [ccxt](https://github.com/ccxt/ccxt), [hummingbot](https://github.com/hummingbot/hummingbot), [gekko](https://github.com/askmike/gekko), and [Stock-Prediction-Models](https://github.com/huseinzol05/Stock-Prediction-Models).

---

## Implemented ✓

| Feature                                 | From                                    | Status                                                                                                                                                                                                                                                   |
| --------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dry-run / preview mode                  | Freqtrade                               | ✓ `--preview` in compare_historical.py                                                                                                                                                                                                                   |
| Volatility-based probability            | Stock-Prediction-Models, options theory | ✓ `--use-volatility` log-normal model; per-contract time-to-expiry parsed from title (e.g. "tomorrow at 5pm ET"); `intraday_other_min_p_win_with_vol: 0.80` eases strict threshold (90%→80%) when vol model is used                                      |
| Balance-aware order execution           | Freqtrade                               | ✓ `create_order_safe`, `max_pct_of_balance`                                                                                                                                                                                                              |
| Low-balance limit fallback              | Freqtrade                               | ✓ `min_balance_for_limit_fallback_cents` — place 1-contract limit instead of skipping when balance is low                                                                                                                                                |
| Config-driven strategy                  | Freqtrade, Gekko                        | ✓ `config.yaml` strategy, min_edge                                                                                                                                                                                                                       |
| Kelly criterion position sizing         | Freqtrade, Stock-Prediction-Models      | ✓ `strategy.use_kelly`, `kelly_fraction` (default 0.25); reduces size for farther-OTM contracts (lower P(win)); enabled by default in config                                                                                                                                                                                                                 |
| Whitelist / blacklist                   | Freqtrade                               | ✓ `strategy.whitelist`, `strategy.blacklist`                                                                                                                                                                                                             |
| File logging for run history            | Freqtrade                               | ✓ `--log` or `--log DIR`; eval_YYYYMMDDHH.log per hour, append within hour                                                                                                                                                                                |
| Systematic edges (arbitrage)            | QuantPedia                              | ✓ Intra-exchange + inter-exchange (Kalshi↔Gemini) arb; `min_arb_fee`, `enable_intra_arb`, `enable_inter_arb`                                                                                                                                             |
| XRP (Ripple) market support             | —                                       | ✓ KXXRPD, XRP-USD; crypto treatment, arbitrage, intraday                                                                                                                                                                                                 |
| Ollama LLM analysis                     | —                                       | ✓ Local free analysis; `scripts/debug_ollama.py`; llama3.2:1b fallback                                                                                                                                                                                   |
| **Budget-aware order allocation**       | —                                       | ✓ `_allocate_budget_to_orders`; when total cost exceeds budget, prefers 1 contract each (diversification) over 10 of one; `trading.max_total_buy_pct` (default 50%)                                                                                      |
| **Ollama per-trade analysis**           | —                                       | ✓ Each proposed buy/sell is interpreted by Ollama as it's displayed (not batch at end); `analyze_single_trade()` in ollama_analysis.py                                                                                                                   |
| **Flexible margin config**              | —                                       | ✓ `trading.max_pct_of_balance` (per order), `trading.max_total_buy_pct` (all buys combined)                                                                                                                                                              |
| Recommendation constants                | —                                       | ✓ `constants.py`: REC*SKIP, REC_BUY_YES, REC_BUY_NO, REC_ARB*\*, REC_LIKELY_LOSE, REC_RULE_VIOLATION                                                                                                                                                     |
| **Order review (cancel/update/keep)**   | —                                       | ✓ Before placing, review resting orders on Kalshi + Gemini; cancel if no longer recommended or if we want a higher limit; keep if existing order is same or better; `cancel_order_safe`, `create_order_safe`, `place_order_safe`                         |
| **Kalshi 4xx error handling**           | Kalshi docs                             | ✓ `parse_kalshi_error()`, `round_price_to_tick()` using `price_ranges`; `create_order_safe` catches 4xx, logs `invalid_price`/`insufficient_balance` hints; never crashes on order rejection                                                             |
| **Gemini 4xx/406 error handling**       | Gemini docs                             | ✓ `parse_gemini_error()`, `place_order_safe`, `cancel_order_safe`; price rounded to 0.01; catches InvalidPrice, InsufficientFunds, InvalidQuantity, MarketNotOpen, InvalidSymbol; see [docs.gemini.com/error-codes](https://docs.gemini.com/error-codes) |
| **Vol blend (empirical + volatility)**  | Taylor, IMPROVEMENTS §6                 | ✓ Dynamic empirical/vol blending (`vol_dynamic_n0`) with configurable base weight; feeds into optional signal-model scoring pipeline                                                                                                                          |
| **EWMA volatility**                     | Taylor Ch 9                             | ✓ `strategy.use_ewma_vol`, `ewma_lambda` (0.94); σ²*t = λ·σ²*{t-1} + (1−λ)·r²_t; time-varying vol in `_volatility_probability`                                                                                                                           |
| **High-confidence limit orders**        | —                                       | ✓ `strategy.limit_dip_pct_high_confidence` (e.g. 15%); when P(win) ≥ 90%, place limit 15% below bid to prioritize catching bigger dips on high-certainty contracts                                                                                       |
| **Limit dip when price might decrease** | —                                       | ✓ `strategy.limit_dip_pct_price_decline` (e.g. 10%); when vol model is on or 2+ days to expiry, use larger dip to prioritize limit orders—contract price may dip before settlement; stacks with high-confidence                                          |
| **Local historical storage**            | Freqtrade, backtesting                  | ✓ SQLite under `~/.trading-bot-data/`; yfinance closes cache (1h TTL), market snapshots (yes/no bids, hist_p, vol_p, rec per eval), resolved_outcomes schema for calibration; enables backtesting & prediction calibration; not committed to git         |
| **Kalshi & Gemini fee accounting**      | Kalshi/Gemini docs                      | ✓ Maker fee = rate × P × (1−P) per contract; `kalshi_maker_fee_rate`, `gemini_maker_fee_rate` (0.0175); min_edge is _net_ edge after fees (e.g. 0.01 = 1¢ profit per contract)                                                                           |
| **BUY YES/NO preference**               | —                                       | ✓ When both YES and NO have edge, `_recommend()` prefers whichever yields the larger net return                                                                                                                                                        |
| **Contradictory-position avoidance**    | —                                       | ✓ `_filter_contradictory_buys()` skips BUYs that would conflict with kept positions OR other pending BUYs on same underlying + resolution (Kalshi + Gemini). E.g. NO on SOL<$83 and YES on SOL≥$92 are mutually exclusive; keeps higher-edge when both sides compete. Arb (YES+NO same contract) allowed.                                                    |
| **Gemini crypto events**               | —                                       | ✓ `get_crypto_events()` fetches `crypto` category (daily/weekly); `hourly` category may return 400—we catch and skip, still get daily markets (e.g. BTC2603162100).                                                                                                                                   |
| **Gemini position sell (losers)**      | Kalshi sell logic                       | ✓ Evaluates Gemini positions (like Kalshi) and sells contracts we judge as losers: REC_LIKELY_LOSE, rec flip (BUY YES→now BUY NO), or REC_RULE_VIOLATION. Uses `get_positions()`, `gemini_symbol_to_market` map, same hist_p/vol_p logic; SELL via `place_order_safe`. |
| **Calibration-based prediction tuning** | FreqAI-style ML                         | ✓ `_apply_bucket_penalty()` dampens hist_p when hist_p bucket underperforms (overconfidence correction); `_get_effective_min_edge()` raises min_edge when overall win rate < 50%; `_should_downgrade_buy_yes_above()` requires extra edge for BUY YES on "above X" when above+YES underperforms. Config: `use_bucket_penalty`, `min_bucket_samples`, `calibration_extra_edge_above_yes`, `calibration_extra_edge_overall`, `min_calibration_samples_overall`. |
| **Stricter far OTM threshold**         | —                                       | ✓ `min_vol_confidence_far_otm` (default 0.97) — when strike far above current, require vol model ≥97% (not 95%) before BUY YES; targets losing "above X" YES trades. |
| **Price-after-buy calibration**       | —                                       | ✓ `min_price_after_buy`, `max_price_after_buy` stored from market_snapshots after purchase; calibration stats show % dipped/rose after buy (improve limit_dip_pct). |
| **Hours until resolution in output** | —                                       | ✓ Contract title shows "(in X.X hours)" until resolution; decimal for sub-hour (e.g. 0.3 hours). |
| **Calibration loss weighting**       | —                                       | ✓ `strategy.calibration_loss_weight` (default 1.5); losers weighted higher in calibration; `get_calibration_stats()` uses weighted wins/loses for bucket and by_direction_side win rates. Learn more from mistakes. |
| **Sell and re-buy losers**            | —                                       | ✓ `strategy.enable_sell_and_rebuy`; when selling a losing position (LIKELY_LOSE or rec flip), optionally re-buy same contract at current price if model still sees edge; bypasses min 5¢ rule for re-entry. Kalshi + Gemini. |
| **P(lose) with market odds blend**    | —                                       | ✓ `_compute_p_lose()` blends model (hist_p) with Kalshi/Gemini market odds; market weight increases as we approach resolution (more accurate near expiry). Config: `p_lose_blend_market_max_weight` (0.5), `p_lose_blend_market_half_life_days` (0.5). |
| **Easy win (aligned high-confidence)** | —                                       | ✓ When model and market both say ~90%+ (aligned), use `min_edge_easy_win` (current config 0.02 = 2¢) instead of full `min_edge` — still requires positive net edge after fees. Config: `enable_easy_win`, `min_edge_easy_win`, `easy_win_min_p_win`, `easy_win_min_market_p`. |
| **Live price for model & safety**      | —                                       | ✓ `get_snapshot()` used for S0 in volatility model and all safety checks; reflects sharp moves (e.g. last 10 min). Fallback to closes[-1] when snapshot fails. |
| **ML-style signal model pipeline**     | AI/ML roadmap                           | ✓ Signal features (empirical, volatility, moneyness, spread/liquidity, market context) are passed through a configurable weighted scorer; optional sklearn calibration (isotonic/platt) on resolved predictions. |
| **Heavy-tail vol distribution**        | Taylor Ch 4                             | ✓ Optional Student-t (`strategy.vol_distribution: student_t`, `vol_t_df` now tuned to 4.5 in config) for stronger tail-risk protection vs Gaussian. |
| **Volatility spike safety mode**       | Taylor structural-break risk            | ✓ When short-horizon realized vol spikes over baseline (`vol_spike_threshold_mult`), raise edge via `vol_spike_edge_multiplier` to avoid regime-break overtrading. |
| **Directional parity penalty**         | Risk balancing                          | ✓ Dynamic side penalty from recent resolved outcomes (`parity_*` config). Dominant side (YES/NO) gets extra min-edge, reducing one-sided drift. |
| **Momentum crash protection**          | Execution reality                       | ✓ For crypto, skip BUY YES when live-vs-close move breaches `momentum_crash_threshold_pct` (currently -3.5%) to reduce false lockouts from routine intraday noise. |
| **Cross-platform exposure cap**        | Portfolio risk                          | ✓ `max_cross_platform_exposure` caps combined Kalshi+Gemini exposure per underlying + resolution window. |
| **Fee floor + index fee override**     | Kalshi fee nuance                       | ✓ `min_fee_per_order` pessimistic fee floor and `kalshi_sp500_maker_fee_rate` override for S&P-linked markets. |
| **Backtest win slippage tax**          | Freqtrade realism                       | ✓ Synthetic backtest supports `synthetic_win_slippage_tax_bps` to stress winner-side execution friction (in addition to entry slippage/spread). |
| **Dwell-time + partial-fill realism**  | Execution realism                       | ✓ Synthetic backtest now enforces dwell-time fills and partial-fill scaling when depth fields are available; supports assumed order-size fallback (`synthetic_fill_min_dwell_minutes`, `synthetic_assumed_order_size`). |
| **Smart limit-order engine**           | Execution realism                       | ✓ Added dynamic tick quoting (`limit_order_mode`, `tick_offset`), inventory skew (`max_inventory_skew_pct`), stability/depth gates (`min_stability_duration_seconds`, `min_order_book_depth`), layered thin-book size scaling, and vol-spike BUY cancel guard (`cancel_on_vol_spike`). |
| **Active order lifecycle management**  | Execution realism                       | ✓ Resting orders are reviewed each cycle with stale TTL cancellation (`limit_order_ttl_minutes`), no-longer-recommended cleanup, and regime-shift cleanup (`cancel_all_on_regime_shift`). |
| **Adaptive Kelly circuit breaker**     | Risk management                         | ✓ Kelly fraction now auto-scales down during calibration stress and recovers when win-rate stabilizes (`dynamic_kelly_*` settings). |
| **Macro cross-asset signal feature**   | Feature engineering                     | ✓ Signal model now includes `macro_24h_ret` from configurable tickers (current config `SPY`, `UUP`) to capture risk-on/off context with more reliable intraday feeds. |
| **Config validator gate (pre-push)**   | Reliability                             | ✓ `scripts/validate_config.py` runs on pre-push to catch invalid/conflicting risk settings before backtest gate and remote push. |
| **Modular strategy migration**         | Freqtrade architecture                  | ✓ Recommendation + signal feature/scoring paths are now strategy-driven (`strategy_engine`) rather than hardcoded in `compare_historical.py`. |
| **Push-time performance guard**        | Reliability                             | ✓ Pre-push hook runs configurable backtest gate (`scripts/backtest_guard.py`) and blocks pushes on weak performance thresholds. |
| **Daily performance report**           | Reliability                             | ✓ Launchd daily report writes dated `.txt` performance summaries (`scripts/daily_performance_report.py`). |
| **Emergency loss circuit breaker**     | Reliability                             | ✓ `max_daily_loss_pct` / `max_daily_loss_cents` block new BUY orders when realized 24h losses breach limits (SELL handling remains active). |
| **Depth persistence for snapshots**    | Backtest realism                        | ✓ `market_snapshots` now stores top-of-book asks/sizes (`yes_ask`, `no_ask`, `*_size`) so synthetic partial-fill logic can use real historical depth when available. |

---

## Repo & Code Improvements

Refactoring, architecture, and code quality changes.

### Implemented ✓

| Improvement                         | Description |
| ----------------------------------- | ----------- |
| **_StrategyParams consolidation**   | Strategy config loaded once into `_StrategyParams` dataclass; avoids repeated dict lookups; single source of truth for min_edge, fees, thresholds. |
| **_maybe_add_reentry_order helper** | Shared helper for sell-and-rebuy logic; used by Kalshi (cached + orphan) and Gemini position eval; DRY, consistent edge check. |
| **_compute_p_lose helper**         | Blends model P(lose) with market odds; time-weighted (half-life decay) so market gets more weight near resolution. |
| **Calibration loss weighting in storage** | `get_calibration_stats()` accepts `config`; reads `calibration_loss_weight`; applies to bucket and by_direction_side win rates. |
| **Strategy plugin migration**          | Signal feature building/scoring and recommendation routing are delegated to `strategies/` implementations; `compare_historical.py` handles data/execution plumbing. |
| **Ruff + Prettier**                 | Linting (Ruff) and formatting (Prettier for YAML/JSON/MD); pre-commit hooks on push. |
| **pyproject.toml**                  | Ruff config, project metadata; single place for tooling. |

### Future / TODO

| Improvement                         | Effort | Notes |
| ----------------------------------- | ------ | ----- |
| **Position eval extraction**       | Low    | Extract Kalshi/Gemini position eval into `_evaluate_position()` with platform-agnostic core. |
| **Type hints consistency**          | Low    | Add return types and ParamSpec where missing; improve IDE support. |
| **Stricter runtime schema validation** | Low | Current validator runs on pre-push; optional next step is hard fail at process startup for invalid config. |
| **Unit tests for edge cases**      | Medium | Test `_recommend()`, `_fee_per_contract()`, `_apply_bucket_penalty()` with fixture data. |
| **Docstrings for public APIs**     | Low    | Ensure storage, kalshi_client, gemini_client have clear module docstrings. |

---

## Python AI/ML Ecosystem (NumPy, SciPy, scikit-learn, etc.)

Current status: partially implemented.

| Tool / Library   | Potential use                                                                 | Effort | Impact |
| ----------------- | ------------------------------------------------------------------------------ | ------ | ------ |
| **NumPy**         | Vectorized `closes` arrays for empirical P and EWMA variance | Low    | Medium — speed, cleaner numerical code |
| **SciPy**         | `scipy.stats.norm.cdf` in volatility probability model | Low    | Medium — robust probability CDF |
| **pandas**        | Optional DataFrame path in backtest (`real` mode) for cumulative and return series | Medium | Medium — cleaner data handling |
| **scikit-learn**  | Probability calibration from resolved predictions (isotonic / Platt) | Medium | High — better calibrated probabilities |
| **statsmodels**   | GARCH(1,1), ARIMA for time-varying vol; structural break detection | Medium | High — Taylor Ch 9–10 improvements |
| **PyTorch / JAX** | Gradient-based calibration, custom loss for P(YES); research-grade ML models | Research | Research — if we move to learned probability models |
| **optuna / hyperopt** | Hyperparameter tuning for `min_edge`, `limit_dip_pct`, `vol_blend_weight` on backtest data | Medium | High — data-driven strategy param optimization |

**Concrete next steps (remaining):**

1. **Model training pipeline**: persist signal-feature rows with outcomes for offline model fitting/validation.
2. ~~**Feature optimization**: learn `signal_model_weights` from data (instead of hand-tuned defaults).~~ ✓ **Implemented** (`enable_auto_signal_weight_fit`; logistic fit from resolved predictions)
3. ~~**Extended pandas usage**: move synthetic/backtest transformations to DataFrame-first flow.~~ ✓ **Implemented** (`scripts/backtest.py` synthetic + real mode DataFrame-first paths)
4. **Statsmodels/GARCH integration**: add optional GARCH(1,1) volatility source.

---

## Future: External APIs & UI (Ollama suggestions)

- **CoinGecko / Binance API**: Integrate real-time prices as alternative to yfinance. Requires API keys, rate limits.
- **User-friendly UI**: Web dashboard for portfolio, trades, custom notifications. (Streamlit, Flask, or TUI.)

---

## High Priority (Feasible Now)

### 1. ~~Backtesting (Freqtrade, Gekko)~~ ✓ Partially Done

Run our evaluation logic on _historical_ market snapshots instead of live data. Simulate what recommendations we would have made in the past and measure PnL.

- **Scope**: Use stored historical prices (yfinance) + historical Kalshi/Gemini snapshots (if available) or synthetic fills at mid
- **Output**: Sharpe, win rate, max drawdown, edge decay over time
- **Implemented**: `scripts/backtest.py` — modes: `real` (resolved predictions from SQLite) and `synthetic` (market_snapshots + resolved_outcomes). Uses SQLite storage (~/.trading-bot-data/), yfinance `get_aggs_as_of()` for point-in-time historical closes. Settlement sync populates `resolved_outcomes` for synthetic backtest. Includes entry slippage, spread-aware sizing, winner-side slippage tax (`synthetic_win_slippage_tax_bps`), dwell-time fill checks (`synthetic_fill_min_dwell_minutes`), and partial-fill scaling when depth is available.
- **TODO**: Edge decay over time; persist richer historical depth in snapshots/orderbook so partial-fill realism can use true venue depth more often.

### 2. ~~Kelly Criterion Position Sizing~~ ✓ Done

~~Instead of fixed `count=10`, size positions by edge and bankroll.~~
Config: `strategy.use_kelly`, `kelly_fraction` (quarter-Kelly). `_kelly_count()` in compare_historical.py. Reduces position size for farther-OTM contracts (lower P(win)). Enabled by default.

### 3. ~~Whitelist / Blacklist~~ ✓ Done

~~Filter markets by ticker patterns or exclude known low-liquidity / noisy ones.~~
Config: `strategy.whitelist`, `strategy.blacklist` in `config.yaml`.

### 4. ~~File Logging for Run History~~ ✓ Done

~~Log each run (timestamp, recommendations, edge, market ticks) to `logs/eval_YYYYMMDD.log`~~
Use `--log` to append output to `~/Desktop/trading-bot-logs/eval_YYYYMMDDHH.log` (per-hour bucket; runs within same hour append), or `--log /path/to/dir` for a custom directory.

### 5. Learn from position & limit order evaluations (FreqAI-style)

**Implemented**: Orphan Kalshi positions and all Gemini position evals persist via `record_market_snapshot`. **Limit order tracking** (`limit_orders` table): we record every limit order placed (platform, ticker, order_id, limit_price, bid_at_place, dip_pct_used, hist_p, etc.) and record cancel reasons (`no_longer_rec`, `want_better_price`). Use `--limit-analysis` to see unfilled orders, min/max price during order lifetime, and "would have filled" stats — informs `limit_dip_pct` tuning.
- **Kept position outcomes**: When we decide KEEP (don't sell), track that position and when it eventually resolves (win/lose). Currently we only record outcomes for placed buys (`record_prediction` → `record_prediction_outcome`). Add `position_evaluations` or link kept positions to predictions so we can measure: "we kept YES at hist_p=0.75, it won."
- **Cancel reason classification**: Distinguish "no longer recommended" (rec=SKIP) vs "want better limit" (rec still BUY) for finer calibration.

### 6. ~~Modular Strategy Interface (Freqtrade)~~ ✓ Implemented

`compare_historical.py` now routes recommendation and signal feature/scoring paths through `strategies/` implementations (`strategy_engine`), while execution/data plumbing remains centralized.

---

## Medium Priority (More Effort)

### 7. ~~GARCH / EWMA for Time-Varying Volatility~~ ✓ EWMA Done (Stock-Prediction-Models)

~~Current volatility model uses a fixed historical vol.~~ **EWMA implemented**: `strategy.use_ewma_vol: true`, `ewma_lambda: 0.94`; σ²*t = λ·σ²*{t-1} + (1−λ)·r²_t. GARCH(1,1) remains a future upgrade for full regime adaptation.

### 8. Monte Carlo Paths (Stock-Prediction-Models)

Instead of single log-normal P(S>K), run N simulated price paths and compute empirical P. Useful for non-log-normal behavior.

### 9. Implied Volatility from Options (if available)

If we can get options data (e.g. Deribit for BTC), use implied vol instead of historical for richer probability estimates.

### 10. Telegram / WebUI Control (Freqtrade, Hummingbot)

Remote control: start/stop, status, force-exit. Hummingbot’s Condor shows a working pattern.

### 11. Persistence (SQLite) (Freqtrade)

Store recommendations, fills, PnL in a local DB for performance reports and backtest comparison.

---

## Research Insights (Consolidated)

### Taylor (2005): volatility beats direction

Reference: [Lancaster University PDF](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf)

- Directional forecasts are hard; avoid overconfidence in BUY YES/NO calls.
- Volatility forecasting is more reliable; prioritize better volatility modeling.
- Stylized facts matter: non-normal returns and volatility clustering imply heavier tails than pure log-normal assumptions.

| Improvement                                 | Source chapter | Effort | Impact |
| ------------------------------------------- | -------------- | ------ | ------ |
| GARCH(1,1) / stronger EWMA regime modeling  | Ch 9-10        | Medium | High   |
| Implied vol inputs (VIX / options)          | Ch 14-15       | Medium | High   |
| Intraday / realized volatility              | Ch 12          | High   | High   |
| Monte Carlo tail-aware probability          | Ch 16          | Medium | Medium |
| Heavy-tail distribution alternatives         | Ch 4           | Medium | Medium |

### Campbell, Lo, MacKinlay (1997): event-study framing

Reference: [NRC PDF](https://www.nrc.gov/docs/ml1208/ML12088A329.pdf)

- Treat each contract resolution as an event.
- Market-model residuals can reduce noise versus constant-mean assumptions.
- Keep estimation windows separate from event windows.

| Improvement                  | Source | Effort | Impact |
| ---------------------------- | ------ | ------ | ------ |
| Market model for S&P context | Ch 4.3 | Medium | Medium |
| Nonparametric robustness     | Ch 4.7 | Low    | Low    |

### Tsay: financial time-series modeling

Reference: [NUS PDF](https://cpb-us-w2.wpmucdn.com/blog.nus.edu.sg/dist/0/6796/files/2017/03/analysis-of-financial-time-series-copy-2ffgm3v.pdf)

- Reinforces ARCH/GARCH priority and structural-break awareness.
- ARIMA is lower priority for binary resolution probability compared with volatility dynamics.

---

## Longer Term / Research

- **FreqAI-style ML**: Train on historical recommendations + outcomes to tune min_edge / blending.
- **Sentiment consensus**: Add news/social signal when a reliable data source is introduced.
- **~~Cross-platform arbitrage~~** ✓ Done: Inter-exchange arb (Kalshi↔Gemini) via `enable_inter_arb`.
- **~~Parse exact resolution datetime~~** ✓ Done: Per-contract time-to-expiry parsed from title and used in vol model.

---

## Reference Repos Summary

| Repo                        | Key Takeaways                                                                         |
| --------------------------- | ------------------------------------------------------------------------------------- |
| **freqtrade**               | Dry-run, backtest, hyperopt, SQLite, WebUI, Telegram, strategy interface              |
| **ccxt**                    | Unified API, normalized data; we use custom Kalshi/Gemini clients (different product) |
| **hummingbot**              | Modular connectors (CEX/DEX), Gateway middleware                                      |
| **gekko**                   | TA + backtesting; archived but concepts still valid                                   |
| **Stock-Prediction-Models** | LSTM/GRU, RL agents, Monte Carlo, sentiment, portfolio optimization                   |
| **polymarket-agent**        | Multi-model consensus, web dashboard, structured AI prompts, Kelly sizing; see below  |

---
## Polymarket-Agent Inspired ([manuelbarbas/polymarket-agent](https://github.com/manuelbarbas/polymarket-agent))

From the [BlockRunAI polymarket-agent](https://github.com/manuelbarbas/polymarket-agent)—autonomous prediction-market trading with multi-model consensus and x402 micropayments. Ideas we can adapt for Kalshi/Gemini:

| Improvement | Description | Effort | Notes |
| ----------- | ----------- | ------ | ----- |
| **Multi-model AI consensus** | Use 2–3 LLMs (GPT-4o, Claude, Gemini) to vote on each prediction; only trade when 2+ agree | Medium | Reduces single-model bias; we use Ollama only—could add optional API models |
| **Structured AI output** | Prompts request `PROBABILITY:`, `CONFIDENCE:`, `REASONING:`; parse programmatically | Low | Our Ollama output is freeform; structured format improves reliability |
| **Question sanitization** | Sanitize market titles before LLM calls (e.g. "invades" → "territorial change") | Low | Avoid content-filter rejections on sensitive markets |
| **Min AI confidence threshold** | Require AI confidence ≥ 50% before trading; `MIN_CONFIDENCE=5` on 1–10 scale | Low | Add `strategy.min_ai_confidence`; skip low-confidence picks |
| **Absolute max bet cap** | `MAX_BET_SIZE` per trade in addition to `max_pct_of_balance` | Low | Extra safety; add `trading.max_contracts_per_order` |
| **Web dashboard** | Flask/Streamlit UI for portfolio, trades, run status | Medium | polymarket-agent has dark-themed dashboard; we have CLI only |
| **Tiered model usage** | Fast model for screening, deeper model for high-edge candidates | Medium | gpt-4o-mini vs Claude Sonnet; we could use llama3.2:1b for screening |
| **Whale / smart-money signals** | Expert forecaster or top-trader flow data if platform exposes it | Research | polymarket-agent has whale_data; Kalshi/Gemini may not—check APIs |
| **Geographic deployment docs** | Document which regions allow trading (Polymarket blocks US/EU) | Low | Document Kalshi/Gemini restrictions in README |

### Not directly applicable

- **x402 / wallet-based AI payments**: polymarket-agent pays for AI via USDC micropayments (BlockRun). We use API keys; different architecture.
- **Polymarket CLOB / proxy wallet**: Platform-specific; we use Kalshi and Gemini.
