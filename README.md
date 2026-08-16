# Kalshi 15-Minute & Hourly Crypto Trading Bot 🚀

A high-performance Python trading bot for 15-minute and Hourly multi-strike crypto prediction markets on **Kalshi**, recording microstructure snapshots to MongoDB Atlas for Expected Value (EV) optimization and volatility analysis.

## 🛠️ Architecture & Stack
- **Exchange APIs**:
  - **Kalshi Trade API V2** (`POST /portfolio/events/orders` with RSA-PSS digital signatures)
  - *Gemini Trading Client*: Disabled / commented out.
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`, `portfolio_pnl`, `settlements`, `active_positions`)
- **Hosting**: 24/7 Render.com Background Worker (synchronized across 15M and 1H checkpoints)
- **Tracked Assets (12 Series)**:
  - **15-Minute Series**: `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`, `KXADA15M`, `KXTON15M`, `KXBCH15M`, `KXNEAR15M`, `KXZEC15M`
  - **Hourly Multi-Strike Series**: `KXBTCD`, `KXETHD`, `KXSOLD`, `KXXRPD`, `KXDOGED`, `KXBNBD`, `KXHYPED`, `KXTOND`, `KXNEARD`, `KXZECD`

---

## 🛡️ Production Concurrency & Reliability Safeguards

1. **Order Cancellation Verification**:
   - Strictly verifies that 0 resting orders remain before firing taker liquidations or rolls, preventing `position_locked` errors from Kalshi.
2. **Parallel Spot Ingestion**:
   - `batch_fetch_crypto_spot_prices` runs parallel worker threads across all 12 coins simultaneously (<300ms total), preventing timing drift during Coinbase latency.
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

### 2. Hourly Multi-Strike Selection & Tiebreaker Logic 🎯
- **Multi-Strike Option Scanning**: Iterates through all available strike options (e.g. `$X or above`) for the active hourly event.
- **Cheapest YES & Cheapest NO**: Identifies the cheapest valid `BUY_YES` and `BUY_NO` adhering to dynamic price corridors and Coinbase spot momentum.
- **Conflict & Tiebreaker Resolution**:
  - **Same Contract Ticker**: If cheapest YES and cheapest NO are on the exact same contract, the bot picks the strictly cheaper side (e.g. 48¢ NO vs 52¢ YES -> buys NO). If tied in price, picks the side favored by spot momentum.
  - **Different Contract Tickers**: Buys **BOTH** contracts independently (e.g. Strike A YES + Strike B NO) with resting Take-Profit limit orders on each.

### 3. Checkpoint Lifecycle Matrices ⏱️

#### A. 15-Minute Crypto Markets (Repeats Every 15-Minute Block)
| Block Minute | Target Time | Action Type | 15-Minute Market Strategy |
| :--- | :--- | :--- | :--- |
| **`:00`** | **`:00:30`** (`+30s` delay) | **Open Entry** | Broad buy corridor (`30¢–52¢`), TP `82¢` limit order placed |
| **`:02:30`** | **`:02:30`** | **2nd Chance + Disaster Cut** | **2.5m Disaster Cut-Loss** (`<25¢`) + Re-evaluates broad entry (`30¢–52¢`) |
| **`:05:00`** | **`:05:00`** (*no delay*) | **Cut-Loss + 2nd Entry** | **5m Cut-Loss** (`<34¢–40¢`) + Tighter Entry corridor (`28¢–38¢`, TP `76¢`) |
| **`:07:30`** | **`:07:30`** | **Mid-Candle Cut + 3rd Entry** | **7.5m Cut-Loss** (`<34¢–40¢`) + Tighter Entry corridor (`28¢–38¢`, TP `76¢`) |
| **`:10:00`** | **`:10:00`** (*no delay*) | **Winner Roll + Stop-Loss** | **Winner Roll** (`≥70¢` sell at Bid to bank profit, re-buy at Ask) + Stop-Loss |
| **`:12:30`** | **`:12:30`** | **Endgame Safety Check** | Liquidate losing positions 2.5m before expiration; hold winners for `100¢` settlement |

#### B. Hourly Multi-Strike Crypto Markets (Every 10 Minutes Past the Hour)
| Hour Minute | Target Time | Action Type | Hourly Market Strategy |
| :--- | :--- | :--- | :--- |
| **`:00`** | **`:00:30`** (`+30s` delay) | **Open Multi-Strike Entry** | Multi-strike scan, pick cheapest YES & NO (`30¢–52¢`), TP `82¢` |
| **`:10`** | **`:10:00`** (*no delay*) | **2nd Chance + Disaster Cut** | **10m Disaster Cut-Loss** (`<15¢–20¢`) + 2nd Chance Multi-Strike Broad Entry (`30¢–52¢`) |
| **`:20`** | **`:20:00`** (*no delay*) | **Cut-Loss + 3rd Entry** | **20m Cut-Loss** (`<15¢–20¢`) + Tighter Entry corridor (`28¢–38¢`, TP `76¢`) |
| **`:30`** | **`:30:00`** (*no delay*) | **Mid-Hour Cut + Entry** | **30m Mid-Hour Cut-Loss** (`<15¢–20¢`) + Tighter Entry corridor (`28¢–38¢`, TP `76¢`) |
| **`:40`** | **`:40:00`** (*no delay*) | **Winner Roll + Stop-Loss** | **40m Winner Roll** (`≥70¢` sell at Bid to bank profit, re-buy at Ask) + Stop-Loss |
| **`:50`** | **`:50:00`** (*no delay*) | **Endgame Safety Check** | Liquidate losing positions 10m before expiration; hold winners for `100¢` settlement |

### 4. Low-EV Middle Zone Avoidance ($37¢–43¢$) 🎯
- **Spread Friction Gate**: Contracts in the 37¢–43¢ range are automatically bypassed unless Coinbase spot price is strongly in the money ($> 0.08\%$ in favor of the trade), shifting volume to **deep bargains ($\le 36¢$)** and **high-confidence momentum plays ($44¢–48¢$)**.

### 5. Maker Limit Entry (Save 1¢–3¢) 💰
- Submits maker limit buy orders at `Bid + 1¢` (or the current Bid) instead of crossing the spread as a taker, maximizing profit margins on every entry.

### 6. Relative Distance Gate / Spot Momentum Filter 🎯
- **Dead‑Heat Zone** (`|pct_dist| ≤ 0.05%`): Within 0.05% of the strike target, buying `YES` or `NO` is allowed.
- **Directional Zone** (`|pct_dist| > 0.05%`):
  - **Buy `YES` only if**: `spot_price >= target_price`.
  - **Buy `NO` only if**: `spot_price < target_price`.

### 7. Dynamic Asset-Specific Risk Control & Stop-Loss Calibration 🛡️
- **Bouncy / High-Win Coins (e.g. `DOGE`, `BNB`)**: Widens stop-loss threshold (**15¢**), price floor (**25¢**), cut threshold (**34¢**), and endgame floor (**22¢**).
- **Trending Coins (e.g. `BTC`, `SOL`, `XRP`)**: Tight, decisive risk controls (**10¢** SL, **30¢** floor, **40¢** cut, **25¢** endgame floor).
- **High-Volatility Coins (e.g. `ZEC`, `NEAR`, `HYPE`)**: Adaptive parameters (**12¢** SL, **26¢** floor, **36¢** cut, **24¢** endgame floor).

### 8. Winner Roll & Stop‑Loss Execution 🔄🛑
- **Winner Roll & Re-Buy (≥ 70¢)**: Sells the winning position immediately at Bid price to **bank/lock in realized cash profit**! Simultaneously **re-buys 1 contract at Ask price** to reset cost basis and **lets THAT newly re-bought position run toward 100¢ ($1.00 settlement)**.
- **Stop-Loss Trigger**: For losing positions, liquidates immediately via taker execution according to the asset's dynamic threshold.

### 9. Endgame Safety Check (Dynamic Sub-Settlement Liquidation) ⏳🛑
- Checks ALL current holdings before expiration (2.5m before for 15m, 10m before for Hourly):
  - **Losing Positions**: If losing (`current_price < entry_price` or `current_price < endgame_floor`), liquidates immediately via taker execution to salvage capital.
  - **Winning Positions**: If in profit (`current_price ≥ entry_price`), holds position through settlement to collect the **full 100¢ ($1.00 payout)**!

### 10. 05:00 – 11:00 UTC Signal Discovery Window (Midnight – 6:00 AM ET) 🌍
- **Empirical Signal Harvesting**: Between 05:00 and 11:00 UTC (6-hour Asian & European session window), the bot temporarily falls back to broad default corridors (`30¢–52¢` at `:00`, `28¢–38¢` at `:05`/`20m`) to gather empirical execution data across wider price regimes.
- **Recalibration Anchor at 11:00 UTC**: Cache automatically refreshes with a fresh 48-hour calibration query from MongoDB Atlas right as the US trading day begins.

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

## 📝 Roadmap & Future Enhancements
- [ ] **Multi-Contract Sizing Implementation**: Wire account-equity fractional Kelly and conviction scoring directly into live order execution.
- [ ] **Stock Indices**: Add support for index prediction markets on Kalshi (e.g. S&P 500 / `INX` / `SPY`, Nasdaq 100 / `NDX`).
