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
            vv = v.replace("Z", "+00:00") if v.endswith("Z") else v
            dt = datetime.fromisoformat(vv)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
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
            if _parse_ts(rows[-1].get("settled_time")) < min_ts:
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
        ticker = f.get("ticker")
        side = f.get("side")
        qty = int(float(f.get("count_fp") or f.get("count") or 0))
        price_cents = _as_float(f.get("yes_price") or f.get("no_price") or f.get("price") or 0)
        if price_cents == 0:
            pd = _as_float(f.get("yes_price_dollars") or f.get("no_price_dollars") or f.get("price_dollars") or 0)
            price_cents = pd * 100
        
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
            res = (settle.get("result") or "").lower()
            if res:
                yes_settled = (res == "yes")
                won = (side == res)
                outcome = "win" if won else "lose"
                pnl = (100 - price_cents) * qty if won else (-price_cents * qty)
                
                if sync and STORAGE_AVAILABLE:
                    # If we just recorded it or it already existed, resolve it
                    # We might need to find the prediction ID if db_id is None (duplicate)
                    if db_id is None:
                        # Need to find existing ID? record_prediction_outcome returns False if not found/already resolved
                        # In simple sync, we'll try to resolve any unresolved for this ticker
                        resolve_signal_outcomes(ticker, yes_settled, cfg)
                        record_resolved(ticker=ticker, yes_settled=yes_settled, platform="kalshi", config=cfg)
                        # We don't have the specific prediction ID easily here without extra query
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
            "price": int(price_cents),
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
                last_ts = _parse_ts(data[-1].get("expiryTime"))
                if last_ts < min_ts - 86400: break
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
            fills = client.parse_fills_response(r)
    except Exception as e:
        print(f"[!] Gemini fills error: {e}")

    # 3. Process
    trades = []
    for f in fills:
        ts = _parse_ts(f.get("timestamp") or f.get("timestampms") or f.get("fillTime"))
        if ts < min_ts: continue
        
        side_exec = (f.get("side") or "").lower()
        if side_exec == "sell": continue
        
        sym = f.get("symbol")
        outcome_side = (f.get("outcome") or "").lower()
        qty = int(float(f.get("quantity") or f.get("amount") or 0))
        price = _as_float(f.get("price") or 0)
        if price > 1.0: price /= 100.0
        price_cents = price * 100
        
        oid = f.get("orderId") or f.get("order_id")
        fid = f.get("fillId") or f.get("fill_id") or f.get("tradeId") or f.get("trade_id")
        
        # Sync to DB if requested
        db_id = None
        if sync and STORAGE_AVAILABLE:
            # Note: Ticker mapping is tricky from just fills. 
            # We'll use symbol as ticker fallback if mapping unknown.
            res = record_prediction_from_fill(
                platform="gemini",
                ticker=sym, # Fallback
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
            "price": int(price_cents),
            "outcome": outcome,
            "pnl_cents": pnl
        })
        
    return trades


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
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
        dt = datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
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
