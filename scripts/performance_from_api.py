#!/usr/bin/env python3
"""
Performance report directly from Kalshi and Gemini APIs.
Useful when local storage (SQLite) is unavailable or not synced.

Fetches recent fills and matches them with settlement data to calculate PnL.
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


def fetch_kalshi_performance(client: KalshiClient, days: int = 7):
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
            # Safety stop if we have enough
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
        price_cents = int(_as_float(f.get("yes_price") or f.get("no_price") or f.get("price") or 0) * 100)
        if price_cents == 0:
            # Try price_dollars
            pd = _as_float(f.get("yes_price_dollars") or f.get("no_price_dollars") or f.get("price_dollars") or 0)
            price_cents = int(pd * 100)
        
        ts = _parse_ts(f.get("created_time") or f.get("ts"))
        
        settle = settlements.get(ticker)
        outcome = "open"
        pnl = 0.0
        if settle:
            # payout is in cents
            payout = int(settle.get("payout") or 0)
            # If multiple fills for same ticker, this logic is a bit simple (assumes 1 settle per ticker)
            # but usually it works for recent history.
            # Revenue = Payout - Cost
            # A winner pays 100, a loser pays 0.
            # We need to check if this specific fill won.
            # Kalshi settlement 'result' is 'yes' or 'no'
            res = (settle.get("result") or "").lower()
            if res:
                won = (side == res)
                outcome = "win" if won else "lose"
                pnl = (100 - price_cents) * qty if won else (-price_cents * qty)
        
        trades.append({
            "ts": ts,
            "platform": "Kalshi",
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price_cents,
            "outcome": outcome,
            "pnl_cents": pnl
        })
    
    return trades


def fetch_gemini_performance(client: GeminiTradingClient, days: int = 7):
    print(f"[*] Fetching Gemini performance (last {days} days)...")
    min_ts = int(time.time() - days * 86400)
    
    # 1. Fetch settled events to get outcomes
    settled_outcomes = {} # event_ticker -> {threshold: {res_side: 'yes'|'no'}}
    try:
        for cat in ("crypto", "commodities"):
            offset = 0
            while True:
                r = client.get_events(category=cat, status="settled", limit=200, offset=offset)
                data = r.get("data") or []
                for evt in data:
                    et = evt.get("ticker")
                    if not et: continue
                    settled_outcomes[et] = {}
                    for c in evt.get("contracts") or []:
                        rs = (c.get("resolutionSide") or "").lower()
                        if rs in ("yes", "no"):
                            label = c.get("label") or c.get("abbreviatedName") or ""
                            thresh, _ = _extract_threshold_from_title(label)
                            sym = c.get("instrumentSymbol")
                            settled_outcomes[et][sym] = rs
                            if thresh:
                                settled_outcomes[et][thresh] = rs
                if len(data) < 200: break
                offset += len(data)
                # Don't go back too far
                last_ts = _parse_ts(data[-1].get("expiryTime"))
                if last_ts < min_ts - 86400: break
    except Exception as e:
        print(f"[!] Gemini events error: {e}")

    # 2. Fetch fills
    fills = []
    try:
        # Use get_fills_paginated if available
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
        price_cents = int(price * 100)
        
        # Try to find outcome
        outcome = "open"
        pnl = 0.0
        # Match by symbol in settled_outcomes
        found_res = None
        for et, contracts in settled_outcomes.items():
            if sym in contracts:
                found_res = contracts[sym]
                break
        
        if found_res:
            won = (outcome_side == found_res)
            outcome = "win" if won else "lose"
            pnl = (100 - price_cents) * qty if won else (-price_cents * qty)
        
        trades.append({
            "ts": ts,
            "platform": "Gemini",
            "ticker": sym,
            "side": outcome_side,
            "qty": qty,
            "price": price_cents,
            "outcome": outcome,
            "pnl_cents": pnl
        })
        
    return trades


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    cfg = get_config()
    
    all_trades = []
    
    try:
        k_client = KalshiClient()
        all_trades.extend(fetch_kalshi_performance(k_client, args.days))
    except Exception as e:
        print(f"[!] Could not init Kalshi: {e}")

    try:
        g_client = GeminiTradingClient()
        all_trades.extend(fetch_gemini_performance(g_client, args.days))
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


if __name__ == "__main__":
    main()
