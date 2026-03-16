#!/usr/bin/env python3
"""
Temporary script to verify Gemini API works (after adding GEMINI_ACCOUNT for Master keys).
Tests authenticated endpoints that previously failed with MissingAccounts.

Run: python3 scripts/test_gemini_api.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")


def main() -> int:
    print("Testing Gemini Prediction Markets API (authenticated)...")
    print()

    try:
        from gemini_client import GeminiTradingClient

        client = GeminiTradingClient()
    except ValueError as e:
        print(f"FAIL: {e}")
        print("  Set GEMINI_API_KEY and GEMINI_API_SECRET in .env")
        return 1

    # Test positions (authenticated)
    try:
        pos = client.get_positions()
        positions = pos.get("positions") or []
        print(f"  get_positions(): OK — {len(positions)} position(s)")
    except Exception as e:
        print(f"  get_positions(): FAIL — {e}")
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.json()
                print(f"    reason: {body.get('reason', '?')}")
                if body.get("message"):
                    print(f"    message: {body.get('message')}")
            except Exception:
                print(f"    raw: {resp.text[:200] if resp.text else '(empty)'}")
        return 1

    # Test active orders (authenticated)
    try:
        orders = client.get_active_orders(limit=10)
        order_list = orders.get("orders") or []
        print(f"  get_active_orders(): OK — {len(order_list)} active order(s)")
    except Exception as e:
        print(f"  get_active_orders(): FAIL — {e}")
        if hasattr(e, "response") and getattr(e, "response", None) is not None:
            try:
                body = e.response.json()
                print(f"    Gemini reason: {body.get('reason', '?')}")
                if body.get("message"):
                    print(f"    Gemini message: {body['message']}")
            except Exception:
                pass
        return 1

    print()
    print("SUCCESS: Gemini API is working. MissingAccounts resolved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
