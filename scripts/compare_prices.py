#!/usr/bin/env python3
"""
Quick live snapshot: Kalshi & Gemini market prices vs current yfinance price.
Ends with Ollama LLM analysis (local, free) when available.

# TODO: Add --cron mode: minimal output for 15-min runs, log to file
"""
import argparse
import sys
from pathlib import Path

# Add project root to path so imports work when run from scripts/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config
from gemini_client import (
    GeminiPredictionClient,
    _event_to_asset,
    _get_yes_contract,
    get_yes_price,
)
from kalshi_client import KalshiClient
from ollama_analysis import capture_and_analyze
from yfinance_client import get_snapshot as yf_get_snapshot

# Gemini only supports crypto (BTC, ETH, SOL)
GEMINI_CRYPTO_YF = {"BTC-USD", "ETH-USD", "SOL-USD"}


# ---------------------------------------------------------------------------
# Main snapshot logic
# ---------------------------------------------------------------------------
def _run() -> int:
    cfg = get_config()
    markets = cfg.get("markets", [])
    yf_mapping = cfg.get("yfinance_tickers", {})

    if not markets:
        print("No markets in config.yaml under 'markets:'")
        return 1

    if not yf_mapping:
        print("No yfinance_tickers in config.yaml - add mapping for price comparison")
        return 1

    kalshi = KalshiClient()
    gemini = GeminiPredictionClient()
    gemini_events_by_asset: dict[str, list] = {}
    try:
        r = gemini.get_events(category="crypto", status="active", limit=200)
        for evt in r.get("data") or []:
            asset = _event_to_asset(evt)
            if asset:
                gemini_events_by_asset.setdefault(asset, []).append(evt)
    except Exception as e:
        print(f"Gemini: could not fetch events ({e}) - skipping Gemini checks\n")

    print("Kalshi & Gemini vs yfinance (live snapshot)\n" + "=" * 70)
    print("For BUY/NO/SKIP recommendations, run: python3 scripts/compare_historical.py\n")

    for series in markets:
        series = series.strip()
        yf_ticker = yf_mapping.get(series.upper())
        if not yf_ticker:
            print(f"\n[{series}] No yfinance_tickers mapping - add to config.yaml")
            continue

        price = yf_get_snapshot(yf_ticker)
        if price is None:
            print(f"\n[{series}] Could not get yfinance price for {yf_ticker}")
            continue

        print(f"\n[{series}] → {yf_ticker}  ${price:,.2f}")

        try:
            r = kalshi.get_markets(series_ticker=series.upper(), status="open")
            mkt_list = r.get("markets", [])
        except Exception as e:
            print(f"  Kalshi: {e}")
            mkt_list = []

        if mkt_list:
            print(f"  Kalshi ({len(mkt_list)}):")
            for m in mkt_list[:5]:
                ticker = m.get("ticker", "?")
                title = (m.get("title") or "")[:55]
                yes_bid = float(m.get("yes_bid_dollars") or 0)
                no_bid = float(m.get("no_bid_dollars") or 0)
                print(f"    {ticker}: YES {yes_bid:.2f} / NO {no_bid:.2f} | {title}")
            if len(mkt_list) > 5:
                print(f"    ... +{len(mkt_list) - 5} more")
        else:
            print(f"  Kalshi: No open markets")

        if yf_ticker in GEMINI_CRYPTO_YF:
            evts = gemini_events_by_asset.get(yf_ticker, [])
            if evts:
                print(f"  Gemini ({len(evts)}):")
                for evt in evts[:5]:
                    yes_c = _get_yes_contract(evt)
                    p = get_yes_price(yes_c) if yes_c else 0.0
                    title = (evt.get("title") or evt.get("ticker", ""))[:55]
                    print(f"    {evt.get('ticker','?')}: YES {p:.2f} | {title}")
                if len(evts) > 5:
                    print(f"    ... +{len(evts) - 5} more")
            else:
                print(f"  Gemini: No crypto markets")

    print("\n" + "=" * 70)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="Skip Ollama analysis")
    args = parser.parse_args()
    return capture_and_analyze(_run, "scripts/compare_prices.py", use_llm=not args.no_llm)


if __name__ == "__main__":
    raise SystemExit(main())
