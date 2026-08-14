# Kalshi 15-Minute Crypto Trading & Data Pipeline Bot

A clean, human-readable Python trading bot designed to trade 15-minute crypto prediction markets on Kalshi and record time-series market snapshots into MongoDB Atlas for data engineering and volatility analysis.

---

## Strategy & Workflow Overview

### 1. The 5-Minute Schedule
The bot is designed to be triggered every 5 minutes (e.g. via Render.com Cron Job `*/5 * * * *`):
- **On 15-minute marks (:00, :15, :30, :45)**:
  - **TRADING + DATA LOGGING**: Checks active 15-minute crypto markets (`KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`).
  - If YES or NO is priced **between 25¢ and 45¢**, buys 1 contract (share).
  - Immediately places a resting limit SELL order at **75¢** for auto-selling (take profit).
  - Records market snapshot document into MongoDB.
- **On other 5-minute marks (:05, :10, :20, :25, :35, :40, :50, :55)**:
  - **DATA LOGGING ONLY**: Records current market prices into MongoDB for time-series graphing, but places NO trades.

---

## Setting Up Render.com & MongoDB Atlas (100% Free)

### Step 1: Create a Free MongoDB Database (MongoDB Atlas)
1. Sign up for a free account at [MongoDB Atlas](https://www.mongodb.com/cloud/atlas).
2. Create a **Free Shared Cluster (M0)**.
3. Under **Database Access**, create a database user and password.
4. Under **Network Access**, add IP `0.0.0.0/0` (allows cloud servers like Render to connect).
5. Click **Connect** -> **Drivers**, and copy your connection string:
   ```
   mongodb+srv://<username>:<password>@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
   ```

### Step 2: Set Up Local Environment
1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Fill in your Kalshi credentials and MongoDB connection string in `.env`:
   ```env
   KALSHI_API_KEY_ID=your_kalshi_api_key_id
   KALSHI_PRIVATE_KEY_PATH=/path/to/your/trading-bot.key
   KALSHI_USE_DEMO=true
   MONGODB_URI=mongodb+srv://username:password@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
   ```

### Step 3: Deploy on Render.com
1. Push this repository to GitHub.
2. Sign in to [Render.com](https://render.com).
3. Click **New +** -> **Cron Job**.
4. Connect your GitHub repository.
5. Set:
   - **Environment**: `Python 3`
   - **Schedule**: `*/5 * * * *` (runs every 5 minutes)
   - **Command**: `python main.py`
6. Under **Environment Variables**, add:
   - `KALSHI_API_KEY_ID`
   - `KALSHI_PRIVATE_KEY` (paste RSA PEM contents)
   - `KALSHI_USE_DEMO` (`true` or `false`)
   - `MONGODB_URI` (your MongoDB Atlas connection string)

---

## Data Engineering: Plotting Volatility Graphs

Every snapshot saved to MongoDB contains:
- `timestamp`: ISO UTC time string (X-axis)
- `ticker`: Market symbol (e.g. `KXBTC15M-26AUG132245-45`)
- `yes_price_cents`: Price of YES in cents (Y-axis)
- `no_price_cents`: Price of NO in cents (Y-axis)

### Example Python Script to Query & Plot:
```python
import pymongo
import pandas as pd
import matplotlib.pyplot as plt

# 1. Connect to MongoDB
client = pymongo.MongoClient("YOUR_MONGODB_URI")
db = client["trading_bot_db"]
collection = db["market_snapshots"]

# 2. Load data into Pandas DataFrame
data = list(collection.find({}, {"_id": 0}))
df = pd.DataFrame(data)

# 3. Plot Price over Time
df['timestamp'] = pd.to_datetime(df['timestamp'])
plt.figure(figsize=(12, 6))

for ticker, group in df.groupby('ticker'):
    plt.plot(group['timestamp'], group['yes_price_cents'], label=f"{ticker} (YES)")

plt.title("Kalshi 15-Minute Crypto Market Volatility Over Time")
plt.xlabel("Time")
plt.ylabel("Price / Win Chance (Cents)")
plt.legend()
plt.grid(True)
plt.show()
```

---

## Repository Structure

```
trading-bot/
├── main.py              # Main 5-min execution script & trading logic
├── database.py          # MongoDB Atlas logger module
├── kalshi_client.py     # Root shim re-exporting KalshiClient
├── gemini_client.py     # Root shim (commented-out Gemini API)
├── requirements.txt     # Dependencies (requests, cryptography, pymongo)
├── .env.example         # Environment template
└── signals/
    ├── __init__.py      # Module exports
    ├── kalshi_client.py # Kalshi API client & 15m crypto retrieval
    └── gemini_client.py # Commented-out Gemini API client
```
