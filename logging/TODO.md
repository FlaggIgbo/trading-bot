# Logging & Analytics Layer

The ClickHouse analytics layer is now implemented in [`logging/clickhouse.py`](file:///home/flaggigbo/git/trading-bot/logging/clickhouse.py) using official [`clickhouse-connect`](https://clickhouse.com/docs/integrations/language-clients/python).

### Implemented:
- **`trading_events` MergeTree Table**: Real-time event persistence partitioned and ordered by `(event, timestamp)`.
- **Automatic Column-Oriented Queries**: `summary()` and `get_events()` run analytical SQL directly against ClickHouse.
- **Graceful Fallback**: Buffers in-memory and outputs formatted console logs if ClickHouse is offline or not configured.

### Future Roadmap:
- Ingestion of full L2/L3 orderbook depth ticks.
- Real-time tick aggregations using ClickHouse Materialized Views.
