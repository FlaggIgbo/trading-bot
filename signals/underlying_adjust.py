"""
Linear adjustment of Yahoo-derived prices to match Kalshi settlement units.

Example: RBOB futures (RB=F) are wholesale $/gal; AAA national retail is typically
~$0.70–$1.10 higher — configure add/mult per Kalshi series in config.yaml.
"""
from __future__ import annotations


def underlying_adjust_params(cfg: dict | None, kalshi_series: str) -> tuple[float, float]:
    """
    Returns (multiply, add) such that adjusted_price = raw * multiply + add.
    """
    if not cfg or not (kalshi_series or "").strip():
        return (1.0, 0.0)
    raw = cfg.get("yfinance_underlying_adjust") or {}
    if not isinstance(raw, dict):
        return (1.0, 0.0)
    key = kalshi_series.strip().upper()
    entry = raw.get(key)
    if not isinstance(entry, dict):
        return (1.0, 0.0)
    mult = float(entry.get("multiply", entry.get("mult", 1.0)))
    add = float(entry.get("add", entry.get("add_per_unit", entry.get("offset", 0.0))))
    return (mult, add)


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
