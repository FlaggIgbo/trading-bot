#!/usr/bin/env python3
"""
Performance report directly from Kalshi and Gemini APIs.
Useful when local storage (SQLite) is unavailable or not synced.

Fetches recent fills and matches them with settlement data to calculate PnL.
Can also sync this data into the local SQLite database.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config
from signals.kalshi_client import KalshiClient
from signals.gemini_client import GeminiTradingClient, _extract_threshold_from_title

# Optional storage imports for --sync
try:
    from data.storage import (
        record_prediction_from_fill,
        record_prediction_outcome,
        record_resolved,
        resolve_signal_outcomes,
        get_unresolved_predictions
    )
    STORAGE_AVAILABLE = True
except ImportError:
    STORAGE_AVAILABLE = False


def _as_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_ts(val) -> float:
    """Parse API timestamps: unix seconds, millis, or ISO-8601 string."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        x = float(val)
        if x > 1e12:  # milliseconds
            x /= 1000.0
        return x
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return 0.0
        if v.isdigit():
            x = float(v)
            if x > 1e12:
                x /= 1000.0
            return x
        try:
            # Handle fractional seconds and Z suffix
            vv = v.replace("Z", "+00:00")
            # If there's more than 6 digits for microseconds, truncate them (Python 3.9- compatibility)
            if "." in vv:
                base, rest = vv.split(".")
                tz_part = ""
                if "+" in rest:
                    rest, tz_part = rest.split("+")
                    tz_part = "+" + tz_part
                elif "-" in rest:
                    rest, tz_part = rest.split("-")
                    tz_part = "-" + tz_part
                
                if len(rest) > 6:
                    rest = rest[:6]
                vv = f"{base}.{rest}{tz_part}"
                
            dt = datetime.fromisoformat(vv)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            # Try basic unix if ISO fails
            try:
                return float(v)
            except:
                return 0.0
    return 0.0


def fetch_kalshi_performance(client: KalshiClient, days: int = 7, sync: bool = False, cfg: dict | None = None):
    print(f"[*] Fetching Kalshi performance (last {days} days)...")
    min_ts = int(time.time() - days * 86400)
    
    # 1. Fetch settlements
    settlements = {}
    cursor = ""
    while True:
        try:
            r = client.get_settlements(limit=200, cursor=cursor)
            rows = r.get("settlements") or []
            for s in rows:
                t = s.get("ticker") or s.get("market_ticker")
                if t:
                    settlements[t] = s
            cursor = r.get("cursor") or ""
            if not cursor or not rows:
                break
            # Use settled_time to stop pagination
            last_st = _parse_ts(rows[-1].get("settled_time"))
            if last_st > 0 and last_st < min_ts:
                break
        except Exception as e:
            print(f"[!] Kalshi settlements error: {e}")
            break

    # 2. Fetch fills
    fills = []
    cursor = ""
    while True:
        try:
            r = client.get_fills(limit=200, min_ts=min_ts, cursor=cursor)
            rows = r.get("fills") or []
            fills.extend(rows)
            cursor = r.get("cursor") or ""
            if not cursor or not rows:
                break
        except Exception as e:
            print(f"[!] Kalshi fills error: {e}")
            break

    # 3. Process
    trades = []
    for f in fills:
        if f.get("action") != "buy":
            continue
        ticker = f.get("ticker") or f.get("market_ticker")
        side = f.get("side")
        qty = int(float(f.get("count_fp") or f.get("count") or 0))
        
        # Kalshi fill price logic
        price_cents = 0.0
        if side == "yes":
            price_cents = _as_float(f.get("yes_price") or f.get("yes_price_dollars") or 0)
        else:
            price_cents = _as_float(f.get("no_price") or f.get("no_price_dollars") or 0)
        
        if price_cents <= 1.0: # If it's in dollars
            price_cents *= 100.0
        
        ts = _parse_ts(f.get("created_time") or f.get("ts"))
        oid = f.get("order_id")
        fid = f.get("fill_id") or f.get("trade_id") or f.get("id")
        
        # Sync to DB if requested
        db_id = None
        if sync and STORAGE_AVAILABLE:
            res = record_prediction_from_fill(
                platform="kalshi",
                ticker=ticker,
                side=side,
                fill_id=str(fid),
                order_id=str(oid) if oid else None,
                entry_price=price_cents / 100.0,
                count=qty,
                fill_ts=ts,
                config=cfg,
                source="api_performance_sync"
            )
            db_id = res.get("id")

        settle = settlements.get(ticker)
        outcome = "open"
        pnl = 0.0
        if settle:
            # market_result is 'yes' or 'no'
            res = (settle.get("market_result") or settle.get("result") or "").lower()
            if res:
                yes_settled = (res == "yes")
                won = (side == res)
                outcome = "win" if won else "lose"
                pnl = (100 - price_cents) * qty if won else (-price_cents * qty)
                
                if sync and STORAGE_AVAILABLE:
                    if db_id is None:
                        resolve_signal_outcomes(ticker, yes_settled, cfg)
                        record_resolved(ticker=ticker, yes_settled=yes_settled, platform="kalshi", config=cfg)
                    else:
                        record_prediction_outcome(db_id, yes_settled, int(pnl), config=cfg)
                        resolve_signal_outcomes(ticker, yes_settled, cfg)
                        record_resolved(ticker=ticker, yes_settled=yes_settled, platform="kalshi", config=cfg)

        trades.append({
            "ts": ts,
            "platform": "Kalshi",
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": int(round(price_cents)),
            "outcome": outcome,
            "pnl_cents": pnl
        })
    
    return trades


def fetch_gemini_performance(client: GeminiTradingClient, days: int = 7, sync: bool = False, cfg: dict | None = None):
    print(f"[*] Fetching Gemini performance (last {days} days)...")
    min_ts = int(time.time() - days * 86400)
    
    # 1. Fetch settled events to get outcomes
    settled_outcomes = {} 
    try:
        for cat in ("crypto", "commodities"):
            offset = 0
            while True:
                r = client.get_events(category=cat, status="settled", limit=200, offset=offset)
                data = r.get("data") or []
                for evt in data:
                    et = evt.get("ticker")
                    if not et: continue
                    settled_outcomes[et] = {"_raw": evt}
                    for c in evt.get("contracts") or []:
                        rs = (c.get("resolutionSide") or "").lower()
                        if rs in ("yes", "no"):
                            label = c.get("label") or c.get("abbreviatedName") or ""
                            thresh, _ = _extract_threshold_from_title(label)
                            sym = c.get("instrumentSymbol")
                            if sym: settled_outcomes[et][sym] = rs
                            if thresh: settled_outcomes[et][thresh] = rs
                if len(data) < 200: break
                offset += len(data)
                last_ts = _parse_ts(data[-1].get("expiryDate") or data[-1].get("resolvedAt"))
                if last_ts > 0 and last_ts < min_ts - 86400: break
    except Exception as e:
        print(f"[!] Gemini events error: {e}")

    # 2. Fetch fills
    fills = []
    try:
        if hasattr(client, "get_fills_paginated"):
            raw_fills, _ = client.get_fills_paginated(page_size=200, max_pages=10)
            fills = raw_fills
        else:
            r = client.get_fills(limit=500)
            if hasattr(client, "parse_fills_response"):
                fills = client.parse_fills_response(r)
            elif isinstance(r, list):
                fills = r
            else:
                fills = r.get("fills") or r.get("trades") or r.get("data") or []
    except Exception as e:
        print(f"[!] Gemini fills error: {e}")

    # 3. Process
    trades = []
    for raw_f in fills:
        if not isinstance(raw_f, dict): continue
        # Robust flattening (matches execution/fill_reconciliation logic)
        f = raw_f.copy()
        for k in ("fill", "execution", "trade", "data", "details", "order"):
            nested = raw_f.get(k)
            if isinstance(nested, dict):
                for nk, nv in nested.items():
                    if f.get(nk) in (None, ""):
                        f[nk] = nv

        ts = _parse_ts(
            f.get("timestamp") or f.get("timestampms") or f.get("fillTime") or 
            f.get("createdAt") or f.get("created_at") or f.get("time")
        )
        if ts > 0 and ts < min_ts: continue
        
        side_exec = (f.get("side") or f.get("orderSide") or f.get("executionSide") or "").lower()
        if side_exec == "sell": continue
        
        sym = f.get("symbol") or f.get("instrumentSymbol") or f.get("marketSymbol")
        outcome_side = (
            f.get("outcome") or f.get("contractOutcome") or f.get("contractSide") or 
            f.get("label") or f.get("position") or ""
        ).lower()
        
        qty = int(float(
            f.get("filledQuantity") or f.get("quantity") or f.get("fillQuantity") or 
            f.get("executedAmount") or f.get("amount") or 0
        ))
        if qty <= 0: continue

        price = _as_float(
            f.get("avgExecutionPrice") or f.get("price") or f.get("avgPrice") or 
            f.get("averagePrice") or f.get("fillPrice") or 0
        )
        if price > 1.0: price /= 100.0
        price_cents = price * 100
        
        oid = f.get("orderId") or f.get("order_id")
        fid = f.get("fillId") or f.get("fill_id") or f.get("tradeId") or f.get("trade_id") or f.get("executionId")
        
        # Sync to DB if requested
        db_id = None
        if sync and STORAGE_AVAILABLE:
            res = record_prediction_from_fill(
                platform="gemini",
                ticker=sym, 
                side=outcome_side,
                fill_id=str(fid),
                order_id=str(oid) if oid else None,
                entry_price=price,
                count=qty,
                fill_ts=ts,
                config=cfg,
                source="api_performance_sync"
            )
            db_id = res.get("id")

        # Try to find outcome
        outcome = "open"
        pnl = 0.0
        found_res = None
        event_ticker = None
        for et, contracts in settled_outcomes.items():
            if sym in contracts:
                found_res = contracts[sym]
                event_ticker = et
                break
        
        if found_res:
            yes_settled = (found_res == "yes")
            won = (outcome_side == found_res)
            outcome = "win" if won else "lose"
            pnl = (100 - price_cents) * qty if won else (-price_cents * qty)
            
            if sync and STORAGE_AVAILABLE:
                if db_id is not None:
                    record_prediction_outcome(db_id, yes_settled, int(pnl), config=cfg)
                if event_ticker:
                    resolve_signal_outcomes(event_ticker, yes_settled, cfg)
                    record_resolved(ticker=event_ticker, yes_settled=yes_settled, platform="gemini", config=cfg)

        trades.append({
            "ts": ts,
            "platform": "Gemini",
            "ticker": sym,
            "side": outcome_side,
            "qty": qty,
            "price": int(round(price_cents)),
            "outcome": outcome,
            "pnl_cents": pnl
        })
        
    return trades


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--sync", action="store_true", help="Sync data to local SQLite database")
    args = parser.parse_args()

    cfg = get_config()
    
    if args.sync and not STORAGE_AVAILABLE:
        print("[!] Storage module not available; --sync will be ignored.")

    all_trades = []
    
    try:
        k_client = KalshiClient()
        all_trades.extend(fetch_kalshi_performance(k_client, args.days, args.sync, cfg))
    except Exception as e:
        print(f"[!] Could not init Kalshi: {e}")

    try:
        g_client = GeminiTradingClient()
        all_trades.extend(fetch_gemini_performance(g_client, args.days, args.sync, cfg))
    except Exception as e:
        print(f"[!] Could not init Gemini: {e}")

    if not all_trades:
        print("No trades found in the last %d days." % args.days)
        return

    # Sort by time
    all_trades.sort(key=lambda x: x["ts"])

    print("\n" + "="*80)
    print(f"{'DATE (UTC)':<20} {'PLATFORM':<10} {'TICKER':<30} {'SIDE':<5} {'PRICE':<5} {'QTY':<5} {'RESULT':<8} {'PNL'}")
    print("-" * 80)
    
    total_pnl = 0.0
    wins = 0
    losses = 0
    
    for t in all_trades:
        ts_val = t["ts"]
        if ts_val <= 0:
            dt = "  ---  "
        else:
            dt = datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            
        pnl_str = f"${t['pnl_cents']/100:>7.2f}" if t["outcome"] != "open" else "  ---  "
        print(f"{dt:<20} {t['platform']:<10} {t['ticker'][:30]:<30} {t['side']:<5} {t['price']:>3}¢ {t['qty']:>5} {t['outcome']:<8} {pnl_str}")
        
        if t["outcome"] == "win":
            wins += 1
            total_pnl += t["pnl_cents"]
        elif t["outcome"] == "lose":
            losses += 1
            total_pnl += t["pnl_cents"]

    print("-" * 80)
    print(f"TOTAL PNL: ${total_pnl/100:.2f}")
    if (wins + losses) > 0:
        print(f"WIN RATE:  {wins/(wins+losses):.1%} ({wins}W, {losses}L)")
    print("="*80 + "\n")
    
    if args.sync:
        print("[*] Sync completed. Local database updated.")


if __name__ == "__main__":
    main()
