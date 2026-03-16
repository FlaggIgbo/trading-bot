# Kalshi Prediction Market Trading Bot

A bot that connects to the [Kalshi API](https://docs.kalshi.com/) to monitor selected prediction markets and help you decide when to trade.

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
     - KXETHD # Ethereum daily
     - KXSOLD # Solana
     - KXINXU # S&P 500
     - KXXRPD # XRP daily
   ```

   Or: `MARKETS=KXBTCD,KXETHD,KXSOLD,KXINXU,KXXRPD`

Run scripts from the project root (e.g. `python scripts/bot.py`). Scripts add the project root to `sys.path` so imports work correctly.

## Running

```bash
python scripts/bot.py
```

The bot will:

- Connect using your API credentials
- Fetch open markets for each series/ticker you configured
- Log prices (YES/NO bids) and status
- Apply strategy rules when you add them in `config.yaml` / `scripts/bot.py`

## Kalshi API Overview

- **Auth**: RSA-PSS signature with your private key. Required headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.
- **Endpoints**:
  - Public: `/markets`, `/markets/{ticker}`, `/markets/{ticker}/orderbook` (no auth for market data)
  - Authenticated: `/portfolio/balance`, `/portfolio/orders`, `POST /portfolio/orders` for trading
- **URLs**:
  - Production: `https://api.elections.kalshi.com/trade-api/v2`
  - Demo: `https://demo-api.kalshi.co/trade-api/v2`

See [Kalshi API docs](https://docs.kalshi.com/welcome) and [Making your first request](https://docs.kalshi.com/getting_started/making_your_first_request).

## Project structure

```
trading-bot/
├── scripts/                 # Runnable entry points (run from project root)
│   ├── backtest.py          # Backtest: real (resolved preds) or synthetic (snapshots) mode
│   ├── bot.py               # Main polling bot
│   ├── compare_historical.py # Evaluate contracts → BUY YES / NO / SKIP
│   ├── compare_prices.py    # Quick live price snapshot
│   ├── debug_ollama.py      # Debug Ollama LLM integration (run when analysis fails)
│   └── test_connection.py   # Test API connections
├── docs/                    # Documentation
│   ├── CODE_EXPLAINER.md    # Step-by-step flow, file-by-file reference, code pointers
│   └── IMPROVEMENTS.md      # Roadmap and backlog
├── launchd/                 # macOS scheduling (run every 15 min)
│   ├── README.md            # Setup instructions
│   └── com.user.compare_historical.plist
├── config.py                # Config loader (yaml + env overrides)
├── config.yaml              # Markets, strategy, trading settings
├── kalshi_client.py         # Kalshi API client
├── gemini_client.py         # Gemini Prediction Markets client
├── yfinance_client.py       # Price data (yfinance + CoinGecko fallback)
├── storage.py               # Local SQLite storage (yf cache, market snapshots, resolved outcomes)
├── ollama_analysis.py       # Ollama LLM analysis integration
├── pyproject.toml           # Ruff (linter/formatter) config
├── .pre-commit-config.yaml  # Ruff + Prettier on git push
└── .env                     # Credentials (from .env.example)
```

All scripts in `scripts/` add the project root to the Python path and load `.env` from the project root, so run them from the project directory: `python scripts/compare_historical.py`.

**Code walkthrough:** For a step-by-step flow and file-by-file reference with code pointers, see [docs/CODE_EXPLAINER.md](docs/CODE_EXPLAINER.md).

### Development (linting, formatting)

- **Ruff** (Python): `pip install ruff` then `ruff check . --fix && ruff format .`
- **Pre-commit** (runs on `git push`): `pip install pre-commit && pre-commit install --hook-type pre-push`
- **Prettier** (YAML/JSON/MD): `npm install` then `npx prettier --write "*.yaml" "*.json" "*.md"` (or use pre-commit)

## Improvement roadmap

See [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) for the full roadmap. Implemented: Kelly criterion, whitelist/blacklist, file logging, volatility-based probability (per-contract resolution time; volatility-aware intraday threshold 80%), systematic edges (intra- and inter-exchange arbitrage), XRP support, low-balance limit-order fallback, Ollama LLM analysis (llama3.2:1b fallback), recommendation constants, order review (cancel/update/keep open limit orders on Kalshi + Gemini), Kalshi/Gemini 4xx error handling (parse error codes, round prices to valid tick), **local historical storage** (yfinance cache, market snapshots, resolved-outcomes schema for backtesting and calibration), **BUY YES/NO preference** (when both have edge, prefer whichever yields larger net return), **contradictory-position avoidance** (skip BUYs that would conflict with existing positions on the same underlying), **backtest script** (`scripts/backtest.py`), **Gemini balance checking** (get_balance, place_order_safe caps by balance like Kalshi), **per-platform budget allocation** (Kalshi and Gemini balances allocated separately), **Gemini nonce fix**, **Gemini crypto events** (daily/weekly for BTC, ETH, SOL, XRP; hourly category may return 400—we gracefully skip and use crypto only), **Gemini position sell (losers)** (evaluates Gemini positions like Kalshi; sells when P(lose) ≥ 55%, rec flip, or rule violation).

## Contract evaluation workflow

**Goal:** Use historical price data to estimate fair probability, then compare to Kalshi/Gemini market prices → recommend **BUY YES** / **BUY NO** / **SKIP**.

**Example in plain English:** Imagine a bet: "Will Bitcoin be above $81,250 on March 16?" The bot looks at the last year of Bitcoin prices and finds that Bitcoin closed above that level only 5% of the time. So the "fair" chance of YES is about 5%. If the market is charging 40¢ for YES (i.e. people think there's a 40% chance), the market is overpricing YES. The bot would recommend **BUY NO** (bet against it) or **SKIP** if the edge isn't big enough.

Run `scripts/compare_historical.py` every **15 minutes** locally for continuous evaluation. See [Cron setup](#run-every-15-minutes) below.

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
        B --> C[Fetch yfinance closes — cache when storage on]
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
        L[Kalshi: get_positions | Gemini: get_positions] --> M[Parse: position_fp (Kalshi) or outcome/qty (Gemini)]
        L2[Gemini get_positions] --> M2[Map symbol→market; parse outcome, qty]
        M --> N{Cached or fetch market}
        N --> O[Compute P lose, rec with vol blend]
        O --> P{Sell trigger?}
        P -->|LIKELY_LOSE ≥55%| Q[pending_sells]
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
  │ Load config  │────▶│ Fetch yfinance       │────▶│ For each series (KXBTCD, KXETHD,  │
  │ markets,     │     │ closes (cache 1h     │     │ KXSOLD, KXINXU, KXXRPD):         │
  │ yf_tickers   │     │ when storage on)     │     │   • Get Kalshi open markets       │
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
  │       - LIKELY_LOSE:     We now think the bet will probably lose (≥55% chance)│
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

## Scripts

Scripts send their output to a local [Ollama](https://ollama.com) model for trading opinions. Install and run Ollama, then pull a model:

```bash
# Install Ollama from https://ollama.com, then:
ollama pull llama3.2
```

When you run `scripts/compare_prices.py`, `scripts/compare_historical.py`, or `scripts/test_connection.py`, the script prints verbose data as usual, then adds an **OLLAMA ANALYSIS** section with the model’s take (e.g. mispriced contracts, risks, suggestions). Uses free local models (`llama3.2`, `mistral`, `phi3`, etc.).

- **Skip LLM** for cron or non-interactive runs: add `--no-llm`
- **Override model**: `OLLAMA_MODEL=qwen2.5:3b python3 scripts/compare_prices.py`
- **Long output truncation**: Very long runs are truncated so the model fits them in context (keeps head + tail). Override via `OLLAMA_MAX_CHARS=25000` (default), `OLLAMA_HEAD_CHARS=8000`, `OLLAMA_TAIL_CHARS=12000`

If Ollama isn’t running or the model is missing, scripts still run and finish without analysis.

## Scripts

| Script                                      | Purpose                                                |
| ------------------------------------------- | ------------------------------------------------------ |
| `python3 scripts/test_connection.py`        | Test Kalshi & Gemini connections                       |
| `python3 scripts/compare_prices.py`        | Quick live snapshot (prices only)                       |
| **`python3 scripts/compare_historical.py`** | **Evaluate contracts → BUY YES / BUY NO / SKIP** |
| `python3 scripts/backtest.py`              | Backtest on historical data (`--mode real` or `synthetic`) |

### Ollama LLM analysis (local, free)

All three scripts send their raw output to a local [Ollama](https://ollama.com) LLM for a brief analysis: which contracts look mispriced, risks, and suggestions.

1. Install & run Ollama: [ollama.com](https://ollama.com)
2. Pull a model: `ollama pull llama3.2` (or `mistral`, `phi3`, `qwen2.5:3b`)
3. Run any script; the last output is Ollama's opinion

To skip LLM analysis (e.g. for cron): `python3 scripts/compare_historical.py --no-llm`

### Main evaluation: `scripts/compare_historical.py`

**In plain English:** The bot looks at how often the asset (e.g. Bitcoin) closed above or below a target price in the past year. It compares that "historical chance" to what the market is charging. If the market is underpricing (e.g. we think 30% chance, market says 15¢), we **BUY YES**. If overpricing, we **BUY NO**. If there's not enough of a bargain, we **SKIP**.

```bash
python3 scripts/compare_historical.py --days 365 --markets 0 --edge 0.15   # 0 = all markets
python3 scripts/compare_historical.py --preview              # Show planned orders without placing them
python3 scripts/compare_historical.py --execute              # Place real buy/sell orders (one-shot live run)
python3 scripts/compare_historical.py --execute --no-llm    # Live run, skip LLM analysis
python3 scripts/compare_historical.py --use-volatility      # Add option-style probability (log-normal model)
```

**Single production live run** — Evaluate markets and place real orders if the bot finds opportunities:

```bash
python3 scripts/compare_historical.py --execute --no-llm
```

Requires `trading.dry_run: false` in config.yaml.

**Flags**

| Flag               | Description                                                                               |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `--days`           | Historical lookback (default: 365, or config `strategy.days_back`)                        |
| `--markets`        | Max markets per series (0 = all, default)                                                 |
| `--edge`           | Min "bargain size" to recommend buy—how much cheaper than fair we need (default: 15%)     |
| `--execute`        | Place real Kalshi orders for buys and sells (requires dry_run: false)                     |
| `--preview`        | **Preview mode**: print Kalshi/Gemini API calls we would make, without placing any orders |
| `--use-volatility` | Add volatility-based probability (option-style log-normal model) alongside empirical P    |
| `--verbose`, `-v`  | Show closes range vs threshold (explains historical P)                                    |
| `--no-llm`         | Skip Ollama analysis                                                                      |
| `--log`            | Log run to `~/Desktop/trading-bot-logs/eval_YYYYMMDD.log` (or `--log DIR` for custom dir) |

Config: `strategy.min_edge`, `strategy.min_yes_price` (skip BUY YES when market YES &lt; 5¢), `strategy.min_no_price` (skip BUY NO when market NO &lt; 5¢), `strategy.days_back`, `strategy.days_to_expiry`, `strategy.limit_dip_pct`, `strategy.limit_dip_pct_high_confidence` (15% when P(win) ≥ 90%), `strategy.vol_blend_weight` (blend empirical + vol; 0.5 = equal), `strategy.use_ewma_vol`, `strategy.ewma_lambda`, `strategy.intraday_other_min_p_win`, `strategy.intraday_other_min_p_win_with_vol`, `strategy.use_kelly`, `strategy.min_arb_fee`, `strategy.enable_intra_arb`, `strategy.enable_inter_arb`, etc. in `config.yaml`.

### Intraday markets (5pm, 8pm, etc.)

**In plain English:** Some bets settle at a specific time (e.g. "Bitcoin price at 5pm ET today"). We support 5pm for crypto and 4pm for S&P 500. For other times (e.g. 8pm), we're extra cautious and only suggest buying when we're almost certain (≥90% chance).

The bot evaluates **all** crypto and S&P 500 intraday markets (e.g. "Bitcoin today at 8pm ET"), not only 5pm crypto / 4pm S&P:

| Resolution                             | Logic                                                                                                                     |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **5pm ET crypto** / **4pm ET S&P 500** | Standard `min_edge` (e.g. 10%) — same as before                                                                           |
| **Other intraday** (e.g. 8pm ET)       | Strict threshold: only BUY when P(win) ≥ 90% (or 80% with `--use-volatility` — vol model accounts for time & uncertainty) |
| **Non–crypto / non–S&P** with intraday | Skipped (no supported time)                                                                                               |

For "Bitcoin today at 8pm ET", we only recommend BUY when P(win) ≥ 90% (empirical only) or ≥ 80% with `--use-volatility` (vol model accounts for time-to-expiry and uncertainty). Uses daily closes as a proxy (no intraday data yet).

**Config:** `strategy.intraday_other_min_p_win: 0.90` (empirical only); `strategy.intraday_other_min_p_win_with_vol: 0.80` when `--use-volatility` (vol model accounts for uncertainty).

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
```

_In plain English:_ `min_arb_fee` is the smallest profit we demand before acting. 0.02 = 2%: we only buy both sides when the guaranteed profit is at least 2¢ per $1 bet (after fees).

The bot checks for arb _before_ making normal BUY YES/NO suggestions. If it finds one, it places both sides of the bet and skips the directional recommendation.

### Limit orders & dip catching (Kalshi + Gemini)

**In plain English:** Instead of "buy at whatever price is available now," we say "buy only if the price hits X or better." Your order waits until someone matches it. We also place slightly _below_ the current price so that if the market dips briefly, we might get a better deal.

Both platforms place **limit orders** (good-til-cancel) so orders rest on the book until filled. To catch dips when the market temporarily drops:

- **`limit_dip_pct`** (default `0.05`): Place buy limits this much _below_ the current bid. E.g. `0.05` = 5% below bid — if bid is 40¢, we place at 38¢. Set to `0` to use the bid directly.
- **`limit_dip_pct_high_confidence`** (e.g. `0.15`): When P(win) ≥ 90%, use this larger dip instead—e.g. 15% below bid. Prioritizes catching bigger dips on high-certainty contracts (perfect for 10¢ limit on YES/NO we're confident will hit). Set to `0` to use `limit_dip_pct` for all.
- **`limit_dip_pct_price_decline`** (e.g. `0.10`): When the model suggests the contract price might decrease—e.g. volatility model is active, or 2+ days to expiry—use this larger dip (10%) to prioritize limit orders and catch price dips. Stacks with high-confidence when both apply.
- **Order review (cancel/update/keep)**: Before placing new orders, the bot reviews existing resting orders on Kalshi and Gemini. If we no longer recommend a trade, it cancels the order. If we still recommend it but at a higher limit price, it cancels and replaces. If the existing order is same or better, it keeps it and skips placing a duplicate.

### Absolute winner (slam-dunk buys)

**In plain English:** When we have a near-certainty—e.g. buying at 1¢ when our model says 95%+ chance of winning—the bot will buy more than one contract, up to a configurable cap. It respects your USD balance on both Kalshi and Gemini: each platform's balance is checked before allocation and at order placement; insufficient funds cause the order to be skipped (not placed) with a log message.

**Config** (`config.yaml` → `strategy`):

```yaml
absolute_winner_min_p_win: 0.95 # P(win) must be ≥ 95% to treat as slam-dunk
absolute_winner_max_price: 0.05 # Price must be ≤ 5¢ (e.g. 1¢ on a near-certainty)
absolute_winner_max_pct_of_balance: 0.50 # Willing to spend up to 50% of balance on one absolute winner
absolute_winner_max_contracts: 500 # Cap contracts per order (balance still enforced at execution)
```

### Local historical storage

**In plain English:** The bot stores data locally to speed runs and to build history for future backtesting and calibration. All data stays on your machine and is **not committed to git**.

| What we store                | Purpose                                                                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **yfinance closes cache**    | Daily closes cached for 1 hour → fewer API calls, faster runs                                                                  |
| **Market snapshots**         | For each evaluation: yes/no bids, our hist_p, vol_p, recommendation, threshold, direction. Builds time series for backtesting. |
| **Resolved outcomes schema** | Ready for when we record contract settlements → calibrate our predictions (e.g. "when we said 70% YES, did we win?")           |

**Config** (`config.yaml` → `storage`):

```yaml
storage:
  enabled: true
  data_dir: ~/.trading-bot-data # Override with TRADING_BOT_DATA_DIR env
```

Disable with `storage.enabled: false` or `TRADING_BOT_STORAGE=false`.

### Probability model

**In plain English:** We estimate the chance of YES two ways: (1) **Empirical** = "In the last year, how often did the price end above (or below) the target?" and (2) **Volatility** = Using how much the price typically swings, we calculate the odds. We use the higher of the two when both are available.

- **Empirical**: fraction of past days the close was above/below the threshold.
- **Volatility** (with `--use-volatility`): log-normal model using historical annualized vol and time-to-expiry. Time-to-expiry is parsed per contract from the title (e.g. "tomorrow at 5pm ET", "March 16 at 5pm") and converted to UTC; falls back to `strategy.days_to_expiry` when parsing fails. When both empirical and volatility P are available, uses a weighted blend (`vol_blend_weight`, default 0.5) or `max(empirical, volatility_probability)` if no blend is set. EWMA volatility (`use_ewma_vol: true`, `ewma_lambda: 0.94`) adapts σ to recent regime.
- **min_yes_price** (default 0.05): Skip BUY YES when the market prices YES below 5¢. If the market thinks there's a &lt;5% chance, we avoid those contracts—they're often illiquid, or our model may overestimate probability for far out-of-the-money strikes.
- **min_no_price** (default 0.05): Skip BUY NO when the market prices NO below 5¢ (i.e., YES above ~95¢). Same logic as `min_yes_price`: avoid unlikely bets unless the volatility model strongly disagrees (P(NO) ≥ 95%).
- **Kalshi & Gemini fees**: Maker fee = rate × P × (1−P) per contract (rounded up to 1¢). `kalshi_maker_fee_rate` and `gemini_maker_fee_rate` default to 0.0175 (1.75%). `min_edge` and displayed "Edge" are **net after fees** — so 1¢ profit per contract is fine (`min_edge: 0.01`).

### Quick snapshot: `compare_prices.py`

Live prices only. For BUY/NO/SKIP recommendations, run `scripts/compare_historical.py`:

```bash
python3 scripts/compare_prices.py
```

### Run every 15 minutes

Run the evaluation locally every 15 minutes for continuous monitoring.

**launchd (recommended on macOS):** Uses Apple's native scheduler. See [`launchd/README.md`](launchd/README.md) for full setup.

**Setup commands:**

```bash
cp launchd/com.user.compare_historical.plist ~/Library/LaunchAgents/
# Edit plist if paths differ (Python, script, WorkingDirectory)
launchctl load ~/Library/LaunchAgents/com.user.compare_historical.plist
```

**Keep Mac awake** (otherwise macOS may sleep and pause the timer): run `caffeinate -di` in a separate terminal, or use [Amphetamine](https://apps.apple.com/app/amphetamine/id937984704).

**launchctl reference:**

| Purpose                            | Command                                                                     |
| ---------------------------------- | --------------------------------------------------------------------------- |
| View stdout (script output)        | `tail -f /tmp/compare_historical.out.log`                                   |
| View stderr (errors)               | `tail -f /tmp/compare_historical.err.log`                                   |
| Check if agent is loaded & running | `launchctl list \| grep compare_historical`                                 |
| Stop (unload)                      | `launchctl unload ~/Library/LaunchAgents/com.user.compare_historical.plist` |

The script runs every 15 minutes. With `RunAtLoad: false` (default), the first run happens ~15 minutes after loading. To run immediately once: `RunAtLoad: true` in plist and reload, or run manually:
`cd /path/to/trading-bot && .venv/bin/python3 scripts/compare_historical.py --days 365 --use-volatility --execute --no-llm`

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

**Note:** `scripts/compare_prices` only shows prices. `scripts/compare_historical` with `--execute` places real orders on Kalshi and Gemini when `trading.dry_run` is `false`. `scripts/bot.py` can also place orders when configured (see below).

### Balance-aware order execution (Kalshi & Gemini)

**In plain English:** The bot never spends more than a set percentage of your account on a single order (e.g. 20%). If an order fails (e.g. insufficient funds), it logs a warning and continues—it doesn't stop the whole run. When balance is very low (below a threshold), instead of skipping the buy entirely, it places a small limit order at a price you can afford—hoping to get filled if the market dips.

Orders placed via `create_order_safe` are capped to your available balance and never cause the script to crash on 4xx (e.g. insufficient funds).

- **Balance cap:** Each order uses at most `max_pct_of_balance` (configurable in `config.yaml`, e.g. 20%) of your balance. Kalshi and Gemini each have separate balances; the bot fetches both and allocates budget per platform. Gemini orders use `place_order_safe` (which fetches balance and caps quantity before placing).
- **Low-balance limit fallback:** When balance is below `min_balance_for_limit_fallback_cents` (e.g. 100 = $1) and you can't afford the target order, the bot places a 1-contract limit at a price you can afford. Set to `0` to disable.
- **4xx resilience:** If the API returns 4xx (insufficient funds, invalid market, etc.), the script logs a warning and continues.
- **Config** (`config.yaml` → `trading`):

  ```yaml
  trading:
    dry_run: true # false = place real orders (scripts/bot.py)
    max_pct_of_balance: 0.20 # use at most 20% of balance per order (configurable)
    min_contracts: 1 # skip if capped count < this
    min_balance_for_limit_fallback_cents: 100 # when balance < this, place 1-contract limit instead of skipping; 0 = disabled
  ```

Enable execution in `scripts/bot.py` by setting `buy_yes_below` / `buy_no_below` in `strategy` and `dry_run: false`. Add funds manually as needed.

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
