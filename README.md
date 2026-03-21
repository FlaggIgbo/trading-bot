# Prediction Market Trading Bot

A bot that connects to the [Kalshi API](https://docs.kalshi.com/) and the [Gemini API](https://docs.gemini.com/prediction-markets/markets) to monitor selected prediction markets and help you decide when to trade.

**What is a prediction market?** Think of it like betting on yes/no questions—e.g. "Will Bitcoin be above $81,250 on March 16?" You buy YES if you think it will happen, or NO if you think it won't. If you're right, you profit (usually $1 per contract); if wrong, you lose what you paid. Prices in cents represent the market's estimated probability—e.g. 40¢ means people think there's about a 40% chance. The bot looks for times when that crowd price is off compared to historical data.

## Setup

1. **Install dependencies** (recommended: use a virtual environment)

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

   If you use system Python and see `ModuleNotFoundError`, create a venv and install there.

2. **Configure credentials**

   Copy `.env.example` to `.env` and fill in your values:

   ```bash
   cp .env.example .env
   ```

   | Variable                  | Description                             | Where to get it                                                   |
   | ------------------------- | --------------------------------------- | ----------------------------------------------------------------- |
   | `KALSHI_API_KEY_ID`       | Your API Key ID (UUID format)           | [Account Settings → API Keys](https://kalshi.com/account/profile) |
   | `KALSHI_PRIVATE_KEY_PATH` | Path to your `.key` file **or**         | Download when you create an API key                               |
   | `KALSHI_PRIVATE_KEY`      | Raw PEM content (alternative to path)   | Same key, as string with `\n` for newlines                        |
   | `KALSHI_USE_DEMO`         | `true` for demo, `false` for production | Demo: [demo.kalshi.com](https://demo.kalshi.com)                  |

   For **demo**, use `https://demo-api.kalshi.co` and create keys at [demo.kalshi.com](https://demo.kalshi.com/account/profile).

3. **Select markets to watch**

   Edit `config.yaml` or set the `MARKETS` env var:

   ```yaml
   markets:
     - KXBTCD # Bitcoin daily
     - KXBTC15M # Bitcoin 15-minute up/down (optional; see `strategy.ultra_short_*`)
     - KXETHD # Ethereum daily
     - KXSOLD # Solana
     - KXINXU # S&P 500
     - KXXRPD # XRP daily
   ```

   Or: `MARKETS=KXBTCD,KXETHD,KXSOLD,KXINXU,KXXRPD` (repo `config.yaml` also lists `KXBTC15M`, `KXDOGE15M`, `KXDOGED`, `KXBNB15M`, `KXHYPE15M`; Hyperliquid uses Yahoo `HYPE32196-USD`)

Run scripts from the project root (e.g. `python scripts/compare_historical.py`). Scripts add the project root to `sys.path` so imports work correctly.

## Running

```bash
python scripts/compare_historical.py --preview --no-llm
```

The engine will:

- Connect using your API credentials
- Evaluate Kalshi and Gemini contracts with empirical + volatility + calibrated signals
- Recommend BUY YES / BUY NO / SKIP and optionally stage limit orders
- Apply strategy-engine logic from `strategy.strategy_engine` in `config.yaml`

### Barbell strategy (`strategy.barbell_enabled`)

When **`barbell_enabled: true`** (default in repo `config.yaml`), directional **BUY YES / BUY NO** trades must fall into one of two buckets:

- **Base:** High quoted price on the side you buy (consensus), high model P(win), and model P not far below the market bid (tunable via `barbell_base_*`). Lets **expensive YES** through the usual steamroller cap **only** for this base bucket (up to `barbell_base_max_yes_price`), still subject to `min_payout_ratio` and other gates.
- **Alpha:** **Cheap** leg (bid ≤ `barbell_alpha_max_cheap_price`), minimum model P, and a large **net** edge after maker fees (`barbell_alpha_min_net_edge`). Kelly sizing is scaled down via `barbell_alpha_kelly_mult`.

Trades in the **middle** of the book (not cheap enough for alpha, not high-P enough for base) are **SKIP**ped. Logs show **`├─ Barbell: base`**, **`alpha`**, **`blocked (not base/alpha)`**, or **`—`** when the verdict was already SKIP before the barbell gate. **Arb**, disagreement-dip, and lottery-limit paths are unchanged.

Set **`barbell_enabled: false`** to restore the prior “any edge” directional behavior (still subject to `min_edge`, strike guards, etc.).

## Kalshi API Overview

- **Auth**: RSA-PSS signature with your private key. Required headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.
- **Endpoints**:
  - Public: `/markets`, `/markets/{ticker}`, `/markets/{ticker}/orderbook` (no auth for market data)
  - Authenticated: `/portfolio/balance`, `/portfolio/orders`, `POST /portfolio/orders` for trading
- **URLs**:
  - Production: `https://api.elections.kalshi.com/trade-api/v2`
  - Demo: `https://demo-api.kalshi.co/trade-api/v2`

See [Kalshi API docs](https://docs.kalshi.com/welcome) and [Making your first request](https://docs.kalshi.com/getting_started/making_your_first_request).

## Project structure (ML pipeline)

The repo is organized as an ML pipeline: **signals** (inputs) → **models** (compute) → **execution** (outputs), with **data** for persistence and **scripts** as entry points.

```
trading-bot/
│
├── config.yaml              # Signal configuration: markets, strategy params, risk limits
├── config.py                # Config loader (yaml + env overrides)
├── constants.py             # Shared constants (recommendation strings, platform IDs)
│
├── signals/                 # INPUT LAYER — market data source clients
│   ├── kalshi_client.py     #   Kalshi exchange API (auth, orders, positions)
│   ├── gemini_client.py     #   Gemini prediction markets API (public + trading)
│   ├── yfinance_client.py   #   Underlying prices: Gemini Exchange spot (crypto) + Yahoo + CoinGecko fallback
│   └── ollama_analysis.py   #   LLM-powered trade analysis (optional)
│
├── models/                  # COMPUTE LAYER — ML strategies & signal scoring
│   ├── anti_hallucination.py #  Anti-hallucination pipeline (Bayesian, EWMA, vol Z-score, adaptive edge, regime filter)
│   ├── alpha_engine.py      #  Alpha + risk upgrades (OBI signal, markout/toxic flow, vol-adjusted Kelly, lead-lag)
│   └── strategies/          #   Pluggable recommendation engines
│       ├── base.py          #     Abstract strategy interface
│       ├── default_threshold.py # Threshold + fee + signal model
│       └── vol_regime.py    #     Production default (volatility-regime aware)
│
├── execution/               # OUTPUT LAYER — trade execution & order lifecycle
│   ├── engine.py            #   Order sync, cancel, and resting-order management
│   └── fill_reconciliation.py # Post-trade fill matching and PnL accounting
│
├── data/                    # DATA LAYER — persistent storage & calibration
│   └── storage.py           #   SQLite: predictions, snapshots, cache, resolved outcomes
│
├── scripts/                 # Entry points (orchestration)
│   ├── compare_historical.py #  Main pipeline: evaluate → recommend → execute
│   ├── backtest.py          #   Backtest: real or synthetic mode
│   ├── daily_performance_report.py # Daily report + performance graphs
│   ├── compare_prices.py    #   Quick live price snapshot
│   └── ...                  #   Test, debug, validation scripts
│
├── docs/                    # Documentation
│   ├── CODE_EXPLAINER.md    #   Step-by-step flow, file-by-file reference
│   └── IMPROVEMENTS.md      #   Roadmap and backlog
│
├── launchd/                 # macOS scheduling (run every 5 min / daily)
├── pyproject.toml           # Ruff (linter/formatter) config
├── .pre-commit-config.yaml  # Pre-push hooks (lint, config validation, backtest guard)
└── .env                     # Credentials (from .env.example)
```

**Pipeline flow:** `config.yaml` defines input signals (markets, tickers, strategy params) → `signals/` clients fetch market data → `models/strategies/` compute P(YES) and recommendations → `models/anti_hallucination.py` corrects overconfident predictions via Bayesian update, EWMA probability, vol Z-score dampening, adaptive edge, and regime filter → `models/alpha_engine.py` applies alpha signals (OBI edge adjustment, markout/toxic-flow detection, vol-adjusted Kelly sizing, lead-lag edge bonus) → `execution/` places orders on exchanges → `data/storage.py` persists outcomes for calibration.

Root-level shim files (`kalshi_client.py`, `gemini_client.py`, etc.) re-export from the new package locations for backward compatibility. All scripts in `scripts/` add the project root to `sys.path` so both old and new import paths work.

**Code walkthrough:** For a step-by-step flow and file-by-file reference with code pointers, see [docs/CODE_EXPLAINER.md](docs/CODE_EXPLAINER.md).

### Development (linting, formatting)

- **Ruff** (Python): `pip install ruff` then `ruff check . --fix && ruff format .`
- **Pre-commit** (runs on `git push`): `pip install pre-commit && pre-commit install --hook-type pre-push`
  - Includes a config validator hook (`scripts/validate_config.py`) that catches invalid or conflicting risk settings before push.
  - Includes a backtest guard hook (`scripts/backtest_guard.py`) that can block pushes if recent performance falls below thresholds in `config.yaml` (`backtest.pre_push_*`).
- **Prettier** (YAML/JSON/MD): `npm install` then `npx prettier --write "*.yaml" "*.json" "*.md"` (or use pre-commit)

## Improvement roadmap

See [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) for the full roadmap. Implemented highlights include: **anti-hallucination pipeline** (Bayesian update, EWMA probability, vol Z-score dampening, adaptive edge scaling, EMA regime filter), **alpha engine** (Order Book Imbalance signal, markout PnL / toxic flow detection, volatility-adjusted Kelly sizing, lead-lag cross-asset signal), modular strategy engine wiring (`strategy_engine`), Student-t + EWMA/GARCH(auto-fallback) volatility modeling with Monte Carlo path simulation, continuous vol-ratio scaling (edge/dip/size), dynamic stability windows, cross-exchange sanity gating, directional parity penalties, emergency + defensive circuit breakers, position evaluation tracking, bet-vs-resolution accuracy tracking (distance metrics in daily reports), DTE-aware short-dated contract guards (volatility time floor, adaptive strike limits, empirical/vol blend decay), and slippage-aware synthetic backtesting with dwell-time + partial-fill realism.

### Recent changes (2026-03-17)

- **Gemini Exchange spot for crypto underlying prices:** `signals/yfinance_client.py` calls [Gemini public market data](https://docs.gemini.com/rest-api/market-data) first for BTC/ETH/SOL/XRP (`v2/ticker` for live snapshot, `v2/candles` for daily/hourly history) when `YFINANCE_CRYPTO_GEMINI` is on (default). **S&P 500** (`^GSPC` / `SPY`) now uses the SPY ETF as a proxy (converted ×10) and averages it with the index when `YFINANCE_SPY_AS_SP500` is enabled (default **on**). Disable the SPY proxy by setting `YFINANCE_SPY_AS_SP500=0` — the bot then relies on Yahoo’s `^GSPC` only. Order after Gemini fails: yfinance, then CoinGecko (BTC/ETH/SOL only). `get_aggs_as_of()` uses one candle request with a UTC end-of-day cutoff (no double-fetch). Disable Gemini spot with `YFINANCE_CRYPTO_GEMINI=0`; override base URL with `GEMINI_SPOT_API_BASE`; debug logs via `YFINANCE_DEBUG=1`.

### Recent changes (2026-03-19)

- **Layered limit order fix — passive dip pricing**: The `layered` limit order mode was penny-jumping ABOVE the bid (`bid + tick`), completely ignoring the carefully computed dip depth (`limit_dip_pct`, `limit_dip_pct_high_confidence`, `_adaptive_limit_dip`, `_dte_confidence_dip`). Rewritten to place passive limits BELOW the bid using the full dip stack (`bid × (1 − dip)`), with staggered depth on re-buys (additional same-side orders target even cheaper levels for better avg cost). Fair-value cap prevents overpaying. This is the single largest fix for recent losing trades — entries now fill at better prices instead of fighting for best bid.
- **RSI signal integration**: Added `_compute_rsi()` (Wilder RSI-14 on underlying asset closes) with two uses: **(1) Dip modulation** via `_rsi_dip_adjustment()` — oversold (RSI<30) tightens dip by 40% to fill faster on the bounce; overbought (RSI>70) widens dip by 40% to wait for correction. Applied at both Kalshi and Gemini limit order paths. **(2) Signal model feature** — `rsi_norm` added to `build_signal_features()` and `score_signal_probability()` with weight `-0.3` (oversold boosts P(YES) for mean-reversion; overbought dampens).
- **Vol-spike safety tuning**: `vol_spike_threshold_mult` raised from 2.0 to **3.0** — dips happen during volatility spikes, so the old 2x trigger was canceling resting limit orders at the exact moments they should be catching value.
- **Momentum lockout widened**: `momentum_crash_threshold_pct` widened from -3.5% to **-5.0%** — allows more downside before locking out new buys; passive limit orders need room to work during dips.
- **Faster stability re-entry**: `min_stability_duration_seconds` reduced from 30s to **15s** — faster re-posting of limit orders after short dips instead of waiting half a minute.
- **Deeper default dip**: `limit_dip_pct` raised from 5% to **7%** — catches more transient price wicks.
- **Dynamic EV-optimal sell threshold** (`dynamic_sell_threshold: true`): The sell trigger for held positions is now computed dynamically from the current sell price (bid) and exit fee, rather than using a fixed percentage. The formula `P(lose) > 1 − (sell_price − exit_fee)` maximizes expected value by comparing the guaranteed net proceeds of selling immediately against the expected value of holding to settlement. High bids → low threshold (sell readily to lock in profit); low bids → high threshold (hold unless very likely to lose). Applied to all three sell paths: Kalshi orphan positions, Kalshi regular positions, and Gemini positions. Configurable via `strategy.dynamic_sell_threshold`; falls back to `sell_losing_pct` when disabled.
- **Per-ticker entry cap** (`max_entries_per_ticker: 3`): Prevents the bot from re-entering the same losing contract every cycle. Previously, tickers like `KXBTCD-26MAR1617-T75249.99` accumulated 54 entries with zero wins. Now, once a ticker has 3 entries in the predictions table (filled or pending, excluding cancelled), new orders for that ticker are blocked. Configurable via `strategy.max_entries_per_ticker`.
- **Near-expiry order protection** (`near_expiry_keep_dte: 0.02`): The `want_better_price` cancellation logic no longer cancels resting BUY orders when the contract resolves within ~30 minutes. This prevents losing winning ITM positions by chasing pennies near expiry — e.g. SOL > $87 at 1am EDT was cancelled for `want_better_price` with 3 minutes left, and the replacement didn't fill.
- **Anti-hallucination pipeline tuning:** Reduced aggressive dampening that was causing the pipeline to SKIP most ITM contracts despite clear edge. Key changes:
  - `min_edge` reduced from 0.20 to **0.15** — AH layers already provide overconfidence protection.
  - EWMA blend weight made configurable (`ewma_prob_blend_weight`, default 0.30, was hardcoded at 0.50) — model now retains 70% of its estimate instead of 50%.
  - `bayesian_market_strength` reduced from 2.0 to **1.5** — less aggressive pull toward market price for ITM contracts.
  - `vol_zscore_tail_mult` reduced from 0.5 to **0.3** — less aggressive pull toward 0.5 during vol spikes.
  - `adaptive_edge_otm_base_mult` reduced from 2.5 to **1.8** — less punishing for cheap/expensive contracts.
  - `adaptive_edge_trend_opposed_mult` reduced from 2.0 to **1.5** — less punishing for trend-opposed trades.
  - Strike distance multiplier made configurable (`adaptive_edge_strike_dist_mult`, default 1.2, was hardcoded at 2.0) — reduces compounding with OTM mult.
  - `uncertainty_edge_mult` reduced from 0.8 to **0.5** — AH layers already dampen overconfidence.
- **OBI edge adjustment (Gemini):** Fixed `UnboundLocalError` on `market_for_book_q` in the Gemini OBI (Order Book Imbalance) code path within `compare_historical.py`. The variable was referenced before assignment; now correctly resolved from the local contract/event context.
- **yfinance FutureWarning:** Fixed deprecation warning (`Calling float on a single element Series`) in `yfinance_client.py` intraday aggregation by using `iloc[0]` when the row value is a Series.
- **yfinance_client.py cleanup:** Consolidated 19 duplicate `get_aggs_as_of` function definitions into a single implementation that accepts `datetime | str | None` for `end_date`, with CoinGecko fallback for crypto tickers.
- **Adaptive learning system:** The bot now learns from recent trades in real-time. Recency-weighted performance tracking (last 6h win rate, EWMA win rate), loss-streak detection with circuit breaker (extra edge required after consecutive losses), per-series cooldown when a market series has consecutive losses, and `sold` trades now count in calibration stats (previously excluded). Config: `adaptive_learning_enabled`, `adaptive_edge_lookback_hours`, `loss_streak_threshold`, `series_cooldown_threshold`, `ewma_halflife_trades`.
- **Fresh-circuit breaker:** When the last 6h win rate (via `get_recent_performance`) falls below `adaptive_loss_block_win_rate: 0.35`, the bot blocks new BUY orders until performance stabilizes. This gives the strategy time to recover rather than averaging down into a losing streak.
- **YES bias correction:** The model was structurally biased toward YES (86% YES vs 14% NO). Five fixes: `yes_overconfidence_extra_edge: 0.04` (YES trades require 4% more edge than NO), `model_probability_shrinkage: 0.65` (stronger pull toward 50%), removed vol-model override on `min_yes_price`, `parity_max_penalty` doubled from 0.10 to 0.20, and `easy_win_min_p_win`/`easy_win_min_market_p` raised to 0.93/0.88.
- **Gemini settlement sync fix:** Gemini trades were not being resolved. Fixed API category filter (was causing 400 errors) and rewrote contract-to-prediction matching to use threshold-based matching instead of label matching.
- **Intraday vol model corrections:** Reduced vol model overconfidence on intraday crypto. `vol_blend_weight` reduced from 0.75 to 0.45; added `vol_disagree_threshold`/`vol_disagree_dampen_blend` to dampen blending when empirical and vol models strongly disagree; added `intraday_vol_cap: 0.85` (tighter cap) and `intraday_min_empirical_weight: 0.20`.
- **Defensive profitability preset (forward PnL):** Tightened risk after analyzing concentration + Kalshi efficiency. Defaults now: `min_edge: 0.22`, `enable_easy_win: false`, `absolute_winner_max_pct_of_balance: 0.02`, `bayesian_market_strength: 2.75`, `arb_inter_allow_single_leg_fallback: false`, `enable_sell_and_rebuy: false`, `kelly_fraction: 0.15`, `trading.max_pct_of_balance: 0.15`. Underlying data remains yfinance + live snapshot—not sub-second websocket; limit/maker flow avoids taker fee drag. Use `trading.dry_run: true` or `--preview` until you validate forward results.

## Contract evaluation workflow

**Goal:** Use historical price data to estimate fair probability, then compare to Kalshi/Gemini market prices → recommend **BUY YES** / **BUY NO** / **SKIP**.

**Example in plain English:** Imagine a bet: "Will Bitcoin be above $81,250 on March 16?" The bot looks at the last 90 days of Bitcoin prices (rolling window) and finds that Bitcoin closed above that level only 5% of the time. So the "fair" chance of YES is about 5%. If the market is charging 40¢ for YES (i.e. people think there's a 40% chance), the market is overpricing YES. The bot would recommend **BUY NO** (bet against it) or **SKIP** if the edge isn't big enough.

Run `scripts/compare_historical.py` every **5 minutes** locally for continuous evaluation. See [Cron setup](#run-every-5-minutes) below.

### Key terms in plain English

| Term                   | Plain-English meaning                                                                                                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **BUY YES**            | Bet that the event _will_ happen (e.g. Bitcoin _will_ be above $81,250). You profit if you're right.                                                                            |
| **BUY NO**             | Bet that the event _won't_ happen (e.g. Bitcoin _won't_ be above $81,250). You profit if you're right.                                                                          |
| **Edge**               | How much we think we're getting a bargain. If we think the real chance is 20% but the market prices it at 10¢ (10%), we have a 10% edge.                                        |
| **Bid**                | The highest price someone is willing to _pay_ to buy.                                                                                                                           |
| **Ask**                | The lowest price someone is willing to _accept_ to sell. (Buying at the ask = paying the seller's price.)                                                                       |
| **Strike / threshold** | The number the bet is about. E.g. "$81,250" in "Bitcoin above $81,250".                                                                                                         |
| **Resolution**         | When the bet settles—e.g. "5pm ET" means we use the price at 5pm Eastern that day to determine who wins.                                                                        |
| **Limit order**        | "Buy only if the price is X or better." Your order waits in the book until someone matches it. Safer than "buy at whatever price is available now."                             |
| **Volatility**         | How much prices swing. High volatility = bigger moves up and down; low = calmer. Affects how likely the price is to reach a target.                                             |
| **Arbitrage**          | Risk-free profit from a pricing mistake. E.g. if YES costs 40¢ and NO costs 50¢ on the same bet, buying both costs 90¢ but one must win (payout $1), so you lock in 10¢ profit. |
| **Dry run**            | Practice mode: the bot figures out what it _would_ do but doesn't place any real orders.                                                                                        |
| **Contract**           | One market/bet—e.g. "Bitcoin above $81,250 on March 16." Each contract has YES and NO sides.                                                                                    |
| **Position**           | A bet you already hold. If you bought 10 YES contracts, you have a "position" of 10 YES.                                                                                        |
| **Payout**             | What you get if you win. Usually $1 per contract—so if you paid 30¢ and win, you profit 70¢.                                                                                    |

### Flow diagram: `scripts/compare_historical.py`

```mermaid
flowchart TB
    subgraph init["1. Initialize"]
        A[Load config] --> B[Fetch Gemini events]
        B --> C[Fetch underlying history — Gemini spot crypto + yfinance; cache when storage on]
    end

    subgraph eval["2. Evaluate Markets"]
        C --> D[For each series: KXBTCD, KXETHD, KXSOLD, KXINXU, KXXRPD]
        D --> E[Kalshi: get open markets]
        D --> F[Gemini: get crypto events]
        E --> G{Skip? Non-crypto/non-S&P intraday}
        F --> G
        G -->|No| G2{Arb? intra or inter}
        G2 -->|Yes| J
        G2 -->|No| H[Extract strike, emp+vol blend, hist_p]
        H --> I{Recommendation}
        I -->|BUY YES/NO| J[pending_orders]
        I -->|SKIP| K[Skip]
        I --> J2[Record snapshot]
        J --> J2
    end

    subgraph positions["3. Evaluate Positions"]
        L["Kalshi: get_positions + Gemini: get_positions"] --> M["Parse: position_fp or outcome/qty"]
        L2[Gemini get_positions] --> M2[Map symbol→market; parse outcome, qty]
        M --> N{Cached or fetch market}
        N --> O[Compute P lose, rec with vol blend]
        O --> P{Sell trigger?}
        P -->|EV-optimal sell threshold| Q[pending_sells]
        P -->|Edge flip| Q
        P -->|RULE_VIOLATION| Q
        P -->|INTRADAY unsupported| Q
        M2 --> N
    end

    %% Eval completes → positions (both J and K lead to next phase)
    J --> L
    J --> L2
    K --> L

    subgraph execute["4. Execute or Preview"]
        Q --> R{Flags?}
        J --> R
        R -->|--execute| S[Review orders: cancel/update/keep]
        S --> T[SELL first: Kalshi + Gemini]
        T --> U[BUY then: limit orders - dip based on P-win, skip kept]
        R -->|--preview| U[Show planned orders]
        R -->|none| V[Note: N losers, run --preview]
    end

    subgraph optional["5. Optional"]
        T --> W[Ollama LLM analysis]
        U --> W
        V --> W
    end

```

**ASCII version** (for terminals / plain-text viewers):

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         compare_historical.py                                │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────┐     ┌─────────────────────┐     ┌──────────────────────────────────┐
  │ Load config  │────▶│ Fetch underlying     │────▶│ For each series (KXBTCD, KXETHD,  │
  │ markets,     │     │ closes (Gemini crypto│     │ KXSOLD, KXINXU, KXXRPD):         │
  │ yf_tickers   │     │ + yfinance; cache 1h) │     │   • Get Kalshi open markets       │
  └──────────────┘     └─────────────────────┘     │   • Get Gemini events (crypto)    │
                                            └────────────────┬─────────────────┘
                                                             │
                         ┌───────────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ For each market (Kalshi + Gemini):                                           │
  │   1. Skip? → Non–crypto/non–S&P intraday only. Crypto/S&P: all intraday OK   │
  │   2. Arb first: intra (yes+no<1) or inter (Kalshi↔Gemini). If found → orders  │
  │   3. Extract strike (e.g. $72,250 above) from market                          │
  │   4. hist_p = empirical + vol blend (--use-volatility; EWMA vol when enabled)  │
  │   5. Compare hist_p vs market YES price → BUY YES / BUY NO / SKIP            │
  │   6. If not SKIP → add to pending_orders (limit = bid × (1−dip); 15% when P≥90%)│
  │   7. Record market snapshot (storage) for all evaluated markets               │
  └─────────────────────────────────────────────────────────────────────────────┘
                                                             │
                                                             ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ Evaluate ALL positions (Kalshi get_positions + Gemini get_positions):        │
  │   • Parse position_fp (positive=YES, negative=NO) for each position          │
  │   • Cached? Use market_data_cache. Uncached? Fetch market + closes           │
  │   • Sell triggers (when we recommend selling a position you hold):            │
  │       - LIKELY_LOSE:     EV-optimal: sell when P(lose) > 1 − (bid − fee)     │
  │       - Edge flip:       We used to say BUY YES; now we say BUY NO (or vice versa) — cut losses, don't double down
  │       - RULE_VIOLATION:  You hold NO on "price above X" but price already > X — the bet is going against you
  │       - INTRADAY_MARKET: Non–crypto/non–S&P intraday → exit position         │
  │   • Add to pending_sells                                                     │
  └─────────────────────────────────────────────────────────────────────────────┘
                                                             │
           ┌─────────────────────────────────────────────────┼─────────────────┐
           ▼                                                 ▼                 ▼
  ┌──────────────────┐                           ┌──────────────────┐  ┌─────────────┐
  │ --execute         │                           │ --preview         │  │ No flags    │
  │ Place real orders │                           │ Show planned      │  │ Eval only   │
  └────────┬─────────┘                           │ orders (no exec)  │  │ + note if   │
           │                                      └──────────────────┘  │ pending     │
           ▼                                                                      │
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ EXECUTE order: Review → SELL → BUY                                          │
  │   0. Review: cancel orders no longer wanted or needing higher price          │
  │   1. SELL: Kalshi + Gemini limit sells at bid (skip if kept)                 │
  │   2. BUY:  Kalshi + Gemini limit buys (skip if kept; else place/replace)    │
  │            Price = bid × (1−dip); 15% dip when P(win)≥90%                     │
  └─────────────────────────────────────────────────────────────────────────────┘
                                                             │
                                                             ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ Optional: Ollama LLM analysis (local) — skip with --no-llm                  │
  └─────────────────────────────────────────────────────────────────────────────┘
```

## Ollama LLM analysis (optional)

When [Ollama](https://ollama.com) is running locally, scripts send their output to a free local LLM for trading opinions.

1. Install [Ollama](https://ollama.com) and pull a model:
   ```bash
   ollama pull llama3.2
   ```
2. Ensure Ollama is running (often auto-starts in the background).
3. Run any script — the last output will be the LLM’s analysis:
   ```bash
   python3 scripts/compare_prices.py
   python3 scripts/compare_historical.py
   python3 scripts/test_connection.py
   ```

To skip LLM analysis (e.g. for cron or automation):

```bash
python3 scripts/compare_historical.py --no-llm
python3 scripts/compare_prices.py --no-llm
python3 scripts/test_connection.py --no-llm
```

Override model via `OLLAMA_MODEL`:

```bash
OLLAMA_MODEL=mistral python3 scripts/compare_historical.py
```

**Troubleshooting: "Ollama skipped: not running or model missing"**

If you see this message at the end of a run, either Ollama isn't running or the model isn't installed:

1. **Start Ollama** — Open the Ollama app (Applications or Spotlight). It must be running before you run the script.
2. **Pull a model** — Run `ollama pull llama3.2` or `ollama pull llama3.2:1b` (smaller, faster). The bot tries `llama3.2` first, then `llama3.2:1b`, then `mistral`, etc.
3. **Verify** — Run `ollama list` (or `curl http://localhost:11434/api/tags`) to confirm models are installed. If `ollama list` crashes (known macOS Metal bug), the server may still work—the Python client uses the HTTP API.
4. **Debug** — Run `python3 scripts/debug_ollama.py` to test the integration and surface any errors.
5. **Override model** — If you only have `llama3.2:1b`: `OLLAMA_MODEL=llama3.2:1b python3 scripts/compare_historical.py`

If Ollama isn't running or the model is missing, scripts still run and finish without the LLM analysis.

### `ollama_codex_loop.sh` (metrics gate + leaderboard)

The repo root script builds `docs/ollama_runs.html`, appends `~/comparisons/metrics.csv`, refreshes `docs/series_leaderboard.html`, and runs **`ollama_codex_generate.py`** (HTTP API, temperature from **`OLLAMA_TEMPERATURE`**) → `git apply` → **`scripts/ollama_patch_validate.py`**. **Metrics rows:** **`metrics_interval_position_eval.py`** — win rate and PnL for **`position_evaluations`** rows with `resolved_ts` in **(previous CSV timestamp, loop time]** (SQLite). **No resolutions in the interval** → CSV/HTML use **`null`** for both (not `0%` / `$0`). If that script fails, falls back to **`parse_compare_log_metrics.py`** on the eval log. The prompt uses **`extract_ollama_log_context.py`** for compact log excerpts.

| Env | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_AUTO_COMMIT_DOCS` | `1` | If `1`, **`git commit`** `docs/ollama_runs.html`, `docs/series_leaderboard.html`, and `docs/ollama_journal.jsonl` whenever they differ from HEAD — including **`no_patch`**, **`metrics_gate_skip`**, and Ollama/apply/validate failures — not only when a code patch lands. Set `0` to leave dashboard files unstaged. |
| `OLLAMA_METRICS_INTERVAL_FALLBACK_HOURS` | `24` | When `metrics.csv` is **empty** (first row), interval stats use resolutions in the last *N* hours ending at the loop time. |
| `OLLAMA_METRICS_GATE` | `0` | If `1`, **skip** the Ollama/backtest block when `metrics.csv` shows **no** net PnL drop ≥ `OLLAMA_METRICS_GATE_DROP` over the last `OLLAMA_METRICS_GATE_HOURS`. **Default `0`** so flat/misparsed PnL does not block the loop; set `1` for stress-only runs. If gate is on, **identical PnL** across samples in the window is treated as “no signal” and Ollama still runs. |
| `OLLAMA_TEMPERATURE` | `0.2` | Sampling temperature for **`scripts/ollama_codex_generate.py`** (`/api/generate`); plist may set explicitly. |
| `OLLAMA_AUTO_PUSH` | `0` in script | This repo’s **launchd** plist sets **`1`**: validated commits **`git push`** to `origin/main` (needs credentials). |
| `OLLAMA_METRICS_GATE_HOURS` | `4` | Lookback window (hours) for first-vs-last PnL in `metrics.csv`. |
| `OLLAMA_METRICS_GATE_DROP` | `0.20` | Trigger Ollama when `(last_pnl - first_pnl) ≤ -0.20` in that window. |
| `OLLAMA_LEADERBOARD_HOURS` | `336` | Lookback for `export_series_leaderboard.py` / `suggest_series_blacklist.py` (14 days). |
| `OLLAMA_BLACKLIST_MIN_TRADES` | `50` | Min resolved trades before a series is suggested for blacklist (win rate under 30%). |
| `OLLAMA_PROMPT_LB_HOURS` | `72` | Leaderboard lookback **embedded in the Ollama prompt** (HTML stripped to text). |
| `OLLAMA_AUTO_BLACKLIST` | `1` | If `1`, runs `apply_series_blacklist.py --merge` → `BLACKLIST_EXTRA_FILE`. Set `0` to disable. |
| `BLACKLIST_EXTRA_FILE` | `~/…/series_blacklist_auto.txt` | Output path; must match `strategy.blacklist_extra_file` in config. |
| `OLLAMA_AUTO_BLACKLIST_HOURS` / `MIN_TRADES` / `MAX_WR` | `168` / `20` / `0.25` | Thresholds for the auto-blacklist writer. |

Stronger coding models (if your machine can handle them): `ollama pull phi4` or `ollama pull mistral-small`, then `OLLAMA_MODEL=phi4 ./ollama_codex_loop.sh`.

### Micro-balance / robustness (`config.yaml` → `strategy`)

- **`passive_limit_quote`**: `mid` or `bid_plus_tick` reduces spread tax vs deep dip; unfilled = move on.
- **`empirical_weighting`**: `exponential` + **`empirical_exponential_halflife_bars`** weights recent bars more than a flat year of history.
- **`enable_model_prob_hard_cap`** / **`model_prob_hard_lo`** / **`model_prob_hard_hi`**: clip blended P (e.g. **0.02–0.98**) before edge/Kelly and early in the vol/emp blend path.
- **`execution.price_strategy`** / **`limit_offset_cents`**: optional spread squeeze (`passive` + `0` → **mid**; `1` → **bid+1¢**), merged into effective `passive_limit_quote`.
- **`blacklist_extra_file`**: newline-separated series merged into the runtime blacklist; `ollama_codex_loop.sh` can refresh it via **`apply_series_blacklist.py`** (avoids corrupting `config.yaml`).
- **Lottery / fills:** **`lottery_limit_price_mode: penny_jump`** places **best bid + 1¢** (capped vs ask) on the traded side; **`lottery_limit_max_price`** + **`lottery_penny_jump_apply_cap_bid_below`** keep cheap-leg tickets small while leaving expensive legs uncapped. Optional **`lottery_market_yes_band_*`** restricts lottery to an ATM YES band (40–60%) for churn-heavy books.
- **`max_buy_limit_price`:** drops any proposed BUY whose **limit price** exceeds the cap (e.g. **0.20** stops 99¢ NO steamrollers). Set **0** to disable.
- **`recovery:`** (top-level): merges into strategy — e.g. **`min_edge`**, **`adaptive_edge_max_extra: 0`**, **`force_min_buy_contracts_one: true`** so Kelly/budget rounding doesn’t leave only `Count: 0` lines when one contract would fit the buy budget.
- **Adaptive penalty reset:** there is no `calibration_data.json`; see **`docs/RESET_ADAPTIVE_STATE.md`** (SQLite + `adaptive_learning_enabled`).

## Scripts

| Script                                      | Purpose                                                |
| ------------------------------------------- | ------------------------------------------------------ |
| `python3 scripts/test_connection.py`        | Test Kalshi & Gemini connections                       |
| `python3 scripts/compare_prices.py`        | Quick live snapshot (prices only)                       |
| **`python3 scripts/compare_historical.py`** | **Evaluate contracts → BUY YES / BUY NO / SKIP** |
| `python3 scripts/compare_historical.py --show-config` | Print resolved config (Freqtrade-style) |
| `python3 scripts/compare_historical.py --ticker TICKER` | Evaluate only this ticker (Gnosis pick-one) |
| `python3 scripts/backtest.py`              | Backtest on historical data (`--mode real` or `synthetic`) |
| `python3 scripts/lookahead_analysis.py`   | Audit backtest for lookahead bias |
| `python3 scripts/hyperopt.py`             | Tune backtest params via grid search |
| `python3 scripts/export_series_leaderboard.py` | HTML per-series PnL / win rate from SQLite |
| `python3 scripts/suggest_series_blacklist.py` | Print commented `strategy.blacklist` hints (does not edit YAML) |
| `python3 scripts/apply_series_blacklist.py` | Write weak series to `strategy.blacklist_extra_file` (safe merge; no YAML append) |
| `streamlit run scripts/streamlit_research_app.py` | Research UI: preview + multi-strategy compare |

### Main evaluation: `scripts/compare_historical.py`

**In plain English:** The bot looks at how often the asset (e.g. Bitcoin) closed above or below a target price in a recent rolling window (default 90 days). It compares that "historical chance" to what the market is charging. If the market is underpricing (e.g. we think 30% chance, market says 15¢), we **BUY YES**. If overpricing, we **BUY NO**. If there's not enough of a bargain, we **SKIP**. With **`strategy.barbell_enabled: true`**, only **base** (high-P consensus) or **alpha** (cheap + large net edge) directional trades proceed; see **Barbell strategy** above.

```bash
python3 scripts/compare_historical.py --days 365 --markets 0 --edge 0.22   # 0 = all markets; edge flag overrides config min_edge for that run
python3 scripts/compare_historical.py --preview              # Show planned orders without placing them
python3 scripts/compare_historical.py --ticker KXBTC-25MAR2017  # Evaluate only this ticker (Gnosis pick-one)
python3 scripts/compare_historical.py --preview --compare-strategies vol_regime,default_threshold,strict_threshold  # Same model P, alternate recommend()
python3 scripts/compare_historical.py --execute              # Place real buy/sell orders (one-shot live run)
python3 scripts/compare_historical.py --execute --no-llm    # Live run, skip LLM analysis
python3 scripts/compare_historical.py --use-volatility      # Add option-style probability (log-normal model)
python3 scripts/compare_historical.py --reconcile-only      # Fill reconciliation diagnostics only (no eval/order placement)
python3 scripts/compare_historical.py --reconcile-diagnostics --preview --no-llm  # Include reconciliation counters in normal run logs
```

**Single production live run** — Evaluate markets and place real orders if the bot finds opportunities:

```bash
python3 scripts/compare_historical.py --execute --no-llm
```

Requires `trading.dry_run: false` in config.yaml.

**Run banner:** Every run prints **Mode** plus explicit `CLI flags: --execute=… --preview=… trading.dry_run=…`. If you expected live orders but see `PREVIEW` or `EVALUATE ONLY`, you are not calling the order APIs—use `--execute` (and `dry_run: false`). The macOS plist in `launchd/com.user.compare_historical.plist` includes `--execute` by default; a hand-typed command without it will not trade.

**Optional preview-then-execute gate:** `COMPARE_TWO_PHASE=1 ./scripts/compare_live_guarded.sh …your normal args…` runs a fast `--preview --no-llm` pass first and only then your full command if the log shows proposed trades / lottery lines. See `launchd/README.md` § *Live orders vs preview*.

**Strategy engine note:** You do not need to manually switch strategies during normal usage. Keep `strategy.strategy_engine: vol_regime` and treat `default_threshold` as a testing baseline only.

**Flags**

| Flag               | Description                                                                               |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `--days`           | Historical lookback (default: 365, or config `strategy.days_back`)                        |
| `--markets`        | Max markets per series (0 = all, default)                                                 |
| `--edge`           | Min "bargain size" to recommend buy—how much cheaper than fair we need (default: 15%)     |
| `--execute`        | Place real Kalshi orders for buys and sells (requires dry_run: false)                     |
| `--preview`        | **Preview mode**: print Kalshi/Gemini API calls we would make, without placing any orders |
| `--use-volatility` | Force-enable volatility model for this run (also enabled by default when `use_volatility_by_default: true`)    |
| `--verbose`, `-v`  | Show closes range vs threshold (explains historical P)                                    |
| `--no-llm`         | Skip Ollama analysis                                                                      |
| `--log`            | Log run to `~/Desktop/trading-bot-logs/eval_YYYYMMDDHH.log` (per-hour bucket, append within hour; or `--log DIR` for custom dir) |
| `--limit-analysis` | Print analysis of unfilled limit orders (why they didn't hit; informs limit_dip_pct tuning) |
| `--position-eval-analysis` | Print position evaluation stats: KEEP vs SELL outcomes, win rate by hist_p bucket |
| `--reconcile-diagnostics` | Print reconciliation counters per run (`seen / inserted / dup / skipped`) for Kalshi + Gemini fills |
| `--reconcile-only` | Run fill reconciliation pass only and exit (quick auditing command) |

**Fill reconciliation (Kalshi + Gemini):** Confirmed fills are written to SQLite as `predictions` (idempotent by `fill_id`). Gemini uses **paginated** fetch (`get_fills_paginated`) and flexible JSON parsing so fills aren’t dropped after the first 200 rows or under alternate response keys (`fills`, `trades`, `data`, …). Timestamps are normalized (ISO-8601, ms, or unix) and rows outside the lookback window are skipped. Reconciliation errors are logged instead of failing silently; `record_prediction_from_fill` logs SQLite insert failures.

Config: `strategy.strategy_engine`, `strategy.min_edge`, `strategy.barbell_enabled`, `strategy.barbell_*` (base/alpha thresholds and Kelly multipliers), `strategy.enable_easy_win`, `strategy.vol_blend_weight`, `strategy.use_volatility_by_default`, `strategy.use_ewma_vol`, `strategy.ewma_lambda`, `strategy.empirical_recent_days`, `strategy.empirical_recent_weight`, `strategy.empirical_use_rolling_window`, `strategy.enable_otm_smile_adjustment`, `strategy.otm_smile_strength`, `strategy.otm_smile_max_mult`, `strategy.uncertainty_edge_mult`, `strategy.stop_loss_pct`, `strategy.enable_strike_validation`, `strategy.liquidity_spread_bps_soft`, `strategy.liquidity_spread_bps_hard`, `strategy.enable_signal_model`, `strategy.signal_model_weights`, `strategy.use_sklearn_calibration`, `strategy.mc_enabled`, `strategy.mc_paths`, `strategy.mc_blend_weight`, and related keys in `config.yaml`.

### Intraday markets (5pm, 8pm, etc.)

**In plain English:** Some bets settle at a specific time (e.g. "Bitcoin price at 5pm ET today"). We support 5pm for crypto and 4pm for S&P 500. For other times (e.g. 8pm), we're extra cautious and only suggest buying when we're almost certain (≥90% chance).

The bot evaluates **all** crypto and S&P 500 intraday markets (e.g. "Bitcoin today at 8pm ET"), not only 5pm crypto / 4pm S&P:

| Resolution                             | Logic                                                                                                                     |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **5pm ET crypto** / **4pm ET S&P 500** | Standard `min_edge` (e.g. 10%) — same as before                                                                           |
| **Other intraday** (e.g. 8pm ET)       | Strict threshold: only BUY when P(win) ≥ 90% (or 80% with `--use-volatility` — vol model accounts for time & uncertainty) |
| **Non–crypto / non–S&P** with intraday | Skipped (no supported time)                                                                                               |

For "Bitcoin today at 8pm ET", we only recommend BUY when P(win) ≥ 90% (empirical only) or ≥ 80% with `--use-volatility` (vol model accounts for time-to-expiry and uncertainty).

**Intraday empirical:** For 5pm crypto and 4pm S&P 500, when `strategy.use_intraday_empirical: true` (default), the bot uses hourly bars at the resolution hour—not daily closes. **Crypto:** Gemini `1hr` candles when `YFINANCE_CRYPTO_GEMINI` is on, else Yahoo `1h`; falls back to daily closes if intraday fails. **S&P:** SPY `1h` (converted ×10) when `YFINANCE_SPY_AS_SP500` is on with `^GSPC` as a fallback; falls back to daily closes if intraday fails. Set `use_intraday_empirical: false` to use only the volatility model for intraday.

**Config:** `strategy.intraday_other_min_p_win: 0.90` (empirical only); `strategy.intraday_other_min_p_win_with_vol: 0.80` when `--use-volatility` (vol model accounts for uncertainty); `strategy.use_intraday_empirical: true` (hourly path for 5pm/4pm empirical P).

### Systematic edges (arbitrage, cross-platform hedging)

**In plain English:** Sometimes the market prices YES and NO in a way that guarantees profit if you buy both. For example, if YES costs 40¢ and NO costs 50¢ on the same bet, buying both costs 90¢—but one side always wins (payout $1), so you lock in 10¢. The bot looks for these "free money" situations before making regular bets.

| Edge                   | Description                                                                | When                                                        |
| ---------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------- |
| **Intra-exchange arb** | Buy YES and NO on the same market when `yes_ask + no_ask < 1 - fee`        | Same outcome, one platform (Kalshi or Gemini)               |
| **Inter-exchange arb** | Buy YES on one platform and NO on the other when total cost &lt; `1 - fee` | Matching Kalshi + Gemini crypto events (same threshold ±1%) |

**Config** (`config.yaml` → `strategy`):

```yaml
min_arb_fee: 0.02 # Min edge for arb (2%): only act when profit >= 2%
enable_intra_arb: true # Intra-exchange: buy YES+NO when sum < 1
enable_inter_arb: true # Inter-exchange: Kalshi↔Gemini cross-hedge
arb_inter_require_group_preflight: true # Require both inter-arb legs to pass paired balance preflight before execution
inter_arb_balance_aware_routing: true # Prefer route that places cheaper leg on lower-balance account
arb_inter_allow_single_leg_fallback: true # If pair funding fails, keep the funded highest-likelihood leg
```

_In plain English:_ `min_arb_fee` is the smallest profit we demand before acting. 0.02 = 2%: we only buy both sides when the guaranteed profit is at least 2¢ per $1 bet (after fees).

The bot checks for arb _before_ making normal BUY YES/NO suggestions. If it finds one, it places both sides of the bet and skips the directional recommendation.
During live execution, inter-exchange arb groups now run through a paired preflight: if either leg is unaffordable or missing, the entire `ARB_INTER` group is dropped before order placement so you do not get unintentionally legged from a one-sided fill attempt.
When balance-aware routing is enabled, inter-arb route selection also considers account balances and prefers the profitable route that maximizes paired affordability while nudging the cheaper leg toward the lower-balance account.
If `arb_inter_allow_single_leg_fallback` is enabled, inter-arb preflight can degrade to one funded leg (the highest implied-win leg) instead of dropping the group outright when paired funding is impossible.

### Limit orders & dip catching (Kalshi + Gemini)

**In plain English:** Instead of "buy at whatever price is available now," we say "buy only if the price hits X or better." Your order waits until someone matches it. We also place slightly _below_ the current price so that if the market dips briefly, we might get a better deal.

Both platforms place **limit orders** (good-til-cancel) so orders rest on the book until filled. To catch dips when the market temporarily drops:

- **`limit_dip_pct`** (default `0.05`): Place buy limits this much _below_ the current bid. E.g. `0.05` = 5% below bid — if bid is 40¢, we place at 38¢. Set to `0` to use the bid directly.
- **`limit_dip_pct_high_confidence`** (e.g. `0.15`): When P(win) ≥ 90%, use this larger dip instead—e.g. 15% below bid. Prioritizes catching bigger dips on high-certainty contracts (perfect for 10¢ limit on YES/NO we're confident will hit). Set to `0` to use `limit_dip_pct` for all.
- **`limit_dip_pct_price_decline`** (e.g. `0.10`): When the model suggests the contract price might decrease—e.g. volatility model is active, or 2+ days to expiry—use this larger dip (10%) to prioritize limit orders and catch price dips. Stacks with high-confidence when both apply.
- **DTE-confidence dip scaling** (`far_dte_*`): When resolution is more than ~1 hour away (`far_dte_threshold_days`, default 0.042 days) AND model confidence is high (P(win) >= `far_dte_min_confidence`, default 80%), limit dips scale proportionally with DTE: deeper limits catch the transient uncertainty-driven price drops common in far-dated contracts. Extra dip = `far_dte_dip_scale_per_day` × excess days, capped at `far_dte_max_extra_dip`. When P(win) >= 90%, the DTE extra dip gets an additional `far_dte_confidence_boost` multiplier. Threshold is intentionally low because crypto markets can resolve hourly.
- **`limit_order_mode`**: `fixed_pct` (dip below bid), `dynamic_tick` (penny-jump one tick above best bid, capped by model fair value), or `layered` (passive dip-based pricing below bid with staggered depth on re-buys and size-scaled participation for thinner books). `layered` is the recommended production mode.
- **`tick_offset`**: Number of ticks used for dynamic quote stepping (default `1`).
- **`max_inventory_skew_pct`**: Inventory-aware quote skew; same-side inventory lowers quote aggressiveness, opposite-side inventory can modestly increase it.
- **`min_stability_duration_seconds`**: Anti-sniping guard; requires recent mid-price stability before posting new BUY limits.
- **`min_order_book_depth`**: In normal modes, skip posting BUY limits when same-side depth is too thin; in `layered` mode, downscale order size instead of hard-skipping.
- **`cancel_on_vol_spike`**: When vol-spike safety triggers, cancel resting BUY limits and block new BUYs until regime normalizes.
- **`limit_order_ttl_minutes`**: Cancel and refresh stale resting BUY limits so order prices are based on current model state.
- **`cancel_all_on_regime_shift`**: On strong momentum/volatility regime shifts, cancel risky resting BUY limits before re-quoting.
- **Order review (cancel/update/keep)**: Before placing new orders, the bot reviews existing resting orders on Kalshi and Gemini. This lifecycle now lives in `execution_engine.py` (called by `compare_historical.py`) so execution sync is isolated from probability math. If we no longer recommend a trade, it cancels the order. If we still recommend it but at a higher limit price, it cancels and replaces — **except** within `near_expiry_keep_dte` (~30 min) of resolution, where existing resting BUY orders are kept to avoid losing a winning position by chasing pennies. If the existing order is same or better, it keeps it and skips placing a duplicate.
- **Intent vs execution accounting**: canceled buy intents are marked `cancelled` (excluded from realized PnL), and successful early sells are recorded as `sold` with FIFO realized PnL so performance stats better match exchange balance movement.
- **Fee-aware realized PnL**: settlement/early-exit PnL now applies maker-fee estimates (`*_maker_fee_rate`, `min_fee_per_order`) instead of reporting gross-only outcomes.
- **Confirmed-fill reconciliation**: a dedicated reconciliation pass ingests exchange fills (Kalshi + Gemini) and creates `predictions` from confirmed fills, rather than from order-accepted responses.
- **Capital-efficiency allocator**: when budget is constrained, order ranking now prioritizes expected net return per dollar-at-risk per hour-to-resolution (with edge as tie-breaker), improving account-level capital usage.

### Easy win (model and market aligned)

**In plain English:** When both our model and the market agree the outcome is likely (e.g. both say ~90%+), we can accept a smaller edge—as long as we still profit after fees. **Default is now `enable_easy_win: false`** (high-priced “aligned” YES was a major tail-risk on crypto). Turn on only if you accept steamroller risk.

**Config** (`config.yaml` → `strategy`):

```yaml
enable_easy_win: false # Default off — set true only if you want relaxed edge when aligned
min_edge_easy_win: 0.03 # If enabled, keep ≥ fee floor; must be <= min_edge
easy_win_min_p_win: 0.93 # Model must say P(win) >= this for easy-win
easy_win_min_market_p: 0.88 # Market must be in ballpark (yes_bid or no_bid >= this)
```

### Kelly criterion (position sizing)

**In plain English:** The bot uses the Kelly criterion (when `use_kelly: true`, the default) to size positions: higher P(win) and better odds → more contracts; lower P(win) (e.g. farther out-of-the-money) → fewer contracts. This automatically reduces size for strikes farther from the current price. Uses fractional Kelly (`kelly_fraction`, default **0.18** in defensive preset) for a more conservative bet size.

**Config** (`config.yaml` → `strategy`):

```yaml
use_kelly: true # Kelly criterion for position sizing — reduces size for farther-OTM contracts
kelly_fraction: 0.18 # Fractional Kelly (defensive default; raise only after stable forward PnL)
```

### Absolute winner (slam-dunk buys)

**In plain English:** When we have a near-certainty—e.g. buying at 1¢ when our model says 95%+ chance of winning—the bot will buy more than one contract, up to a configurable cap. It respects your USD balance on both Kalshi and Gemini: each platform's balance is checked before allocation and at order placement; insufficient funds cause the order to be skipped (not placed) with a log message.

**Config** (`config.yaml` → `strategy`):

```yaml
absolute_winner_min_p_win: 0.95 # P(win) must be ≥ 95% to treat as slam-dunk
absolute_winner_max_price: 0.05 # Price must be ≤ 5¢ (e.g. 1¢ on a near-certainty)
absolute_winner_max_pct_of_balance: 0.02 # Hard cap: 2% of balance per slam-dunk (avoid bankroll concentration)
absolute_winner_max_contracts: 500 # Cap contracts per order (balance still enforced at execution)
```

### Calibration and sell-and-rebuy

- **`dynamic_sell_threshold`** (default true): EV-optimal sell threshold for binary contracts. Instead of a fixed `sell_losing_pct`, the sell trigger is computed dynamically via `_ev_sell_threshold(sell_price, maker_fee_rate, min_fee_per_order)`: sell when `P(lose) > 1 − (sell_price − exit_fee)`. At high bids (90¢), threshold ≈ 11% (sell unless very confident); at low bids (20¢), threshold ≈ 81% (only sell if almost certain to lose). Entry price is a sunk cost and does not factor into the EV-optimal decision. When disabled, falls back to the fixed `sell_losing_pct`.
- **`sell_losing_pct`** (default 0.55): Fixed fallback sell threshold when `dynamic_sell_threshold` is false. Sell when P(lose) ≥ this.
- **`calibration_loss_weight`** (default 1.5): Weight losers higher in calibration so the bot learns more from mistakes. Bucket and direction-side win rates use weighted loses; higher values make the system more conservative.
- **`enable_sell_and_rebuy`** (default **false** in defensive preset): When selling a losing position (via dynamic or fixed threshold, or rec flip), optionally re-buy the same contract at the current price if the model still sees edge. Off by default to reduce fee churn on fast markets.
- **`p_lose_blend_market_max_weight`** (default 0.5): When computing P(lose) for sell decisions, blend our model with Kalshi/Gemini market odds. At expiry, up to this fraction comes from the market (0 = model only; 0.5 = 50/50).
- **`p_lose_blend_market_half_life_days`** (default 0.5): Half-life for market-weight decay; closer to resolution → more weight on market odds (more accurate near expiry).
- **`max_daily_loss_pct` / `max_daily_loss_cents`**: Circuit breaker for live trading. If realized prediction losses over the last 24h exceed thresholds, the bot blocks new BUY orders until performance recovers or limits are adjusted.
- **Position evaluation tracking**: When the bot evaluates a held position and decides to KEEP it (not sell), the evaluation is persisted to `position_evaluations` with full model context (hist_p, vol_p, p_lose, rec, entry/current price). When the market resolves, the outcome (win/lose) is linked automatically. Use `--position-eval-analysis` to see win rates for KEEP vs SELL decisions and identify if the KEEP threshold should be tightened.

### Local historical storage

**In plain English:** The bot stores data locally to speed runs and to build history for future backtesting and calibration. All data stays on your machine and is **not committed to git**.

Data is stored in SQLite at `~/.trading-bot-data/trading_bot.db` (configurable via `storage.data_dir` or `TRADING_BOT_DATA_DIR`).

#### Storage schema overview

| Table                | Purpose                                           | TTL      | Primary Key                                   |
| -------------------- | ------------------------------------------------- | -------- | --------------------------------------------- |
| **yf_cache**         | Daily closes (Gemini crypto + yfinance pipeline; fewer API calls) | 1 hour   | `(ticker, days_back)`                          |
| **yf_intraday_cache**| Hourly-at-resolution prices (5pm/4pm ET); Gemini or Yahoo for crypto | 1 hour   | `(ticker, days_back, resolution_hour_et)`      |
| **market_snapshots** | Per-evaluation snapshot (yes/no bids/asks/sizes, hist_p, rec)| None     | `id` (auto-increment)                          |
| **resolved_outcomes**| Contract settlements (for calibration)            | None     | `ticker`                                      |
| **predictions**      | Placed orders + outcomes + resolution prices (backtest, calibration, accuracy tracking) | None     | `id` (auto-increment)                          |
| **limit_orders**    | Limit orders placed + cancel reasons: `no_longer_rec`, `want_better_price`, `stale_ttl`, `regime_shift_guard`, `expired_unfilled` | None | `id` (auto-increment)                          |
| **markout_snapshots** | Post-fill price tracking for toxic flow detection (1/5/15 min markout) | None | `id` (auto-increment) |

#### Table structures (columns)

```
yf_cache:           ticker, days_back, closes_json, updated_at
yf_intraday_cache:  ticker, days_back, resolution_hour_et, closes_json, updated_at
market_snapshots:   id, platform, ticker, title, yf_ticker, ts, yes_bid, no_bid, hist_p, vol_p, rec, threshold, direction, edge
resolved_outcomes:  ticker, platform, title, resolution_ts, yes_settled, our_last_rec, our_last_hist_p, market_yes_at_last
predictions:        id, platform, ticker, order_id, side, hist_p, vol_p, entry_price, count, ts, outcome, pnl_cents, resolved_ts, resolution_price, ...
```

**TTL behavior:** `yf_cache` and `yf_intraday_cache` entries older than 1 hour are treated as stale; callers re-fetch via `yfinance_client` (Gemini + yfinance as implemented). Other tables are append-only. Full schema details: [docs/CODE_EXPLAINER.md](docs/CODE_EXPLAINER.md#local-storage-schema).

**Config** (`config.yaml` → `storage`):

```yaml
storage:
  enabled: true
  data_dir: ~/.trading-bot-data # Override with TRADING_BOT_DATA_DIR env
```

Disable with `storage.enabled: false` or `TRADING_BOT_STORAGE=false`.

### Probability model

**In plain English:** We estimate the chance of YES from multiple signals: empirical history, volatility model, live-vs-close context, liquidity cues, and market pricing. Those signals are blended into a final model probability used for recommendations.

- **Live price blend for S0 and safety**: The bot uses **both** signals—live price (via `get_snapshot`) and the last close (`closes[-1]`)—blended with slightly more weight on live (`live_price_weight`, default 0.6). So `current_price = 0.6×live + 0.4×closes[-1]` when both are available. This reflects sharp recent moves while anchoring to the last close. Falls back to `closes[-1]` if the snapshot API fails.
- **Empirical**: fraction of past days the close was above/below the threshold.
- **Empirical**: recency-weighted and rolling-window capable (`empirical_recent_days` default 90, `empirical_recent_weight`, `empirical_use_rolling_window`) with regime-sensitive weight boosts. The confidence-interval uncertainty penalty uses the rolling-window sample size (not full history) so edge requirements honestly reflect the data the model actually used.
- **Volatility**: log-normal model using `vol_model` (`auto|ewma|garch|sample`) and time-to-expiry. `auto` prefers GARCH(1,1) when available and safely falls back to EWMA/sample. OTM smile proxy (`enable_otm_smile_adjustment`) boosts tail estimates for far-OTM contracts. **Monte Carlo blend** (`mc_enabled`, `mc_paths: 10000`, `mc_blend_weight: 0.3`): runs N GBM paths (daily steps, Student-t innovations when configured) and blends with the analytical CDF — better captures non-log-normal tails and path-dependent effects. **Short-dated time floor**: `T` (time to expiry in years) now floors at `1e-6` (~30 seconds) instead of `0.01` years (~3.65 days), so sub-hour contracts correctly compute near-zero probability for large price moves.
- **Intraday EWMA override**: intraday contracts can use a faster EWMA decay (`intraday_ewma_lambda`, currently 0.90) while daily contracts keep `ewma_lambda` (0.94).
- **Volatility defense stack**: continuous vol-ratio scaling increases `min_edge`, deepens limit dips, and dampens size; extreme-volatility `defensive mode` switches to sells-only; stability window can auto-expand with volatility.
- **Cross-exchange sanity gate**: blocks stale-side buys when Kalshi/Gemini YES prices diverge beyond tolerance during sharp spot moves.
- **Dynamic blend**: empirical/vol blend weight adapts with sample size (`vol_dynamic_n0`) rather than relying on a fixed blend only. **DTE-aware decay**: when DTE is less than 1 day, the empirical weight linearly decays toward zero (`_dte_adjusted_empirical_weight`), so very short-dated contracts rely almost entirely on the volatility model (which conditions on current price and remaining time).
- **Signal model layer**: when enabled (`enable_signal_model`), normalized signals are passed through a weighted logistic scorer (`signal_model_weights`) to produce final P(YES).
- **Optional sklearn calibration**: when enough resolved predictions exist, isotonic or Platt calibration maps raw model probability to calibrated probability (`use_sklearn_calibration`).
- **DTE-aware strike guard**: `max_buy_yes_strike_above_pct` and `max_buy_no_strike_above_pct` now scale with `sqrt(DTE)` via `_dte_adjusted_strike_pct()`. For a 1-day contract the full configured percentage applies; for a 45-minute contract (~0.03 days), the effective limit shrinks to ~18% of the base value. This prevents the bot from buying YES on strikes that require unrealistic short-term price moves.
- **min_yes_price** (default 0.05): Skip BUY YES when the market prices YES below 5¢. If the market thinks there's a &lt;5% chance, we avoid those contracts—they're often illiquid, or our model may overestimate probability for far out-of-the-money strikes. The 5¢ floor always applies (no vol-model override).
- **min_no_price** (current config 0.02): Skip BUY NO when the market prices NO below this floor (i.e., YES near certainty). Same logic as `min_yes_price`: avoid unlikely bets unless the volatility model strongly disagrees (P(NO) ≥ 95%).
- **Anti-hallucination pipeline** (`models/anti_hallucination.py`): Five composable layers that run after model P(YES) is computed and before the recommendation engine, preventing the model from assigning 80%+ probability to contracts the market prices at 5%:
  1. **Bayesian update**: treats the market price as a Beta prior and the model prediction as evidence (`bayesian_market_strength: 1.5`). When model says 86% and market says 8%, the posterior lands at ~40% instead of blindly trusting 86%.
  2. **EWMA probability**: exponentially weights recent price data (14-day span) and blends with the model at a configurable weight (`ewma_prob_blend_weight: 0.30`, i.e. 30% EWMA / 70% model).
  3. **Volatility Z-score gate**: when short-window vol exceeds 2σ above baseline, pulls probability toward 0.50 (`vol_zscore_tail_mult: 0.3`).
  4. **Adaptive edge**: requires 1.8× base edge for cheap contracts (<15¢), scales with strike distance (`strike_dist_mult: 1.2`), and increases by 1.5× when the trend opposes the trade direction.
  5. **Regime filter (EMA gate)**: blocks BUY YES on "above" contracts when price is below the 20-period EMA (and vice versa for "below"). BUY NO is not blocked (it's trend-aligned).
  Config: `strategy.anti_hallucination.*` in `config.yaml`. Each layer can be independently toggled.
- **Alpha engine** (`models/alpha_engine.py`): Four composable modules applied after anti-hallucination and before order placement:
  1. **Order Book Imbalance (OBI)**: computes `(V_bid − V_ask) / (V_bid + V_ask)` from top-of-book depth. When OBI strongly favors the trade direction, required edge is reduced (bonus up to 4%); when opposed, edge is increased.
  2. **Markout PnL / Toxic flow detection**: tracks price movement 1/5/15 minutes after fills. If average markout for a ticker falls below −50 bps, the ticker is flagged as "toxic" and an extra 3% edge penalty is applied to avoid adverse selection.
  3. **Volatility-adjusted Kelly**: scales the Kelly fraction inversely with realized volatility (`f_adjusted = base × baseline_vol / current_vol`). In high-vol regimes, position size automatically shrinks (floor at 25% of base) to preserve capital.
  4. **Lead-lag signal**: precomputes short-term momentum of correlated leader assets (e.g. `^GSPC`→`KXINXU`). When a leader moves sharply, edge is adjusted ±2% for follower contracts.
  Config: `strategy.alpha_engine.*` in `config.yaml`. Each module can be independently toggled.
- **Kalshi & Gemini fees**: Maker fee = rate × P × (1−P) per contract (rounded up to 1¢). `kalshi_maker_fee_rate` and `gemini_maker_fee_rate` default to 0.0175 (1.75%). `min_edge` and displayed "Edge" are **net after fees**. Current defensive preset uses **`min_edge: 0.22`**; taker fills cost more—favor resting limits (maker) when possible.

### Quick snapshot: `compare_prices.py`

Live prices only. For BUY/NO/SKIP recommendations, run `scripts/compare_historical.py`:

```bash
python3 scripts/compare_prices.py
```

### Run every 5 minutes

Run the evaluation locally every 5 minutes for continuous monitoring.

**launchd (recommended on macOS):** Uses Apple's native scheduler. See [`launchd/README.md`](launchd/README.md) for full setup.

**Setup commands:**

```bash
cp launchd/com.user.compare_historical.plist ~/Library/LaunchAgents/
cp launchd/com.user.daily_performance_report.plist ~/Library/LaunchAgents/
# Edit plist if paths differ (Python, script, WorkingDirectory)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.daily_performance_report.plist
```

**Keep Mac awake** (otherwise macOS may sleep and pause the timer): run `caffeinate -di` in a separate terminal, or use [Amphetamine](https://apps.apple.com/app/amphetamine/id937984704).

**launchctl reference:**

| Purpose                            | Command                                                                     |
| ---------------------------------- | --------------------------------------------------------------------------- |
| View stdout (script output)        | `tail -f /tmp/compare_historical.out.log`                                   |
| View stderr (errors)               | `tail -f /tmp/compare_historical.err.log`                                   |
| View daily report stdout           | `tail -f /tmp/daily_performance_report.out.log`                             |
| View daily report stderr           | `tail -f /tmp/daily_performance_report.err.log`                             |
| Check if agent is loaded & running | `launchctl list \| grep compare_historical`                                 |
| Stop (unload)                      | `launchctl unload ~/Library/LaunchAgents/com.user.compare_historical.plist` |
| Stop daily report                  | `launchctl unload ~/Library/LaunchAgents/com.user.daily_performance_report.plist` |

The script runs every 5 minutes. With `RunAtLoad: false` (default), the first run happens ~5 minutes after loading. To run immediately once: `RunAtLoad: true` in plist and reload, or run manually:
`cd /path/to/trading-bot && .venv/bin/python3 scripts/compare_historical.py --days 365 --use-volatility --execute --no-llm`

The daily report job runs once per day and writes a dated text file (default: `~/.trading-bot-data/reports/performance_YYYYMMDD.txt`) plus **performance graphs** (PNG) showing per-trade P&L over time — green bars for wins, red for losses, with a cumulative P&L overlay. The report also includes a **bet accuracy** section showing the distance between the bot's bets and actual asset resolution prices — how far the underlying asset price was from the threshold at settlement. This data feeds back into model improvement.

**crontab (alternative):**

```bash
# crontab -e
# Add this line (adjust path to your project and venv):
*/15 * * * * cd /path/to/trading-bot && .venv/bin/python scripts/compare_historical.py --days 365 --no-llm >> eval.log 2>&1
```

**Simple loop (for testing):**

```bash
while true; do .venv/bin/python scripts/compare_historical.py; sleep 900; done
```

**Note:** `scripts/compare_prices` only shows prices. `scripts/compare_historical` with `--execute` is the production execution path for Kalshi + Gemini when `trading.dry_run` is `false`.

### Balance-aware order execution (Kalshi & Gemini)

**In plain English:** The bot never spends more than a set percentage of your account on a single order (e.g. 20%). If an order fails (e.g. insufficient funds), it logs a warning and continues—it doesn't stop the whole run. When balance is very low (below a threshold), instead of skipping the buy entirely, it places a small limit order at a price you can afford—hoping to get filled if the market dips.

Orders placed via `create_order_safe` are capped to your available balance and never cause the script to crash on 4xx (e.g. insufficient funds).

- **Balance cap:** Each order uses at most `max_pct_of_balance` (configurable in `config.yaml`, e.g. 20%) of your balance. Kalshi and Gemini each have separate balances; the bot fetches both and allocates budget per platform. Gemini orders use `place_order_safe` (which fetches balance and caps quantity before placing).
- **Low-balance limit fallback:** When balance is below `min_balance_for_limit_fallback_cents` (e.g. 300 = $3) and you can't afford the target order, the bot places a 1-contract limit at a price you can afford. Set to `0` to disable.
- **4xx resilience:** If the API returns 4xx (insufficient funds, invalid market, etc.), the script logs a warning and continues.
- **Config** (`config.yaml` → `trading`):

  ```yaml
  trading:
    dry_run: true # false = place real orders (compare_historical --execute)
    max_pct_of_balance: 0.20 # use at most 20% of balance per order (configurable)
    min_contracts: 1 # skip if capped count < this
    min_balance_for_limit_fallback_cents: 300 # when balance < this, place 1-contract limit instead of skipping; 0 = disabled
  ```

### Troubleshooting: 400 Bad Request on orders

If you see `HTTPError: 400 Client Error: Bad Request` when placing orders:

- **`invalid_price`**: Kalshi markets use a tick size (e.g. 1¢ for `linear_cent`). The bot rounds prices via `round_price_to_tick()` using the market's `price_ranges`. If you still see this, the market may use subpenny pricing (`deci_cent`); ensure you have the latest market data.
- **`insufficient_balance`**: On buys: balance is too low. On sells: no position in that ticker/side, or position was already sold. `create_order_safe` re-fetches positions before sell—stale data can cause this if positions changed between evaluation and execution.
- **Market closed**: The market may be paused or closed; Kalshi rejects orders for markets that are no longer tradeable.
- **Order size**: Ensure `count` is at least 1 and within exchange limits.
- **Error details**: The bot parses Kalshi error codes (`code`, `message`) and logs them. Check the log output for the specific code (e.g. `order rejected TICKER - invalid_price (round price to market tick)`).
- **Resilience**: Order failures are handled so one bad order does not stop the run. Failed orders are logged; the script continues with the next order.

**Gemini (4xx / 406):** The bot uses `place_order_safe`, which catches 4xx/406 and parses `reason`/`message` from the response. Common reasons:

- **`InsufficientFunds`** (406): Balance too low. Add funds or reduce order size.
- **`InvalidPrice`**: Price must be valid for the market (e.g. 0–1, appropriate tick). Round to 0.01 steps if needed.
- **`InvalidQuantity`**: Quantity must be a positive integer.
- **`InvalidSymbol`**: Contract/symbol not found or no longer tradeable.
- **`MarketNotOpen`**: Market closed or paused. See [Gemini error codes](https://docs.gemini.com/error-codes).
- **`MissingAccounts`**: You're using a Master API key (`master-*`). Add `GEMINI_ACCOUNT=your_account_nickname` to `.env` (e.g. `Primary`), or create an account-scoped key at [Settings → API](https://exchange.gemini.com/settings/api).

### Configuring tickers

Add `yfinance_tickers` in `config.yaml` to map Kalshi series to Yahoo Finance tickers:

```yaml
yfinance_tickers:
  KXBTCD: "BTC-USD" # Bitcoin
  KXETHD: "ETH-USD" # Ethereum
  KXSOLD: "SOL-USD" # Solana
  KXINXU: "^GSPC" # S&P 500
  KXXRPD: "XRP-USD" # XRP (Ripple)
```

Tickers: `BTC-USD`, `ETH-USD`, `^GSPC`, `SPY`, etc. See [yfinance](https://github.com/ranaroussi/yfinance).

**Gemini Exchange vs Yahoo (underlying spot, not prediction markets):** For the tickers above that map to Gemini USD pairs (`BTC-USD`, `ETH-USD`, `SOL-USD`, `XRP-USD`), `get_snapshot` / `get_aggs` / `get_intraday_aggs_at_hour` try **[Gemini public REST](https://docs.gemini.com/rest-api/market-data)** first (no API key). S&P 500 data uses SPY `1h`/`1d` (converted ×10) combined with `^GSPC` when `YFINANCE_SPY_AS_SP500` is enabled (default on); set it to 0/false/no to rely on `^GSPC` only. Set `YFINANCE_CRYPTO_GEMINI=0` to use Yahoo (+ CoinGecko fallback for BTC/ETH/SOL) only. `GEMINI_SPOT_API_BASE` defaults to `https://api.gemini.com`. This is separate from **Gemini Prediction Markets** below (contracts/events API).

### Gemini Prediction Markets (crypto only)

The comparison scripts also fetch [Gemini Prediction Markets](https://docs.gemini.com/prediction-markets/markets) for **crypto only** (BTC, ETH, SOL, XRP)—**daily/weekly** events via `category=crypto`. S&P 500 is not included. The events API is public; no API key required for listing. Optional env vars:

- `GEMINI_USE_SANDBOX=true` - use sandbox API
- `GEMINI_PREDICTION_BASE_URL` - override base URL

**Troubleshooting:** If you see `[Gemini] No events fetched` with a proxy or 403 error, try `NO_PROXY=api.gemini.com,*.gemini.com` or run without proxy (e.g. `env -u HTTP_PROXY -u HTTPS_PROXY`).

**Relevant markets (Gemini Exchange UI):**

| Asset        | Type              | URL                                                                                                                                                    |
| ------------ | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Bitcoin      | Daily             | [BTC price on March 16](https://exchange.gemini.com/predictions/BTC2603162100/btc-price-on-march-16?categoryPath=crypto%2Cdaily&status=active)         |
| Bitcoin      | Hourly            | [BTC price today at 1am EDT](https://exchange.gemini.com/predictions/BTC2603160500/btc-price-today-at-1am-edt?categoryPath=crypto%2Ccrypto_bitcoin%2Chourly&status=active) |
| Ether        | Daily             | [ETH price on March 16](https://exchange.gemini.com/predictions/ETH2603162100/eth-price-on-march-16?categoryPath=crypto%2Cdaily&status=active)         |
| Ether        | Hourly            | [ETH price today at 1am EDT](https://exchange.gemini.com/predictions/ETH2603160500/eth-price-today-at-1am-edt?categoryPath=crypto%2Chourly&status=active) |
| Solana       | Weekly (no daily) | [SOL price on March 20](https://exchange.gemini.com/predictions/SOL2603202100/sol-price-on-march-20?categoryPath=crypto%2Ccrypto_solana&status=active) |
| Solana       | Hourly            | [SOL price today at 1am EDT](https://exchange.gemini.com/predictions/SOL2603160500/sol-price-today-at-1am-edt?categoryPath=crypto%2Chourly&status=active) |
| XRP (Ripple) | Daily             | [XRP price on March 16](https://exchange.gemini.com/predictions/XRP2603162100/xrp-price-on-march-16?categoryPath=crypto%2Cdaily&status=active)         |
| XRP (Ripple) | Hourly            | [XRP price today at 1am EDT](https://exchange.gemini.com/predictions/XRP2603160500/xrp-price-today-at-1am-edt?categoryPath=crypto%2Chourly&status=active) |

**Note:** With `GEMINI_API_KEY` and `GEMINI_API_SECRET` set, `scripts/compare_historical.py --execute` places limit orders on both Kalshi and Gemini. Use `--preview` to see planned orders without placing them.

### References & strategy foundations

The evaluation logic draws on econometric and financial time-series literature:

| Source                                                                                                                                                                 | Relevance                                                                                      |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [Taylor (2005)](https://www.lancaster.ac.uk/staff/afasjt/apdvp_contents.pdf) _Asset Price Dynamics, Volatility, and Prediction_                                        | Volatility forecasting works better than directional; GARCH/implied vol improve P(above/below) |
| [Campbell, Lo, MacKinlay (1997)](https://www.nrc.gov/docs/ml1208/ML12088A329.pdf) _The Econometrics of Financial Markets_                                              | Event-study methodology; market model reduces variance of abnormal returns                     |
| [Tsay](https://cpb-us-w2.wpmucdn.com/blog.nus.edu.sg/dist/0/6796/files/2017/03/analysis-of-financial-time-series-copy-2ffgm3v.pdf) _Analysis of Financial Time Series_ | GARCH, ARIMA, structural breaks for volatility and regime modeling                             |

See [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) for prioritized enhancements (GARCH, implied vol, market-model adjustment, etc.).
