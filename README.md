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

---

## TODO / Future Extensions

- **Real-Time Kalshi Order Execution & Kill-Switch Daemon**:
  - **Overview**: Move beyond basic API trading scripts to a low-latency, resilient order manager that runs 24/7 on a headless box. Manages live Kalshi market orders, enforces strict drawdown stop-outs, and hedges positions automatically when event probabilities swing.
  - **State Machine & Finite Execution Engine**: Manage order lifecycles (`PENDING_SUBMIT`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`) using a deterministic state machine to avoid orphan limit orders or ghost fills during network timeouts.
  - **Heartbeat & Fail-Safe Circuit Breaker**: Implement a watchdog process. If the WebSocket stream drops or exchange latency exceeds a tight threshold (e.g., >500ms), trip a circuit breaker that immediately cancels open resting orders and blocks new executions.
  - **In-Memory Order Book State**: Maintain a localized, fast in-memory representation of resting bids/asks fed by a persistent WebSocket delta stream, decoupled from strategy decision threads via bounded channels or worker queues.

