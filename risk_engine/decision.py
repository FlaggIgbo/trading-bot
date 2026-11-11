"""
Decision Data Model
===================
Represents the outcome of evaluating a candidate market through risk rules and strategy.
Every market evaluation (accepted, skipped, or rejected) produces a Decision record.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Union

from db import PositionSide
from risk_engine.rules import RiskRejectReason


@dataclass
class Decision:
    ticker: str
    market: dict[str, Any]
    strategy: str
    side: Optional[PositionSide] = None
    price_cents: Optional[int] = None
    reason: Optional[Union[RiskRejectReason, str]] = None  # None indicates OK to trade; non-empty reason is reject reason
    shadow: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_accepted(self) -> bool:
        """True if the decision is approved for execution (not rejected, and has a side)."""
        return self.reason is None and self.side is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "strategy": self.strategy,
            "side": self.side.value if self.side else None,
            "price_cents": self.price_cents,
            "reason": self.reason.value if hasattr(self.reason, "value") else self.reason,
            "shadow": self.shadow,
            "timestamp": self.timestamp,
        }
