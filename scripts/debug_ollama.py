#!/usr/bin/env python3
"""
Debug script for Ollama integration.
Surfaces errors that ollama_analysis.py swallows.
Run from project root: python scripts/debug_ollama.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from constants import REC_BUY_NO


def main() -> None:
    print("=== Ollama Debug ===\n")

    # 1. Check Ollama server
    print("1. Checking Ollama server (http://localhost:11434)...")
    try:
        import urllib.request

        r = urllib.request.urlopen("http://localhost:11434/api/version", timeout=5)
        version = r.read().decode()
        print(f"   OK: {version.strip()}")
    except Exception as e:
        print(f"   FAIL: {e}")
        print("   -> Is Ollama running? Start it from Applications or: ollama serve")
        return

    # 2. List models
    print("\n2. Listing models...")
    try:
        r = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
        data = __import__("json").loads(r.read())
        models = [m.get("name", "?") for m in data.get("models", [])]
        print(f"   Available: {models}")
        if not models:
            print("   -> No models. Run: ollama pull llama3.2")
    except Exception as e:
        print(f"   FAIL: {e}")
        return

    # 3. Python ollama package
    print("\n3. Importing ollama package...")
    try:
        from ollama import chat

        print("   OK: ollama package imported")
    except ImportError as e:
        print(f"   FAIL: {e}")
        print("   -> Run: pip install ollama  (or activate venv: source .venv/bin/activate)")
        return

    # 4. Try a minimal chat (use first available model)
    model = models[0] if models else "llama3.2"
    print(f"\n4. Testing chat with model '{model}'...")
    test_data = f"KXBTCD: Bitcoin above $81,250 | hist_p=0.05 | market YES=0.40 | rec={REC_BUY_NO}"
    prompt = f"One sentence: what would you do with this? DATA: {test_data}"

    try:
        response = chat(model=model, messages=[{"role": "user", "content": prompt}], stream=False)
        if hasattr(response, "message") and hasattr(response.message, "content"):
            content = (response.message.content or "").strip()
        elif isinstance(response, dict):
            content = (response.get("message", {}) or {}).get("content", "")
        else:
            content = str(response)
        print(f"   Response: {content[:200]}..." if len(content) > 200 else f"   Response: {content}")
        print("   OK: Chat succeeded")
    except Exception as e:
        print(f"   FAIL: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return

    # 5. Test via ollama_analysis (what scripts actually use)
    print("\n5. Testing ollama_analysis.run_analysis()...")
    from ollama_analysis import run_analysis

    success = run_analysis(test_data, "debug_ollama.py")
    print(f"   Result: {'OK' if success else 'FAILED (no output)'}")

    print("\n=== Debug complete ===")


if __name__ == "__main__":
    main()
