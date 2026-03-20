"""
DeployableTraderAgent — Gnosis prediction-market-agent style pluggable trader.

Subclass or use RUNNABLE_AGENTS registry; each agent maps to a strategy_engine name.
Execution (Kalshi/Gemini API) stays in compare_historical / execution layer.
"""
from __future__ import annotations

from abc import ABC
from typing import ClassVar

from models.strategies.base import BaseStrategy
from models.strategy_registry import build_strategy_engine_by_name, list_strategies


class DeployableTraderAgent(ABC):
    """
    Minimal deployable unit: a named strategy engine + flags for research vs live.

    Gnosis pattern: subclass, set strategy_engine_name, register in RUNNABLE_AGENTS.
    """

    strategy_engine_name: ClassVar[str] = "vol_regime"
    place_trades: bool = True
    store_predictions: bool = True

    def __init__(
        self,
        *,
        place_trades: bool | None = None,
        store_predictions: bool | None = None,
    ) -> None:
        if place_trades is not None:
            self.place_trades = place_trades
        if store_predictions is not None:
            self.store_predictions = store_predictions

    def get_engine(self) -> BaseStrategy:
        return build_strategy_engine_by_name(self.strategy_engine_name)

    @classmethod
    def display_name(cls) -> str:
        return cls.__name__.replace("Deployable", "").replace("Agent", "") or cls.strategy_engine_name


class DeployableVolRegimeAgent(DeployableTraderAgent):
    strategy_engine_name = "vol_regime"


class DeployableDefaultThresholdAgent(DeployableTraderAgent):
    strategy_engine_name = "default_threshold"


class DeployableStrictThresholdAgent(DeployableTraderAgent):
    strategy_engine_name = "strict_threshold"


# Registry: name -> agent class (for Streamlit / future run_agent.py)
RUNNABLE_AGENTS: dict[str, type[DeployableTraderAgent]] = {
    "vol_regime": DeployableVolRegimeAgent,
    "default_threshold": DeployableDefaultThresholdAgent,
    "strict_threshold": DeployableStrictThresholdAgent,
}


def list_runnable_agent_names() -> list[str]:
    return sorted(RUNNABLE_AGENTS.keys())


def agent_class_for_strategy(strategy_name: str) -> type[DeployableTraderAgent]:
    n = strategy_name.strip().lower()
    if n in {"default", "threshold"}:
        n = "default_threshold"
    if n not in RUNNABLE_AGENTS:
        raise KeyError(f"No DeployableTraderAgent for {strategy_name!r}; try {list_runnable_agent_names()}")
    return RUNNABLE_AGENTS[n]
