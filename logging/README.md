# Logging & Analytics Layer

Lightweight structured event logging and [ClickHouse](https://clickhouse.com/docs) analytics integration for the trading bot.

## Overview

Replaces unstructured `print` statements across the codebase with structured event logging, in-memory aggregation, optional file export (JSON Lines), and direct integration with **ClickHouse** via `clickhouse-connect`.

## Quick Start

```python
from logging.analytics import analytics

# Log informational messages with custom event tags and metadata
analytics.info("Bot starting", event="bot_start", strategy="last_3")

# Track domain events with structured properties
analytics.track("order_live", "Order executed", ticker="KXBTC15M", side="YES", count=1)
analytics.track("settlement", "Market settled", ticker="KXBTC15M", pnl_cents=45)

# Query events (from ClickHouse if connected, or in-memory)
events = analytics.get_events(event="settlement")

# Get analytical metrics (win rate, total PnL, orders placed, etc.)
metrics = analytics.summary()
print(metrics)
# {
#   "total_events": 2,
#   "orders_placed": 1,
#   "positions_settled": 1,
#   "total_pnl_cents": 45,
#   "win_rate": 1.0,
#   "backend": "clickhouse",  # or "in_memory" if ClickHouse is offline
#   ...
# }
```

## ClickHouse Integration

ClickHouse is supported via the official [`clickhouse-connect`](https://clickhouse.com/docs/integrations/language-clients/python) driver.

### Configuration (`.env`)

```ini
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=default
CLICKHOUSE_SECURE=false
```

When `CLICKHOUSE_HOST` is set, `Analytics` automatically initializes the `trading_events` table:

```sql
CREATE TABLE IF NOT EXISTS trading_events (
    timestamp DateTime64(3, 'UTC'),
    level LowCardinality(String),
    event LowCardinality(String),
    message String,
    ticker LowCardinality(String),
    side LowCardinality(String),
    price_cents Nullable(Int32),
    count Nullable(Int32),
    pnl_cents Nullable(Int32),
    properties String
) ENGINE = MergeTree()
ORDER BY (event, timestamp)
```

If ClickHouse is offline or not configured, it gracefully falls back to in-memory buffering and console output without crashing the bot.
