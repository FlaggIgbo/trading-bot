#!/usr/bin/env python3
"""
Quick script to test Kalshi and Gemini API connections and fetch data for configured markets.
Ends with Ollama LLM analysis (local, free) when available.

Run: python scripts/test_connection.py
      python scripts/test_connection.py --debug    # diagnose auth issues
      python scripts/test_connection.py --inspect  # show API response types/schema
      python scripts/test_connection.py --no-llm   # skip Ollama analysis (e.g. for cron)
"""
import argparse
import os
import sys
from pathlib import Path

# Add project root to path so imports work when run from scripts/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")


# ---------------------------------------------------------------------------
# Debug / diagnostics
# ---------------------------------------------------------------------------
def debug_check():
    """Run diagnostic checks before attempting auth."""
    print("=== Debug: Environment check ===\n")

    # Env vars
    key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    base_url = os.getenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2")

    print(f"KALSHI_API_KEY_ID: {'SET' if key_id else 'NOT SET'}")
    if key_id:
        parts = key_id.split("-")
        print(f"  Value: {key_id[:8]}...{key_id[-4:]}")
        if len(parts) != 5 or any(len(p) not in (8, 4, 12) for p in parts):
            print("  ⚠️  Key ID should be a UUID like: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
            print("     (8-4-4-4-12 chars). Double-check you copied the full Key ID.")

    print(f"\nKALSHI_PRIVATE_KEY_PATH: {'SET' if key_path else 'NOT SET'}")
    if key_path:
        p = Path(key_path).expanduser().resolve()
        exists = p.exists()
        print(f"  Path: {p}")
        print(f"  Exists: {exists}")
        if exists:
            try:
                content = p.read_text()
                first = content.splitlines()[0] if content.strip() else "(empty)"
                if "-----BEGIN" in first:
                    print(f"  Format: OK (PEM key found)")
                else:
                    print(f"  ⚠️  First line: {first[:50]}...")
                    print("     Key file should start with: -----BEGIN PRIVATE KEY-----")
            except Exception as e:
                print(f"  Read error: {e}")

    print(f"\nKALSHI_BASE_URL: {base_url}")
    if "demo" in base_url.lower():
        print("  Using DEMO. Keys must be from https://demo.kalshi.com (not kalshi.com)")
    else:
        print("  Using PRODUCTION. Keys must be from https://kalshi.com (not demo.kalshi.com)")

    print("\n=== Attempting connection ===\n")


def _type_of(val):
    """Return readable type label for API response inspection."""
    if val is None:
        return "null"
    t = type(val)
    if t is dict:
        return f"dict[{len(val)} keys]"
    if t is list:
        return f"list[{len(val)} items]"
    return t.__name__


def inspect_output_types(client):
    """Dump API response schema / output types for balance, markets, orderbook."""
    print("=== API Response Types ===\n")

    # 1. Balance
    bal = client.get_balance()
    print("GET /portfolio/balance")
    print("  Top-level keys:", list(bal.keys()))
    for k, v in bal.items():
        print(f"    {k}: {_type_of(v)} = {repr(v)[:60]}")
    print()

    # 2. Markets (first market from first series)
    from config import get_config
    cfg = get_config()
    series = (cfg.get("markets") or ["KXBTCD"])[0]
    r = client.get_markets(series_ticker=series.upper(), status="open")
    mkt_list = r.get("markets", [])
    print(f"GET /markets?series_ticker={series}&status=open")
    print(f"  Response keys: {list(r.keys())}")
    print(f"  markets: list[{len(mkt_list)} items]")
    if mkt_list:
        m = mkt_list[0]
        print(f"  Sample market (ticker={m.get('ticker')}) keys:")
        for k, v in sorted(m.items()):
            print(f"    {k}: {_type_of(v)} = {repr(v)[:50]}")
    print()

    # 3. Orderbook
    if mkt_list:
        ticker = mkt_list[0]["ticker"]
        ob = client.get_orderbook(ticker)
        print(f"GET /markets/{ticker}/orderbook")
        print(f"  Response keys: {list(ob.keys())}")
        for k, v in ob.items():
            t = _type_of(v)
            print(f"    {k}: {t}")
            if isinstance(v, dict) and k == "orderbook_fp":
                for k2, v2 in v.items():
                    preview = str(v2)[:80] if isinstance(v2, (list, tuple)) else repr(v2)[:80]
                    print(f"      {k2}: {_type_of(v2)} {preview}...")
    print("\n=== End schema ===")


def _run(args) -> int:
    from config import get_config
    from gemini_client import GeminiPredictionClient, _get_yes_contract, get_yes_price
    from kalshi_client import KalshiClient

    if args.debug:
        debug_check()

    cfg = get_config()
    markets = cfg.get("markets", [])

    if not markets:
        print("No markets in config.yaml. Add some under 'markets:'")
        return 1

    print("Connecting to Kalshi API...")
    client = KalshiClient()

    if args.inspect:
        inspect_output_types(client)
        return 0

    # 1. Test auth with balance (requires valid API keys)
    try:
        bal = client.get_balance()
        balance_cents = bal.get("balance", 0)
        print(f"\n✓ Auth OK | Balance: ${balance_cents / 100:.2f}")
    except Exception as e:
        print(f"\n✗ Auth failed: {e}")
        return 1

    # 2. Fetch data for each configured market series
    print(f"\nFetching data for {len(markets)} series: {', '.join(markets)}\n")
    print("-" * 80)

    for series in markets:
        series = series.strip()
        try:
            r = client.get_markets(series_ticker=series.upper(), status="open")
            mkt_list = r.get("markets", [])
            print(f"\n{series} ({len(mkt_list)} open markets)")
            for m in mkt_list[:5]:  # Show first 5 per series
                ticker = m.get("ticker", "?")
                title = m.get("title", "?")[:60]
                yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
                no_bid = float(m.get("no_bid_dollars", 0) or 0)
                yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
                no_ask = float(m.get("no_ask_dollars", 0) or 0)
                vol = m.get("volume", 0)
                print(f"  {ticker}: YES bid {yes_bid:.2f} ask {yes_ask:.2f} / NO bid {no_bid:.2f} ask {no_ask:.2f} | vol={vol} | {title}")
            if len(mkt_list) > 5:
                print(f"  ... and {len(mkt_list) - 5} more")
        except Exception as e:
            print(f"\n{series}: Error - {e}")

    # 3. Test Gemini prediction markets (crypto, public API - no auth)
    print("\n" + "-" * 80)
    print("Connecting to Gemini Prediction Markets API (crypto)...")
    try:
        gemini = GeminiPredictionClient()
        r = gemini.get_events(category="crypto", status="active", limit=50)
        events = r.get("data") or []
        print(f"\n✓ Gemini OK | {len(events)} crypto events")

        for evt in events[:5]:
            ticker = evt.get("ticker", "?")
            title = (evt.get("title") or "")[:55]
            yes_c = _get_yes_contract(evt)
            yes_p = get_yes_price(yes_c) if yes_c else 0.0
            print(f"  {ticker}: YES {yes_p:.2f} | {title}")
        if len(events) > 5:
            print(f"  ... and {len(events) - 5} more")
    except Exception as e:
        print(f"\n✗ Gemini: {e} (optional - used for crypto price comparison)")

    # 4. Optional: --test-apis read-only verification (positions, orders)
    if getattr(args, "test_apis", False):
        print("\n" + "-" * 80)
        print("--test-apis: Read-only API verification (no buy/sell orders)")
        # Kalshi
        try:
            pos_r = client.get_positions()
            pos_list = pos_r.get("market_positions") or []
            ord_r = client.get_orders()
            ord_list = ord_r.get("orders") or []
            print(f"\n✓ Kalshi positions: {len(pos_list)} | orders: {len(ord_list)}")
        except Exception as e:
            print(f"\n✗ Kalshi positions/orders: {e}")
        # Gemini trading (if creds set)
        try:
            from gemini_client import GeminiTradingClient
            gemini_trading = GeminiTradingClient()
            act = gemini_trading.get_active_orders(limit=10)
            orders = act.get("orders") or []
            pos = gemini_trading.get_positions()
            pos_list_g = pos.get("positions") or []
            print(f"✓ Gemini positions: {len(pos_list_g)} | active orders: {len(orders)}")
        except ValueError:
            print("Gemini trading: GEMINI_API_KEY/SECRET not set (skip auth test)")
        except Exception as e:
            print(f"✗ Gemini trading: {e}")

    print("\n" + "-" * 80)
    print("Done. Kalshi .env correct if balance and markets shown. Gemini optional for crypto.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Run env/key diagnostics before connecting")
    parser.add_argument("--inspect", action="store_true", help="Show API response types/schema")
    parser.add_argument("--no-llm", action="store_true", help="Skip Ollama analysis")
    parser.add_argument("--test-apis", action="store_true", help="Read-only test: verify Kalshi & Gemini APIs return data (positions, orders)")
    args = parser.parse_args()

    from ollama_analysis import capture_and_analyze
    return capture_and_analyze(lambda: _run(args), "scripts/test_connection.py", use_llm=not args.no_llm)


if __name__ == "__main__":
    raise SystemExit(main())
