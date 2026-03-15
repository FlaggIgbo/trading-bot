# Trading Bot Improvement Roadmap

Inspired by top open-source trading repositories: [freqtrade](https://github.com/freqtrade/freqtrade), [ccxt](https://github.com/ccxt/ccxt), [hummingbot](https://github.com/hummingbot/hummingbot), [gekko](https://github.com/askmike/gekko), and [Stock-Prediction-Models](https://github.com/huseinzol05/Stock-Prediction-Models).

---

## Implemented ✓

| Feature | From | Status |
|---------|------|--------|
| Dry-run / preview mode | Freqtrade | ✓ `--preview` in compare_historical.py |
| Volatility-based probability | Stock-Prediction-Models, options theory | ✓ `--use-volatility` log-normal model |
| Balance-aware order execution | Freqtrade | ✓ `create_order_safe`, `max_pct_of_balance` |
| Config-driven strategy | Freqtrade, Gekko | ✓ `config.yaml` strategy, min_edge |
| Kelly criterion position sizing | Freqtrade, Stock-Prediction-Models | ✓ `strategy.use_kelly`, `kelly_fraction` |
| Whitelist / blacklist | Freqtrade | ✓ `strategy.whitelist`, `strategy.blacklist` |
| File logging for run history | Freqtrade | ✓ `--log` or `--log DIR` |

---

## High Priority (Feasible Now)

### 1. Backtesting (Freqtrade, Gekko)

Run our evaluation logic on *historical* market snapshots instead of live data. Simulate what recommendations we would have made in the past and measure PnL.

- **Scope**: Use stored historical prices (yfinance) + historical Kalshi/Gemini snapshots (if available) or synthetic fills at mid
- **Output**: Sharpe, win rate, max drawdown, edge decay over time
- **TODO**: Fetch historical orderbook/market data; Kalshi may expose historical prices

### 2. Kelly Criterion Position Sizing (Freqtrade, Stock-Prediction-Models)

Instead of fixed `count=10`, size positions by edge and bankroll:

```
f* = (p*b - q) / b   # p=win prob, q=1-p, b=odds
```

- **Scope**: `strategy.use_kelly: true`, `kelly_fraction: 0.25` (quarter-Kelly) in config
- **Output**: Recommended contract count in preview and live execution

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

### 6. GARCH / EWMA for Time-Varying Volatility (Stock-Prediction-Models)

Current volatility model uses a fixed historical vol. GARCH or EWMA adapts vol to recent regime (e.g. post-news spike).

### 7. Monte Carlo Paths (Stock-Prediction-Models)

Instead of single log-normal P(S>K), run N simulated price paths and compute empirical P. Useful for non-log-normal behavior.

### 8. Implied Volatility from Options (if available)

If we can get options data (e.g. Deribit for BTC), use implied vol instead of historical for richer probability estimates.

### 9. Telegram / WebUI Control (Freqtrade, Hummingbot)

Remote control: start/stop, status, force-exit. Hummingbot’s Condor shows a working pattern.

### 10. Persistence (SQLite) (Freqtrade)

Store recommendations, fills, PnL in a local DB for performance reports and backtest comparison.

---

## Longer Term / Research

- **FreqAI-style ML**: Train a model on historical recommendations + outcomes to tune min_edge or blend probabilities
- **Sentiment consensus** (Stock-Prediction-Models): Incorporate news/social sentiment if we add a data source
- **Cross-platform arbitrage**: Compare Kalshi vs Gemini prices for same event and flag arb opportunities
- **Parse exact resolution datetime**: Use per-contract expiry for precise time-to-expiry in volatility model

---

## Reference Repos Summary

| Repo | Key Takeaways |
|------|----------------|
| **freqtrade** | Dry-run, backtest, hyperopt, SQLite, WebUI, Telegram, strategy interface |
| **ccxt** | Unified API, normalized data; we use custom Kalshi/Gemini clients (different product) |
| **hummingbot** | Modular connectors (CEX/DEX), Gateway middleware |
| **gekko** | TA + backtesting; archived but concepts still valid |
| **Stock-Prediction-Models** | LSTM/GRU, RL agents, Monte Carlo, sentiment, portfolio optimization |
