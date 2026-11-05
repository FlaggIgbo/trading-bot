"""
ClickHouse Analytics Backend
============================
Integrates the trading bot's logging layer with ClickHouse (https://clickhouse.com/docs)
for fast, column-oriented analytical queries, real-time metrics, and trade event history.
"""

from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional

try:
    import clickhouse_connect
    from clickhouse_connect.driver.client import Client as CHClient
except ImportError:
    clickhouse_connect = None
    CHClient = Any


CREATE_TABLE_SQL = """
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
"""


class ClickHouseBackend:
    """
    Manages ClickHouse client connection, table initialization, event ingestion,
    and analytical aggregation queries.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        secure: Optional[bool] = None,
        client: Optional[CHClient] = None,
        auto_init: bool = True,
    ) -> None:
        self.host = host or os.getenv("CLICKHOUSE_HOST")
        self.port = int(port or os.getenv("CLICKHOUSE_PORT", "8123"))
        self.username = username or os.getenv("CLICKHOUSE_USER", "default")
        self.password = password or os.getenv("CLICKHOUSE_PASSWORD", "")
        self.database = database or os.getenv("CLICKHOUSE_DATABASE", "default")

        secure_env = os.getenv("CLICKHOUSE_SECURE", "false").lower() in ("true", "1", "yes")
        self.secure = secure if secure is not None else secure_env

        self.client: Optional[CHClient] = client
        self._is_connected: bool = False

        if client is not None:
            self._is_connected = True
            if auto_init:
                self.create_table_if_not_exists()
        elif self.host and auto_init:
            self.connect()

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self.client is not None

    def connect(self) -> bool:
        if not self.host or clickhouse_connect is None:
            self._is_connected = False
            return False

        try:
            self.client = clickhouse_connect.get_client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                database=self.database,
                secure=self.secure,
                connect_timeout=3,
                send_receive_timeout=10,
            )
            self._is_connected = True
            self.create_table_if_not_exists()
            return True
        except Exception:
            self._is_connected = False
            self.client = None
            return False

    def create_table_if_not_exists(self) -> None:
        if not self.is_connected or not self.client:
            return
        try:
            self.client.command(CREATE_TABLE_SQL)
        except Exception:
            pass

    def insert_event(
        self,
        event: str,
        message: str,
        level: str = "INFO",
        properties: Optional[dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> bool:
        if not self.is_connected or not self.client:
            return False

        props = properties or {}
        # Parse timestamp
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp)
            except Exception:
                dt = datetime.now(timezone.utc)
        else:
            dt = datetime.now(timezone.utc)

        ticker = str(props.get("ticker", ""))
        side = str(props.get("side", ""))
        price_cents = props.get("price_cents")
        count = props.get("count")
        pnl_cents = props.get("pnl_cents")

        price_int = int(price_cents) if price_cents is not None else None
        count_int = int(count) if count is not None else None
        pnl_int = int(pnl_cents) if pnl_cents is not None else None

        row = [
            dt,
            level.upper(),
            event,
            message,
            ticker,
            side,
            price_int,
            count_int,
            pnl_int,
            json.dumps(props),
        ]

        try:
            self.client.insert(
                "trading_events",
                [row],
                column_names=[
                    "timestamp",
                    "level",
                    "event",
                    "message",
                    "ticker",
                    "side",
                    "price_cents",
                    "count",
                    "pnl_cents",
                    "properties",
                ],
            )
            return True
        except Exception:
            return False

    def get_events(
        self,
        event: Optional[str] = None,
        level: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Optional[list[dict[str, Any]]]:
        if not self.is_connected or not self.client:
            return None

        conditions = []
        parameters: dict[str, Any] = {}

        if event:
            conditions.append("event = {event:String}")
            parameters["event"] = event
        if level:
            conditions.append("level = {level:String}")
            parameters["level"] = level.upper()

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = f"LIMIT {int(limit)}" if limit else ""

        query = f"""
        SELECT
            formatDateTime(timestamp, '%Y-%m-%dT%H:%i:%sZ') as ts,
            level,
            event,
            message,
            ticker,
            side,
            price_cents,
            count,
            pnl_cents,
            properties
        FROM trading_events
        {where_clause}
        ORDER BY timestamp DESC
        {limit_clause}
        """

        try:
            result = self.client.query(query, parameters=parameters)
            rows = []
            for r in result.result_rows:
                props = json.loads(r[9]) if r[9] else {}
                rows.append(
                    {
                        "timestamp": r[0],
                        "level": r[1],
                        "event": r[2],
                        "message": r[3],
                        "properties": props,
                    }
                )
            return rows
        except Exception:
            return None

    def summary(self) -> Optional[dict[str, Any]]:
        if not self.is_connected or not self.client:
            return None

        query = """
        SELECT
            count() as total_events,
            countIf(event = 'markets_scanned') as markets_scanned_count,
            sumIf(count, event in ('order_placed', 'order_live', 'order_simulated')) as orders_placed,
            countIf(event in ('settlement', 'position_settled')) as positions_settled,
            sumIf(pnl_cents, event in ('settlement', 'position_settled')) as total_pnl_cents,
            countIf(event in ('settlement', 'position_settled') and pnl_cents > 0) as wins,
            countIf(event in ('settlement', 'position_settled') and pnl_cents < 0) as losses,
            countIf(event in ('settlement', 'position_settled') and pnl_cents == 0) as break_evens,
            countIf(level = 'ERROR') as errors_count
        FROM trading_events
        """

        types_query = "SELECT event, count() FROM trading_events GROUP BY event"
        levels_query = "SELECT level, count() FROM trading_events GROUP BY level"

        try:
            main_res = self.client.query(query).result_rows[0]
            types_res = dict(self.client.query(types_query).result_rows)
            levels_res = dict(self.client.query(levels_query).result_rows)

            wins = int(main_res[5] or 0)
            losses = int(main_res[6] or 0)
            decisions = wins + losses
            win_rate = (wins / decisions) if decisions > 0 else 0.0

            return {
                "total_events": int(main_res[0] or 0),
                "events_by_type": types_res,
                "events_by_level": levels_res,
                "markets_scanned": int(main_res[1] or 0),
                "orders_placed": int(main_res[2] or 0),
                "positions_settled": int(main_res[3] or 0),
                "total_pnl_cents": int(main_res[4] or 0),
                "wins": wins,
                "losses": losses,
                "break_evens": int(main_res[7] or 0),
                "win_rate": round(win_rate, 4),
                "errors_count": int(main_res[8] or 0),
                "backend": "clickhouse",
            }
        except Exception:
            return None
