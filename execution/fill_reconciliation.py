from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

_log = logging.getLogger(__name__)

try:
    from gemini_client import GeminiTradingClient
except ImportError:
    GeminiTradingClient = None  # type: ignore[misc,assignment]


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _kalshi_fill_price(fill: dict[str, Any], side: str) -> float:
    side_l = (side or "").lower()
    if side_l == "yes":
        for k in ("yes_price_dollars", "yes_price", "price_dollars", "price"):
            p = _as_float(fill.get(k), 0.0)
            if p > 0:
                return p if p <= 1.0 else (p / 100.0)
    if side_l == "no":
        for k in ("no_price_dollars", "no_price", "price_dollars", "price"):
            p = _as_float(fill.get(k), 0.0)
            if p > 0:
                return p if p <= 1.0 else (p / 100.0)
    return 0.0


def _gemini_fill_price(fill: dict[str, Any]) -> float:
    for k in (
        "price",
        "avgPrice",
        "averagePrice",
        "fillPrice",
        "executionPrice",
        "avg_price",
        "fill_price",
        "execution_price",
    ):
        raw = fill.get(k)
        if raw is None or raw == "":
            continue
        p = _as_float(raw, 0.0)
        if p > 0:
            return p if p <= 1.0 else (p / 100.0)
    return 0.0


def _gemini_fill_qty(fill: dict[str, Any]) -> int:
    for k in ("quantity", "filledQuantity", "fillQuantity", "executedAmount", "size", "filledQty"):
        q = _as_int(fill.get(k), 0)
        if q > 0:
            return q
    return 0


def _parse_unix_ts(val: Any) -> float:
    """Parse API timestamps: unix seconds, millis, or ISO-8601 string."""
    if val is None:
        return time.time()
    if isinstance(val, (int, float)):
        x = float(val)
        if x > 1e12:  # milliseconds
            x /= 1000.0
        return x
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return time.time()
        if v.isdigit():
            x = float(v)
            if x > 1e12:
                x /= 1000.0
            return x
        try:
            vv = v.replace("Z", "+00:00") if v.endswith("Z") else v
            dt = datetime.fromisoformat(vv)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return time.time()
    return time.time()


def _gemini_execution_side(fill: dict[str, Any]) -> str:
    s = (
        fill.get("side")
        or fill.get("orderSide")
        or fill.get("order_side")
        or fill.get("action")
        or fill.get("type")
        or ""
    )
    return str(s).lower().strip()


def _gemini_contract_outcome(fill: dict[str, Any]) -> str:
    o = (
        fill.get("outcome")
        or fill.get("label")
        or fill.get("contractOutcome")
        or fill.get("contract_outcome")
        or fill.get("position")
        or ""
    )
    return str(o).lower().strip()


def _gemini_symbol(fill: dict[str, Any]) -> str:
    return str(
        fill.get("symbol")
        or fill.get("instrumentSymbol")
        or fill.get("instrument_symbol")
        or fill.get("marketSymbol")
        or ""
    ).strip()


def _collect_gemini_fill_rows(gemini_trading: Any) -> tuple[list[dict[str, Any]], str | None]:
    """
    Return raw fill dicts from Gemini using paginated fetch when available.
    """
    if hasattr(gemini_trading, "get_fills_paginated"):
        rows, path = gemini_trading.get_fills_paginated(page_size=200, max_pages=50, symbol=None)
        return list(rows or []), path
    page = gemini_trading.get_fills(limit=500, offset=0)
    if GeminiTradingClient is not None and hasattr(GeminiTradingClient, "parse_fills_response"):
        rows = GeminiTradingClient.parse_fills_response(page)
    else:
        rows = page.get("fills") or page.get("trades") or page.get("data") or []
        if not isinstance(rows, list):
            rows = []
    return [x for x in rows if isinstance(x, dict)], None


def _symbol_to_ticker_map(
    gemini_events_by_asset: dict[str, list[dict[str, Any]]],
    get_instrument_symbol: Callable[[dict[str, Any] | None, str], str | None],
) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for events in (gemini_events_by_asset or {}).values():
        for evt in events or []:
            tkr = str(evt.get("ticker") or "")
            if not tkr:
                continue
            for side in ("yes", "no"):
                sym = get_instrument_symbol(evt, side)
                if sym:
                    out[(str(sym), side)] = tkr
    return out


def reconcile_confirmed_fills(
    *,
    kalshi: Any,
    gemini_trading: Any | None,
    cfg: dict[str, Any],
    gemini_events_by_asset: dict[str, list[dict[str, Any]]],
    get_instrument_symbol: Callable[[dict[str, Any] | None, str], str | None],
    record_prediction_from_fill: Callable[..., Any] | None,
    platform_kalshi: str,
    platform_gemini: str,
    logger: Callable[[str], None] = print,
    lookback_hours: int = 48,
    diagnostics: bool = False,
) -> dict[str, int]:
    """
    Create predictions from confirmed fills only (not order-accepted events).
    Idempotency is handled by storage with platform+fill_id.
    """
    if not record_prediction_from_fill:
        return {
            "kalshi_seen": 0,
            "kalshi_inserted": 0,
            "kalshi_duplicate": 0,
            "kalshi_skipped": 0,
            "gemini_seen": 0,
            "gemini_inserted": 0,
            "gemini_duplicate": 0,
            "gemini_skipped": 0,
            "created_kalshi": 0,
            "created_gemini": 0,
        }

    created_k = 0
    created_g = 0
    min_ts = int(time.time() - max(1, int(lookback_hours)) * 3600)

    kalshi_seen = kalshi_dup = kalshi_skip = 0
    gemini_seen = gemini_dup = gemini_skip = 0

    # Kalshi fills
    try:
        cursor = ""
        while True:
            page = kalshi.get_fills(limit=200, min_ts=min_ts, **({"cursor": cursor} if cursor else {}))
            for f in page.get("fills") or []:
                action = (f.get("action") or "").lower()
                if action != "buy":
                    continue
                kalshi_seen += 1
                ticker = str(f.get("ticker") or "")
                side = str((f.get("side") or "").lower())
                if not ticker or side not in ("yes", "no"):
                    kalshi_skip += 1
                    continue
                qty = _as_int(f.get("count") or f.get("count_fp") or f.get("quantity") or f.get("filled_count"), 0)
                if qty <= 0:
                    kalshi_skip += 1
                    continue
                price = _kalshi_fill_price(f, side)
                if price <= 0:
                    kalshi_skip += 1
                    continue
                fill_id = str(
                    f.get("fill_id")
                    or f.get("id")
                    or f.get("trade_id")
                    or f"{f.get('order_id','')}-{f.get('created_time') or f.get('ts') or f.get('timestamp')}-{side}-{qty}-{price:.4f}"
                )
                oid = str(f.get("order_id") or "")
                fill_ts = _as_float(f.get("created_time") or f.get("ts") or f.get("timestamp"), time.time())
                rec = record_prediction_from_fill(
                    platform=platform_kalshi,
                    ticker=ticker,
                    side=side,
                    fill_id=fill_id,
                    order_id=oid,
                    entry_price=price,
                    count=qty,
                    fill_ts=fill_ts if fill_ts > 0 else time.time(),
                    source="kalshi_fills",
                    config=cfg,
                )
                status = str((rec or {}).get("status") or "")
                if status == "inserted":
                    created_k += 1
                elif status == "duplicate":
                    kalshi_dup += 1
                else:
                    kalshi_skip += 1
            cursor = page.get("cursor") or ""
            if not cursor:
                break
    except Exception as exc:
        _log.warning("Kalshi fill reconciliation failed: %s", exc, exc_info=True)
        logger(f"[Reconcile] Kalshi fills error: {exc}")

    # Gemini fills
    gemini_path_used: str | None = None
    gemini_raw_rows = 0
    if gemini_trading:
        try:
            sym_to_ticker = _symbol_to_ticker_map(gemini_events_by_asset, get_instrument_symbol)
            fills, gemini_path_used = _collect_gemini_fill_rows(gemini_trading)
            gemini_raw_rows = len(fills)
            if diagnostics and fills:
                logger(
                    f"[Reconcile] Gemini: fetched {len(fills)} raw fill row(s)"
                    f"{f' via {gemini_path_used}' if gemini_path_used else ''}"
                )
            for f in fills:
                fill_ts = _parse_unix_ts(
                    f.get("timestamp")
                    or f.get("createdAt")
                    or f.get("created_at")
                    or f.get("fillTime")
                    or f.get("time")
                )
                if fill_ts < float(min_ts):
                    continue
                side_exec = _gemini_execution_side(f)
                # Only record opening buys; skip sells and unknown sides unless API omits side on buy-only feed.
                if side_exec in ("sell", "ask"):
                    continue
                if side_exec not in ("buy", ""):
                    gemini_skip += 1
                    continue
                gemini_seen += 1
                outcome = _gemini_contract_outcome(f)
                if outcome not in ("yes", "no"):
                    gemini_skip += 1
                    continue
                symbol = _gemini_symbol(f)
                if not symbol:
                    gemini_skip += 1
                    continue
                qty = _gemini_fill_qty(f)
                if qty <= 0:
                    gemini_skip += 1
                    continue
                price = _gemini_fill_price(f)
                if price <= 0:
                    gemini_skip += 1
                    continue
                ticker = sym_to_ticker.get((symbol, outcome), symbol)
                fill_id = str(
                    f.get("fillId")
                    or f.get("fill_id")
                    or f.get("tradeId")
                    or f.get("trade_id")
                    or f.get("executionId")
                    or f.get("id")
                    or f"{f.get('orderId') or f.get('order_id')}-{fill_ts}-{outcome}-{qty}-{price:.4f}"
                )
                oid = str(f.get("orderId") or f.get("order_id") or "")
                rec = record_prediction_from_fill(
                    platform=platform_gemini,
                    ticker=ticker,
                    side=outcome,
                    fill_id=fill_id,
                    order_id=oid,
                    entry_price=price,
                    count=qty,
                    fill_ts=fill_ts if fill_ts > 0 else time.time(),
                    source="gemini_fills",
                    config=cfg,
                )
                status = str((rec or {}).get("status") or "")
                if status == "inserted":
                    created_g += 1
                elif status == "duplicate":
                    gemini_dup += 1
                else:
                    gemini_skip += 1
        except Exception as exc:
            _log.warning("Gemini fill reconciliation failed: %s", exc, exc_info=True)
            logger(f"[Reconcile] Gemini fills error: {exc}")

    stats = {
        "kalshi_seen": kalshi_seen,
        "kalshi_inserted": created_k,
        "kalshi_duplicate": kalshi_dup,
        "kalshi_skipped": kalshi_skip,
        "gemini_seen": gemini_seen,
        "gemini_inserted": created_g,
        "gemini_duplicate": gemini_dup,
        "gemini_skipped": gemini_skip,
        "created_kalshi": created_k,
        "created_gemini": created_g,
        "gemini_raw_rows": gemini_raw_rows,
        "gemini_fills_endpoint": gemini_path_used,
    }
    if diagnostics or created_k or created_g or gemini_raw_rows > 0:
        extra = ""
        if diagnostics and gemini_path_used is not None:
            extra = f" | Gemini path={gemini_path_used} raw_rows={gemini_raw_rows}"
        logger(
            "[Reconcile] "
            f"Kalshi seen={kalshi_seen} inserted={created_k} dup={kalshi_dup} skipped={kalshi_skip} | "
            f"Gemini seen={gemini_seen} inserted={created_g} dup={gemini_dup} skipped={gemini_skip}"
            f"{extra}"
        )
    return stats
