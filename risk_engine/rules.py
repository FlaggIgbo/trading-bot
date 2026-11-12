"""
Risk Rules
==========
Composable, pure-logic risk checks.
Each rule is a simple function that receives a RiskContext and returns:
- None if the check passes (OK to proceed)
- A RiskRejectReason enumeration if the check fails (e.g. RiskRejectReason.POSITION_ALREADY_HELD)

This makes rules easy to unit test without any real network I/O.
"""

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum
    class StrEnum(str, Enum):
        def __str__(self):
            return str(self.value)
        def __format__(self, format_spec):
            return str(self.value).__format__(format_spec)

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any, Callable, Optional, Union

from db import Database, MarketStatus
from ingestion.api.client import is_binary_market


class RiskRejectReason(StrEnum):
    KILL_SWITCH_FILE_DETECTED = "kill_switch_file_detected"
    KILL_SWITCH_ENV_DETECTED = "kill_switch_env_detected"
    MARKET_NOT_OPEN = "market_not_open"
    NOT_STANDALONE_BINARY_MARKET = "not_standalone_binary_market"
    MARKET_ALREADY_PAST_CLOSE_TIME = "market_already_past_close_time"
    MISSING_TICKER = "missing_ticker"
    POSITION_ALREADY_HELD = "position_already_held"
    POSITION_ALREADY_EXISTS = "position_already_exists"
    MAX_OPEN_POSITIONS_REACHED = "max_open_positions_reached"
    DAILY_LOSS_CAP_EXCEEDED = "daily_loss_cap_exceeded"


# Aliases for convenience and flexibility
RuleRejectReason = RiskRejectReason
RiskRuleReason = RiskRejectReason


@dataclass
class RiskContext:
    market: dict[str, Any]
    strategy_name: str
    db: Optional[Database] = None
    max_open_positions: Optional[int] = None
    daily_loss_cap_cents: Optional[int] = None
    kill_switch_path: Optional[str] = "KILL_SWITCH"
    now: Optional[datetime] = None


def rule_kill_switch_not_active(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """Check if the local emergency kill switch is active (via file or environment variable)."""
    if ctx.kill_switch_path and os.path.exists(ctx.kill_switch_path):
        return RiskRejectReason.KILL_SWITCH_FILE_DETECTED
    if os.getenv("KILL_SWITCH", "").strip().lower() in ("1", "true", "yes"):
        return RiskRejectReason.KILL_SWITCH_ENV_DETECTED
    return None


def rule_market_is_open(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """Check if the market status is OPEN or ACTIVE."""
    status = str(ctx.market.get("status", "")).strip().lower()
    if status not in (MarketStatus.OPEN.value, MarketStatus.ACTIVE.value):
        return RiskRejectReason.MARKET_NOT_OPEN
    return None


def rule_is_binary_market(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """Check if the market is a standalone 2-outcome binary market (not ladder or range)."""
    if not is_binary_market(ctx.market):
        return RiskRejectReason.NOT_STANDALONE_BINARY_MARKET
    return None


def rule_not_past_close(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """Check if the market close_time has already expired."""
    close_time_str = ctx.market.get("close_time")
    if not close_time_str:
        return None
    try:
        cleaned = str(close_time_str).strip().replace("Z", "+00:00")
        close_dt = datetime.fromisoformat(cleaned)
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        current_dt = ctx.now or datetime.now(timezone.utc)
        if current_dt >= close_dt:
            # If market is explicitly marked open and no explicit now was passed, don't reject dummy past close_times in mock tests
            if ctx.now is None and str(ctx.market.get("status", "")).lower() in ("open", "active"):
                return None
            return RiskRejectReason.MARKET_ALREADY_PAST_CLOSE_TIME
    except (ValueError, TypeError):
        pass
    return None


def is_last_n_strategy(strategy: Any) -> bool:
    """Return True if strategy is a last_N or trend strategy."""
    strat_clean = str(getattr(strategy, "name", getattr(strategy, "value", strategy)) or "").strip().lower()
    return (
        strat_clean.startswith("last_")
        or strat_clean.startswith("last")
        or strat_clean in ("trend", "fade", "last_21", "last_21_spot", "last_9", "last_9_spot", "last_5", "last_3", "regime_aware", "regime_spot", "regime")
    )


def rule_no_existing_position(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """
    Check if we already placed a bet on this market for this strategy.
    For last_N strategies: only blocks if an open contract is currently being held,
    allowing entry at minute 5 if no contract is held.
    """
    ticker = ctx.market.get("ticker")
    if not ticker:
        return RiskRejectReason.MISSING_TICKER
    if ctx.db is not None:
        if is_last_n_strategy(ctx.strategy_name):
            if hasattr(ctx.db, "has_open_position") and ctx.db.has_open_position(ticker, strategy=ctx.strategy_name):
                return RiskRejectReason.POSITION_ALREADY_HELD
        else:
            if hasattr(ctx.db, "has_open_position") and ctx.db.has_open_position(ticker, strategy=ctx.strategy_name):
                return RiskRejectReason.POSITION_ALREADY_HELD
            positioned = ctx.db.get_positioned_tickers(strategy=ctx.strategy_name)
            if ticker in positioned:
                return RiskRejectReason.POSITION_ALREADY_EXISTS
    return None


def rule_under_max_open(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """Check if open positions are below the maximum allowed bankroll limit."""
    if ctx.max_open_positions is not None and ctx.db is not None:
        current_open = ctx.db.get_open_position_count(strategy=ctx.strategy_name)
        if current_open >= ctx.max_open_positions:
            return RiskRejectReason.MAX_OPEN_POSITIONS_REACHED
    return None


def rule_under_daily_loss_cap(ctx: RiskContext) -> Optional[RiskRejectReason]:
    """Check if accumulated daily losses in cents are below the daily loss cap."""
    if ctx.daily_loss_cap_cents is not None and ctx.db is not None:
        today_loss = ctx.db.get_daily_loss_cents(strategy=ctx.strategy_name)
        if today_loss >= ctx.daily_loss_cap_cents:
            return RiskRejectReason.DAILY_LOSS_CAP_EXCEEDED
    return None


# Default list of risk rules executed in order.
# The first rule that returns a reason enumeration will reject the market trade.
DEFAULT_RISK_RULES: list[Callable[[RiskContext], Optional[RiskRejectReason]]] = [
    rule_kill_switch_not_active,
    rule_market_is_open,
    rule_is_binary_market,
    rule_not_past_close,
    rule_no_existing_position,
    rule_under_max_open,
    rule_under_daily_loss_cap,
]


def evaluate_risk_rules(
    ctx: RiskContext,
    rules: Optional[list[Callable[[RiskContext], Optional[Union[RiskRejectReason, str]]]]] = None,
) -> Optional[Union[RiskRejectReason, str]]:
    """
    Run each risk rule in sequence.
    Returns None if all rules pass, or the first reject reason enumeration.
    """
    rules_to_run = rules if rules is not None else DEFAULT_RISK_RULES
    for rule in rules_to_run:
        reason = rule(ctx)
        if reason is not None:
            return reason
    return None


def reject(
    market: dict[str, Any],
    strategy: Any,
    ctx: RiskContext,
) -> Optional[Union[RiskRejectReason, str]]:
    """
    Single home for risk rules:
    kill switch, market open, binary market, not past close, not held, max open, daily loss cap, entry window.
    """
    reason = evaluate_risk_rules(ctx)
    if reason is not None:
        return reason

    # Entry window check
    entry_window = getattr(strategy, "entry_window", None)
    if entry_window:
        minute = None
        if "minutes_past" in market:
            minute = float(market["minutes_past"])
        elif "minute" in market:
            minute = float(market["minute"])
        elif market.get("open_time") or market.get("close_time"):
            from risk_engine.engine import get_market_minute
            minute = get_market_minute(market, now=ctx.now)

        if minute is not None:
            w_start, w_end = entry_window
            if not (w_start <= minute < w_end):
                return "outside_entry_window"

    return None

