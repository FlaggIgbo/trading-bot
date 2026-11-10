"""
Broker Interface & Implementations
==================================
Decouples trade execution from the risk engine.
- KalshiBroker: Calls Kalshi REST API for live or demo exchange orders.
- SimulatedBroker: Simulates fills in-memory without making network requests.

The bot chooses one broker at startup in main.py, eliminating `if live:` branches.
"""

from typing import Any, Optional, Protocol
from db import PositionSide
from logging.analytics import analytics


class Broker(Protocol):
    """Protocol for placing, checking, and canceling orders."""
    is_live: bool

    def place_order(
        self,
        ticker: str,
        side: PositionSide,
        count: int,
        price_cents: int,
        client_order_id: str,
        action: str = "buy",
        time_in_force: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        ...

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        ...

    def cancel_order(self, order_id: str) -> Optional[dict[str, Any]]:
        ...


class SimulatedBroker:
    """
    Simulated broker for paper trading and unit testing.
    Executes all valid orders immediately in-memory without sending network requests.
    """
    def __init__(self, client: Optional[Any] = None) -> None:
        self.client = client
        self.is_live = False
        self._orders: dict[str, dict[str, Any]] = {}

    def place_order(
        self,
        ticker: str,
        side: PositionSide,
        count: int,
        price_cents: int,
        client_order_id: str,
        action: str = "buy",
        time_in_force: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record and simulate an executed order."""
        side_val = side.value if hasattr(side, "value") else str(side)
        cost_dollars = round((price_cents * count) / 100.0, 4)

        order_record = {
            "order_id": client_order_id,
            "client_order_id": client_order_id,
            "ticker": ticker,
            "side": side_val,
            "action": action,
            "count": count,
            "fill_count": count,
            "fill_count_fp": str(count),
            "status": "executed",
            "price_cents": price_cents,
            "yes_price": price_cents if side_val.upper() == "YES" else 100 - price_cents,
            "no_price": price_cents if side_val.upper() == "NO" else 100 - price_cents,
            "taker_fill_cost_dollars": cost_dollars,
            "maker_fill_cost_dollars": 0.0,
            "simulated": True,
        }
        self._orders[client_order_id] = order_record

        analytics.info(
            f"[SIMULATED {action.upper()}] {count} contract(s) on {ticker} {side_val} at {price_cents}c.",
            event="simulated_order",
            ticker=ticker,
            side=side_val,
            count=count,
            price_cents=price_cents,
            client_order_id=client_order_id,
        )
        return {"order": order_record, "client_order_id": client_order_id, "order_id": client_order_id}

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        return self._orders.get(order_id)

    def cancel_order(self, order_id: str) -> Optional[dict[str, Any]]:
        order = self._orders.get(order_id)
        if order:
            order["status"] = "canceled"
            return {"status": "canceled", "order_id": order_id}
        return {"error": "order_not_found"}


class KalshiBroker:
    """
    Live broker communicating directly with the Kalshi REST API client.
    """
    def __init__(self, client: Any) -> None:
        self.client = client
        self.is_live = bool(getattr(client, "live_orders", False))

    def place_order(
        self,
        ticker: str,
        side: PositionSide,
        count: int,
        price_cents: int,
        client_order_id: str,
        action: str = "buy",
        time_in_force: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit an order to the Kalshi exchange via KalshiClient."""
        params: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "count": count,
            "price_cents": price_cents,
            "client_order_id": client_order_id,
            "action": action,
        }
        if time_in_force:
            params["time_in_force"] = time_in_force
        params.update(kwargs)

        try:
            return self.client.place_order(**params)
        except TypeError:
            # Fall back without optional arguments if the client has a simpler signature
            return self.client.place_order(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price_cents,
                client_order_id=client_order_id,
                action=action,
            )

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Look up order details from Kalshi."""
        if hasattr(self.client, "get_order"):
            return self.client.get_order(order_id)
        return None

    def cancel_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Cancel an unfilled or resting order on Kalshi."""
        if hasattr(self.client, "cancel_order"):
            return self.client.cancel_order(order_id)
        return None
