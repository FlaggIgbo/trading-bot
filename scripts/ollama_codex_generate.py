#!/usr/bin/env python3
"""Non-streaming /api/generate call so options like temperature are honored.

`ollama run` does not expose sampling temperature on the CLI; code patches are
more stable with low temperature (default 0.2).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("prompt_file", type=Path)
    ap.add_argument("--temperature", type=float, default=None)
    args = ap.parse_args()

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    temp = args.temperature
    if temp is None:
        env_t = os.environ.get("OLLAMA_TEMPERATURE", "").strip()
        temp = float(env_t) if env_t else 0.2

    prompt = args.prompt_file.read_text(encoding="utf-8", errors="replace")
    body = json.dumps(
        {
            "model": args.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": float(temp)},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{host}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"ollama /api/generate HTTP {e.code}: {err}\n")
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(f"ollama /api/generate connection error: {e}\n")
        return 1

    msg = (data.get("response") or "").strip()
    if not msg:
        sys.stderr.write("ollama /api/generate returned empty response\n")
        return 1
    sys.stdout.write(msg)
    if not msg.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
