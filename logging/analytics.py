"""
Super Simple Analytics Library with ClickHouse Support
======================================================
Provides structured event logging, in-memory buffering, and seamless
ClickHouse integration (https://clickhouse.com/docs) for the trading bot.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from logging.clickhouse import ClickHouseBackend


@dataclass
class AnalyticsEvent:
    event: str
    message: str
    level: str = "INFO"
    properties: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "event": self.event,
            "message": self.message,
            "properties": self.properties,
        }


class Analytics:
    def __init__(
        self,
        console_output: bool = True,
        log_file: Optional[str] = None,
        formatter: Optional[Callable[[AnalyticsEvent], str]] = None,
        clickhouse: Optional[ClickHouseBackend] = None,
        enable_clickhouse: bool = True,
    ) -> None:
        self.console_output: bool = console_output
        self.log_file: Optional[str] = log_file
        self.formatter: Callable[[AnalyticsEvent], str] = formatter or self._default_formatter
        self._events: list[AnalyticsEvent] = []

        # ClickHouse integration
        if clickhouse is not None:
            self.clickhouse: Optional[ClickHouseBackend] = clickhouse
        elif enable_clickhouse and os.getenv("CLICKHOUSE_HOST"):
            self.clickhouse = ClickHouseBackend()
        else:
            self.clickhouse = None

    @staticmethod
    def _default_formatter(event: AnalyticsEvent) -> str:
        ts = event.timestamp
        lvl = event.level.upper()
        name = event.event
        msg = event.message
        if msg:
            return f"[{ts}] [{lvl}] [{name}] {msg}"
        return f"[{ts}] [{lvl}] [{name}] {event.properties}"

    def track(
        self,
        event: str,
        message: Optional[str] = None,
        level: str = "INFO",
        **properties: Any,
    ) -> AnalyticsEvent:
        if message is None:
            message = str(properties.get("msg", ""))

        entry = AnalyticsEvent(
            event=event,
            message=str(message),
            level=level.upper(),
            properties=properties,
        )
        self._events.append(entry)

        if self.console_output:
            out_stream = sys.stderr if entry.level in ("ERROR", "CRITICAL") else sys.stdout
            out_stream.write(self.formatter(entry) + "\n")
            out_stream.flush()

        if self.log_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.log_file)), exist_ok=True)
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_dict()) + "\n")
            except Exception:
                pass

        if self.clickhouse and self.clickhouse.is_connected:
            try:
                self.clickhouse.insert_event(
                    event=entry.event,
                    message=entry.message,
                    level=entry.level,
                    properties=entry.properties,
                    timestamp=entry.timestamp,
                )
            except Exception:
                pass

        return entry

    def info(self, message: str, event: str = "info", **properties: Any) -> AnalyticsEvent:
        return self.track(event=event, message=message, level="INFO", **properties)

    def warning(self, message: str, event: str = "warning", **properties: Any) -> AnalyticsEvent:
        return self.track(event=event, message=message, level="WARNING", **properties)

    def error(self, message: str, event: str = "error", **properties: Any) -> AnalyticsEvent:
        return self.track(event=event, message=message, level="ERROR", **properties)

    def debug(self, message: str, event: str = "debug", **properties: Any) -> AnalyticsEvent:
        return self.track(event=event, message=message, level="DEBUG", **properties)

    def get_events(
        self,
        event: Optional[str] = None,
        level: Optional[str] = None,
        limit: Optional[int] = None,
        prefer_clickhouse: bool = False,
    ) -> list[AnalyticsEvent]:
        if prefer_clickhouse and self.clickhouse and self.clickhouse.is_connected:
            ch_rows = self.clickhouse.get_events(event=event, level=level, limit=limit)
            if ch_rows is not None:
                return [
                    AnalyticsEvent(
                        event=r["event"],
                        message=r["message"],
                        level=r["level"],
                        properties=r.get("properties", {}),
                        timestamp=r["timestamp"],
                    )
                    for r in ch_rows
                ]

        res = self._events
        if event is not None:
            res = [e for e in res if e.event == event]
        if level is not None:
            level_upper = level.upper()
            res = [e for e in res if e.level == level_upper]
        if limit is not None:
            res = res[-limit:]
        return list(res)

    def clear(self) -> None:
        self._events.clear()

    def summary(self) -> dict[str, Any]:
        # If ClickHouse is active, query ClickHouse
        if self.clickhouse and self.clickhouse.is_connected:
            ch_summary = self.clickhouse.summary()
            if ch_summary is not None:
                return ch_summary

        # In-memory aggregation fallback
        total_events = len(self._events)
        events_by_type: dict[str, int] = {}
        events_by_level: dict[str, int] = {}

        markets_scanned = 0
        orders_placed = 0
        positions_settled = 0
        total_pnl_cents = 0
        wins = 0
        losses = 0
        break_evens = 0

        for e in self._events:
            events_by_type[e.event] = events_by_type.get(e.event, 0) + 1
            events_by_level[e.level] = events_by_level.get(e.level, 0) + 1

            if e.event == "markets_scanned":
                markets_scanned += int(e.properties.get("count", 0))

            if e.event in ("order_placed", "order_live", "order_simulated"):
                orders_placed += int(e.properties.get("count", 1))

            if e.event in ("settlement", "position_settled"):
                positions_settled += 1
                pnl = e.properties.get("pnl_cents")
                if pnl is not None:
                    pnl_int = int(pnl)
                    total_pnl_cents += pnl_int
                    if pnl_int > 0:
                        wins += 1
                    elif pnl_int < 0:
                        losses += 1
                    else:
                        break_evens += 1

        resolved_decisions = wins + losses
        win_rate = (wins / resolved_decisions) if resolved_decisions > 0 else 0.0

        return {
            "total_events": total_events,
            "events_by_type": events_by_type,
            "events_by_level": events_by_level,
            "markets_scanned": markets_scanned,
            "orders_placed": orders_placed,
            "positions_settled": positions_settled,
            "total_pnl_cents": total_pnl_cents,
            "wins": wins,
            "losses": losses,
            "break_evens": break_evens,
            "win_rate": round(win_rate, 4),
            "errors_count": events_by_level.get("ERROR", 0),
            "backend": "in_memory",
        }


# Global default instance for repository-wide convenience
analytics = Analytics()
