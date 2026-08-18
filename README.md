# Kalshi & Gemini 15-Minute & Hourly Multi-Asset Prediction Trading Bot 🚀

A high-performance Python algorithmic trading bot and empirical microstructure data engine for 15-minute and Hourly multi-strike prediction markets on **Kalshi** and **Gemini** (Crypto, Commodities, and Equity Indices), recording microstructure snapshots to MongoDB Atlas for Expected Value (EV) optimization and volatility analysis.

## 🛠️ Architecture & Stack
- **Exchange APIs**:
  - **Kalshi Trade API V2** (`POST /portfolio/events/orders` with RSA-PSS digital signatures)
  - **Gemini Trading Client** (`POST /v1/order/new` with HMAC-SHA384 authentication)
- **Official Settlement Oracles & Real-Time Price Feeds**:
  - **Kalshi CF Benchmarks Feed (`/trade-api/v2/cfbenchmarks/values`)**: Live Real-Time Index (RTI) feeds (`BRTI`, `ETHUSD_RTI`, `SOLUSD_RTI`, `DOGEUSD_RTI`, `XRPUSD_RTI`, `HYPEUSD_RTI`, `NEARUSD_RTI`, `ZECUSD_RTI`, `ADAUSD_RTI`, `BNBUSD_RTI`, `BCHUSD_RTI`, `TONUSD_RTI`) with 60-second Time-Weighted Average Price (TWAP) calculation matching official Kalshi settlement rules.
  - **Coinbase Spot API**: High-frequency defensive fallback spot oracle for crypto assets.
  - **Pyth Network Hermes API**: Sub-second spot prices for Commodities (`GOLD`, `SILVER`, `WTI` Oil) matching official settlement sources.
  - **Yahoo Finance Chart API**: Real-time equity index pricing for S&P 500 (`INX` / `^GSPC`) and Nasdaq-100 (`NASDAQ100` / `^NDX`).
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`, `portfolio_pnl`, `settlements`, `active_positions`)
- **Hosting**: 24/7 Render.com Background Worker (synchronized across 15M and 1H checkpoints)
- **Tracked Assets (17 Active Assets Across 23 Series on Kalshi & Gemini)**:
  - **15-Minute Crypto Series**:
    - *Kalshi*: `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`, `KXADA15M`, `KXTON15M`, `KXBCH15M`, `KXNEAR15M`, `KXZEC15M`
    - *Gemini*: `BTC15M`, `ETH15M`, `SOL15M`, `XRP15M`, `DOGE15M`, `BNB15M`, `HYPE15M`, `ADA15M`, `TON15M`, `BCH15M`, `NEAR15M`, `ZEC15M`
  - **15-Minute Commodity Series**:
    - *Kalshi*: `KXGOLD15M` (Gold), `KXWTI15M` (WTI Crude Oil), `KXSILVER15M` (Silver)
    - *Gemini*: `GOLD15M` / `XAU15M` (Gold), `WTI15M` / `CL15M` (WTI Oil), `SILVER15M` / `XAG15M` (Silver)
  - **Hourly Multi-Strike Crypto Series**:
    - *Kalshi*: `KXBTCD`, `KXETHD`, `KXSOLD`, `KXXRPD`, `KXDOGED`, `KXBNBD`, `KXHYPED`, `KXTOND`, `KXNEARD`, `KXZECD`, `KXTONH`, `KXNEARH`, `KXZECH`
    - *Gemini*: `BTCD`/`BTCH`, `ETHD`/`ETHH`, `SOLD`/`SOLH`, `XRPD`/`XRPH`, `DOGED`/`DOGEH`, `BNBD`/`BNBH`, `HYPED`/`HYPEH`, `TOND`/`TONH`, `NEARD`/`NEARH`, `ZECD`/`ZECH`
  - **Hourly Multi-Strike Commodity Series**:
    - *Kalshi*: `KXGOLDH` (Gold), `KXSILVERH` (Silver), `KXWTIH` (WTI Crude Oil)
    - *Gemini*: `GOLDH`/`GOLDD`, `SILVERH`/`SILVERD`, `WTIH`/`WTID`
  - **Hourly Multi-Strike Equity Index Series**:
    - *Kalshi*: `KXINXU` (S&P 500), `KXNASDAQ100U` (Nasdaq-100)
    - *Gemini*: `INXU` (S&P 500), `NASDAQ100U` (Nasdaq-100)

---

## ⚙️ Environment Configuration (`.env`)

```env
# Kalshi API Credentials
KALSHI_API_KEY_ID=your_kalshi_api_key_id
KALSHI_PRIVATE_KEY_PATH=/path/to/your/trading-bot.key
KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2
KALSHI_USE_DEMO=false
BUY_ORDER_EXPIRATION_SECONDS=45

# Gemini API Credentials
GEMINI_API_KEY=your_gemini_api_key
GEMINI_API_SECRET=your_gemini_api_secret
GEMINI_ACCOUNT=trading-bot
GEMINI_USE_SANDBOX=false

# MongoDB Atlas Connection
MONGODB_URI=mongodb+srv://<user>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority
```

---

## 🛡️ Production Concurrency & Microstructure Safeguards

1. **Resting Maker Limit Orders & Spread Capture**:
   - Places resting limit bids inside the spread (`1¢–2¢` discount) on confirmed favorites, capturing spread savings and paying zero maker fees on crypto prediction markets.
2. **EV-Preservation Exit Logic**:
   - Bypasses panic stop-losses and early cuts on favorite holdings (`entry_price >= 70¢` or `strategy_tag == 'endgame_favorite_maker'`), holding them through to `100¢` ($1.00) settlement payout (preserving ~20¢ of positive EV).
3. **Cross-Asset Portfolio Directional Risk Cap (Comment `p3ok6ft`)**:
   - Caps concurrent crypto directional exposure (max 3 active `YES` or `NO` positions) because BTC/ETH/SOL settle co-directionally 74%–84% of the time.
4. **Market Choppiness / Lead-Change Filter**:
   - Detects and rejects markets with `> 2` lead changes across 50¢, filtering out hyper-volatile whipsaw markets.
5. **Parallel Multi-Source Spot Ingestion**:
   - `batch_fetch_spot_prices` runs parallel worker threads across all assets simultaneously (<150ms total), batching Pyth Hermes feeds and querying CF Benchmarks / Coinbase / Yahoo concurrently.
6. **Crash-Proof Persistent State (`active_positions`)**:
   - Records live entry prices, strategy tags (`endgame_favorite_maker`), and spread savings into MongoDB Atlas upon fill. Restarts or redeployments on Render instantly reload true trade state.

---

## 📈 EV Optimization & Strategy Rules

### 1. Endgame Favorite Maker Strategy (`74¢–93¢`) 🎯
- **Target Corridor**: High-probability confirmed favorites priced between `74¢` and `93¢` in the final `1.0m` to `6.0m` before expiration.
- **Maker Bid Placement**: Submits post-only limit bids at `max(bid + 1, ask - 2)` inside the spread.
- **Zero Fee Edge**: Avoids aggressive taker spread friction while capturing maker discounts.
- **Hold to Settlement**: Rides the contract to `100¢` ($1.00) settlement resolution rather than taking premature stop-losses.

### 2. Dynamic 6-Hour MongoDB Calibration & Graveyard Analytics Engine 🧠
- **100% Data-Driven Parameter Tuning**: Every 6 hours (and on cold start), the bot queries historical snapshots and settlements from MongoDB Atlas to compute:
  - **Asset Volatility ($\sigma$)**: Standard deviation of price percentage moves from strike targets.
  - **Price Band Analytics**: Evaluates win rates and average PnL across Longshots (`<40¢`), Coin-Flips (`40¢–60¢`), Moderate Favorites (`60¢–74¢`), and Heavy Favorites (`74¢–93¢`).
  - **Maker vs Taker Performance**: Tracks realized spread capture and execution slippage.
  - **Dipped Favorite Recovery Rate**: Empirically measures % of favorite positions that dipped below entry price but settled as winners.
  - **Crypto Co-Settlement Matrix**: Calculates live empirical correlation between BTC, ETH, and SOL.
- **Dynamic Adaptation**: High-beta assets (e.g. `NEAR`, `ZEC`, `HYPE`) receive deeper discount buy requirements and elevated take-profit targets (85¢), while bouncy assets (e.g. `DOGE`, `BNB`) receive wider stop-loss bands.
- **Session-Bounded Market Calibration (Equity Indices & Commodities)**: For equity index prediction markets (S&P 500, Nasdaq-100) that trade during specific exchange sessions (e.g. 10:00 AM – 6:00 PM ET), snapshots are recorded strictly during active market hours. Volatility ($\sigma$) is calculated only from live market movements without off-hour zero-movement skew, and automatically falls back to default baselines on cold starts or over weekends.

### 3. Hourly Multi-Strike Selection & Tiebreaker Logic 🎯
- **Priority 1 Favorite Maker Scanning**: Evaluates all strikes for resting maker limit bid opportunities (`74¢–93¢`) across YES and NO sides.
- **Multi-Strike Option Scanning**: Iterates through all available strike options (e.g. `$X or above`) for the active hourly event.
- **Cheapest YES & Cheapest NO**: Identifies the cheapest valid `BUY_YES` and `BUY_NO` adhering to dynamic price corridors and underlying spot momentum.
- **Conflict & Tiebreaker Resolution**:
  - **Same Contract Ticker**: If cheapest YES and cheapest NO are on the exact same contract, the bot picks the strictly cheaper side (e.g. 48¢ NO vs 52¢ YES -> buys NO). If tied in price, picks the side favored by spot momentum.
  - **Different Contract Tickers**: Buys **BOTH** contracts independently (e.g. Strike A YES + Strike B NO) with resting Take-Profit limit orders on each.

### 4. Ensemble Consensus Signal Engine & Settlement Streak Filter (DeweyMarco / Reddit Insights) 🚀
- **Multi-Factor Signal Synthesis**: Synthesizes 4 independent market microstructure and trend dimensions before trade execution:
  1. **Oracle Spot Momentum & Breakout Lead**: Validates whether spot is trending favorably relative to the strike target with sufficient distance ($|pct\_dist| \ge 0.15\%$).
  2. **Settlement Regime Autocorrelation (MongoDB Atlas Streak)**: Queries recent consecutive settled outcomes from MongoDB Atlas. Consecutive runs ($\ge 2$ YES or NO) confirm high conviction trend persistence.
  3. **Microstructure Price Corridor & EV**: Scores favorable positive-EV corridors (Discount $18¢–30¢$, Favorite Maker $74¢–93¢$) while applying strict penalties to the low-EV coin-flip middle zone ($31¢–61¢$).
  4. **Choppiness / Whipsaw Oscillation Guard**: Blocks entries when market top-of-book has crossed $50¢$ more than twice.
- **Strict Volume-Preservation Guarantee**:
  - Unseeded or cold-start assets pass with neutral conviction (never blocks baseline trading volume).
  - Aligned streaks boost conviction and priority scoring.
  - Counter-streak fading protection: Blocks low-conviction entries attempting to fade $\ge 3$ consecutive opposing settlements unless spot oracle shows a massive breakout move ($|pct\_dist| \ge 0.20\%$).

### 5. Checkpoint Lifecycle Matrices ⏱️

#### A. 15-Minute Markets (Crypto & Commodities - Repeats Every 15-Minute Block)
| Block Minute | Target Time | Action Type | 15-Minute Sniper Strategy |
| :--- | :--- | :--- | :--- |
| **`:00`** | **`:00:30`** (`+30s` delay) | **Open Entry** | Deep discount sniper corridor (`18¢–30¢`, TP `62¢`, `1` contract) + Strict momentum corridor (`62¢–68¢`, TP `88¢` with `|pct_dist| ≥ 0.15%`) |
| **`:02:30`** | **`:02:30`** | **2nd Chance + Disaster Cut** | **2.5m Disaster Cut-Loss** (`<15¢`) + Re-evaluates sniper entry corridors |
| **`:05:00`** | **`:05:00`** (*no delay*) | **Theta Cut + 2nd Entry** | **5m Cut-Loss** (`<15¢`) + Clamped discount (`18¢–24¢`, TP `58¢`) & Momentum (`62¢–65¢`) |
| **`:07:30`** | **`:07:30`** | **Mid-Candle Cut + 3rd Entry** | **7.5m Cut-Loss** (`<15¢`) + Clamped discount (`18¢–24¢`, TP `58¢`) |
| **`:10:00`** | **`:10:00`** (*no delay*) | **Winner Bank + Stop-Loss** | **Winner Profit Bank** (`≥70¢` or `≥+15¢` gain: sell at Bid to bank cash profit directly) + Stop-Loss (`sl_threshold=20¢`, `sl_floor=15¢`) |
| **`:12:30`** | **`:12:30`** | **Endgame Safety Check** | Liquidate losing positions 2.5m before expiration (`<15¢`); hold winners for `100¢` settlement |

#### B. Hourly Multi-Strike Markets (Crypto, Commodities, Equity Indices - Every 10 Minutes Past the Hour)
| Hour Minute | Target Time | Action Type | Hourly Core Trend Strategy |
| :--- | :--- | :--- | :--- |
| **`:00`** | **`:00:30`** (`+30s` delay) | **Open Multi-Strike Entry** | Multi-strike scan, pick cheapest YES & NO (`18¢–44¢`, TP `62¢`, dynamic bankroll sizing up to `3` contracts) + Momentum (`56¢–68¢`, TP `88¢`, `|pct_dist| ≥ 0.10%`) |
| **`:10`** | **`:10:00`** (*no delay*) | **2nd Chance + Disaster Cut** | **10m Disaster Cut-Loss** (`<15¢`) + 2nd Chance Multi-Strike Entry (`18¢–44¢`) |
| **`:20`** | **`:20:00`** (*no delay*) | **Cut-Loss + 3rd Entry** | **20m Cut-Loss** (`<15¢`) + Theta-compressed Entry corridor (`18¢–32¢`, TP `58¢`) |
| **`:30`** | **`:30:00`** (*no delay*) | **Mid-Hour Cut + Entry** | **30m Mid-Hour Cut-Loss** (`<15¢`) + Tighter Entry corridor (`18¢–32¢`, TP `58¢`) |
| **`:40`** | **`:40:00`** (*no delay*) | **Winner Bank + Stop-Loss** | **40m Winner Profit Bank** (`≥70¢` or `≥+15¢` gain: sell at Bid to bank cash profit) + Stop-Loss (`sl_threshold=25¢`, `sl_floor=15¢`) |
| **`:50`** | **`:50:00`** (*no delay*) | **Endgame Safety Check** | Liquidate losing positions 10m before expiration (`<15¢`); hold winners for `100¢` settlement |

### 6. Low-EV Middle Zone Avoidance & Risk-Free Counterfactual Tracking 🎯
- **Timeframe-Aware Dead Zones**:
  - **15-Minute Markets**: Expands the dead-zone to **`31¢–61¢`** to completely eliminate 50/50 noise and false breakouts.
  - **1-Hour Markets**: Rejects **`37¢–55¢`** coin-flip contracts unless supported by strong spot trend continuation.
- **Counterfactual Empirical Snapshots**: When a mid-zone contract is screened and skipped, the bot logs a `counterfactual_mid_zone` snapshot to MongoDB Atlas. This allows the calibration engine to monitor statistical win rates and spreads without risking account equity.

### 7. Dynamic Bankroll Sizing & Risk Management 💰
- **Fractional Bankroll Sizing**: Contract sizing is scaled dynamically to risk at most 15% of available cash per position.
- **Small Bankroll Protection**: When cash balance is below $10.00, trades are strictly sized to **1 contract** to prevent ruin from consecutive drawdowns.
- **Insufficient Cash Guard**: Automatically checks live account balance and skips execution if cash cannot cover the order.

### 8. Dynamic Market & Asset Restrictions (Heavy Loss Throttle) 🛡️
- **WTI Crude Oil Restriction**: WTI commodities are restricted to 1 contract max, tighter max buy corridors (`≤30¢` at open, `≤24¢` compressed), and stricter momentum spot lead (`≥0.20%`).
- **Dynamic Loss-Based Restriction Engine**: If MongoDB trade history shows an asset with `<35% win rate` or negative net PnL (over `≥3` trades), the bot automatically restricts it to 1 contract, enforces tight price ceilings (`≤28¢/22¢`), and demands higher spot confirmation (`≥0.20%`).
- **Heavy Loss Disabling**: Assets with `<20% win rate` (over `≥4` trades) disable momentum breakouts and trade strictly deep discount (`≤20¢`).

### 9. Winner Profit Banking & Stop‑Loss Execution 🔄🛑
- **Winner Profit Bank (≥ 70¢ or ≥+15¢ Gain)**: Sells the winning position immediately at Bid price to **bank/lock in realized cash profit directly into your balance**! Eliminates high-price re-buys into negative EV, protecting accumulated profits against end-of-lifecycle reversal.
- **Stop-Loss Trigger**: For losing positions, liquidates immediately via taker execution according to the asset's dynamic threshold.

### 10. Endgame Safety Check (Dynamic Sub-Settlement Liquidation) ⏳🛑
- Checks ALL current holdings before expiration (2.5m before for 15m, 10m before for Hourly):
  - **Losing Positions**: If losing (`current_price < entry_price` or `current_price < endgame_floor`), liquidates immediately via taker execution to salvage capital.
  - **Winning Positions**: If in profit (`current_price ≥ entry_price`), holds position through settlement to collect the **full 100¢ ($1.00 payout)**!

### 11. 15-Minute Empirical Discovery Window (`05:00–05:15 UTC`) 🌍
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
  - `trading_bot_db.market_snapshots`: Microstructure snapshots (tagged with `ex: "kalshi"` or `ex: "gemini"`, `tf: "15m"` or `tf: "1h"`, enriched with `strk`, `strk_side`, `c_score`, `c_regime`).
  - `trading_bot_db.portfolio_pnl`: Time-series balance, portfolio value, open exposure, and realized PnL totals.
  - `trading_bot_db.settlements`: Individual market settlement records with cost, revenue, and net profit/loss.
  - `trading_bot_db.active_positions`: Persistent cost-basis records for open contracts across restarts.
- **Console Log Output**:
  ```text
  💰 [PORTFOLIO PNL] [Kalshi] Cash: $500.00 | Port Value: $520.00 | Settled PnL: +$14.20 (+1420¢ across 8 mkts) | Active: 2 pos ($0.80)
  💰 [PORTFOLIO PNL] [Gemini] Cash: $300.00 | Port Value: $315.00 | Settled PnL: +$8.50 (+850¢ across 5 mkts) | Active: 1 pos ($0.45)
  ```

---

## 🧪 Testing & Verification

Run the comprehensive unit test suite:

```bash
pytest
```
