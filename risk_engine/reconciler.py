"""
Startup & Periodic Reconciler
=============================
Reconciles PENDING and ambiguous positions with the broker/exchange.
Addresses failure scenario #1: If the process crashes between sending an order and writing
the database record, a PENDING position written prior to the API call will remain in SQLite.

On startup (and periodically):
- Checks all PENDING positions against the broker/Kalshi.
- If the order exists on the exchange -> transitions PENDING to OPEN.
- If the order does NOT exist on the exchange -> transitions PENDING to FAILED.
"""

from typing import Any, Optional
from db import Database, PositionStatus
from logging.analytics import analytics
from risk_engine.broker import Broker


class Reconciler:
    def __init__(self, broker: Broker, db: Database) -> None:
        self.broker = broker
        self.db = db

    def reconcile(self) -> list[dict[str, Any]]:
        """
        Reconcile all PENDING positions with the exchange.
        Returns the list of positions updated.
        """
        pending_positions = self.db.get_pending_positions()
        if not pending_positions:
            return []

        analytics.info(
            f"[RECONCILER] Found {len(pending_positions)} PENDING position(s) to reconcile.",
            event="reconciliation_start",
            count=len(pending_positions),
        )

        reconciled = []
        for pos in pending_positions:
            client_order_id = pos["client_order_id"]
            ticker = pos["ticker"]

            # Query the broker/exchange for this order
            order_info = None
            try:
                order_info = self.broker.get_order(client_order_id)
            except Exception as e:
                analytics.warning(f"[RECONCILER] Error checking order {client_order_id}: {e}")

            order_payload = order_info.get("order", order_info) if isinstance(order_info, dict) else {}
            status = str(order_payload.get("status") or "").lower()

            # If the broker has record of the order being executed or resting
            if status in ("executed", "resting", "open"):
                self.db.update_position_status(client_order_id, PositionStatus.OPEN)
                self.db.log_event(
                    stage="reconciler",
                    level="INFO",
                    message=f"Reconciled {client_order_id} ({ticker}) as OPEN (found on exchange status='{status}').",
                    ticker=ticker,
                    strategy=pos.get("strategy"),
                )
                analytics.info(
                    f"[RECONCILER] Position {client_order_id} for {ticker} transitioned from PENDING -> OPEN.",
                    event="reconciled_open",
                    ticker=ticker,
                    client_order_id=client_order_id,
                )
                pos["status"] = PositionStatus.OPEN
                reconciled.append(pos)
            else:
                # Order not on exchange or rejected/canceled -> transition to FAILED
                self.db.update_position_status(client_order_id, PositionStatus.FAILED)
                self.db.log_event(
                    stage="reconciler",
                    level="WARNING",
                    message=f"Reconciled {client_order_id} ({ticker}) as FAILED (not executed on exchange).",
                    ticker=ticker,
                    strategy=pos.get("strategy"),
                )
                analytics.info(
                    f"[RECONCILER] Position {client_order_id} for {ticker} transitioned from PENDING -> FAILED.",
                    event="reconciled_failed",
                    ticker=ticker,
                    client_order_id=client_order_id,
                )
                pos["status"] = PositionStatus.FAILED
                reconciled.append(pos)

        return reconciled
