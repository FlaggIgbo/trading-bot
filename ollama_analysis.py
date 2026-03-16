"""
Ollama LLM analysis for prediction market data.

Captures script output and sends to local Ollama for trading opinions.
Uses free local models (llama3.2, mistral, phi3, etc.).
Requires Ollama running: https://ollama.com

# TODO: Add config.yaml option for model selection
# TODO: Support streaming output
"""
from __future__ import annotations

import json
import os
from io import StringIO
from typing import Callable

# Default: small fast free model. Override via OLLAMA_MODEL env.
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
# Models tried in order; we also prefer any model from Ollama's /api/tags if none of these exist
FALLBACK_MODELS = ["llama3.2:1b", "llama3.1", "mistral", "phi3", "qwen2.5:3b", "gemma2:2b"]
OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://localhost:11434")
VERBOSE = os.getenv("OLLAMA_VERBOSE", "").lower() in ("1", "true", "yes")

# Max chars to send to Ollama (avoids context overflow for small models, ~8k tokens ≈ 25k chars)
OLLAMA_MAX_CHARS = int(os.getenv("OLLAMA_MAX_CHARS", "25000"))
OLLAMA_HEAD_CHARS = int(os.getenv("OLLAMA_HEAD_CHARS", "8000"))  # Keep from start
OLLAMA_TAIL_CHARS = int(os.getenv("OLLAMA_TAIL_CHARS", "12000"))  # Keep from end (orders/summary)


def _truncate_for_ollama(text: str, max_chars: int | None = None) -> str:
    """
    Truncate long output so Ollama fits it in context. Keeps head (setup, markets) and tail (orders, summary).
    """
    limit = max_chars or OLLAMA_MAX_CHARS
    if len(text) <= limit:
        return text
    head_size = min(OLLAMA_HEAD_CHARS, limit // 2)
    tail_size = min(OLLAMA_TAIL_CHARS, limit - head_size - 100)  # Reserve for separator
    head = text[:head_size].rstrip()
    tail = text[-tail_size:].lstrip()
    omitted = text[head_size:-tail_size]
    omitted_lines = omitted.count("\n") + 1
    sep = f"\n\n... [truncated: {omitted_lines} lines omitted, {len(omitted):,} chars] ...\n\n"
    return head + sep + tail


# ---------------------------------------------------------------------------
# Connectivity & model discovery
# ---------------------------------------------------------------------------
def _get_available_models() -> list[str]:
    """Fetch full model names from Ollama API (e.g. llama3.2:1b). Returns [] if unreachable."""
    try:
        import urllib.request

        r = urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=5)
        data = json.loads(r.read().decode())
        return [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]
    except Exception:
        return []


def _ollama_reachable() -> bool:
    """Quick check if Ollama server is up."""
    try:
        import urllib.request

        urllib.request.urlopen(f"{OLLAMA_BASE}/api/version", timeout=3)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ollama API call
# ---------------------------------------------------------------------------
def _get_analysis(data_text: str, script_name: str, model: str) -> str | None:
    """Call Ollama chat API. Returns response content or None on failure."""
    try:
        from ollama import chat
    except ImportError:
        return None

    prompt = f"""You are a prediction market analyst. Below is the full output from running {script_name}.

Give a general analysis of the run:
- Summary of what the bot evaluated (markets, recommendations, any orders placed)
- Which contracts or trades stand out as interesting, risky, or mispriced?
- Any caveats, correlations (e.g. many similar strikes on same asset), or things to watch?
Be concise but substantive. Use actual tickers and numbers from the output.
Do not cherry-pick a single trade — analyze the overall picture.

OUTPUT:
{data_text}
"""

    try:
        response = chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        if hasattr(response, "message") and hasattr(response.message, "content"):
            return (response.message.content or "").strip()
        if isinstance(response, dict):
            return (response.get("message", {}).get("content") or "").strip()
        return None
    except Exception as e:
        if VERBOSE:
            print(f"\n(Ollama model '{model}' failed: {e})")
        return None


def analyze_single_trade(trade: dict, action: str, model: str | None = None) -> str | None:
    """
    Analyze a single proposed buy or sell trade. Returns Ollama's interpretation or None.
    action: "buy" or "sell"
    """
    if action not in ("buy", "sell"):
        return None
    if not _ollama_reachable():
        return None

    available = _get_available_models()
    preferred = model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
    models_to_try = [preferred] + [m for m in FALLBACK_MODELS if m != preferred]
    if available:
        models_to_try = available + [m for m in models_to_try if m not in available]

    if action == "sell":
        desc = (
            f"SELL {trade.get('side', '?').upper()} {trade.get('ticker', '?')} | "
            f"{trade.get('title', '')[:50]} | Count: {trade.get('count', 0)} @ "
            f"${trade.get('price', 0):.2f} | Reason: {trade.get('rec', 'N/A')}"
        )
    else:
        desc = (
            f"BUY {trade.get('side', '?').upper()} {trade.get('ticker', '?')} | "
            f"{trade.get('title', '')[:50]} | Edge: {trade.get('edge', 0):.1%} | "
            f"Price: ${trade.get('price', 0):.2f} | Count: {trade.get('count', 10)} | "
            f"Recommendation: {trade.get('rec', 'N/A')}"
        )

    try:
        from ollama import chat
    except ImportError:
        return None

    prompt = f"""You are a prediction market analyst. Below is ONE proposed trade.

TRADE:
{desc}

In 1-3 sentences, interpret this trade for the user: what does it mean, is it reasonable, any risks or caveats? Be concise."""

    for m in models_to_try:
        try:
            response = chat(model=m, messages=[{"role": "user", "content": prompt}], stream=False)
            content = None
            if hasattr(response, "message") and hasattr(response.message, "content"):
                content = (response.message.content or "").strip()
            elif isinstance(response, dict):
                content = (response.get("message", {}).get("content") or "").strip()
            if content:
                return content
        except Exception as e:
            if VERBOSE:
                print(f"\n(Ollama '{m}' failed: {e})")
            continue
    return None


def check_ollama_ready() -> tuple[bool, str | None]:
    """Send data to Ollama and print the LLM's analysis. Returns True if printed."""
    if not data_text or not data_text.strip():
        return False

    if not _ollama_reachable():
        print(
            "\n(Ollama skipped: server not running. Start it with: ollama serve  "
            "Then run: ollama pull llama3.2:1b)"
        )
        return False

    available = _get_available_models()  # Full names from /api/tags, e.g. ["llama3.2:1b"]
    preferred = model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
    models_to_try = [preferred] + [m for m in FALLBACK_MODELS if m != preferred]
    # Use available models first (exact names that Ollama knows)
    if available:
        models_to_try = available + [m for m in models_to_try if m not in available]

    for m in models_to_try:
        result = _get_analysis(data_text, script_name, m)
        if result:
            print("\n" + "=" * 70)
            print("OLLAMA ANALYSIS")
            print("=" * 70)
            print(result)
            print("=" * 70)
            return True

    suggest = "ollama pull llama3.2:1b" if not available else f"ollama pull {models_to_try[0]}"
    print(
        f"\n(Ollama skipped: no model responded. "
        f"Run: {suggest}  |  Or skip LLM: --no-llm)"
    )
    return False


def check_ollama_ready() -> tuple[bool, str | None]:
    """
    Early check: is Ollama reachable, has models, and can chat?
    Returns (ok, error_message). If ok, error_message is None.
    """
    if not _ollama_reachable():
        return False, "Ollama server not running. Start it with: ollama serve"
    available = _get_available_models()
    if not available:
        return False, "No models installed. Run: ollama pull llama3.2:1b"
    # Warm-up: verify chat() works (catches ollama pkg / client issues)
    try:
        from ollama import chat
        r = chat(model=available[0], messages=[{"role": "user", "content": "Hi"}], stream=False)
        if not (hasattr(r, "message") and getattr(r.message, "content", None)) and not (isinstance(r, dict) and (r.get("message") or {}).get("content")):
            return False, f"Model {available[0]} returned no content. Try: ollama pull llama3.2:1b"
    except ImportError:
        return False, "ollama package missing. Run: pip install ollama"
    except Exception as e:
        return False, f"Chat failed: {e}. Use --no-llm to skip LLM analysis."
    return True, None


def capture_and_analyze(
    run_fn: Callable[[], int],
    script_name: str,
    use_llm: bool = True,
    per_trade_analysis: bool = True,
) -> int:
    """
    Run run_fn(). If use_llm and per_trade_analysis, run_fn does its own per-trade Ollama calls.
    Otherwise (legacy batch mode), capture stdout and run one analysis at the end.
    """
    import sys

    if not use_llm:
        return run_fn()

    # Early check: if LLM requested but Ollama isn't ready, exit before running the full evaluation
    ok, err = check_ollama_ready()
    if not ok:
        print(f"\nOllama unavailable: {err}")
        print("  Fix it and re-run, or use --no-llm to skip LLM analysis.")
        return 1

    if per_trade_analysis:
        # Script does its own per-trade analysis; no capture/batch
        return run_fn()

    # Legacy batch mode: capture output, analyze at end
    buffer = StringIO()
    real_stdout = sys.stdout

    class Tee:
        """Forwards writes to both real stdout and a capture buffer."""

        def __init__(self, primary, secondary):
            self.primary = primary
            self.secondary = secondary

        def write(self, data):
            self.primary.write(data)
            self.secondary.write(data)

        def flush(self):
            self.primary.flush()
            self.secondary.flush()

    sys.stdout = Tee(real_stdout, buffer)
    try:
        exit_code = run_fn()
    finally:
        sys.stdout = real_stdout

    data_to_analyze = buffer.getvalue()
    if data_to_analyze:
        data_to_analyze = _truncate_for_ollama(data_to_analyze)
        run_analysis(data_to_analyze, script_name)

    return exit_code
