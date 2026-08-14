# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot

A clean, human-readable Python trading bot designed to trade 15-minute crypto prediction markets on Kalshi and record time-series market snapshots into MongoDB Atlas for quantitative analysis and volatility research.

### Stack & Architecture
- **Exchange API**: Kalshi Trade API V2 (RSA-PSS signature authentication)
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`)
- **Hosting**: Render.com Background Worker (runs continuous 24/7 self-scheduling loop)
- **Assets Covered (12 Crypto Series)**: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `HYPE`, `ADA`, `TON`, `BCH`, `NEAR`, `ZEC`

---

## Strategy & Risk Management Rules

### 1. Dynamic Buy Ranges
- **Market Start Mark (`:00`, `:15`, `:30`, `:45`)**: High volatility at market opening. Buys 1 contract if YES or NO is priced between **35¢ and 45¢**.
- **5-Minute Past Mark (`:05`, `:20`, `:35`, `:50`)**: Tighter volatility band (10 mins remaining). Buys 1 contract if YES or NO is priced between **40¢ and 45¢**.

### 2. Auto-Sell Take Profit (75¢)
- Immediately upon buy order execution, the bot places a resting limit **SELL** order at **75¢** for automatic take-profit execution.

### 3. Stop-Loss Evaluation (15¢ Loss Threshold at `:10`, `:25`, `:40`, `:55`)
- At 10 minutes past market start (`:10`, `:25`, `:40`, `:55`), active open positions are evaluated.
- If a position is suffering a loss $\ge 15\text{¢}$ relative to entry, the bot cancels the resting 75¢ limit order and **sells immediately at current market price** to cap losses.

### 4. Open Position Gate
- The bot checks active market positions and resting orders before buying, ensuring it never over-accumulates multiple contracts for the same ticker.

---

## Data Engineering & Microstructure Logging

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
- `sold_early_saved_from_loss`: Exited early on 75¢ Take-Profit, and market subsequently reversed into a loss (Saved from reversal).
- `sl_cap_saved`: Sold early on Stop-Loss, and market rightfully expired a loss (Rightful stop-loss).
- `tp_full_win`: Exited early on Take-Profit, and market expired a winner.

---

## Local Commands

```bash
# Preview trading decisions right now (dry-run mode):
python3 main.py --dry-run --force-trade

# Run 1 single-pass pass and exit:
python3 main.py --once

# Continuous 24/7 background worker mode (30s clock-aligned loop):
python3 main.py
```

---

## Repository Structure

```
trading-bot/
├── main.py              # 24/7 Render Worker script with clock-alignment loop & strategy logic
├── database.py          # MongoDB Atlas logger module (microstructure & outcome schema)
├── kalshi_client.py     # Root shim re-exporting KalshiClient
├── requirements.txt     # Python dependencies (requests, cryptography, pymongo, pandas, matplotlib)
└── signals/
    ├── __init__.py      # Module exports
    └── kalshi_client.py # Kalshi V2 API client, order execution, & stop-loss logic
```
