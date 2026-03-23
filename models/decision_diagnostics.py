"""
Optional one-line JSON diagnostics for compare_historical decisions (grep-friendly).
"""
from __future__ import annotations

import json
from typing import Any


def print_decision_diagnostics_line(payload: dict[str, Any]) -> None:
    """Emit a single stdout line with structured fields (stable keys for log parsers)."""
    try:
        line = json.dumps(payload, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        line = json.dumps({"error": "non_serializable_payload"}, separators=(",", ":"))
    print(f"    ├─ decision_diag: {line}")
