from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Any, Callable


@dataclass
class OrderSyncResult:
    kalshi_keep_buys: set[tuple[str, str]] = field(default_factory=set)
    kalshi_keep_sells: set[tuple[str, str]] = field(default_factory=set)
    gemini_keep_buys: set[tuple[str, str]] = field(default_factory=set)
    gemini_keep_sells: set[tuple[str, str]] = field(default_factory=set)
    cancels_done: int = 0


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _order_created_ts(order: dict[str, Any]) -> float | None:
    """Best-effort parse of order created timestamp across APIs."""
    if not isinstance(order, dict):
        return None
    for key in (
        "created_time",
        "created_at",
        "createdAt",
        "created_ts",
        "updated_at",
        "timestamp",
    ):
        val = order.get(key)
        if val is None:
            continue
        try:
            if isinstance(val, (int, float)):
                ts = float(val)
                if ts > 1e12:
                    ts = ts / 1000.0
                return ts if ts > 0 else None
            s = str(val).strip()
            if not s:
                continue
            if s.isdigit():
                ts = float(s)
                if ts > 1e12:
                    ts = ts / 1000.0
                return ts if ts > 0 else None
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            continue
    return None


def _is_order_stale(order: dict[str, Any], ttl_minutes: int, now_ts: float | None = None) -> bool:
    if ttl_minutes <= 0:
        return False
    created_ts = _order_created_ts(order)
    if created_ts is None:
        return False
    now_v = float(now_ts if now_ts is not None else time.time())
    return (now_v - created_ts) >= (float(ttl_minutes) * 60.0)


def sync_and_cancel_resting_orders(
    *,
    kalshi: Any,
    gemini_trading: Any | None,
    pending_orders: list[dict[str, Any]],
    pending_sells: list[dict[str, Any]],
    regime_shift_active: bool,
    limit_order_ttl_minutes: int,
    passive_bid_chase_enabled: bool = False,
    passive_bid_chase_refresh_minutes: int = 0,
    resting_cancel_if_market_bid_above_limit_by: float = 0.0,
    platform_kalshi: str,
    platform_gemini: str,
    gemini_events_by_asset: dict[str, list[dict[str, Any]]],
    find_gemini_event: Callable[[dict[str, list[dict[str, Any]]], str], dict[str, Any] | None],
    get_instrument_symbol: Callable[[dict[str, Any] | None, str], str | None],
    record_order_cancel: Callable[..., Any] | None,
    mark_prediction_cancelled: Callable[..., Any] | None,
    cfg: dict[str, Any],
    logger: Callable[[str], None] = print,
) -> OrderSyncResult:
    """
    Sync open orders against current recommendations:
    - keep if same/better than pending recommendation
    - cancel if stale / no longer recommended / repricing needed
    """
    result = OrderSyncResult()

    near_expiry_dte = float(cfg.get("strategy", {}).get("near_expiry_keep_dte", 0.02))

    kalshi_pending_buys = {(o["ticker"], o["side"]): o for o in pending_orders if o.get("platform") == platform_kalshi}
    kalshi_pending_sells = {(s["ticker"], s["side"]): s for s in pending_sells if s.get("platform") == platform_kalshi}
    gemini_pending_buys: dict[tuple[str, str], dict[str, Any]] = {}
    gemini_pending_sells: dict[tuple[str, str], dict[str, Any]] = {}

    for o in pending_orders:
        if o.get("platform") != platform_gemini:
            continue
        sym = o.get("instrument_symbol")
        if not sym:
            evt = o.get("gemini_evt") or find_gemini_event(gemini_events_by_asset, o.get("ticker", ""))
            sym = get_instrument_symbol(evt, o["side"]) if evt else None
        if sym:
            gemini_pending_buys[(str(sym), o["side"])] = o

    for s in pending_sells:
        if s.get("platform") != platform_gemini:
            continue
        sym = s.get("instrument_symbol")
        if not sym:
            evt = s.get("gemini_evt") or find_gemini_event(gemini_events_by_asset, s.get("ticker", ""))
            sym = get_instrument_symbol(evt, s["side"]) if evt else None
        if sym:
            gemini_pending_sells[(str(sym), s["side"])] = s

    to_cancel: list[dict[str, Any]] = []
    now_ts = time.time()

    try:
        cursor = ""
        while True:
            page = kalshi.get_orders(status="resting", limit=200, **({"cursor": cursor} if cursor else {}))
            for ord_ in page.get("orders") or []:
                oid = ord_.get("order_id")
                ticker = ord_.get("ticker")
                side = (ord_.get("side") or "").lower()
                action = (ord_.get("action") or "").lower()
                if not oid or not ticker or not side:
                    continue

                yes_d = ord_.get("yes_price_dollars")
                no_d = ord_.get("no_price_dollars")
                old_price = _safe_float(yes_d if side == "yes" else no_d, 0.0)
                label = f"{ticker} {action} {side}"

                if action == "buy":
                    pend = kalshi_pending_buys.get((ticker, side))
                    if regime_shift_active:
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "regime_shift_guard",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    elif (
                        resting_cancel_if_market_bid_above_limit_by > 0
                        and pend
                        and _safe_float(pend.get("bid_at_place"), 0.0)
                        >= old_price + resting_cancel_if_market_bid_above_limit_by
                    ):
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "market_bid_lifted_past_limit",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    elif (
                        passive_bid_chase_enabled
                        and passive_bid_chase_refresh_minutes > 0
                        and pend
                        and _is_order_stale(ord_, passive_bid_chase_refresh_minutes, now_ts)
                    ):
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "bid_chase_refresh",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    elif _is_order_stale(ord_, limit_order_ttl_minutes, now_ts):
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "stale_ttl",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    elif not pend:
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "no_longer_rec",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    elif _safe_float(pend.get("price"), 0.0) > old_price:
                        pend_dte = _safe_float(pend.get("dte"), 999.0)
                        if pend_dte < near_expiry_dte:
                            result.kalshi_keep_buys.add((ticker, side))
                        else:
                            to_cancel.append(
                                {
                                    "platform": platform_kalshi,
                                    "order_id": oid,
                                    "label": label,
                                    "reason": "want_better_price",
                                    "ticker": ticker,
                                    "side": side,
                                    "old_price": old_price,
                                }
                            )
                    else:
                        result.kalshi_keep_buys.add((ticker, side))
                else:
                    pend = kalshi_pending_sells.get((ticker, side))
                    if not pend:
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "no_longer_rec",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    elif _safe_float(pend.get("price"), 0.0) > old_price:
                        to_cancel.append(
                            {
                                "platform": platform_kalshi,
                                "order_id": oid,
                                "label": label,
                                "reason": "want_better_price",
                                "ticker": ticker,
                                "side": side,
                                "old_price": old_price,
                            }
                        )
                    else:
                        result.kalshi_keep_sells.add((ticker, side))

            cursor = page.get("cursor") or ""
            if not cursor:
                break
    except Exception:
        pass

    if gemini_trading:
        try:
            active = gemini_trading.get_active_orders(limit=200)
            for ord_ in active.get("orders") or []:
                oid = ord_.get("orderId") or ord_.get("order_id")
                symbol = ord_.get("symbol") or ord_.get("instrumentSymbol")
                outcome = (ord_.get("outcome") or ord_.get("label") or "").lower()
                side = (ord_.get("side") or "").lower()
                if not oid or not symbol or outcome not in ("yes", "no"):
                    continue
                old_price = _safe_float(ord_.get("price") or ord_.get("originalAmount"), 0.0)
                key = (str(symbol), outcome)
                label = f"{symbol} {side} {outcome}"

                if side == "buy":
                    pend = gemini_pending_buys.get(key)
                    if regime_shift_active:
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "regime_shift_guard",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    elif (
                        resting_cancel_if_market_bid_above_limit_by > 0
                        and pend
                        and _safe_float(pend.get("bid_at_place"), 0.0)
                        >= old_price + resting_cancel_if_market_bid_above_limit_by
                    ):
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "market_bid_lifted_past_limit",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    elif (
                        passive_bid_chase_enabled
                        and passive_bid_chase_refresh_minutes > 0
                        and pend
                        and _is_order_stale(ord_, passive_bid_chase_refresh_minutes, now_ts)
                    ):
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "bid_chase_refresh",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    elif _is_order_stale(ord_, limit_order_ttl_minutes, now_ts):
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "stale_ttl",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    elif not pend:
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "no_longer_rec",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    elif _safe_float(pend.get("price"), 0.0) > old_price:
                        pend_dte = _safe_float(pend.get("dte"), 999.0)
                        if pend_dte < near_expiry_dte:
                            result.gemini_keep_buys.add(key)
                        else:
                            to_cancel.append(
                                {
                                    "platform": platform_gemini,
                                    "order_id": str(oid),
                                    "label": label,
                                    "reason": "want_better_price",
                                    "ticker": str(symbol),
                                    "side": outcome,
                                    "old_price": old_price,
                                }
                            )
                    else:
                        result.gemini_keep_buys.add(key)
                else:
                    pend = gemini_pending_sells.get(key)
                    if not pend:
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "no_longer_rec",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    elif _safe_float(pend.get("price"), 0.0) > old_price:
                        to_cancel.append(
                            {
                                "platform": platform_gemini,
                                "order_id": str(oid),
                                "label": label,
                                "reason": "want_better_price",
                                "ticker": str(symbol),
                                "side": outcome,
                                "old_price": old_price,
                            }
                        )
                    else:
                        result.gemini_keep_sells.add(key)
        except Exception:
            pass

    for item in to_cancel:
        platform = item["platform"]
        order_id = item["order_id"]
        label = item["label"]
        reason = item.get("reason", "no_longer_rec")
        if platform == platform_kalshi:
            ok = kalshi.cancel_order_safe(order_id, ticker=label)
        elif platform == platform_gemini and gemini_trading:
            ok = gemini_trading.cancel_order_safe(order_id, ticker_label=label)
        else:
            ok = False
        if not ok:
            continue
        if record_order_cancel:
            record_order_cancel(
                platform,
                order_id,
                reason,
                cfg,
                ticker=item.get("ticker"),
                side=item.get("side"),
                limit_price=item.get("old_price"),
            )
        if mark_prediction_cancelled:
            mark_prediction_cancelled(
                platform=platform,
                order_id=str(order_id),
                cancel_reason=str(reason),
                config=cfg,
            )
        logger(f"  Cancelled ({'Kalshi' if platform == platform_kalshi else 'Gemini'}) {label} — {reason}")
        result.cancels_done += 1

    return result
