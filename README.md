# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot 🚀

A high-performance Python trading bot for 15-minute crypto prediction markets on **Kalshi**, recording microstructure snapshots to MongoDB Atlas for Expected Value (EV) optimization and volatility analysis.

## 🛠️ Architecture & Stack
- **Exchange APIs**:
  - **Kalshi Trade API V2** (`POST /portfolio/events/orders` with RSA-PSS digital signatures)
  - *Gemini Trading Client*: Disabled / commented out.
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`, `portfolio_pnl`, `settlements`)
- **Hosting**: 24/7 Render.com Background Worker (30s past 5‑min clock alignment)
- **Tracked Assets (12 Series)**: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `HYPE`, `ADA`, `TON`, `BCH`, `NEAR`, `ZEC`

---

## 📈 EV Optimization & Strategy Rules

### 1. Dynamic 6-Hour MongoDB Calibration Engine 🧠
- **100% Data-Driven Parameter Tuning**: Every 6 hours (and on cold start), the bot queries historical snapshots and settlements from MongoDB Atlas to compute:
  - **Asset Volatility ($\sigma$)**: Standard deviation of 15-minute price percentage moves from the strike.
  - **False-Alarm Rates**: Ratio of early stop-loss exits where the market ended up settling as a winner.
  - **Empirical Win Rates**: Historical win percentages by entry price band.
- **Dynamic Adaptation**: High-beta assets (e.g. `NEAR`, `ZEC`, `HYPE`) receive deeper discount buy requirements and elevated take-profit targets (85¢), while bouncy assets (e.g. `DOGE`, `BNB`) receive wider stop-loss bands.

### 2. Runway Timing & Theta Optimization (Recommendation 1) ⏱️
- **Minute 00 Entry (15m Runway)**: Broad buy corridor up to **52¢** (e.g. 30¢–52¢) with dynamic Take-Profit targets (82¢–85¢), capturing high-EV early entries.
- **Minute 05 Entry (10m Runway)**: Strictly clamps the maximum buy price to **$\le 38¢$** (and `min_buy` to 28¢–34¢) to account for accelerated nonlinear theta decay and eliminate negative-EV late entries.

### 3. Low-EV Middle Zone Avoidance ($37¢–43¢$) (Recommendation 3) 🎯
- **Spread Friction Gate**: Empirical audit proved that entries in the 37¢–43¢ band suffer negative edge due to bid-ask spread friction.
- **Dynamic Screening**: Contracts in the 37¢–43¢ range are automatically bypassed unless Coinbase spot price is strongly in the money ($> 0.08\%$ in favor of the trade), shifting volume to **deep bargains ($\le 36¢$)** and **high-confidence momentum plays ($44¢–48¢$)**.

### 4. Maker Limit Entry (Save 1¢–3¢) 💰
- Submits maker limit buy orders at `Bid + 1¢` (or the current Bid) instead of crossing the spread as a taker, maximizing profit margins on every entry.

### 5. Relative Distance Gate / Spot Momentum Filter 🎯
- **Dead‑Heat Zone** (`|pct_dist| ≤ 0.05%`): Within 0.05% of the strike target, buying `YES` or `NO` is allowed.
- **Directional Zone** (`|pct_dist| > 0.05%`):
  - **Buy `YES` only if**: `spot_price >= target_price`.
  - **Buy `NO` only if**: `spot_price < target_price`.

### 6. Dynamic Asset-Specific Risk Control & Stop-Loss Calibration (Recommendation 2) 🛡️
- **Bouncy / High-Win Coins (e.g. `DOGE`, `BNB`)**: Widens stop-loss threshold (**15¢**), price floor (**25¢**), 5-min cut (**34¢**), and 12.5-min endgame floor (**22¢**) to let positions breathe and prevent premature shakeouts on mean-reverting pullbacks.
- **Trending Coins (e.g. `BTC`, `SOL`, `XRP`)**: Tight, decisive risk controls (**10¢** SL, **30¢** floor, **40¢** 5-min cut, **25¢** endgame floor) to cut losses quickly on genuine reversals.
- **High-Volatility Coins (e.g. `ZEC`, `NEAR`, `HYPE`)**: Adaptive parameters (**12¢** SL, **26¢** floor, **36¢** 5-min cut, **24¢** endgame floor).

### 7. 10‑Minute Winner Roll & Stop‑Loss Execution 🔄🛑
- Evaluated at `:10`, `:25`, `:40`, `:55` past the hour.
- **Winner Roll & Re-Buy (≥ 70¢)**: Sells the winning position immediately at Bid price to **bank/lock in realized cash profit**! Simultaneously **re-buys 1 contract at Ask price** to reset cost basis and **lets THAT newly re-bought position run toward 100¢ ($1.00 settlement)**.
- **Stop-Loss Trigger**: For losing positions, liquidates immediately via taker execution according to the asset's dynamic threshold.

### 8. 12.5-Minute Endgame Safety Check (Dynamic Sub-Settlement Liquidation) ⏳🛑
- Evaluated at `:12:30`, `:27:30`, `:42:30`, `:57:30` (exactly 2.5 minutes before contract expiration).
- Checks ALL current holdings (including positions re-bought at :10):
  - **Losing Positions**: If the market is losing (`current_price < entry_price` or `current_price < endgame_12_5_floor` [dynamically calibrated from MongoDB]), liquidates immediately via taker execution to salvage capital and prevent losing all capital gains or settling at 0¢.
  - **Winning Positions**: If in profit (`current_price ≥ entry_price`), holds position through the final 2.5 minutes to collect the **full 100¢ ($1.00 payout)** at expiration!

---

## 📊 Microstructure & Outcome Logging

Every 5 minutes, an ultra‑compact snapshot (~100 bytes) is saved to MongoDB Atlas:

```json
{
  "ts": 1786680114,
  "m": 1,
  "sym": "BTC",
  "tick": "KXBTC15M-26AUG140015-15",
  "ex": "kalshi",
  "y": 41,
  "n": 60,
  "y_bid": 40,
  "y_ask": 41,
  "n_bid": 59,
  "n_ask": 60,
  "sprd": 1,
  "p": 63223.035,
  "tgt": 63252.79,
  "diff": -29.755,
  "pct": -0.047,
  "rem": 13.1,
  "vol": 189046
}
```

---

## 💰 Portfolio Balance & Realized PnL Tracking

The bot queries native exchange REST API endpoints on each pass to log and store account financial metrics:

- **Collections**:
  - `trading_bot_db.market_snapshots`: 5-minute pricing and trade execution snapshots.
  - `trading_bot_db.portfolio_pnl`: Time-series balance, portfolio value, open exposure, and realized PnL totals.
  - `trading_bot_db.settlements`: Individual market settlement records with cost, revenue, and net profit/loss.
- **Console Log Output**:
  ```text
  💰 [PORTFOLIO PNL] [Kalshi] Cash: $500.00 | Port Value: $520.00 | Settled PnL: +$14.20 (+1420¢ across 8 mkts) | Active: 2 pos ($0.80)
  ```

---

## 📝 Roadmap & TODO

- [ ] **Multi-Contract Position Sizing**: Add support for buying and managing more than 1 contract per opportunity (e.g. dynamic position sizing based on account equity, confidence score, or fixed quantity parameters).
- [ ] **Expansion to Additional Asset Classes & Market Timeframes**:
  - **Stock Indices**: Add support for index prediction markets on Kalshi (e.g. S&P 500 / `INX` / `SPY`, Nasdaq 100 / `NDX`).
  - **Extended Time horizons**: Expand beyond 15-minute intervals to support hourly crypto contracts (`1H`), daily closes, and macro event markets.
