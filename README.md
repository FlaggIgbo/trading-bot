# Kalshi 15-Minute & Hourly Multi-Asset Trading Bot 🚀

A high-performance Python trading bot for 15-minute and Hourly multi-strike prediction markets on **Kalshi** (Crypto, Commodities, and Equity Indices), recording microstructure snapshots to MongoDB Atlas for Expected Value (EV) optimization and volatility analysis.

## 🛠️ Architecture & Stack
- **Exchange APIs**:
  - **Kalshi Trade API V2** (`POST /portfolio/events/orders` with RSA-PSS digital signatures)
  - *Gemini Trading Client*: Disabled / commented out.
- **Spot Price Oracle Feeds**:
  - **Coinbase Spot API**: High-frequency spot prices for Crypto assets.
  - **Pyth Network Hermes API**: Sub-second spot prices for Commodities (`GOLD`, `SILVER`, `WTI` Oil) matching Kalshi's official settlement source.
  - **Yahoo Finance Chart API**: Real-time equity index pricing for S&P 500 (`INX` / `^GSPC`) and Nasdaq-100 (`NASDAQ100` / `^NDX`).
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`, `portfolio_pnl`, `settlements`, `active_positions`)
- **Hosting**: 24/7 Render.com Background Worker (synchronized across 15M and 1H checkpoints)
- **Tracked Assets (17 Active Assets Across 23 Series)**:
  - **15-Minute Crypto Series**: `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`, `KXADA15M`, `KXTON15M`, `KXBCH15M`, `KXNEAR15M`, `KXZEC15M`
  - **15-Minute Commodity Series**: `KXGOLD15M` (Gold), `KXWTI15M` (WTI Crude Oil), `KXSILVER15M` (Silver)
  - **Hourly Multi-Strike Crypto Series**: `KXBTCD`, `KXETHD`, `KXSOLD`, `KXXRPD`, `KXDOGED`, `KXBNBD`, `KXHYPED`, `KXTOND`, `KXNEARD`, `KXZECD`, `KXTONH`, `KXNEARH`, `KXZECH`
  - **Hourly Multi-Strike Commodity Series**: `KXGOLDH` (Gold), `KXSILVERH` (Silver), `KXWTIH` (WTI Crude Oil)
  - **Hourly Multi-Strike Equity Index Series**: `KXINXU` (S&P 500), `KXNASDAQ100U` (Nasdaq-100)

---

## 🛡️ Production Concurrency & Reliability Safeguards

1. **Order Cancellation Verification**:
   - Strictly verifies that 0 resting orders remain before firing taker liquidations or rolls, preventing `position_locked` errors from Kalshi.
2. **Parallel Multi-Source Spot Ingestion**:
   - `batch_fetch_spot_prices` runs parallel worker threads across all 17 assets simultaneously (<150ms total), batching Pyth Hermes feeds and querying Coinbase/Yahoo concurrently.
3. **Crash-Proof Persistent State (`active_positions`)**:
   - Records live entry prices into MongoDB Atlas upon fill. Restarts or redeployments on Render instantly reload true cost bases rather than falling back to static estimates.
4. **Per-Market & Per-Symbol Error Boundaries**:
   - Isolates each market snapshot and symbol group in localized `try/except` guards so that payload anomalies never halt remaining execution passes.
5. **IOC-Only Liquidation & No Dangerous 99¢ GTC Bids**:
   - Eliminates resting 99¢ GTC bids during NO liquidations. Employs dynamic book re-quoting and immediate-or-cancel execution to prevent catastrophic downside exposure.

---

## 📈 EV Optimization & Strategy Rules

### 1. Dynamic 6-Hour MongoDB Calibration Engine 🧠
- **100% Data-Driven Parameter Tuning**: Every 6 hours (and on cold start), the bot queries historical snapshots and settlements from MongoDB Atlas to compute:
  - **Asset Volatility ($\sigma$)**: Standard deviation of price percentage moves from strike targets.
  - **False-Alarm Rates**: Ratio of early stop-loss exits where the market ended up settling as a winner.
  - **Empirical Win Rates**: Historical win percentages by entry price band.
- **Dynamic Adaptation**: High-beta assets (e.g. `NEAR`, `ZEC`, `HYPE`) receive deeper discount buy requirements and elevated take-profit targets (85¢), while bouncy assets (e.g. `DOGE`, `BNB`) receive wider stop-loss bands.
- **Session-Bounded Market Calibration (Equity Indices & Commodities)**: For equity index prediction markets (S&P 500, Nasdaq-100) that trade during specific exchange sessions (e.g. 10:00 AM – 6:00 PM ET), snapshots are recorded strictly during active market hours. Volatility ($\sigma$) is calculated only from live market movements without off-hour zero-movement skew, and automatically falls back to default baselines on cold starts or over weekends.

### 2. Hourly Multi-Strike Selection & Tiebreaker Logic 🎯
- **Multi-Strike Option Scanning**: Iterates through all available strike options (e.g. `$X or above`) for the active hourly event.
- **Cheapest YES & Cheapest NO**: Identifies the cheapest valid `BUY_YES` and `BUY_NO` adhering to dynamic price corridors and underlying spot momentum.
- **Conflict & Tiebreaker Resolution**:
  - **Same Contract Ticker**: If cheapest YES and cheapest NO are on the exact same contract, the bot picks the strictly cheaper side (e.g. 48¢ NO vs 52¢ YES -> buys NO). If tied in price, picks the side favored by spot momentum.
  - **Different Contract Tickers**: Buys **BOTH** contracts independently (e.g. Strike A YES + Strike B NO) with resting Take-Profit limit orders on each.

### 3. Checkpoint Lifecycle Matrices ⏱️

#### A. 15-Minute Markets (Crypto & Commodities - Repeats Every 15-Minute Block)
| Block Minute | Target Time | Action Type | 15-Minute Sniper Strategy |
| :--- | :--- | :--- | :--- |
| **`:00`** | **`:00:30`** (`+30s` delay) | **Open Entry** | Deep discount sniper corridor (`18¢–30¢`, TP `62¢`, `1` contract) + Strict momentum corridor (`62¢–68¢`, TP `88¢` with `\|pct_dist\| ≥ 0.15%`) |
| **`:02:30`** | **`:02:30`** | **2nd Chance + Disaster Cut** | **2.5m Disaster Cut-Loss** (`<15¢`) + Re-evaluates sniper entry corridors |
| **`:05:00`** | **`:05:00`** (*no delay*) | **Theta Cut + 2nd Entry** | **5m Cut-Loss** (`<15¢`) + Clamped discount (`18¢–24¢`, TP `58¢`) & Momentum (`62¢–65¢`) |
| **`:07:30`** | **`:07:30`** | **Mid-Candle Cut + 3rd Entry** | **7.5m Cut-Loss** (`<15¢`) + Clamped discount (`18¢–24¢`, TP `58¢`) |
| **`:10:00`** | **`:10:00`** (*no delay*) | **Winner Roll + Stop-Loss** | **Winner Roll** (`≥70¢` sell at Bid to bank profit, re-buy at Ask) + Stop-Loss (`sl_threshold=20¢`, `sl_floor=15¢`) |
| **`:12:30`** | **`:12:30`** | **Endgame Safety Check** | Liquidate losing positions 2.5m before expiration (`<15¢`); hold winners for `100¢` settlement |

#### B. Hourly Multi-Strike Markets (Crypto, Commodities, Equity Indices - Every 10 Minutes Past the Hour)
| Hour Minute | Target Time | Action Type | Hourly Core Trend Strategy |
| :--- | :--- | :--- | :--- |
| **`:00`** | **`:00:30`** (`+30s` delay) | **Open Multi-Strike Entry** | Multi-strike scan, pick cheapest YES & NO (`18¢–44¢`, TP `62¢`, `3` contracts core sizing) + Momentum (`56¢–68¢`, TP `88¢`, `\|pct_dist\| ≥ 0.10%`) |
| **`:10`** | **`:10:00`** (*no delay*) | **2nd Chance + Disaster Cut** | **10m Disaster Cut-Loss** (`<15¢`) + 2nd Chance Multi-Strike Entry (`18¢–44¢`) |
| **`:20`** | **`:20:00`** (*no delay*) | **Cut-Loss + 3rd Entry** | **20m Cut-Loss** (`<15¢`) + Theta-compressed Entry corridor (`18¢–32¢`, TP `58¢`) |
| **`:30`** | **`:30:00`** (*no delay*) | **Mid-Hour Cut + Entry** | **30m Mid-Hour Cut-Loss** (`<15¢`) + Tighter Entry corridor (`18¢–32¢`, TP `58¢`) |
| **`:40`** | **`:40:00`** (*no delay*) | **Winner Roll + Stop-Loss** | **40m Winner Roll** (`≥70¢` sell at Bid to bank profit, re-buy at Ask) + Stop-Loss (`sl_threshold=25¢`, `sl_floor=15¢`) |
| **`:50`** | **`:50:00`** (*no delay*) | **Endgame Safety Check** | Liquidate losing positions 10m before expiration (`<15¢`); hold winners for `100¢` settlement |

### 4. Low-EV Middle Zone Avoidance & Risk-Free Counterfactual Tracking 🎯
- **Timeframe-Aware Dead Zones**:
  - **15-Minute Markets**: Expands the dead-zone to **`31¢–61¢`** to completely eliminate 50/50 noise and false breakouts.
  - **1-Hour Markets**: Rejects **`37¢–55¢`** coin-flip contracts unless supported by strong spot trend continuation.
- **Counterfactual Empirical Snapshots**: When a mid-zone contract is screened and skipped, the bot logs a `counterfactual_mid_zone` snapshot to MongoDB Atlas. This allows the calibration engine to monitor statistical win rates and spreads without risking account equity.

### 5. Maker Limit Entry (Save 1¢–3¢) 💰
- Submits maker limit buy orders at `Bid + 1¢` (or the current Bid) instead of crossing the spread as a taker, maximizing profit margins on every entry.

### 6. Relative Distance Gate & Breakout Lead Thresholds 🎯
- **Spot Alignment Gate**:
  - **Buy `YES` only if**: `spot_price >= target_price` (or within dead-heat `|pct_dist| <= 0.05%`).
  - **Buy `NO` only if**: `spot_price < target_price` (or within dead-heat `|pct_dist| <= 0.05%`).
- **Momentum Breakout Lead**: For higher-priced momentum entries (56¢–68¢), requires a confirmed spot lead of `|pct_dist| >= 0.10%` to prevent buying tops.

### 7. Dynamic Asset-Specific Risk Control & Stop-Loss Calibration 🛡️
- **Dynamic 6-Hour MongoDB Calibration**: Stop-loss loss thresholds (`25¢`), price floors (`15¢`), 5-minute cuts (`15¢`), and endgame thresholds (`15¢`) are tuned adaptively per asset based on rolling 48-hour volatility standard deviations ($\sigma$) and false-alarm historical rates.

### 8. Winner Roll & Stop‑Loss Execution 🔄🛑
- **Winner Roll & Re-Buy (≥ 70¢)**: Sells the winning position immediately at Bid price to **bank/lock in realized cash profit**! Simultaneously **re-buys 1 contract at Ask price** to reset cost basis and **lets THAT newly re-bought position run toward 100¢ ($1.00 settlement)**.
- **Stop-Loss Trigger**: For losing positions, liquidates immediately via taker execution according to the asset's dynamic threshold.

### 9. Endgame Safety Check (Dynamic Sub-Settlement Liquidation) ⏳🛑
- Checks ALL current holdings before expiration (2.5m before for 15m, 10m before for Hourly):
  - **Losing Positions**: If losing (`current_price < entry_price` or `current_price < endgame_floor`), liquidates immediately via taker execution to salvage capital.
  - **Winning Positions**: If in profit (`current_price ≥ entry_price`), holds position through settlement to collect the **full 100¢ ($1.00 payout)**!

### 10. 15-Minute Empirical Discovery Window (`05:00–05:15 UTC`) 🌍
- **Rate-Limited Discovery**: Between `05:00 and 05:15 UTC` (one 15-minute cycle), the bot permits broad corridor sampling up to a strict cap of **3 live exploration trades per session** across the entire bot to measure live orderbook slippage.
- **Post-Discovery Recalibration Anchor (`05:15 UTC`)**: Immediately upon exiting the 15-minute discovery window, the cache automatically refreshes from MongoDB Atlas to lock in calibrated parameters.

---

## 💰 Portfolio Tracking & Position Sizing Framework

### Current Production Sizing (Strict 1-Contract Fixed Sizing)
- Every buy order submits **`count = 1`**, strictly capping total risk per opportunity to **$0.28 – $0.52**.
- Exits (Take-Profit, Cut-Loss, Stop-Loss, Winner-Rolls) dynamically match and manage the active position quantity.

---

## 📊 Portfolio Balance & Realized PnL Tracking
- **Collections**:
  - `trading_bot_db.market_snapshots`: Microstructure snapshots (tagged with `tf: "15m"` or `tf: "1h"`).
  - `trading_bot_db.portfolio_pnl`: Time-series balance, portfolio value, open exposure, and realized PnL totals.
  - `trading_bot_db.settlements`: Individual market settlement records with cost, revenue, and net profit/loss.
  - `trading_bot_db.active_positions`: Persistent cost-basis records for open contracts across restarts.
- **Console Log Output**:
  ```text
  💰 [PORTFOLIO PNL] [Kalshi] Cash: $500.00 | Port Value: $520.00 | Settled PnL: +$14.20 (+1420¢ across 8 mkts) | Active: 2 pos ($0.80)
  ```

---

## 📝 TODO & Engineering Roadmap

Transitioning this project from a functional trading script into an enterprise-grade data engineering and distributed systems portfolio piece involves focusing on streaming architecture, schema enforcement, asynchronous I/O, and backtesting infrastructure.

### 1. Data Engineering & Pipeline Architecture
- [ ] **Migrate from Polling to WebSocket Streams**:
  - Replace scheduled HTTP polling with real-time WebSocket connections (e.g., Kalshi WS and Coinbase WS).
  - Instead of periodic snapshots at fixed minute marks, maintain an in-memory, event-driven order book and stream raw tick-level deltas.
- [ ] **Separation of OLTP and OLAP Storage**:
  - Keep MongoDB or Redis for fast, low-latency OLTP (active positions, order tracking).
  - Stream historical market snapshots and trade logs to an OLAP time-series database (e.g., **ClickHouse**, **TimescaleDB**, or partitioned **Parquet** files on S3/GCS queried via DuckDB) for scalable EV aggregations and backtesting.
- [ ] **Strict Data Contracts & Schema Validation**:
  - Replace ad-hoc dictionary lookups (`market.get(...)`) and regex parsers with **Pydantic V2** or **Msgspec** models.
  - Define explicit schemas for incoming market payloads, orders, and snapshot documents to catch malformed payloads at the ingestion boundary.

### 2. Software Engineering & System Design
- [ ] **Full Asynchronous I/O (`asyncio` + `aiohttp` / `httpx`)**:
  - Convert network and database layers to `asyncio` to eliminate thread-switching overhead, simplify concurrent exchange queries, and prevent timing jitter during critical checkpoint windows.
- [ ] **Clean Architecture & Decoupled Domain Layers**:
  - Decouple `main.py` into independent services or modules:
    - **Data Feed / Ingestor**: Ingests and normalizes order books and spot feeds.
    - **Strategy / Signal Engine**: Pure, stateless functions taking market state and returning buy/sell signals without side effects.
    - **Execution / Risk Manager**: Handles order submission, fill tracking, and circuit breakers.
    - **Sink Service**: Asynchronously batches and flushes metrics and snapshots to persistent storage.
- [ ] **State Machine for Order Lifecycle**:
  - Replace loosely coupled flags and order IDs with a formal **Finite State Machine (FSM)** for trade lifecycles (`PENDING_SUBMISSION` $\to$ `OPEN_RESTING` $\to$ `FILLED` $\to$ `TP_ARMED` $\to$ `EXIT_LIQUIDATED` $\to$ `SETTLED`).

### 3. Observability & Backtesting Framework
- [ ] **Structured Logging & OpenTelemetry Metrics**:
  - Replace standard string formatting with structured JSON logs (`structlog`) and expose a **Prometheus** metrics endpoint (`/metrics`).
  - Track REST request latency, order fill ratios, slippage against top-of-book, and WebSocket message lag.
- [ ] **Event-Driven Historical Backtester**:
  - Build a deterministic backtesting simulator consuming stored historical snapshots from MongoDB to replay market events chronologically.
  - Test strategy adjustments, cut-loss timings, and corridor parameters against empirical data without capital risk.

### 4. Strategy & Asset Expansion
- [ ] **Multi-Contract Sizing Implementation**: Wire account-equity fractional Kelly and conviction scoring directly into live order execution.
- [x] **Commodities & Stock Indices**: Supported prediction markets on Kalshi:
  - Commodities: Gold (`KXGOLD15M`, `KXGOLDH`), Silver (`KXSILVER15M`, `KXSILVERH`), WTI Oil (`KXWTI15M`, `KXWTIH`) via Pyth Network Hermes API.
  - Stock Indices: S&P 500 (`KXINXU`), Nasdaq-100 (`KXNASDAQ100U`) via Yahoo Finance Chart API.

