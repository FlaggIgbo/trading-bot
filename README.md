# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot 🚀

A clean, human-readable Python trading bot designed to trade 15-minute crypto prediction markets on Kalshi and record time-series market snapshots into MongoDB Atlas for quantitative Expected Value ($EV$) optimization and volatility analysis.

### 🛠️ Stack & Architecture
- **Exchange API**: Kalshi Trade API V2 (RSA-PSS signature authentication)
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`)
- **Hosting**: Render.com Background Worker (runs continuous 24/7 self-scheduling loop)
- **Covered Crypto Assets (12 Series)**: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `HYPE`, `ADA`, `TON`, `BCH`, `NEAR`, `ZEC`

---

## 📈 Expected Value ($EV$) Optimization Strategy & Rules

### 1. Maker Limit Entry Pricing (Save 1¢–3¢ per Entry) 💰
- Instead of crossing the spread as a Taker at Ask price, buy orders target Maker limit pricing at `Bid + 1¢` (or current Bid), saving **1¢–3¢** per entry and boosting profit margins on every winning trade.

### 2. Relative Distance Gate / Spot Momentum Filter (Option 2) 🎯
- **Dead-Heat / Near-Strike Zone ($|\text{pct\_dist}| \le 0.05\%$)**: If spot price is within 0.05% of the strike target (~$0.94 on ETH, ~$31 on BTC), buying YES or NO in the 35¢–45¢ range is allowed.
- **Out-of-The-Money Zone ($|\text{pct\_dist}| > 0.05\%$)**: Enforces directional momentum:
  - **Buy YES Only If**: `spot_price >= target_price` (`pct_dist >= 0.0`).
  - **Buy NO Only If**: `spot_price < target_price` (`pct_dist < 0.0`).

### 3. Dynamic Take-Profit Targets Based on Market Age 🚀
- **Minute 00 Entry (15 Mins Remaining)**: Sets resting Take-Profit limit order at **82¢** (plenty of time for full swing).
- **Minute 05 Entry (10 Mins Remaining)**: Sets resting Take-Profit limit order at **76¢** (shorter runway; securing a profit at 76¢ is statistically safer).
- **Run to 100¢ Settlement**: At $t = 10\text{m}$, if the position is performing strongly ($\ge 70\text{¢}$), the bot cancels the resting Take-Profit order and lets the contract settle for full **100¢**.

### 4. Tightened 10-Minute Stop-Loss (10¢ Loss Limit / 30¢ Price Floor) 🛑
- At 10 minutes past market start (`:10`, `:25`, `:40`, `:55`), active positions are evaluated:
- Liquidates immediately if **loss $\ge 10\text{¢}$** relative to entry OR if position price drops **$\le 30\text{¢}$**.

### 5. Open Position Gate 🛡️
- Checks active positions and resting orders before placing a buy, preventing over-accumulating contracts on the same market.

### 6. Standardized `LogTag` Enum Logging 📋
- All info log brackets use the `LogTag` Enum class with clear code comments and emojis (`🎯`, `🚀`, `🛑`, `📊`, `⚡`, `🛡️`, `💰`, `⏱️`).

### 7. Cheapest Entry Tiebreaker ⚖️
- When both YES and NO qualify during a dead heat (e.g. YES is 43¢ and NO is 40¢), the bot dynamically selects $\min(\text{yes\_price}, \text{no\_price})$ to buy the cheaper side (NO at 40¢), injecting +3¢ of EV into the trade instead of favoring YES by default.

---

## 📊 Data Engineering & Microstructure Logging

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
```

### Counterfactual Trade Outcomes Tracked
- `sold_early_would_have_won`: Sold early on Stop-Loss, but market ultimately expired a winner (False Alarm).
- `sold_early_saved_from_loss`: Exited early on Take-Profit, and market subsequently reversed into a loss (Saved from reversal).
- `sl_cap_saved`: Sold early on Stop-Loss, and market rightfully expired a loss (Rightful stop-loss).
- `tp_full_win`: Exited early on Take-Profit, and market expired a winner.

---

## 💻 Local Commands

```bash
# Preview trading decisions right now (dry-run mode):
python3 main.py --dry-run --force-trade

# Run 1 single-pass pass and exit:
python3 main.py --once

# Continuous 24/7 background worker mode (30s clock-aligned loop):
python3 main.py
```

---

## 📁 Repository Structure

```
trading-bot/
├── main.py              # 24/7 Render Worker script with clock-alignment loop & strategy logic
├── database.py          # MongoDB Atlas logger module (microstructure & outcome schema)
├── requirements.txt     # Python dependencies (requests, cryptography, pymongo, pandas, matplotlib)
├── README.md            # Strategy & data pipeline documentation
├── .env.example         # Environment configuration template
├── .gitignore           # Git exclusion rules
└── signals/
    ├── __init__.py      # Module exports
    └── kalshi_client.py # Kalshi V2 API client, order execution, LogTag enum, & stop-loss logic
```
