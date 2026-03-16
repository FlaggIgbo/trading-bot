# Kalshi Prediction Market Trading Bot

A bot that connects to the [Kalshi API](https://docs.kalshi.com/) to monitor selected prediction markets and help you decide when to trade.

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

   | Variable | Description | Where to get it |
   |----------|-------------|-----------------|
   | `KALSHI_API_KEY_ID` | Your API Key ID (UUID format) | [Account Settings → API Keys](https://kalshi.com/account/profile) |
   | `KALSHI_PRIVATE_KEY_PATH` | Path to your `.key` file **or** | Download when you create an API key |
   | `KALSHI_PRIVATE_KEY` | Raw PEM content (alternative to path) | Same key, as string with `\n` for newlines |
   | `KALSHI_USE_DEMO` | `true` for demo, `false` for production | Demo: [demo.kalshi.com](https://demo.kalshi.com) |

   For **demo**, use `https://demo-api.kalshi.co` and create keys at [demo.kalshi.com](https://demo.kalshi.com/account/profile).

3. **Select markets to watch**

   Edit `config.yaml` or set the `MARKETS` env var:

   ```yaml
   markets:
     - KXHIGHNY      # e.g. "Highest temp in NYC today"
     - KXBTC        # Bitcoin price series
     - KALSHI_YES   # Election markets
   ```

   Or: `MARKETS=KXHIGHNY,KXBTC,KALSHI_YES`

## Project structure

```
trading-bot/
├── config.py           # Config loader (config.yaml + env overrides)
├── config.yaml         # Markets, strategy, trading settings
├── kalshi_client.py    # Kalshi API client
├── gemini_client.py   # Gemini Prediction Markets API client
├── yfinance_client.py # Price data (yfinance + CoinGecko fallback)
├── ollama_analysis.py  # Ollama LLM analysis integration
├── scripts/            # Runnable entry points
│   ├── bot.py          # Main bot loop (watches markets, applies strategy)
│   ├── compare_historical.py  # Evaluate contracts → BUY YES / BUY NO / SKIP
│   ├── compare_prices.py      # Quick live price snapshot
│   └── test_connection.py     # Test API connections
├── .env                # Credentials (not in git)
└── requirements.txt
```

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
│   ├── bot.py               # Main polling bot
│   ├── compare_historical.py # Evaluate contracts → BUY YES / NO / SKIP
│   ├── compare_prices.py    # Quick live price snapshot
│   └── test_connection.py   # Test API connections
├── config.py                # Config loader (yaml + env overrides)
├── config.yaml              # Markets, strategy, trading settings
├── kalshi_client.py         # Kalshi API client
├── gemini_client.py         # Gemini Prediction Markets client
├── yfinance_client.py       # Price data (yfinance + CoinGecko fallback)
├── ollama_analysis.py       # Ollama LLM analysis integration
└── .env                     # Credentials (from .env.example)
```

All scripts in `scripts/` add the project root to the Python path and load `.env` from the project root, so run them from the project directory: `python scripts/compare_historical.py`.

## Improvement roadmap

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for a roadmap of features inspired by top open-source trading bots (freqtrade, ccxt, hummingbot, Stock-Prediction-Models). Implemented: Kelly criterion, whitelist/blacklist, file logging, volatility-based probability.

## Contract evaluation workflow

**Goal:** Use historical price data to estimate fair probability, then compare to Kalshi/Gemini market prices → recommend **BUY YES** / **BUY NO** / **SKIP**.

Example: "Bitcoin above $81,250 on Mar 16?" → Historical data says 5% of past days closed above that level → If market YES = 40¢, the market is overpricing YES → **BUY NO** or **SKIP**.

Run `scripts/compare_historical.py` every **15 minutes** locally for continuous evaluation. See [Cron setup](#run-every-15-minutes) below.

### Flow diagram: `scripts/compare_historical.py`

```mermaid
flowchart TB
    subgraph init["1. Initialize"]
        A[Load config] --> B[Fetch Gemini events]
        B --> C[Fetch yfinance closes per series]
    end

    subgraph eval["2. Evaluate Markets"]
        C --> D[For each series: KXBTCD, KXETHD, KXSOLD, KXINXU]
        D --> E[Kalshi: get open markets]
        D --> F[Gemini: get crypto events]
        E --> G{Skip? Non-crypto/non-S&P intraday}
        F --> G
        G -->|No| H[Extract strike, hist_p, vol_p]
        H --> I{Recommendation}
        I -->|BUY YES/NO| J[pending_orders]
        I -->|SKIP| K[Skip]
    end

    subgraph positions["3. Evaluate Positions"]
        L[Paginate Kalshi get_positions] --> M[Parse position_fp: positive=YES, negative=NO]
        M --> N{Cached or fetch market}
        N --> O[Compute P lose, rec]
        O --> P{Sell trigger?}
        P -->|LIKELY_LOSE ≥55%| Q[pending_sells]
        P -->|Edge flip| Q
        P -->|RULE_VIOLATION| Q
        P -->|INTRADAY unsupported| Q
    end

    %% Eval completes → positions (both J and K lead to next phase)
    J --> L
    K --> L

    subgraph execute["4. Execute or Preview"]
        Q --> R{Flags?}
        J --> R
        R -->|--execute| S[SELL first: Kalshi + Gemini]
        S --> T[BUY then: limit orders, skip duplicates]
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

  ┌──────────────┐     ┌──────────────┐     ┌──────────────────────────────────┐
  │ Load config  │────▶│ Fetch        │────▶│ For each series (KXBTCD, KXETHD,  │
  │ markets,     │     │ yfinance     │     │ KXSOLD, KXINXU):                  │
  │ yf_tickers   │     │ closes       │     │   • Get Kalshi open markets       │
  └──────────────┘     └──────────────┘     │   • Get Gemini events (crypto)    │
                                            └────────────────┬─────────────────┘
                                                             │
                         ┌───────────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ For each market (Kalshi + Gemini):                                           │
  │   1. Skip? → Non–crypto/non–S&P intraday only. Crypto/S&P: all intraday OK   │
  │   2. Extract strike (e.g. $72,250 above) from market                         │
  │   3. hist_p = empirical P(close above/below strike) from past N days         │
  │   4. vol_p  = volatility model (optional, --use-volatility)                  │
  │   5. Compare hist_p vs market YES price → BUY YES / BUY NO / SKIP            │
  │   6. If not SKIP → add to pending_orders (limit price = bid × (1−dip_pct))   │
  └─────────────────────────────────────────────────────────────────────────────┘
                                                             │
                                                             ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ Evaluate ALL positions (paginate Kalshi get_positions):                      │
  │   • Parse position_fp (positive=YES, negative=NO) for each position          │
  │   • Cached? Use market_data_cache. Uncached? Fetch market + closes           │
  │   • Sell triggers:                                                           │
  │       - LIKELY_LOSE:     P(lose) ≥ 55% (sell_losing_pct)                     │
  │       - Edge flip:       Hold YES but now BUY NO (or vice versa)             │
  │       - RULE_VIOLATION:  Hold NO on "above X" but price > X×(1+cushion)      │
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
  │ EXECUTE order: SELL first → then BUY (so balance reflects sell proceeds)    │
  │   1. SELL: Kalshi + Gemini limit sells at bid                               │
  │   2. BUY:  Kalshi + Gemini limit buys (skip if open order exists)           │
  │            Price = bid × (1 − limit_dip_pct) to catch dips                   │
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
2. **Pull the model** — Run `ollama pull llama3.2`, or a smaller/faster option: `ollama pull llama3.2:1b`.
3. **Verify** — Run `ollama list` to confirm models are installed and the Ollama server is reachable.
4. **Re-run** — `python3 scripts/compare_historical.py --preview --log` (or any script)

To use a different model (e.g. if llama3.2 fails): `OLLAMA_MODEL=mistral python3 scripts/compare_historical.py`

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

If Ollama isn’t running or the model is missing, scripts still run and finish without analysis.

## Scripts

| Script | Purpose |
|--------|---------|
| `python3 scripts/test_connection.py` | Test Kalshi & Gemini connections |
| `python3 scripts/compare_prices.py` | Quick live snapshot (prices only) |
| **`python3 scripts/compare_historical.py`** | **Evaluate contracts → BUY YES / BUY NO / SKIP** |

### Ollama LLM analysis (local, free)

All three scripts send their raw output to a local [Ollama](https://ollama.com) LLM for a brief analysis: which contracts look mispriced, risks, and suggestions.

1. Install & run Ollama: [ollama.com](https://ollama.com)
2. Pull a model: `ollama pull llama3.2` (or `mistral`, `phi3`, `qwen2.5:3b`)
3. Run any script; the last output is Ollama's opinion

To skip LLM analysis (e.g. for cron): `python3 scripts/compare_historical.py --no-llm`

### Main evaluation: `scripts/compare_historical.py`

Uses historical daily closes (yfinance) to compute P(price above/below threshold). Compares to market YES price → recommends BUY YES / BUY NO / SKIP when edge exceeds `min_edge` (config or `--edge`).

```bash
python3 scripts/compare_historical.py --days 30 --markets 0 --edge 0.15   # 0 = all markets
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

| Flag | Description |
|------|-------------|
| `--days` | Historical lookback (default: 30) |
| `--markets` | Max markets per series (0 = all, default) |
| `--edge` | Min edge to recommend buy (default: 0.15 = 15%) |
| `--execute` | Place real Kalshi orders for buys and sells (requires dry_run: false) |
| `--preview` | **Preview mode**: print Kalshi/Gemini API calls we would make, without placing any orders |
| `--use-volatility` | Add volatility-based probability (option-style log-normal model) alongside empirical P |
| `--verbose`, `-v` | Show closes range vs threshold (explains historical P) |
| `--no-llm` | Skip Ollama analysis |
| `--log` | Log run output to `logs/eval_YYYYMMDD.log` (append mode) |

Config: `strategy.min_edge`, `strategy.days_to_expiry`, `strategy.limit_dip_pct`, `strategy.intraday_other_min_p_win`, `strategy.use_kelly`, etc. in `config.yaml`.

### Intraday markets (5pm, 8pm, etc.)

The bot evaluates **all** crypto and S&P 500 intraday markets (e.g. "Bitcoin today at 8pm ET"), not only 5pm crypto / 4pm S&P:

| Resolution | Logic |
|------------|-------|
| **5pm ET crypto** / **4pm ET S&P 500** | Standard `min_edge` (e.g. 10%) — same as before |
| **Other intraday** (e.g. 8pm ET) | Strict threshold: only BUY when P(win) ≥ `intraday_other_min_p_win` (default 90%) — "almost certain" |
| **Non–crypto / non–S&P** with intraday | Skipped (no supported time) |

For "Bitcoin today at 8pm ET", we only recommend BUY YES when historical says ≥90% chance price ends above the strike, and BUY NO when ≥90% chance it ends below. Uses daily closes as a proxy (no intraday data yet).

**Config:** `strategy.intraday_other_min_p_win: 0.90` in `config.yaml`.

### Limit orders & dip catching (Kalshi + Gemini)

Both platforms place **limit orders** (good-til-cancel) so orders rest on the book until filled. To catch dips when the market temporarily drops:

- **`limit_dip_pct`** (default `0.05`): Place buy limits this much *below* the current bid. E.g. `0.05` = 5% below bid — if bid is 40¢, we place at 38¢. Set to `0` to use the bid directly.
- **Duplicate prevention**: The bot skips placing a new buy if you already have an open limit order for that market and side.

### Probability model

- **Empirical**: fraction of past days the close was above/below the threshold.
- **Volatility** (with `--use-volatility`): log-normal model using historical annualized vol and time-to-expiry (see `strategy.days_to_expiry`). Used as `max(empirical, volatility_probability)` for more robust recommendations.

### Quick snapshot: `compare_prices.py`

Live prices only. For BUY/NO/SKIP recommendations, run `scripts/compare_historical.py`:

```bash
python3 scripts/compare_prices.py
```

### Run every 15 minutes

Run the evaluation locally every 15 minutes for continuous monitoring:

```bash
# crontab -e
# Add this line (adjust path to your project and venv):
*/15 * * * * cd /path/to/trading-bot && .venv/bin/python scripts/compare_historical.py --days 30 --no-llm >> eval.log 2>&1
```

Or use a simple loop (for testing):

```bash
while true; do .venv/bin/python scripts/compare_historical.py; sleep 900; done
```

**Note:** `scripts/compare_prices` and `scripts/compare_historical` do not place orders—they only recommend. `scripts/bot.py` can place orders when `trading.dry_run` is `false` (see below).

### Balance-aware order execution (Kalshi)

Orders placed via `create_order_safe` are capped to your available balance and never cause the script to crash on 4xx (e.g. insufficient funds).

- **Balance cap:** Each order uses at most `max_pct_of_balance` (default 90%) of your Kalshi balance.
- **4xx resilience:** If the API returns 4xx (insufficient funds, invalid market, etc.), the script logs a warning and continues.
- **Config** (`config.yaml` → `trading`):

  ```yaml
  trading:
    dry_run: true              # false = place real orders (scripts/bot.py)
    max_pct_of_balance: 0.90   # use at most 90% per order
    min_contracts: 1            # skip if capped count < this
  ```

Enable execution in `scripts/bot.py` by setting `buy_yes_below` / `buy_no_below` in `strategy` and `dry_run: false`. Add funds manually as needed.

### Troubleshooting: 400 Bad Request on orders

If you see `HTTPError: 400 Client Error: Bad Request` when placing orders:

- **Price format**: The script sends prices in the format Kalshi expects (4 decimal places, e.g. `0.4500`). If you've customised price formatting elsewhere, ensure it matches.
- **Market closed**: The market may be paused or closed; Kalshi rejects orders for markets that are no longer tradeable.
- **Insufficient balance/position**: `create_order_safe` caps orders by balance (buys) or position size (sells). If the API still returns 400, check your Kalshi balance and positions.
- **Order size**: Ensure `count` is at least 1 and within exchange limits.
- **Error details**: Check the log output—when an order is rejected, the script prints the API error body (e.g. `order rejected (4xx) 400 TICKER - {"error": "..."}`). Use that message to diagnose the cause.
- **Resilience**: Order failures are handled so one bad order does not stop the run. Failed orders are logged; the script continues with the next order.

### Configuring tickers

Add `yfinance_tickers` in `config.yaml` to map Kalshi series to Yahoo Finance tickers:

```yaml
yfinance_tickers:
  KXBTCD: "BTC-USD"   # Bitcoin
  KXETHD: "ETH-USD"   # Ethereum
  KXSOLD: "SOL-USD"   # Solana
  KXINXU: "^GSPC"     # S&P 500
```

Tickers: `BTC-USD`, `ETH-USD`, `^GSPC`, `SPY`, etc. See [yfinance](https://github.com/ranaroussi/yfinance).

### Gemini Prediction Markets (crypto only)

The comparison scripts also fetch [Gemini Prediction Markets](https://docs.gemini.com/prediction-markets/markets) for **crypto only** (BTC, ETH, SOL). S&P 500 is not included. The events API is public; no API key required for listing. Optional env vars:

- `GEMINI_USE_SANDBOX=true` - use sandbox API
- `GEMINI_PREDICTION_BASE_URL` - override base URL

**Relevant markets (Gemini Exchange UI):**

| Asset | Type | URL |
|-------|------|-----|
| Bitcoin | Daily | [BTC price on March 16](https://exchange.gemini.com/predictions/BTC2603162100/btc-price-on-march-16?categoryPath=crypto%2Cdaily&status=active) |
| Ether | Daily | [ETH price on March 16](https://exchange.gemini.com/predictions/ETH2603162100/eth-price-on-march-16?categoryPath=crypto%2Cdaily&status=active) |
| Solana | Weekly (no daily) | [SOL price on March 20](https://exchange.gemini.com/predictions/SOL2603202100/sol-price-on-march-20?categoryPath=crypto%2Ccrypto_solana&status=active) |

**Note:** With `GEMINI_API_KEY` and `GEMINI_API_SECRET` set, `scripts/compare_historical.py --execute` places limit orders on both Kalshi and Gemini. Use `--preview` to see planned orders without placing them.
