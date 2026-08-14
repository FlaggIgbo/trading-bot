# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot

A clean, human-readable Python trading bot designed to trade 15-minute crypto prediction markets on Kalshi and record time-series market snapshots into MongoDB Atlas for data engineering and volatility analysis.

---

## Strategy & Clock Alignment Workflow

### 1. Continuous 24/7 Self-Scheduling Loop
The bot runs continuously 24/7 as a **Render Background Worker** (`python main.py` or `python3 main.py`):
- **Clock Alignment**: The source code calculates the exact number of seconds until the next 5-minute clock boundary (:00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55) and sleeps for that exact duration.
- **On 15-Minute Marks (:00, :15, :30, :45)**:
  - **TRADING + DATA LOGGING**: Evaluates active 15-minute crypto markets (`KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`).
  - If YES or NO is priced **between 25¢ and 45¢**, buys 1 contract (share).
  - Immediately places a resting limit SELL order at **75¢** for auto-selling (take profit).
  - Records ultra-compact market snapshot documents into MongoDB Atlas.
- **On Intermediate Marks (:05, :10, :20, :25, :35, :40, :50, :55)**:
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

## Render.com Background Worker Configuration

> **Account & Worker Status**:
> - ✅ **Render Background Worker Service**: [`srv-d7fq0gjbc2fs739p38ng`](https://dashboard.render.com/worker/srv-d7fq0gjbc2fs739p38ng/settings)
> - ✅ **MongoDB Atlas Cluster**: Created & live (`cluster0.kygmg3k.mongodb.net`).

### Configuring Your Render Background Worker Settings

1. Open your Render Service Settings at [`srv-d7fq0gjbc2fs739p38ng`](https://dashboard.render.com/worker/srv-d7fq0gjbc2fs739p38ng/settings).
2. Set the **Start Command**:
   ```bash
   python3 main.py
   ```
3. Under **Environment Variables**, configure the following variables:
   - `MONGODB_URI`:
     ```text
     mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority
     ```
   - `KALSHI_API_KEY_ID`:
     ```text
     82787744-e854-4524-9a93-dc5a516b75c6
     ```
   - `KALSHI_PRIVATE_KEY`:
     *(Paste the full contents of your RSA `.key` file including BEGIN and END header lines)*
   - `KALSHI_USE_DEMO`:
     `false` (or `true` for paper trading)

---

## Local Testing Commands (macOS / Linux)

On macOS, system Python 3 is invoked using `python3` or via the project's virtual environment (`.venv/bin/python`):

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
uri = "mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority"
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
