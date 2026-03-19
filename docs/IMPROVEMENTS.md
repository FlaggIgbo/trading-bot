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
| **Volatility hardening stack**         | Execution + risk                        | ✓ Added `vol_model` (`auto|ewma|garch|sample`) with GARCH fallback, continuous vol-ratio scaling (edge/dip/size), dynamic stability-window scaling, and defensive sells-only mode under extreme volatility. |
| **Cross-exchange sanity gate**         | Execution safety                        | ✓ Added Kalshi↔Gemini repricing divergence checks (`cross_exchange_*`) that block stale-side buys during sharp spot moves. |
| **Inter-arb group preflight**          | Execution safety                        | ✓ Added paired `REC_ARB_INTER` preflight to drop incomplete/underfunded cross-exchange leg groups and resize surviving pairs to matched affordable counts before BUY execution. |
| **Inter-arb balance-aware routing**    | Capital efficiency                      | ✓ Inter-arb route selection now uses per-platform balance-aware scoring to favor profitable pairings that keep the cheaper leg on the lower-balance account when feasible. |
| **Inter-arb single-leg fallback**      | Capital utilization                     | ✓ Optional preflight degrade mode keeps one funded highest-likelihood inter-arb leg when paired funding is unavailable. |
| **Active order lifecycle management**  | Execution realism                       | ✓ Resting orders are reviewed each cycle with stale TTL cancellation (`limit_order_ttl_minutes`), no-longer-recommended cleanup, and regime-shift cleanup (`cancel_all_on_regime_shift`). Near-expiry protection (`near_expiry_keep_dte: 0.02`) preserves resting BUY orders within ~30 min of resolution instead of cancelling for `want_better_price`. |
| **Per-ticker entry cap**               | Risk management                         | ✓ `max_entries_per_ticker: 3` prevents the bot from re-entering the same contract every cycle. `get_ticker_entry_counts()` in storage loads non-cancelled entry counts per ticker; orders for tickers at the cap are blocked before execution. Fixes 54-entry ticker scenarios with 0 wins. |
| **Execution engine extraction**        | Code quality / testability              | ✓ Moved order sync/cancel path into `execution_engine.py` so lifecycle safety logic can be tested and evolved independently from model/probability math. |
| **Accurate realized PnL lifecycle**    | Reliability / reporting accuracy        | ✓ Canceled buy intents are marked `cancelled`, early sells are realized as `sold` (FIFO lots), and settlement/exit PnL applies fee-aware accounting instead of gross-only math. |
| **Confirmed-fill reconciliation pass** | Reliability / reporting accuracy        | ✓ Added dedicated Kalshi + Gemini fill reconciliation so `predictions` are created from confirmed fills (idempotent by fill ID), not from order-accepted responses. |
| **Account-level capital efficiency**   | Portfolio optimization                  | ✓ Budget-constrained order selection now ranks by expected net return per dollar-at-risk per hour-to-resolution (edge remains a tie-breaker). |
| **Adaptive Kelly circuit breaker**     | Risk management                         | ✓ Kelly fraction now auto-scales down during calibration stress and recovers when win-rate stabilizes (`dynamic_kelly_*` settings). |
| **Macro cross-asset signal feature**   | Feature engineering                     | ✓ Signal model now includes `macro_24h_ret` from configurable tickers (current config `SPY`, `UUP`) to capture risk-on/off context with more reliable intraday feeds. |
| **Config validator gate (pre-push)**   | Reliability                             | ✓ `scripts/validate_config.py` runs on pre-push to catch invalid/conflicting risk settings before backtest gate and remote push. |
| **Modular strategy migration**         | Freqtrade architecture                  | ✓ Recommendation + signal feature/scoring paths are now strategy-driven (`strategy_engine`) rather than hardcoded in `compare_historical.py`. |
| **Push-time performance guard**        | Reliability                             | ✓ Pre-push hook runs configurable backtest gate (`scripts/backtest_guard.py`) and blocks pushes on weak performance thresholds. |
| **Daily performance report**           | Reliability                             | ✓ Launchd daily report writes dated `.txt` performance summaries (`scripts/daily_performance_report.py`). |
| **Emergency loss circuit breaker**     | Reliability                             | ✓ `max_daily_loss_pct` / `max_daily_loss_cents` block new BUY orders when realized 24h losses breach limits (SELL handling remains active). |
| **Depth persistence for snapshots**    | Backtest realism                        | ✓ `market_snapshots` now stores top-of-book asks/sizes (`yes_ask`, `no_ask`, `*_size`) so synthetic partial-fill logic can use real historical depth when available. |
| **Daily performance graphs**           | Reliability                             | ✓ `scripts/daily_performance_report.py` now generates PNG performance charts with per-trade P&L bars (green=wins, red=losses) and cumulative P&L overlay line, saved alongside the text report. |
| **DTE-confidence limit dip scaling**   | —                                       | ✓ New `_dte_confidence_dip()` in compare_historical.py. When resolution is more than ~1 hour away (`far_dte_threshold_days: 0.042`) and model confidence high (P(win)>=80%), limit order dips scale proportionally with DTE to catch uncertainty-driven price drops. Config: `far_dte_*` params. When P(win)>=90%, extra DTE dip gets a 1.5x confidence boost. Threshold is low because crypto markets resolve hourly. |
| **Anti-hallucination pipeline**        | Distributional mismatch fix              | ✓ `models/anti_hallucination.py`: five composable layers between model P(YES) and recommendation engine. **(1) Bayesian update**: treats market price as Beta prior, model as evidence (`market_strength: 1.5`); model=86%/market=8% → posterior ~40%. **(2) EWMA probability**: exponentially weights recent closes (14-day span); blends with model at configurable weight (`ewma_prob_blend_weight: 0.30`). **(3) Vol Z-score gate**: when short-window vol Z-score > 2.0, pulls probability toward 0.50 (`tail_mult: 0.3`). **(4) Adaptive edge**: 1.8× base edge for cheap contracts (<15¢), scales with strike distance (`strike_dist_mult: 1.2`), 1.5× when trend opposes trade direction. **(5) Regime filter (EMA gate)**: blocks BUY YES on "above" contracts when price < 20-EMA (and vice versa); BUY NO is not blocked (trend-aligned). Applied to Kalshi, Gemini, and position re-evaluation paths. Config: `strategy.anti_hallucination.*`. |
| **CI sample-size fix + wider empirical window** | —                            | ✓ `_empirical_ci_halfwidth` now uses the rolling-window sample size (`empirical_recent_days`) instead of the full `days_back` history length, so the uncertainty penalty honestly reflects the data the empirical model actually used. `empirical_recent_days` increased from 30 → 90 for more stable empirical P estimates while still adapting to regime shifts. |
| **Position evaluation tracking (FreqAI-style)** | FreqAI-style ML              | ✓ New `position_evaluations` table records KEEP decisions with full model context (hist_p, vol_p, p_lose, rec, entry/current price). Outcomes linked on settlement via `resolve_position_evaluations()`. `--position-eval-analysis` prints KEEP vs SELL win rates and PnL. Cancel reasons now fully classified: `no_longer_rec`, `want_better_price`, `stale_ttl`, `regime_shift_guard`, `expired_unfilled`. |
| **Monte Carlo probability paths**               | Stock-Prediction-Models       | ✓ `_monte_carlo_probability()` runs N GBM paths (daily steps, Student-t innovations when configured) blended with analytical CDF via `_volatility_probability_with_mc()`. Config: `mc_enabled`, `mc_paths` (10000), `mc_blend_weight` (0.3). Better tail-risk estimation for non-log-normal behavior. |
| **Bet-vs-resolution accuracy tracking**          | —                             | ✓ `resolution_price` persisted in `predictions` at settlement time; `get_bet_accuracy_stats()` in storage computes distance metrics (how far the underlying asset price was from the bet threshold). Daily report includes accuracy section: mean/median absolute distance, signed distance, correct-side %, avg distance when correct vs wrong. Designed as a feedback signal for model improvement. |
| **DTE-aware short-dated contract guards**        | —                             | ✓ Three compounding fixes for sub-hour contracts: **(1)** Volatility time floor reduced from `0.01` years (~3.65 days) to `1e-6` years (~30s) in `_volatility_probability` and `_volatility_probability_with_mc`, so analytical CDF and Monte Carlo correctly compute near-zero probability for large moves in short windows. **(2)** `max_buy_yes_strike_above_pct` and `max_buy_no_strike_above_pct` safety guards now scale with `sqrt(DTE)` via `_dte_adjusted_strike_pct()`—a 45-minute contract gets ~18% of the base threshold vs 100% for a 1-day contract. **(3)** Empirical/vol blend weight decays linearly toward 0 when DTE < 1 day via `_dte_adjusted_empirical_weight()`, so short-dated contracts rely almost entirely on the volatility model (which conditions on current price and remaining time). Applied to both Kalshi and Gemini buy paths AND all 3 sell/keep position evaluation paths. |
| **Consistent DTE guards across all paths**       | —                             | ✓ Comprehensive audit fixed 5 gaps: **(1)** `_dte_adjusted_empirical_weight` now applied to all 3 position re-evaluation paths (Kalshi orphan, Kalshi cached, Gemini) — previously sell/keep decisions used stale empirical-heavy blend even on sub-hour contracts. **(2)** Re-entry orders (`_maybe_add_reentry_order`) now respect DTE-adjusted strike guards — previously bypassed all safety checks. **(3)** Vol model confidence override (`min_vol_confidence_far_otm`) blocked when DTE < 0.1 days (~2.4h) — prevents high vol_p from bypassing tightened strike limits near expiry. **(4)** `favor_yes_when_above_strike` now subtracts maker fee in edge check — prevents false-positive override when after-fee edge is negative. **(5)** Gemini settlement sync added with per-contract `resolutionSide` matching — resolved Gemini positions now correctly recorded as wins/losses. |
| **Alpha engine (OBI, markout, vol-Kelly, lead-lag)** | Quant trading best practices | ✓ `models/alpha_engine.py`: four composable modules. **(1) Order Book Imbalance (OBI)**: `(V_bid − V_ask) / (V_bid + V_ask)` directional signal adjusts required edge ±4% when depth strongly favors/opposes trade direction. **(2) Markout PnL / Toxic flow detection**: tracks price 1/5/15 min after fills; tickers with avg markout < −50 bps are flagged toxic (+3% edge penalty) to avoid adverse selection. New `markout_snapshots` table in storage. **(3) Volatility-adjusted Kelly**: `f = base × (baseline_vol / current_vol)`; auto-shrinks position size in high-vol regimes (floor 25% of base). **(4) Lead-lag signal**: precomputes short-term momentum of leader assets (e.g. `^GSPC`→`KXINXU`) and adjusts edge ±2% for follower contracts. Applied to Kalshi and Gemini buy paths. Config: `strategy.alpha_engine.*`. |
| **Log display fixes (strike, direction, DTE)**   | —                             | ✓ Four fixes for confusing evaluation/preview logs: **(1)** `_fmt_price()` helper formats strikes and distances with appropriate decimals based on magnitude ($81,250 for large, $1.24 for small, $0.0034 for tiny) — previously `,.0f` rounded XRP $1.24 to "$1" and showed "$0 above strike". **(2)** Strike label changed from `(above)` to `(pays if above)` to remove ambiguity — users read `$1 (above)` as "strike is above current price" rather than "contract pays if price is above strike". **(3)** "Bet:" line in proposed trades and preview now shows the effective bet direction (accounts for YES/NO side) — previously always showed contract direction, so BUY NO on "above $X" misleadingly said "Bet: price above $X". **(4)** `dte` field added to re-entry (`_maybe_add_reentry_order`) and two-sided quote (`_maybe_add_two_sided_quotes`) order dicts — previously missing, causing "Resolves in: unknown". |

---

## Repo & Code Improvements

Refactoring, architecture, and code quality changes.

### Implemented ✓

| Improvement                         | Description |
| ----------------------------------- | ----------- |
| **_StrategyParams consolidation**   | Strategy config loaded once into `_StrategyParams` dataclass; avoids repeated dict lookups; single source of truth for min_edge, fees, thresholds. |
| **_maybe_add_reentry_order helper** | Shared helper for sell-and-rebuy logic; used by Kalshi (cached + orphan) and Gemini position eval; DRY, consistent edge check. Now includes `current_price` and `dte_days` params for DTE-aware strike guard enforcement. |
| **_compute_p_lose helper**         | Blends model P(lose) with market odds; time-weighted (half-life decay) so market gets more weight near resolution. |
| **Calibration loss weighting in storage** | `get_calibration_stats()` accepts `config`; reads `calibration_loss_weight`; applies to bucket and by_direction_side win rates. |
| **Strategy plugin migration**          | Signal feature building/scoring and recommendation routing are delegated to `strategies/` implementations; `compare_historical.py` handles data/execution plumbing. |
| **CI sample-size honesty**          | `_empirical_ci_halfwidth` now uses the effective rolling-window sample size (`empirical_recent_days`) instead of the full `days_back` history length, preventing artificially tight confidence intervals when rolling window is enabled. |
| **Position eval persistence**       | New `position_evaluations` table + `record_position_evaluation()` / `resolve_position_evaluations()` / `get_position_evaluation_stats()` in storage; KEEP decisions tracked at all position eval paths (Kalshi cached/uncached + Gemini). |
| **Cancel reason classification**    | `record_order_cancel` docstring and schema updated to document all 5 cancel reasons: `no_longer_rec`, `want_better_price`, `stale_ttl`, `regime_shift_guard`, `expired_unfilled`. |
| **`_fmt_price` smart formatter**    | Formats price/threshold values with appropriate decimal places based on magnitude ($81,250 for BTC, $1.24 for XRP, $0.0034 for sub-penny). Applied to strike display, distance display, reason strings, and closes range. |
| **Monte Carlo GBM simulation**      | `_monte_carlo_probability()` + `_volatility_probability_with_mc()` wrapper; multi-step daily GBM with numpy vectorization (pure-Python fallback); Student-t innovations; blended with analytical CDF at all 5 vol_p call sites. |
| **Expired unfilled order tracking** | `expire_unfilled_orders()` marks still-placed limit orders as `expired_unfilled` when market resolves (platform auto-cancel). Called from settlement sync. |
| **Bet-vs-resolution accuracy tracking** | `resolution_price` stored in predictions at settlement; `get_bet_accuracy_stats()` computes distance between threshold and actual asset price at resolution. Daily report includes accuracy metrics (mean/median distance, correct-side %, avg distance when right/wrong). Data feeds back into model improvement. |
| **Ruff + Prettier**                 | Linting (Ruff) and formatting (Prettier for YAML/JSON/MD); pre-commit hooks on push. |
| **pyproject.toml**                  | Ruff config, project metadata; single place for tooling. |
| **ML pipeline repo restructure**    | Reorganized into `signals/` (data clients), `models/strategies/` (ML strategies), `execution/` (order lifecycle), `data/` (persistence). Root-level backward-compat shims preserve existing import paths. Pipeline flow: config.yaml signals → feature engineering → model scoring → trade execution → data persistence. |

### Future / TODO

| Improvement                         | Effort | Notes |
| ----------------------------------- | ------ | ----- |
| **Position eval extraction**       | Low    | Extract Kalshi/Gemini position eval into `_evaluate_position()` with platform-agnostic core. |
| **Type hints consistency**          | Low    | Add return types and ParamSpec where missing; improve IDE support. |
| **Stricter runtime schema validation** | Low | Current validator runs on pre-push; optional next step is hard fail at process startup for invalid config. |
| **Unit tests for edge cases**      | Medium | Test `_recommend()`, `_fee_per_contract()`, `_apply_bucket_penalty()` with fixture data. |
| **Docstrings for public APIs**     | Low    | Ensure storage, kalshi_client, gemini_client have clear module docstrings. |
| **Inter-arb strict pair mode**     | Medium | Execute `REC_ARB_INTER` as grouped pair transactions instead of leg-by-leg loop iteration. |
| **Inter-arb partial-fill unwind**  | Medium | If one inter-arb leg fills and the second fails, auto-unwind filled leg within slippage cap. |
| **Inter-arb budget reservation**   | Medium | Reserve per-platform budget for paired arb groups before directional allocation to reduce leg starvation. |
| **Inter-arb latency timeout gate** | Low    | Require second leg within short TTL or abort/unwind to limit stale pricing risk. |

---

## Python AI/ML Ecosystem (NumPy, SciPy, scikit-learn, etc.)

Current status: partially implemented.

| Tool / Library   | Potential use                                                                 | Effort | Impact |
| ----------------- | ------------------------------------------------------------------------------ | ------ | ------ |
| **NumPy**         | Vectorized `closes` arrays for empirical P and EWMA variance | Low    | Medium — speed, cleaner numerical code |
| **SciPy**         | `scipy.stats.norm.cdf` in volatility probability model | Low    | Medium — robust probability CDF |
| **pandas**        | Optional DataFrame path in backtest (`real` mode) for cumulative and return series | Medium | Medium — cleaner data handling |
| **scikit-learn**  | Probability calibration from resolved predictions (isotonic / Platt) | Medium | High — better calibrated probabilities |
| **statsmodels**   | ARIMA / structural-break extensions beyond current EWMA/GARCH path | Medium | Medium — useful after current volatility stack stabilizes |
| **PyTorch / JAX** | Gradient-based calibration, custom loss for P(YES); research-grade ML models | Research | Research — if we move to learned probability models |
| **optuna / hyperopt** | Hyperparameter tuning for `min_edge`, `limit_dip_pct`, `vol_blend_weight` on backtest data | Medium | High — data-driven strategy param optimization |

**Concrete next steps (remaining):**

1. **Model training pipeline**: persist signal-feature rows with outcomes for offline model fitting/validation.
2. ~~**Feature optimization**: learn `signal_model_weights` from data (instead of hand-tuned defaults).~~ ✓ **Implemented** (`enable_auto_signal_weight_fit`; logistic fit from resolved predictions)
3. ~~**Extended pandas usage**: move synthetic/backtest transformations to DataFrame-first flow.~~ ✓ **Implemented** (`scripts/backtest.py` synthetic + real mode DataFrame-first paths)
4. ~~Statsmodels/GARCH integration~~ ✓ **Implemented (runtime optional)**: `vol_model: auto` now uses GARCH(1,1) when available and falls back to EWMA/sample safely.

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

### 5. ~~Learn from position & limit order evaluations (FreqAI-style)~~ ✓ Implemented

**Implemented**: Orphan Kalshi positions and all Gemini position evals persist via `record_market_snapshot`. **Limit order tracking** (`limit_orders` table): we record every limit order placed (platform, ticker, order_id, limit_price, bid_at_place, dip_pct_used, hist_p, etc.) and record cancel reasons. Use `--limit-analysis` to see unfilled orders, min/max price during order lifetime, and "would have filled" stats — informs `limit_dip_pct` tuning.
- ~~**Kept position outcomes**~~: ✓ New `position_evaluations` table tracks every KEEP decision with model context (hist_p, vol_p, p_lose, rec, entry_price, current_price). When the market resolves, outcomes are linked automatically via `resolve_position_evaluations()` in settlement sync. Use `--position-eval-analysis` to see KEEP vs SELL win rates, PnL, and win rate by hist_p bucket. Rate-limited to one row per (platform, ticker, side) per hour.
- ~~**Cancel reason classification**~~: ✓ Four distinct cancel reasons are tracked: `no_longer_rec` (model stopped recommending), `want_better_price` (model wants to reprice), `stale_ttl` (order exceeded TTL), `regime_shift_guard` (cancelled during volatility/momentum regime shift). Stored in `limit_orders.cancel_reason`.

### 6. ~~Modular Strategy Interface (Freqtrade)~~ ✓ Implemented

`compare_historical.py` now routes recommendation and signal feature/scoring paths through `strategies/` implementations (`strategy_engine`), while execution/data plumbing remains centralized.

---

## Medium Priority (More Effort)

### 7. ~~GARCH / EWMA for Time-Varying Volatility~~ ✓ EWMA + Optional GARCH Done (Stock-Prediction-Models)

~~Current volatility model uses a fixed historical vol.~~ **Implemented**: EWMA (`strategy.use_ewma_vol`, `ewma_lambda`) and optional GARCH(1,1) path (`vol_model: auto|garch`, `garch_min_returns`) with safe fallback to EWMA/sample when GARCH dependencies or fit conditions are unavailable.

### ~~8. Monte Carlo Paths (Stock-Prediction-Models)~~ ✓ Implemented

`_monte_carlo_probability()` runs N GBM paths (daily steps, Student-t innovations) blended with analytical CDF via `_volatility_probability_with_mc()`. Config: `mc_enabled`, `mc_paths` (10000), `mc_blend_weight` (0.3).

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
| ~~Monte Carlo tail-aware probability~~      | Ch 16          | ✓ Done | ✓ `_monte_carlo_probability()` |
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
