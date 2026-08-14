# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot 🚀

A high-performance Python trading bot for 15-minute crypto prediction markets on Kalshi, recording microstructure snapshots to MongoDB Atlas for Expected Value ($EV$) optimization and volatility analysis.

## 🛠️ Architecture & Stack
- **Exchange API**: Kalshi Trade API V2 (`POST /portfolio/events/orders` with RSA-PSS authentication)
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`)
- **Hosting**: 24/7 Render.com Background Worker (30s past 5-min clock alignment)
- **Tracked Assets (12 Series)**: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `HYPE`, `ADA`, `TON`, `BCH`, `NEAR`, `ZEC`

---

## 📈 EV Optimization & Strategy Rules

### 1. Maker Limit Entry (Save 1¢–3¢) 💰
- Submits Maker Limit Buy orders at `Bid + 1¢` (or current Bid) instead of crossing the spread as a Taker, maximizing profit margins on every entry.

### 2. Relative Distance Gate / Spot Momentum Filter 🎯
- **Dead-Heat Zone (|pct_dist| ≤ 0.05%)**: Within 0.05% of the strike target, buying YES or NO between 35¢–45¢ is allowed.
- **Directional Zone (|pct_dist| > 0.05%)**:
  - **Buy YES Only If**: `spot_price >= target_price`.
  - **Buy NO Only If**: `spot_price < target_price`.

### 3. Dynamic Take-Profit & Fill-Gated Auto-Sell 🚀
- **Minute 00 Entry**: Targets **82¢** Take-Profit (15 mins remaining).
- **Minute 05 Entry**: Targets **76¢** Take-Profit (10 mins remaining).
- **Fill-Gated Execution**: Take-Profit orders are placed only after buy fill confirmation, avoiding phantom resting orders.
- **Run to 100¢ Settlement**: If position price reaches ≥ 70¢ at t = 10m, the resting Take-Profit order is canceled to capture the full 100¢ payout.

### 4. 10-Minute Stop-Loss (10¢ Loss Limit / 30¢ Price Floor) 🛑
- Evaluated at `:10`, `:25`, `:40`, `:55` past the hour.
- Directly inspects portfolio contracts (`position_fp > 0` for YES, `position_fp < 0` for NO) and dynamic cost basis.
- Liquidates immediately if **loss $\ge 10$¢** or if position price drops **$\le 30$¢**.

### 5. Risk Controls & Hygiene 🛡️
- **Open Position Gate**: Skips markets with existing open positions or active orders.
- **Stale Buy Pruning**: Cancels unfilled resting buy orders at the start of each interval while preserving Take-Profit sell orders.
- **Cheapest Entry Tiebreaker**: In dead-heat conditions where both sides qualify, buys the lower-priced side for maximum EV.

---

## 📊 Microstructure & Outcome Logging

Every 5 minutes, an ultra-compact snapshot (~100 bytes) is saved to MongoDB Atlas:

```json
{
  "ts": 1786680114,
  "m": 1,
  "sym": "BTC",
  "tick": "KXBTC15M-26AUG140015-15",
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
