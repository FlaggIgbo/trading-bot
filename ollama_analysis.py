"""
Ollama LLM analysis for prediction market data.

Captures script output and sends to local Ollama for trading opinions.
Uses free local models (llama3.2, mistral, phi3, etc.).
Requires Ollama running: https://ollama.com

# TODO: Add config.yaml option for model selection
# TODO: Support streaming output
"""
from __future__ import annotations

import os
from io import StringIO
from typing import Callable

# Default: small fast free model. Override via OLLAMA_MODEL env.
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
FALLBACK_MODELS = ["mistral", "phi3", "qwen2.5:3b"]  # Tried in order if default model unavailable


# ---------------------------------------------------------------------------
# Ollama API call
# ---------------------------------------------------------------------------
def _get_analysis(data_text: str, script_name: str, model: str) -> str | None:
    """Call Ollama chat API. Returns response content or None on failure."""
    try:
        from ollama import chat
    except ImportError:
        return None

    prompt = f"""You are a prediction market analyst. Below is raw data from running {script_name}.
Analyze this data and give your opinions:
- Which contracts, if any, look mispriced or interesting?
- What risks or caveats do you see?
- Any trading suggestions or things to watch?
Be concise but substantive. Use the actual numbers from the data.

DATA:
{data_text}
"""

    try:
        response = chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        # Handle both dict and object response formats
        if hasattr(response, "message") and hasattr(response.message, "content"):
            return (response.message.content or "").strip()
        if isinstance(response, dict):
            return (response.get("message", {}).get("content") or "").strip()
        return None
    except Exception:
        return None


def run_analysis(data_text: str, script_name: str, model: str | None = None) -> bool:
    """Send data to Ollama and print the LLM's analysis. Returns True if printed."""
    if not data_text or not data_text.strip():
        return False

    model = model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
    models_to_try = [model] + [m for m in FALLBACK_MODELS if m != model]

    for m in models_to_try:
        result = _get_analysis(data_text, script_name, m)
        if result:
            print("\n" + "=" * 70)
            print("OLLAMA ANALYSIS")
            print("=" * 70)
            print(result)
            print("=" * 70)
            return True

    print("\n(Ollama skipped: not running or model missing. Run: ollama pull llama3.2)")
    return False


def capture_and_analyze(
    run_fn: Callable[[], int],
    script_name: str,
    use_llm: bool = True,
) -> int:
    """Run run_fn() while capturing stdout, then run Ollama analysis. Pass use_llm=False to skip."""
    import sys

    if not use_llm:
        return run_fn()

    buffer = StringIO()
    real_stdout = sys.stdout

    class Tee:
        def __init__(self, primary, secondary):
            self.primary, self.secondary = primary, secondary

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

    if buffer.getvalue() and use_llm:
        run_analysis(buffer.getvalue(), script_name)

    return exit_code
