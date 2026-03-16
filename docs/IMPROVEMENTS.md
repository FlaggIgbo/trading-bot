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
| Kelly criterion position sizing         | Freqtrade, Stock-Prediction-Models      | ✓ `strategy.use_kelly`, `kelly_fraction`                                                                                                                                                                                                                 |
| Whitelist / blacklist                   | Freqtrade                               | ✓ `strategy.whitelist`, `strategy.blacklist`                                                                                                                                                                                                             |
| File logging for run history            | Freqtrade                               | ✓ `--log` or `--log DIR`                                                                                                                                                                                                                                 |
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
| **Vol blend (empirical + volatility)**  | Taylor, IMPROVEMENTS §6                 | ✓ `strategy.vol_blend_weight` (0.5 = equal); `w*emp + (1-w)*vol` when both available; replaces `max(emp, vol)` for non-intraday                                                                                                                          |
| **EWMA volatility**                     | Taylor Ch 9                             | ✓ `strategy.use_ewma_vol`, `ewma_lambda` (0.94); σ²*t = λ·σ²*{t-1} + (1−λ)·r²_t; time-varying vol in `_volatility_probability`                                                                                                                           |
| **High-confidence limit orders**        | —                                       | ✓ `strategy.limit_dip_pct_high_confidence` (e.g. 15%); when P(win) ≥ 90%, place limit 15% below bid to prioritize catching bigger dips on high-certainty contracts                                                                                       |
| **Limit dip when price might decrease** | —                                       | ✓ `strategy.limit_dip_pct_price_decline` (e.g. 10%); when vol model is on or 2+ days to expiry, use larger dip to prioritize limit orders—contract price may dip before settlement; stacks with high-confidence                                          |
| **Local historical storage**            | Freqtrade, backtesting                  | ✓ SQLite under `~/.trading-bot-data/`; yfinance closes cache (1h TTL), market snapshots (yes/no bids, hist_p, vol_p, rec per eval), resolved_outcomes schema for calibration; enables backtesting & prediction calibration; not committed to git         |
| **Kalshi & Gemini fee accounting**      | Kalshi/Gemini docs                      | ✓ Maker fee = rate × P × (1−P) per contract; `kalshi_maker_fee_rate`, `gemini_maker_fee_rate` (0.0175); min_edge is _net_ edge after fees (e.g. 0.01 = 1¢ profit per contract)                                                                           |
| **BUY YES/NO preference**               | —                                       | ✓ When both YES and NO have edge, `_recommend()` prefers whichever yields the larger net return                                                                                                                                                        |
| **Contradictory-position avoidance**    | —                                       | ✓ `_filter_contradictory_buys()` skips BUYs that would conflict with kept positions OR other pending BUYs on same underlying + resolution (Kalshi + Gemini). E.g. NO on SOL<$83 and YES on SOL≥$92 are mutually exclusive; keeps higher-edge when both sides compete. Arb (YES+NO same contract) allowed.                                                    |
| **Gemini crypto events**               | —                                       | ✓ `get_crypto_events()` fetches `crypto` category (daily/weekly); `hourly` category may return 400—we catch and skip, still get daily markets (e.g. BTC2603162100).                                                                                                                                   |
| **Gemini position sell (losers)**      | Kalshi sell logic                       | ✓ Evaluates Gemini positions (like Kalshi) and sells contracts we judge as losers: REC_LIKELY_LOSE, rec flip (BUY YES→now BUY NO), or REC_RULE_VIOLATION. Uses `get_positions()`, `gemini_symbol_to_market` map, same hist_p/vol_p logic; SELL via `place_order_safe`. |

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
- **Implemented**: `scripts/backtest.py` — modes: `real` (resolved predictions from SQLite) and `synthetic` (market_snapshots + resolved_outcomes). Uses SQLite storage (~/.trading-bot-data/), yfinance `get_aggs_as_of()` for point-in-time historical closes. Settlement sync populates `resolved_outcomes` for synthetic backtest. Kalshi `get_historical_market()` added for future use.
- **TODO**: Edge decay over time; Gemini historical; Kalshi historical orderbook (Dome API)

### 2. ~~Kelly Criterion Position Sizing~~ ✓ Done

~~Instead of fixed `count=10`, size positions by edge and bankroll.~~
Config: `strategy.use_kelly`, `kelly_fraction` (quarter-Kelly). `_kelly_count()` in compare_historical.py.

### 3. ~~Whitelist / Blacklist~~ ✓ Done

~~Filter markets by ticker patterns or exclude known low-liquidity / noisy ones.~~
Config: `strategy.whitelist`, `strategy.blacklist` in `config.yaml`.

### 4. ~~File Logging for Run History~~ ✓ Done

~~Log each run (timestamp, recommendations, edge, market ticks) to `logs/eval_YYYYMMDD.log`~~
Use `--log` to append output to `~/Desktop/trading-bot-logs/eval_YYYYMMDD.log`, or `--log /path/to/dir` for a custom directory.

### 5. Modular Strategy Interface (Freqtrade)

Extract strategy logic into pluggable classes so we can swap empirical vs volatility vs custom logic without editing `compare_historical.py`.

---

## Medium Priority (More Effort)

### 6. ~~GARCH / EWMA for Time-Varying Volatility~~ ✓ EWMA Done (Stock-Prediction-Models)

~~Current volatility model uses a fixed historical vol.~~ **EWMA implemented**: `strategy.use_ewma_vol: true`, `ewma_lambda: 0.94`; σ²*t = λ·σ²*{t-1} + (1−λ)·r²_t. GARCH(1,1) remains a future upgrade for full regime adaptation.

### 7. Monte Carlo Paths (Stock-Prediction-Models)

Instead of single log-normal P(S>K), run N simulated price paths and compute empirical P. Useful for non-log-normal behavior.

### 8. Implied Volatility from Options (if available)

If we can get options data (e.g. Deribit for BTC), use implied vol instead of historical for richer probability estimates.

### 9. Telegram / WebUI Control (Freqtrade, Hummingbot)

Remote control: start/stop, status, force-exit. Hummingbot’s Condor shows a working pattern.

### 10. Persistence (SQLite) (Freqtrade)

Store recommendations, fills, PnL in a local DB for performance reports and backtest comparison.

---

## Insights from Taylor: Asset Price Dynamics, Volatility, and Prediction (2005)

Reference: [Lancaster University PDF](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf) / Taylor, Princeton University Press.

### Key takeaways for our strategy

1. **Direction prediction is hard**: Taylor states that answering "Which way will the price go?" using historical prices is "very difficult" and aligns with efficient market theory. Our empirical + volatility P(YES) approach is appropriate; we should not over-rely on directional forecasts.

2. **Volatility prediction works better**: "The level of volatility can be measured and predicted, with some success." Our current `_volatility_probability` (log-normal) is a solid base. Improvements Taylor recommends:
   - **GARCH/ARCH** (Ch 9–10): Time-varying volatility; adapts to recent regime (post-news spike).
   - **Implied volatility** (Ch 14–15): Option-implied vol (e.g. VIX for S&P) often outperforms historical vol forecasts.
   - **High-frequency / realized volatility** (Ch 12): Intraday data improves vol estimates and forecasts.

3. **Full distribution via options** (Ch 16): Risk-neutral densities from option prices → transform to real-world with risk aversion. If we could obtain option prices (e.g. Deribit for BTC), we could infer market probability directly.

4. **Stylized facts** (Ch 4): Returns are non-normal, autocorrelations of raw returns ≈ 0, but **squared/absolute returns** show positive autocorrelation. Our model should respect this; GARCH captures it.

### Concrete improvements (prioritized)

| Improvement                                 | Taylor ref | Effort | Impact                                        |
| ------------------------------------------- | ---------- | ------ | --------------------------------------------- |
| GARCH(1,1) for time-varying vol             | Ch 9–10    | Medium | High – better P estimates in volatile regimes |
| EWMA as lightweight GARCH alternative       | Ch 9       | Low    | Medium                                        |
| Implied vol from options (VIX, Deribit)     | Ch 14–15   | Medium | High – market’s forward-looking vol           |
| Monte Carlo paths for non–log-normal tails  | Ch 16      | Medium | Medium – better tail probabilities            |
| Intraday / realized vol (if data available) | Ch 12      | High   | High                                          |

---

## Strategy insights from Taylor (2005): _Asset Price Dynamics, Volatility, and Prediction_

Based on [Taylor (2005)](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf) (Princeton University Press):

| Insight                                | Implication for our bot                                                                                                                                                                                                                                                                         |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------- |
| **Direction prediction is hard**       | "Very difficult to obtain an interesting and satisfactory answer by considering historical prices." Our empirical P and log-normal P are directional—use with caution; favor high-confidence cases (intraday_other_min_p_win; intraday_other_min_p_win_with_vol when volatility model is used). |
| **Volatility prediction works better** | Volatility can be "measured and predicted, with some success" using historical prices or option prices. Our `_volatility_probability` is a start; GARCH/implied vol would improve it.                                                                                                           |
| **Implied vol often beats historical** | Ch 15: "Forecasts derived from option-implied volatilities and intraday asset prices... often provide superior predictions." VIX (S&P), Deribit (crypto) are candidates.                                                                                                                        |
| **Full density, not just mean**        | Ch 16: Option prices encode risk-neutral densities; transform to real-world with risk aversion. For binary contracts, P(YES) = integral above strike—richer than single log-normal P.                                                                                                           |
| **Stylized facts**                     | Returns: not normal, ~zero autocorr, but                                                                                                                                                                                                                                                        | returns | autocorrelated. Our log-normal assumes normal returns; consider fat tails (e.g. Student-t) for extreme strikes. |
| **High-frequency data**                | Ch 12: Intraday returns improve volatility estimates. If we get 5-min crypto/S&P data, use realized vol.                                                                                                                                                                                        |

**Concrete next steps** (in priority order):

1. **GARCH/EWMA** (Ch 9): Replace fixed historical vol with time-varying σ in `_volatility_probability`. E.g. GARCH(1,1) or EWMA(λ≈0.94).
2. **Implied volatility**: Integrate VIX (for ^GSPC) or Deribit/other crypto options IV when available. Blend: `σ_eff = w*σ_implied + (1-w)*σ_hist`.
3. **Fat tails**: For far OTM strikes, consider Student-t or simulation-based P instead of pure log-normal.
4. **Density from options** (Ch 16): If we add options data, estimate full risk-neutral density and extract P(S>K) directly.

---

## Longer Term / Research

- **FreqAI-style ML**: Train a model on historical recommendations + outcomes to tune min_edge or blend probabilities
- **Sentiment consensus** (Stock-Prediction-Models): Incorporate news/social sentiment if we add a data source
- **~~Cross-platform arbitrage~~** ✓ Done: Inter-exchange arb (Kalshi↔Gemini) via `enable_inter_arb`
- **~~Parse exact resolution datetime~~** ✓ Done: Per-contract time-to-expiry parsed from title (e.g. "tomorrow at 5pm ET") and passed to volatility model

---

## Strategy Improvements from Taylor (2005): Asset Price Dynamics, Volatility, and Prediction

Reference: [Lancaster University PDF](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf) — Stephen J. Taylor, _Asset Price Dynamics, Volatility, and Prediction_, Princeton University Press (2005).

### Key insights

1. **Direction prediction is hard** — "Which way will the price go? ... it is very difficult to obtain an interesting and satisfactory answer by considering historical prices." Our BUY YES/NO logic is inherently limited; we should not expect strong directional edge.

2. **Volatility prediction works better** — "The level of volatility can be measured and predicted, with some success, using either historical asset prices or current option prices." Our `--use-volatility` log-normal model aligns with this; we can improve it further.

3. **Full density prediction** — The entire probability distribution can be obtained via Monte Carlo simulation or option prices; risk-neutral densities can be transformed to real-world with risk aversion (Ch 16).

### Concrete improvements (prioritized)

| Improvement                        | Taylor reference | Effort   | Notes                                                                        |
| ---------------------------------- | ---------------- | -------- | ---------------------------------------------------------------------------- |
| **GARCH(1,1) / EWMA volatility**   | Ch 9–10          | Medium   | Replace static historical σ with time-varying vol; better regime awareness   |
| **Implied volatility (options)**   | Ch 14–15         | High     | VIX for S&P; Deribit/others for crypto; "often provide superior predictions" |
| **Intraday / realized volatility** | Ch 12, 15        | Medium   | High-frequency data improves vol forecasts; yfinance has intraday            |
| **ARCH forecasts + implied vol**   | Ch 15.6          | Medium   | Blend historical GARCH with option-implied vol for hybrid forecast           |
| **Monte Carlo paths**              | Ch 16            | Medium   | Simulate N paths for non–log-normal tail behavior                            |
| **Heavy tails / non-normal**       | Ch 4, 9.6        | Medium   | Returns are not normal; consider Student-t or skewed distributions           |
| **Density prediction**             | Ch 16            | Research | Risk-neutral from options → real-world via risk aversion                     |

### Stylized facts we should respect (Ch 4)

- Returns distribution is **not normal** (skewness, kurtosis).
- Autocorrelation of returns is ~0 (direction hard to predict).
- **Absolute returns** (or squared) show positive autocorrelation (volatility clusters).
- Calendar effects (day-of-week) can induce spurious autocorrelation.

### Recommended next step

Implement **GARCH(1,1)** or **EWMA** for time-varying volatility in `_volatility_probability`, so probability estimates adapt to recent volatility regime (e.g. post-news spike). This directly addresses Taylor’s emphasis on volatility dynamics (Ch 8–11).

---

## Insights from Taylor, _Asset Price Dynamics, Volatility, and Prediction_ (2005)

Reference: [Lancaster PDF](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf) · Taylor, S. (2005). Princeton University Press.

### Core takeaway: Direction is hard, volatility is more predictable

> "Which way will the price go, up or down? However hard we try... it is very difficult to obtain an interesting and satisfactory answer by considering historical prices." (Ch 1)

Our empirical P (above/below threshold) is a directional guess. Taylor says volatility _forecasting_ works better than directional forecasting. Recommended upgrades:

| Taylor insight                                                                        | Our current state                      | Action                                                                  |
| ------------------------------------------------------------------------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| **Volatility is time-varying**                                                        | Fixed historical σ in log-normal model | Add GARCH(1,1) or EWMA (Ch 9) so vol adapts to recent regime            |
| **Implied vol often beats historical**                                                | Historical only                        | Use VIX (S&P), Deribit/CBOE (crypto) implied vol when available (Ch 15) |
| **High-frequency data improves vol forecasts**                                        | Daily closes only                      | Use intraday / realized vol if yfinance supports it (Ch 12)             |
| **Risk-neutral densities from options**                                               | N/A                                    | If we had option chain data, infer P(price>K) directly (Ch 16)          |
| **Stylized facts** (returns not normal, autocorr ~0, absolute returns autocorrelated) | Log-normal assumes normality           | Consider fat tails, Monte Carlo paths for non-log-normal (Ch 4, 7.12)   |

### Suggested implementation order

1. **GARCH(1,1) or EWMA volatility** — Replace fixed historical σ with time-varying σ. More responsive to recent regime.
2. **Implied vol integration** — For S&P: VIX. For crypto: Deribit or CBOE options API if accessible.
3. **Monte Carlo paths** — Simulate N paths instead of single log-normal P(S>K) for fat tails.
4. **Blend empirical + volatility** — Already do `max(emp, vol_p)`; consider weighted blend based on regime or recency.

---

## Strategy Improvements from Taylor (Asset Price Dynamics, Volatility, and Prediction)

Based on [Stephen Taylor's book](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf) (Princeton University Press, 2005):

### What the book tells us

1. **Direction prediction is hard** — "Which way will the price go, up or down? However hard we try... it is very difficult to obtain an interesting and satisfactory answer by considering historical prices." Our empirical + volatility approach is appropriate; we should not over-weight directional signals.
2. **Volatility prediction works better** — "The level of volatility can be measured and predicted, with some success, using either historical asset prices or current option prices." Our `_volatility_probability` (log-normal) is a solid start; improving vol forecasts yields better P(above/below).
3. **Implied vol often beats historical** — Ch 15: "Forecasts derived from option-implied volatilities and intraday asset prices... often provide superior predictions."
4. **Stylized facts** (Ch 4): returns are not normal; autocorrelation of returns ~0; absolute/squared returns show autocorrelation; skewness/kurtosis matter.
5. **Full density prediction** (Ch 16): Risk-neutral densities from options can be transformed to real-world with risk aversion. Monte Carlo simulation of assumed dynamics is an alternative.

### Actionable improvements (prioritized)

| Improvement                                                      | Source    | Effort | Impact                                                          |
| ---------------------------------------------------------------- | --------- | ------ | --------------------------------------------------------------- |
| **GARCH(1,1) / EWMA** for time-varying vol                       | Ch 9      | Medium | High — adapts to recent regime (e.g. post-news)                 |
| **Implied volatility** (VIX for S&P, Deribit/options for crypto) | Ch 15     | Medium | High — market’s forward-looking vol                             |
| **Intraday / realized vol** (if data available)                  | Ch 12, 15 | High   | Medium — better vol estimates                                   |
| **Blend empirical + vol** (don’t over-weight empirical)          | Ch 1, 7   | Low    | Medium — already doing `max(emp, vol)`; consider weighted blend |
| **Monte Carlo** for fat tails, non-normality                     | Ch 16     | Medium | Medium — better tail probabilities                              |
| **Risk-neutral → real-world** transformation if using options    | Ch 16     | High   | Research                                                        |

See items 6, 7, 8 in Medium Priority above for existing roadmap entries; this section aligns them with Taylor’s framework.

---

## Insights from Campbell, Lo & MacKinlay: The Econometrics of Financial Markets (1997)

Reference: [NRC PDF](https://www.nrc.gov/docs/ml1208/ML12088A329.pdf) / Campbell, J.Y., Lo, A.W., MacKinlay, A.C. (1997). Princeton University Press.

### Event-study methodology (Ch 4)

Our evaluation is analogous to an event study: the "event" is _price closes above/below threshold on resolution date_. Key insights:

1. **Normal vs abnormal returns**: Abnormal return = actual return − normal (expected) return. We use empirical P (fraction of days above/below) as a simple normal-performance measure.

2. **Market model reduces variance**: R_it = α_i + β_i R_mt + ε_it. By removing market-driven variation, the market model lowers variance of abnormal returns vs constant-mean-return model. Formula: σ²_abnormal = (1 − R²) × σ²_unconditional. **For S&P 500**, a market model (e.g. regress asset return on ^GSPC) could yield more precise "unexpected" component.

3. **Estimation vs event window**: Use estimation window (e.g. 250 days) to fit normal model; event window separate. Our `days_back` (365) serves as estimation window; resolution date is the event.

4. **Brown & Warner (1980, 1985)**: Constant-mean-return model "often yields results similar to those of more sophisticated models." Our empirical approach is defensible; market-model upgrade is optional precision gain.

### Actionable improvement

| Improvement                  | Source   | Effort | Impact                                                                           |
| ---------------------------- | -------- | ------ | -------------------------------------------------------------------------------- |
| **Market model for S&P 500** | Ch 4.3.2 | Low    | Medium – regress ^GSPC return on market factor; use residual for "abnormal" move |

---

## Insights from Tsay: Analysis of Financial Time Series

Reference: [NUS PDF](https://cpb-us-w2.wpmucdn.com/blog.nus.edu.sg/dist/0/6796/files/2017/03/analysis-of-financial-time-series-copy-2ffgm3v.pdf) / Tsay, R.S. (3rd ed., Wiley). _Note: PDF parsing failed; referencing standard Tsay coverage._

Typical coverage: ARIMA, ARCH/GARCH, structural breaks, cointegration. Overlaps with Taylor (2005) and our roadmap:

- **GARCH**: Time-varying volatility (already in IMPROVEMENTS §6)
- **ARMA/ARIMA**: For return modeling; less critical for binary P(above/below)
- **Structural breaks**: Regime changes; GARCH helps adapt

No new unique improvements beyond Taylor + Campbell/Lo/MacKinlay; Tsay reinforces GARCH/ARCH priority.

---

## Insights from Campbell, Lo & MacKinlay: The Econometrics of Financial Markets (1997)

Reference: [NRC PDF](https://www.nrc.gov/docs/ml1208/ML12088A329.pdf) — Campbell, J.Y., Lo, A.W., & MacKinlay, A.C. (1997). Princeton University Press.

### Key takeaways (Event-Study Analysis, Ch 4)

1. **Market model vs constant-mean-return**: The market model \(R*{it} = \alpha_i + \beta_i R*{mt} + \epsilon\_{it}\) reduces the variance of abnormal returns vs a constant-mean model. Higher \(R^2\) = greater variance reduction = more precise inferences. Our empirical P is analogous to a constant-mean approach; incorporating a market factor (e.g. S&P 500 for crypto/equities) could improve precision.

2. **Estimation window vs event window**: Keep them separate. Use pre-event data (e.g. 250 days) for estimating normal performance; the event window is the resolution date. We already do this with `days_back` (estimation) and per-contract expiry (event).

3. **Normal vs abnormal returns**: Abnormal return = actual − expected. Our "edge" is effectively when market price diverges from our expected P(YES). The framework supports testing whether that divergence is statistically meaningful.

4. **Power of event-study tests**: Shorter event windows and larger samples increase power. For binary contracts with a single resolution date, our "event window" is one day—optimal. Sample size = number of similar contracts we evaluate.

### Actionable improvements

| Improvement                                                  | Source   | Effort   | Impact                                                                              |
| ------------------------------------------------------------ | -------- | -------- | ----------------------------------------------------------------------------------- |
| **Market model** for "normal" return                         | Ch 4.3.2 | Medium   | Medium — regress asset return on S&P/benchmark; use residual for abnormal component |
| **Cross-sectional regression** (CAR vs firm characteristics) | Ch 4.8   | Research | Low — relate edge to market cap, volatility regime, etc.                            |
| **Nonparametric tests** (sign, rank) for robustness          | Ch 4.7   | Low      | Low — validate that our recommendations aren't driven by outliers                   |

---

## Insights from Tsay: Analysis of Financial Time Series

Reference: [NUS PDF](https://cpb-us-w2.wpmucdn.com/blog.nus.edu.sg/dist/0/6796/files/2017/03/analysis-of-financial-time-series-copy-2ffgm3v.pdf) — Tsay, R.S. _Analysis of Financial Time Series_ (Wiley). _Note: PDF parsing failed; referencing standard Tsay coverage._

### Standard coverage relevant to our bot

- **ARIMA / unit roots**: For return series; our empirical P doesn't assume stationarity of levels but uses historical closes. Worth verifying no structural breaks in our lookback window.
- **ARCH / GARCH** (aligns with Taylor): Time-varying volatility; Tsay provides implementation detail. Supports our GARCH(1,1) roadmap item.
- **Structural breaks**: Regime changes (e.g. post-2022 crypto) can invalidate long historical windows. Consider shorter `days_back` or regime-aware weighting when volatility spikes.
- **Multivariate models**: For cross-asset (BTC, ETH, S&P) correlation. Useful if we add portfolio-level risk limits.

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
