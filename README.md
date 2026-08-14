# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot

A clean, human-readable Python trading bot designed to trade 15-minute crypto prediction markets on Kalshi and record time-series market snapshots into MongoDB Atlas for data engineering and volatility analysis.

---

## Strategy & Workflow Overview

### 1. The 5-Minute Schedule
The bot runs every 5 minutes (triggered by Render.com Cron Job `*/5 * * * *`):
- **On 15-Minute Marks (:00, :15, :30, :45)**:
  - **TRADING + DATA LOGGING**: Checks active 15-minute crypto markets (`KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`).
  - If YES or NO is priced **between 25¢ and 45¢**, buys 1 contract (share).
  - Immediately places a resting limit SELL order at **75¢** for auto-selling (take profit).
  - Records ultra-compact market snapshot documents into MongoDB Atlas.
- **On Intermediate Marks (:05, :10, :20, :25, :35, :40, :50, :55)**:
  - **DATA LOGGING ONLY**: Records current market prices into MongoDB for time-series graphing, but places NO trades.

### 2. Storage Optimization (MongoDB Atlas Free Tier - 512MB Cap)
To ensure database storage stays small and never hits limits, snapshots are saved as ultra-compact JSON documents (~50 bytes per document):
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

## Account Status & Next Setup Steps

> **Status Check**: 
> - ✅ **MongoDB Atlas Cluster**: Created & configured (`cluster0.kygmg3k.mongodb.net`).
> - ✅ **Render.com Account**: Created & ready for deployment.

### What To Do Next:

#### Step 1: Set Environment Variables on Render.com
In your [Render.com Dashboard](https://dashboard.render.com), navigate to your service and add the following **Environment Variables**:

1. `MONGODB_URI`:
   ```text
   mongodb+srv://theotherogeflagg_db_user:FAwQEHMXrqwSyPas@cluster0.kygmg3k.mongodb.net/?retryWrites=true&w=majority
   ```
2. `KALSHI_API_KEY_ID`:
   ```text
   82787744-e854-4524-9a93-dc5a516b75c6
   ```
3. `KALSHI_PRIVATE_KEY`:
   *(Paste the contents of your RSA `.key` file)*
4. `KALSHI_USE_DEMO`:
   `false` (or `true` for paper trading)

#### Step 2: Deploy the Cron Job on Render.com
1. Click **New +** -> **Cron Job**.
2. Connect your GitHub repository (`FlaggIgbo/trading-bot`).
3. Set the following parameters:
   - **Environment**: `Python 3`
   - **Schedule**: `*/5 * * * *` (runs every 5 minutes)
   - **Command**: `python main.py`
4. Click **Create Cron Job**.

#### Step 3: Test Locally with Dry-Run Mode
You can preview live strategy decisions and test MongoDB storage locally without placing real trades:
```bash
# Preview what the bot would trade right now:
python main.py --dry-run --force-trade
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
├── main.py              # Main 5-min execution script & trading logic
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
