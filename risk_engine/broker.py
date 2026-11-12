"""
Broker Interface & Implementations
==================================
Decouples trade execution from the risk engine.
- KalshiBroker: Calls Kalshi REST API for live or demo exchange orders.
- SimulatedBroker: Simulates fills in-memory without making network requests.

The bot chooses one broker at startup in main.py, eliminating `if live:` branches.
"""

import re
import time
from typing import Any, Optional, Protocol, Union
from db import PositionSide, PositionStatus
from logging.analytics import analytics


class Broker(Protocol):
    """Protocol for placing, checking, canceling, and buying orders."""
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

    def buy(
        self,
        market: dict[str, Any],
        side: PositionSide,
        price_cents: int,
        count: int = 1,
        client_order_id: Optional[str] = None,
        time_in_force: Optional[str] = None,
        resting_price_cents: Optional[int] = None,
        strategy_name: Any = "trend",
        db: Optional[Any] = None,
        entry_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        ...


class BaseBroker:
    """Base broker providing unified write-ahead buy execution."""
    is_live: bool = False
    client: Optional[Any] = None
    db: Optional[Any] = None

    def buy(
        self,
        market: dict[str, Any],
        side: PositionSide,
        price_cents: int,
        count: int = 1,
        client_order_id: Optional[str] = None,
        time_in_force: Optional[str] = None,
        resting_price_cents: Optional[int] = None,
        strategy_name: Any = "trend",
        db: Optional[Any] = None,
        entry_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Unified buy execution:
        1. Write-ahead PENDING state in SQLite.
        2. Submit buy order (IOC or resting) via broker implementation.
        3. Poll fills if resting; cancel on timeout.
        4. Lookup actual fill price.
        5. Transition PENDING -> OPEN (or FAILED).
        6. Place resting take-profit order if configured.
        """
        ticker = market.get("ticker", "")
        repo = db or getattr(self, "db", None)
        side_val = side.value if hasattr(side, "value") else str(side)

        # 1. Deterministic or unique client_order_id
        if not client_order_id:
            safe_env = re.sub(r"[^a-zA-Z0-9_-]", "_", str(getattr(repo, "environment", "demo")).lower())
            safe_strat = re.sub(r"[^a-zA-Z0-9_-]", "_", str(getattr(strategy_name, "value", strategy_name)).lower())
            safe_tick = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
            ts_suffix = str(int(time.time() * 1000))[-6:]
            client_order_id = f"{safe_env}_{safe_strat}_{safe_tick}_{side_val.lower()}_{ts_suffix}"[:64]

        # 2. Write-ahead intent (PENDING) before placing order
        if repo and hasattr(repo, "insert_pending_position"):
            close_time = market.get("close_time", "")
            repo.insert_pending_position(
                client_order_id=client_order_id,
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price_cents,
                close_time=close_time,
                strategy=strategy_name,
            )
        if repo and hasattr(repo, "log_event"):
            repo.log_event(
                stage="trader",
                level="INFO",
                message=f"Write intent: PENDING 1 contract on {side_val} for {ticker} at {price_cents}c",
                ticker=ticker,
                strategy=str(getattr(strategy_name, "value", strategy_name)),
            )

        # 3. Submit order
        order_res = None
        try:
            order_res = self.place_order(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price_cents,
                client_order_id=client_order_id,
                action="buy",
                time_in_force=time_in_force,
                **kwargs,
            )
        except Exception as e:
            if repo and hasattr(repo, "update_position_status"):
                repo.update_position_status(client_order_id, PositionStatus.FAILED)
            return {"error": str(e), "client_order_id": client_order_id}

        if not order_res or (isinstance(order_res, dict) and "error" in order_res):
            if repo and hasattr(repo, "update_position_status"):
                repo.update_position_status(client_order_id, PositionStatus.FAILED)
            return order_res or {"error": "order_rejected", "client_order_id": client_order_id}

        # 4. Check fill status / timeout polling
        order_data = order_res.get("order") if isinstance(order_res, dict) and "order" in order_res else order_res
        order_id = order_res.get("order_id") or (order_data.get("order_id") if isinstance(order_data, dict) else client_order_id)
        status = str(order_data.get("status") if isinstance(order_data, dict) else "executed").lower()

        # Check fill count
        fill_count = 0.0
        if isinstance(order_data, dict):
            fc_val = order_data.get("fill_count_fp") or order_data.get("fill_count") or (1.0 if status == "executed" else 0.0)
            try:
                fill_count = float(fc_val)
            except (ValueError, TypeError):
                fill_count = 1.0 if status == "executed" else 0.0
        else:
            fill_count = 1.0 if status == "executed" else 0.0

        is_filled = (not self.is_live) or (status == "executed") or (fill_count > 0)

        if self.is_live and not is_filled:
            timeout = entry_timeout if entry_timeout is not None else 0.5
            start_poll = time.time()
            while (time.time() - start_poll) < timeout:
                time.sleep(0.05)
                refreshed = self.get_order(order_id)
                if refreshed and isinstance(refreshed, dict):
                    ref_data = refreshed.get("order", refreshed)
                    ref_status = str(ref_data.get("status", "")).lower()
                    ref_fc_val = ref_data.get("fill_count_fp") or ref_data.get("fill_count") or 0.0
                    try:
                        ref_fc = float(ref_fc_val)
                    except (ValueError, TypeError):
                        ref_fc = 1.0 if ref_status == "executed" else 0.0
                    if ref_status == "executed" or ref_fc > 0:
                        order_data = ref_data
                        status = "executed"
                        fill_count = ref_fc
                        is_filled = True
                        break
                    elif ref_status == "canceled":
                        break

            if not is_filled:
                self.cancel_order(order_id)
                if repo and hasattr(repo, "conn"):
                    cursor = repo.conn.cursor()
                    cursor.execute(
                        "DELETE FROM positions WHERE client_order_id = ?",
                        (client_order_id,),
                    )
                    repo.conn.commit()
                return {
                    "order_id": order_id,
                    "client_order_id": client_order_id,
                    "ticker": ticker,
                    "side": side,
                    "status": "unfilled",
                    "entry_filled": False,
                }

        # 5. Extract actual execution fill price
        fill_price = price_cents
        if isinstance(order_data, dict):
            cost_str = order_data.get("taker_fill_cost_dollars")
            if cost_str:
                try:
                    fill_cost = int(round(float(cost_str) * 100))
                    if fill_cost > 0:
                        fill_price = fill_cost
                except (ValueError, TypeError):
                    pass

        # 6. Place resting take-profit order if configured
        resting_order_id = ""
        if resting_price_cents is not None:
            safe_strat = re.sub(r"[^a-zA-Z0-9_-]", "_", str(getattr(strategy_name, "value", strategy_name)).lower())
            safe_tick = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
            rest_cid = f"{safe_strat}_rest_{safe_tick}_{int(time.time() * 1000)}"[:64]
            try:
                rest_res = self.place_order(
                    ticker=ticker,
                    side=side,
                    count=count,
                    price_cents=resting_price_cents,
                    client_order_id=rest_cid,
                    action="sell",
                    time_in_force="good_till_canceled",
                )
                if rest_res and isinstance(rest_res, dict) and "error" not in rest_res:
                    resting_order_id = (
                        rest_res.get("order_id")
                        or (rest_res.get("order", {}).get("order_id") if isinstance(rest_res.get("order"), dict) else None)
                        or rest_cid
                    )
            except Exception as e:
                analytics.warning(f"Could not place resting order for {ticker}: {e}")

        # 7. Update to OPEN in SQLite
        if repo and hasattr(repo, "update_position_status"):
            repo.update_position_status(client_order_id, PositionStatus.OPEN, resting_order_id=resting_order_id)
            if fill_price != price_cents and hasattr(repo, "conn"):
                try:
                    repo.conn.execute(
                        "UPDATE positions SET price_cents = ? WHERE client_order_id = ?",
                        (fill_price, client_order_id),
                    )
                    repo.conn.commit()
                except Exception:
                    pass
        if repo and hasattr(repo, "log_event"):
            repo.log_event(
                stage="trader",
                level="INFO",
                message=f"Order filled: OPEN 1 contract on {side_val} for {ticker} at {fill_price}c",
                ticker=ticker,
                strategy=str(getattr(strategy_name, "value", strategy_name)),
            )

        return {
            "client_order_id": client_order_id,
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "price": fill_price,
            "price_cents": fill_price,
            "count": count,
            "status": "executed",
            "time_in_force": time_in_force or "good_till_canceled",
            "resting_order_id": resting_order_id,
            "resting_price_cents": resting_price_cents,
        }


class SimulatedBroker(BaseBroker):
    """
    Simulated broker for paper trading and unit testing.
    Executes all valid orders immediately in-memory without sending network requests.
    """
    def __init__(self, client: Optional[Any] = None, db: Optional[Any] = None) -> None:
        self.client = client
        self.db = db
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
        if self.client and hasattr(self.client, "cancel_order"):
            try:
                self.client.cancel_order(order_id)
            except Exception:
                pass
        order = self._orders.get(order_id)
        if order:
            order["status"] = "canceled"
            return {"status": "canceled", "order_id": order_id}
        return {"status": "canceled", "order_id": order_id}


class KalshiBroker(BaseBroker):
    """
    Live broker communicating directly with the Kalshi REST API client.
    """
    def __init__(self, client: Any, db: Optional[Any] = None) -> None:
        self.client = client
        self.db = db
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
