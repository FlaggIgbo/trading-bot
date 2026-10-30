# Prediction Market Trading Bot

A simple, human-readable prediction market trading bot for [Kalshi](https://kalshi.com).

---

## Strategy

The trading strategy is straightforward:
- **If a market is open (and when it opens)**: Randomly place a bet of **1 contract** on one side (**"YES"** or **"NO"**).
- **Never sell**: Hold the contract until expiration and hope for the best!
- **No duplicate bets**: We only bet once per market while it is open.

---

## System Design

The repository is organized into two active systems and one future system:

1. **Ingestion Layer (`ingestion/`)**:
   - Connects to the Kalshi REST API.
   - Signs requests with RSA-PSS authentication.
   - Fetches open markets.
   - Submits buy orders (or simulates them in safe mode).

2. **Risk Engine (`risk_engine/`)**:
   - Checks if a market is open.
   - Tracks which markets have already been bet on.
   - Randomly chooses `YES` or `NO`.
   - Never sells. Just holds each position.

3. **Logging Layer (`logging/`) [TODO]**:
   - Marked as **TODO**.
   - We will most likely use **ClickHouse** in the future for fast market telemetry and trade logging.

---

## Folder Structure

```text
trading-bot/
├── ingestion/           # System 1: Ingestion layer (Kalshi client & markets)
│   ├── __init__.py
│   └── client.py
├── risk_engine/         # System 2: Risk engine (random 1-contract bet rule)
│   ├── __init__.py
│   └── engine.py
├── logging/             # System 3: Logging layer (TODO for ClickHouse)
│   └── TODO.md
├── scripts/             # Utility scripts
│   └── purge_mongo.py   # Script to purge online MongoDB cluster
├── tests/               # Clean unit tests
│   ├── test_smoke.py
│   ├── test_ingestion.py
│   ├── test_risk_engine.py
│   └── test_main.py
├── main.py              # Bot entry point
├── render.yaml          # Render worker deployment config
├── requirements.txt     # Python packages
└── README.md            # Documentation
```

---

## Setup & Installation

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Set your Kalshi credentials:
```env
KALSHI_API_KEY_ID=your_api_key_id
KALSHI_PRIVATE_KEY_PATH=/path/to/your/kalshi.key
KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
ENABLE_LIVE_ORDERS=false
```

> **Note**: `ENABLE_LIVE_ORDERS=false` keeps the bot in safe simulation mode. Set it to `true` to place real orders on Kalshi.

---

## Running the Bot

### Run continuously (loop)
```bash
python main.py
```

### Run once and exit
```bash
python main.py --once
```

---

## Running Tests

Run the full pytest suite:
```bash
pytest -v
```

All tests run in less than 1 second!

---

## Database Purge Utility

If you ever need to purge your online MongoDB cluster:
```bash
python scripts/purge_mongo.py
```
This connects to `MONGODB_URI` and drops all non-system databases and collections.
