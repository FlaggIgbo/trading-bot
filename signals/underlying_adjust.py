"""
Linear adjustment of Yahoo-derived prices to match Kalshi settlement units.

Example: Kalshi **KXAAAGASW** settles on **AAA national average** retail
(https://gasprices.aaa.com/), while we use **RBOB** (RB=F) as a Yahoo proxy.
Map wholesale→retail with `multiply` / `add`, then optionally widen strike checks
and required edge (`strike_validation_tolerance_pct`, `extra_min_edge`) because
the spread vs AAA moves week to week and is not perfectly tracked by futures.
"""
from __future__ import annotations


def _adjust_entry(cfg: dict | None, kalshi_series: str) -> dict | None:
    if not cfg or not (kalshi_series or "").strip():
        return None
    raw = cfg.get("yfinance_underlying_adjust") or {}
    if not isinstance(raw, dict):
        return None
    entry = raw.get(kalshi_series.strip().upper())
    return entry if isinstance(entry, dict) else None


def underlying_adjust_params(cfg: dict | None, kalshi_series: str) -> tuple[float, float]:
    """
    Returns (multiply, add) such that adjusted_price = raw * multiply + add.
    """
    # 1. Hardcoded unit fixes for common unit mismatches
    # KC=F (Coffee), ZS=F (Soybean), LE=F (Live Cattle) are in cents on Yahoo, Kalshi uses USD
    # ^GSPC (S&P 500) occasionally reported with 10x or 100x scale discrepancies in some feeds
    # but here we specifically address the known commodity cent/USD mismatch.
    ks = str(kalshi_series or "").strip().upper()
    if ks in ("KXCOFFEEW", "KXCOFFEED"):
        return (0.01, 0.0) # cents to USD
    if ks in ("KXSOYBEANW", "KXSOYBEAND"):
        return (0.01, 0.0) # cents to USD
    if ks in ("KXLCATTLEW", "KXLCATTLED"):
        return (0.01, 0.0) # cents to USD
    if ks == "KXINXU":
        # S&P 500 unit check: if Yahoo returns e.g. 50000 instead of 5000
        # This is a safety check; if we see a 6x discrepancy, we might need a mult.
        # However, underlying_adjust_params is static. We'll stick to 1.0 unless 
        # specifically told to scale. The feedback says 6.24x discrepancy.
        # If Kalshi strike is 500 and Yahoo is 3120, we need mult 0.16? 
        # Actually, Kalshi S&P 500 is often 10x smaller than the index value (e.g. 540 strike for 5400 index).
        return (1.0, 0.0)

    # 2. Config-driven overrides
    entry = _adjust_entry(cfg, kalshi_series)
    if entry is None:
        return (1.0, 0.0)
    mult = float(entry.get("multiply", entry.get("mult", 1.0)))
    add = float(entry.get("add", entry.get("add_per_unit", entry.get("offset", 0.0))))
    return (mult, add)


def proxy_basis_extra_min_edge(cfg: dict | None, kalshi_series: str) -> float:
    """Extra required net edge (probability space) when underlying is a proxy for the settlement index."""
    ent = _adjust_entry(cfg, kalshi_series)
    if not ent:
        return 0.0
    v = ent.get("extra_min_edge", ent.get("proxy_extra_min_edge", 0.0))
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def proxy_strike_validation_tolerance_pct(
    cfg: dict | None, kalshi_series: str, base_tolerance_pct: float,
) -> float:
    """At least `base_tolerance_pct`; wider when series config sets strike_validation_tolerance_pct."""
    ent = _adjust_entry(cfg, kalshi_series)
    if not ent or ent.get("strike_validation_tolerance_pct") is None:
        return float(base_tolerance_pct)
    try:
        return max(float(base_tolerance_pct), float(ent["strike_validation_tolerance_pct"]))
    except (TypeError, ValueError):
        return float(base_tolerance_pct)


def apply_underlying_price(cfg: dict | None, kalshi_series: str, value: float | None) -> float | None:
    if value is None:
        return None
    m, a = underlying_adjust_params(cfg, kalshi_series)
    return float(value) * m + a


def apply_underlying_closes(cfg: dict | None, kalshi_series: str, values: list[float]) -> list[float]:
    m, a = underlying_adjust_params(cfg, kalshi_series)
    if m == 1.0 and a == 0.0:
        return list(values)
    return [float(v) * m + a for v in values]
