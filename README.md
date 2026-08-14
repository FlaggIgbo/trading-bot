# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot

A clean, human-readable Python trading bot designed to trade 15-minute crypto prediction markets on Kalshi and record time-series market snapshots into MongoDB Atlas for data engineering and volatility analysis.

### Stack & Architecture
- **Exchange API**: Kalshi Trade API (RSA-PSS signature authentication)
- **Database**: MongoDB Atlas (`trading_bot_db.market_snapshots`)
- **Hosting**: Render.com Background Worker (runs continuous self-scheduling loop)

---

## Strategy & Clock Alignment Workflow

### 1. Continuous 24/7 Self-Scheduling Loop
The bot runs continuously 24/7 as a background worker (`python main.py` or `python3 main.py`):
- **Clock Alignment**: Calculates exact seconds until 30 seconds past the next 5-minute mark (:00:30, :05:30, :10:30, :15:30, :20:30, :25:30, :30:30, :35:30, :40:30, :45:30, :50:30, :55:30) and sleeps for that exact duration.
- **On 15-Minute Marks (:00:30, :15:30, :30:30, :45:30)**:
  - **TRADING + DATA LOGGING**: Evaluates active 15-minute crypto markets (`KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`).
  - If YES or NO is priced **between 25¢ and 45¢**, buys 1 contract (share).
  - Immediately places a resting limit SELL order at **75¢** for auto-selling (take profit).
  - Records ultra-compact market snapshot documents into MongoDB Atlas.
- **On Intermediate Marks (:05:30, :10:30, :20:30, :25:30, :35:30, :40:30, :50:30, :55:30)**:
  - **DATA LOGGING ONLY**: Records current market prices into MongoDB for time-series graphing, but places NO trades.

### 2. Storage Optimization (MongoDB Atlas Free Tier - 512MB Cap)
Snapshots are saved in an ultra-compact schema (~50 bytes per document):
```json
{
  "ts": 1786675912,
  "m": 45,
  "sym": "BTC",
  "tick": "KXBTC15M-26AUG132300-00",
  "y": 68,
  "n": 33
}
```
*Total consumption: ~100 KB per day (~3 MB per month). The 512MB free tier cluster can store years of data.*

---

## Local Testing Commands

You can test the bot locally using these command-line flags:

```bash
# Preview what the bot would trade right now (dry-run mode):
python3 main.py --dry-run --force-trade
# OR using virtual environment:
.venv/bin/python main.py --dry-run --force-trade

# Single-pass live mode (runs 1 pass and exits):
python3 main.py --once

# Continuous worker mode (runs 24/7 aligned to 5-min clock marks):
python3 main.py
```

---

## Data Engineering: Plotting Volatility Graphs

Each document saved to MongoDB Atlas contains:
- `ts`: Unix timestamp integer (X-axis)
- `sym`: Crypto asset symbol (`BTC`, `ETH`, `SOL`, etc.)
- `y`: Price of YES in cents (Y-axis)
- `n`: Price of NO in cents (Y-axis)

### Example Python Script to Query MongoDB Atlas & Plot Volatility:
```python
import pymongo
import pandas as pd
import matplotlib.pyplot as plt

# 1. Connect to your MongoDB Atlas Cluster
uri = "YOUR_MONGODB_URI"
client = pymongo.MongoClient(uri)
collection = client["trading_bot_db"]["market_snapshots"]

# 2. Fetch data and load into Pandas
df = pd.DataFrame(list(collection.find({}, {"_id": 0})))
df['datetime'] = pd.to_datetime(df['ts'], unit='s')

# 3. Plot Time-Series Graph
plt.figure(figsize=(12, 6))
for sym, group in df.groupby('sym'):
    plt.plot(group['datetime'], group['y'], label=f"{sym} (YES)")

plt.title("Kalshi 15-Minute Crypto Volatility & Probability Tracking")
plt.xlabel("Time")
plt.ylabel("Price / Chance (Cents)")
plt.legend()
plt.grid(True)
plt.show()
```

---

## Repository Structure

```
trading-bot/
├── main.py              # 24/7 Render Worker script with clock-alignment loop
├── database.py          # MongoDB Atlas logger module (ultra-compact schema)
├── kalshi_client.py     # Root shim re-exporting KalshiClient
├── gemini_client.py     # Root shim (commented-out Gemini API)
├── requirements.txt     # Dependencies (requests, cryptography, pymongo)
├── .env.example         # Environment template
└── signals/
    ├── __init__.py      # Module exports
    ├── kalshi_client.py # Kalshi API client & 15m crypto retrieval
    └── gemini_client.py # Commented-out Gemini API client
```
