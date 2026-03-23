"""
Optional one-line JSON diagnostics for compare_historical decisions (grep-friendly).
"""
from __future__ import annotations

import json
from typing import Any


def build_required_edge_breakdown(
    *,
    simple_ev: bool,
    effective_min_final: float,
    effective_min_yes: float,
    effective_min_no: float,
    min_p_win: float,
    us_detail: dict[str, Any] | None,
    eff_after_simple_ev_bundle: float | None,
    proxy_basis_add: float,
    legacy_core_before_parity_obi: float | None,
) -> dict[str, Any]:
    """
    Structured required-edge fields for decision_diag JSON.

    - simple_ev: base → after_ultra_short_late_mult (bundle output) → +proxy → final.
    - legacy: stacked core before per-side parity/OBI/lead-lag add-ons; eff_yes/eff_no passed to recommend_*.
    - Post-recommend filters (min_yes_price, strike caps, barbell, etc.) are not repeated here.
    """
    req: dict[str, Any] = {
        "min_p_win": float(min_p_win),
        "eff_min_final": float(effective_min_final),
        "eff_min": float(effective_min_final),
        "eff_yes": float(effective_min_yes),
        "eff_no": float(effective_min_no),
        "max_side_req_pre_recommend": float(max(effective_min_yes, effective_min_no)),
    }
    if simple_ev and us_detail is not None and eff_after_simple_ev_bundle is not None:
        req["simple_ev"] = {
            "base_from_config": float(us_detail.get("base", 0) or 0),
            "after_ultra_short_late_mult": float(eff_after_simple_ev_bundle),
            "proxy_basis_add": float(proxy_basis_add),
            "final": float(effective_min_final),
        }
    if not simple_ev and legacy_core_before_parity_obi is not None:
        req["legacy"] = {
            "core_before_parity_obi": float(legacy_core_before_parity_obi),
            "req_yes": float(effective_min_yes),
            "req_no": float(effective_min_no),
        }
    return req


def print_decision_diagnostics_line(payload: dict[str, Any]) -> None:
    """Emit a single stdout line with structured fields (stable keys for log parsers)."""
    try:
        line = json.dumps(payload, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        line = json.dumps({"error": "non_serializable_payload"}, separators=(",", ":"))
    print(f"    ├─ decision_diag: {line}")
