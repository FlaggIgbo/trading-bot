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
    """Kalshi v2 Fill uses dollar strings (e.g. yes_price_dollars); legacy int cents also supported."""
    side_l = (side or "").lower()
    if side_l == "yes":
        for k in ("yes_price_dollars", "yes_price_fixed", "yes_price", "price_dollars", "price"):
            p = _as_float(fill.get(k), 0.0)
            if p > 0:
                return p if p <= 1.0 else (p / 100.0)
    if side_l == "no":
        for k in ("no_price_dollars", "no_price_fixed", "no_price", "price_dollars", "price"):
            p = _as_float(fill.get(k), 0.0)
            if p > 0:
                return p if p <= 1.0 else (p / 100.0)
    return 0.0


def _normalize_kalshi_fill(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    merged: dict[str, Any] = dict(raw)
    for nest_key in ("fill", "data", "execution", "result"):
        nested = raw.get(nest_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                if merged.get(k) in (None, ""):
                    merged[k] = v
    return merged


def _kalshi_fill_qty(fill: dict[str, Any]) -> int:
    """Prefer count_fp (v2 fixed-point string, e.g. '10.00') over legacy integer count."""
    cf = fill.get("count_fp")
    if cf is not None and str(cf).strip():
        try:
            q = int(float(str(cf).strip()))
            if q > 0:
                return q
        except (TypeError, ValueError):
            pass
    for k in ("count", "quantity", "filled_count", "size", "filled_qty"):
        q = _as_int(fill.get(k), 0)
        if q > 0:
            return q
    return 0


def _kalshi_fill_timestamp(fill: dict[str, Any]) -> float:
    """Kalshi v2 uses ISO8601 created_time; legacy ts is unix seconds."""
    return _parse_unix_ts(
        fill.get("created_time")
        or fill.get("ts")
        or fill.get("timestamp")
        or fill.get("created_at")
    )


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
        or fill.get("executionSide")
        or fill.get("execution_side")
        or fill.get("exec_side")
        or fill.get("action")
        or fill.get("type")
        or ""
    )
    sl = str(s).lower().strip()
    if sl in ("buy", "bid", "b", "purchase", "long", "maker_buy", "taker_buy"):
        return "buy"
    if sl in ("sell", "ask", "s", "short", "maker_sell", "taker_sell"):
        return "sell"
    return sl


def _gemini_contract_outcome(fill: dict[str, Any]) -> str:
    o = (
        fill.get("outcome")
        or fill.get("label")
        or fill.get("contractOutcome")
        or fill.get("contract_outcome")
        or fill.get("contractSide")
        or fill.get("contract_side")
        or fill.get("legOutcome")
        or fill.get("leg_outcome")
        or fill.get("position")
        or fill.get("side") # Some Gemini endpoints put YES/NO in 'side'
        or ""
    )
    s = str(o).lower().strip()
    if s in ("yes", "no"):
        return s
    if s in ("y", "n"):
        return "yes" if s == "y" else "no"
    for k in ("isYes", "is_yes", "yesLeg", "yes_leg"):
        v = fill.get(k)
        if isinstance(v, bool):
            return "yes" if v else "no"
    return ""


def _gemini_symbol(fill: dict[str, Any]) -> str:
    return str(
        fill.get("symbol")
        or fill.get("instrumentSymbol")
        or fill.get("instrument_symbol")
        or fill.get("marketSymbol")
        or fill.get("market_symbol")
        or fill.get("instrument")
        or ""
    ).strip()


def _merge_gemini_fill_nested(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested fill/trade objects so price/outcome live on one dict."""
    if not isinstance(raw, dict):
        return {}
    merged: dict[str, Any] = dict(raw)
    for nest_key in ("fill", "execution", "trade", "data", "details", "result"):
        nested = raw.get(nest_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                cur = merged.get(k)
                if cur is None or cur == "":
                    merged[k] = v
    return merged


def _get_limit_order_hints(platform: str, order_id: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    if not order_id:
        return None
    try:
        from storage import get_limit_order_hints_by_order_id

        return get_limit_order_hints_by_order_id(platform, order_id, cfg)
    except ImportError:
        return None


def _ticker_from_gemini_symbol(
    sym_to_ticker: dict[tuple[str, str], str],
    symbol: str,
    outcome: str,
) -> str:
    s = str(symbol or "").strip()
    if not s:
        return ""
    o = outcome.lower()
    return (
        sym_to_ticker.get((s, o), "")
        or sym_to_ticker.get((s.upper(), o), "")
        or sym_to_ticker.get((s.lower(), o), "")
        or s
    )


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
            tkr = str(evt.get("ticker") or "").strip()
            if not tkr:
                continue
            for side in ("yes", "no"):
                sym = get_instrument_symbol(evt, side)
                if not sym:
                    continue
                raw = str(sym).strip()
                for sk in {raw, raw.upper(), raw.lower()}:
                    out[(sk, side)] = tkr
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
    kalshi_raw_rows = 0
    gemini_seen = gemini_dup = gemini_skip = 0

    # Kalshi fills (Trade API v2: market_ticker alias, count_fp string, ISO created_time)
    try:
        cursor = ""
        while True:
            page = kalshi.get_fills(limit=200, min_ts=min_ts, **({"cursor": cursor} if cursor else {}))
            rows = page.get("fills") or []
            kalshi_raw_rows += len(rows)
            for raw in rows:
                f = _normalize_kalshi_fill(raw if isinstance(raw, dict) else {})
                action = str(f.get("action") or "").lower()
                if action != "buy":
                    continue
                kalshi_seen += 1
                ticker = str(f.get("ticker") or f.get("market_ticker") or "").strip()
                side = str(f.get("side") or "").lower().strip()
                oid = str(f.get("order_id") or "")
                if not ticker and oid:
                    kh = _get_limit_order_hints(platform_kalshi, oid, cfg)
                    if kh and kh.get("ticker"):
                        ticker = str(kh["ticker"]).strip()
                if not ticker or side not in ("yes", "no"):
                    kalshi_skip += 1
                    continue
                fill_ts = _kalshi_fill_timestamp(f)
                if fill_ts < float(min_ts):
                    kalshi_skip += 1
                    continue
                qty = _kalshi_fill_qty(f)
                if qty <= 0:
                    kalshi_skip += 1
                    continue
                price = _kalshi_fill_price(f, side)
                if price <= 0:
                    kalshi_skip += 1
                    continue
                fill_id = str(
                    f.get("fill_id")
                    or f.get("trade_id")
                    or f.get("id")
                    or f"{oid}-{fill_ts}-{side}-{qty}-{price:.4f}"
                )
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
        if kalshi_raw_rows > 0 and kalshi_seen == 0:
            _log.warning(
                "Kalshi fill reconciliation: %d raw fill row(s) but 0 BUY legs in window — check action filter or payload",
                kalshi_raw_rows,
            )
            logger(
                "[Reconcile] Kalshi: received fills but parsed 0 BUY rows in lookback "
                "(all sells or empty pages?)."
            )
        elif kalshi_seen > 0 and created_k == 0 and kalshi_dup == 0 and kalshi_skip >= kalshi_seen:
            _log.warning(
                "Kalshi fill reconciliation: %d BUY fill(s) seen but none inserted (all skipped/errors); check ticker/price/qty parsing",
                kalshi_seen,
            )
            logger(
                "[Reconcile] Kalshi: BUY fills seen=%d but inserted=0 dup=0 — inspect fill payload vs parsers."
                % kalshi_seen
            )
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
            for raw_row in fills:
                f = _merge_gemini_fill_nested(raw_row)
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
                # Only record opening buys; skip sells; allow empty side on buy-only feeds.
                if side_exec in ("sell", "ask", "short"):
                    continue
                if side_exec not in ("buy", ""):
                    gemini_skip += 1
                    continue
                gemini_seen += 1
                outcome = _gemini_contract_outcome(f)
                oid = str(f.get("orderId") or f.get("order_id") or "")
                hints = _get_limit_order_hints(platform_gemini, oid, cfg) if oid else None
                if outcome not in ("yes", "no") and hints:
                    hs = str(hints.get("side") or "").lower().strip()
                    if hs in ("yes", "no"):
                        outcome = hs
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
                ticker = _ticker_from_gemini_symbol(sym_to_ticker, symbol, outcome)
                if hints and hints.get("ticker"):
                    ht = str(hints["ticker"]).strip()
                    if ht:
                        ticker = ht
                fill_id = str(
                    f.get("fillId")
                    or f.get("fill_id")
                    or f.get("tradeId")
                    or f.get("trade_id")
                    or f.get("executionId")
                    or f.get("id")
                    or f"{f.get('orderId') or f.get('order_id')}-{fill_ts}-{outcome}-{qty}-{price:.4f}"
                )
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
            if gemini_raw_rows > 0 and gemini_seen == 0:
                _log.warning(
                    "Gemini fill reconciliation: %d raw row(s) from API but 0 matched as buy+YES/NO in lookback — "
                    "check response shape or extend parsers",
                    gemini_raw_rows,
                )
                logger(
                    "[Reconcile] Gemini: received raw fills but parsed 0 eligible buy legs in window; "
                    "try --reconcile-diagnostics or inspect API payload."
                )
        except Exception as exc:
            _log.warning("Gemini fill reconciliation failed: %s", exc, exc_info=True)
            logger(f"[Reconcile] Gemini fills error: {exc}")

    stats = {
        "kalshi_seen": kalshi_seen,
        "kalshi_inserted": created_k,
        "kalshi_duplicate": kalshi_dup,
        "kalshi_skipped": kalshi_skip,
        "kalshi_raw_rows": kalshi_raw_rows,
        "gemini_seen": gemini_seen,
        "gemini_inserted": created_g,
        "gemini_duplicate": gemini_dup,
        "gemini_skipped": gemini_skip,
        "created_kalshi": created_k,
        "created_gemini": created_g,
        "gemini_raw_rows": gemini_raw_rows,
        "gemini_fills_endpoint": gemini_path_used,
    }
    if diagnostics or created_k or created_g or kalshi_raw_rows > 0 or gemini_raw_rows > 0:
        extra_k = f" raw_rows={kalshi_raw_rows}" if diagnostics and kalshi_raw_rows else ""
        extra_g = ""
        if diagnostics and gemini_path_used is not None:
            extra_g = f" | Gemini path={gemini_path_used} raw_rows={gemini_raw_rows}"
        elif diagnostics and gemini_raw_rows:
            extra_g = f" | Gemini raw_rows={gemini_raw_rows}"
        logger(
            "[Reconcile] "
            f"Kalshi seen={kalshi_seen} inserted={created_k} dup={kalshi_dup} skipped={kalshi_skip}{extra_k} | "
            f"Gemini seen={gemini_seen} inserted={created_g} dup={gemini_dup} skipped={gemini_skip}"
            f"{extra_g}"
        )
    return stats
