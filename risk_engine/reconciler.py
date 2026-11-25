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
                order_info = self.broker.get_order(client_order_id, ticker=ticker)
            except Exception as e:
                analytics.warning(f"[RECONCILER] Error checking order {client_order_id}: {e}")

            if not order_info or not isinstance(order_info, dict):
                # Temporary network issue or order not yet indexed: remain PENDING
                continue

            order_payload = order_info.get("order", order_info)
            status = str(order_payload.get("status") or "").lower()

            ref_fc_val = order_payload.get("fill_count_fp") or order_payload.get("fill_count") or 0.0
            try:
                ref_fc = float(ref_fc_val)
            except (ValueError, TypeError):
                ref_fc = 1.0 if status == "executed" else 0.0

            # Only transition to OPEN if order actually executed/filled
            if status == "executed" or ref_fc > 0:
                self.db.update_position_status(client_order_id, PositionStatus.OPEN)
                self.db.log_event(
                    stage="reconciler",
                    level="INFO",
                    message=f"Reconciled {client_order_id} ({ticker}) as OPEN (filled on exchange).",
                    ticker=ticker,
                    strategy=pos.get("strategy"),
                )
                analytics.info(
                    f"[RECONCILER] Position {client_order_id} for {ticker} transitioned from PENDING -> OPEN (filled).",
                    event="reconciled_open",
                    ticker=ticker,
                    client_order_id=client_order_id,
                )
                pos["status"] = PositionStatus.OPEN
                reconciled.append(pos)
            elif status in ("resting", "open"):
                # Maker order still resting on exchange book waiting for fill - remain PENDING
                continue
            elif status in ("canceled", "rejected"):
                # Explicitly canceled or rejected by exchange
                self.db.update_position_status(client_order_id, PositionStatus.FAILED)
                self.db.log_event(
                    stage="reconciler",
                    level="WARNING",
                    message=f"Reconciled {client_order_id} ({ticker}) as FAILED (status='{status}').",
                    ticker=ticker,
                    strategy=pos.get("strategy"),
                )
                analytics.info(
                    f"[RECONCILER] Position {client_order_id} for {ticker} transitioned from PENDING -> FAILED ({status}).",
                    event="reconciled_failed",
                    ticker=ticker,
                    client_order_id=client_order_id,
                )
                pos["status"] = PositionStatus.FAILED
                reconciled.append(pos)

        return reconciled
