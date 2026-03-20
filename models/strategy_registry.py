"""
Registry of runnable strategy engines (Gnosis run_agent.py / Freqtrade list-strategies style).
"""
from __future__ import annotations

from models.strategies.base import BaseStrategy
from models.strategies.default_threshold import DefaultThresholdStrategy
from models.strategies.strict_threshold import StrictThresholdStrategy
from models.strategies.vol_regime import VolRegimeStrategy

# Canonical CLI / config names -> strategy class
STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "vol_regime": VolRegimeStrategy,
    "default_threshold": DefaultThresholdStrategy,
    "default": DefaultThresholdStrategy,
    "threshold": DefaultThresholdStrategy,
    "strict": StrictThresholdStrategy,
    "strict_threshold": StrictThresholdStrategy,
}


def normalize_strategy_name(name: str) -> str:
    n = str(name or "").strip().lower()
    if n in {"default", "threshold"}:
        return "default_threshold"
    if n == "strict":
        return "strict_threshold"
    return n if n in STRATEGY_REGISTRY else n


def build_strategy_engine_by_name(name: str) -> BaseStrategy:
    """Instantiate strategy engine from short name (matches config strategy_engine)."""
    n = normalize_strategy_name(name)
    cls = STRATEGY_REGISTRY.get(n)
    if cls is None:
        raise ValueError(f"Unknown strategy_engine: {name!r}. Choose from: {list_strategies()}")
    return cls()


def list_strategies() -> list[str]:
    """Distinct public names for UI / CLI."""
    return sorted({"vol_regime", "default_threshold", "strict_threshold"})


def parse_strategy_list(s: str) -> list[str]:
    """Comma-separated strategy names."""
    return [normalize_strategy_name(x) for x in str(s).split(",") if x.strip()]
