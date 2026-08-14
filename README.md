# Kalshi & Gemini 15-Minute Crypto Trading & Data Pipeline Bot 🚀

A high-performance Python trading bot for 15-minute crypto prediction markets on **Kalshi** and **Gemini**, recording microstructure snapshots to MongoDB Atlas for Expected Value (EV) optimization and volatility analysis.

## 🛠️ Architecture & Stack
- **Exchange APIs**:
  - **Kalshi Trade API V2** (`POST /portfolio/events/orders` with RSA-PSS authentication)
  - **Gemini Prediction Markets API** (HMAC-SHA384 signature headers)
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`, `portfolio_pnl`, `settlements`)
- **Hosting**: 24/7 Render.com Background Worker (30s past 5‑min clock alignment)
- **Tracked Assets (12 Series)**: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `HYPE`, `ADA`, `TON`, `BCH`, `NEAR`, `ZEC`

---

## 📈 EV Optimization & Strategy Rules

### 1. Maker Limit Entry (Save 1¢–3¢) 💰
- Submits maker limit buy orders at `Bid + 1¢` (or the current Bid) instead of crossing the spread as a taker, maximizing profit margins on every entry.

### 2. Relative Distance Gate / Spot Momentum Filter 🎯
- **Dead‑Heat Zone** (`|pct_dist| ≤ 0.05%`): Within 0.05% of the strike target, buying `YES` or `NO` between 35¢–45¢ is allowed.
- **Directional Zone** (`|pct_dist| > 0.05%`):
  - **Buy `YES` only if**: `spot_price >= target_price`.
  - **Buy `NO` only if**: `spot_price < target_price`.

### 3. Dynamic Take‑Profit & Fill‑Gated Auto‑Sell 🚀
- **Minute 00 Entry**: Targets **82¢** take‑profit (15 minutes remaining).
- **Minute 05 Entry**: Targets **76¢** take‑profit (10 minutes remaining).
- **Fill‑Gated Execution**: Take‑profit orders are placed only after buy fill confirmation to avoid phantom resting orders.
- **Run to 100¢ Settlement**: If position price reaches ≥ 70¢ at t = 10m, the resting take‑profit order is canceled to capture the full 100¢ payout.

### 4. 5-Minute Early Cut-Loss (Sub-40¢ Liquidation) ✂️
- Evaluated at `:05`, `:20`, `:35`, `:50` past the hour.
- If a contract bought at the `:00` mark has dropped below the **40¢–49¢ range (< 40¢)** at minute 05, the bot cancels any resting take-profit limit orders and liquidates the position immediately to minimize loss.

### 5. 10‑Minute Stop‑Loss (10¢ Loss Limit / 30¢ Price Floor) 🛑
- Evaluated at `:10`, `:25`, `:40`, `:55` past the hour.
- Directly inspects portfolio contracts (`position_fp > 0` for `YES`, `position_fp < 0` for `NO`) and dynamic cost basis.
- Liquidates immediately if **loss ≥ 10¢** or if position price drops **≤ 30¢**.

### 6. Risk Controls & Hygiene 🛡️
- **Open Position Gate**: Skips markets with existing open positions or active orders.
- **Stale Buy Pruning**: Cancels unfilled resting buy orders at the start of each interval while preserving take‑profit sell orders.
- **Cheapest Entry Tiebreaker**: In dead‑heat conditions where both sides qualify, buy the lower‑priced side for maximum EV.

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
  💰 [PORTFOLIO PNL] [Gemini] Cash: $350.00 | Port Value: $350.00 | Settled PnL: $0.00 (0¢ across 0 mkts) | Active: 0 pos ($0.00)
  ```
