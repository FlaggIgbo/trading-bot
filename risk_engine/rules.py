"""
Risk Rules
==========
Composable, pure-logic risk checks.
Each rule is a simple function that receives a RiskContext and returns:
- None if the check passes (OK to proceed)
- A short reason string if the check fails (e.g. "market_not_open", "max_open_positions_reached")

This makes rules easy to unit test without any real network I/O.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any, Callable, Optional

from db import Database, MarketStatus
from ingestion.api.client import is_binary_market


@dataclass
class RiskContext:
    market: dict[str, Any]
    strategy_name: str
    db: Optional[Database] = None
    max_open_positions: Optional[int] = None
    daily_loss_cap_cents: Optional[int] = None
    kill_switch_path: Optional[str] = "KILL_SWITCH"
    now: Optional[datetime] = None


def rule_kill_switch_not_active(ctx: RiskContext) -> Optional[str]:
    """Check if the local emergency kill switch is active (via file or environment variable)."""
    if ctx.kill_switch_path and os.path.exists(ctx.kill_switch_path):
        return "kill_switch_file_detected"
    if os.getenv("KILL_SWITCH", "").strip().lower() in ("1", "true", "yes"):
        return "kill_switch_env_detected"
    return None


def rule_market_is_open(ctx: RiskContext) -> Optional[str]:
    """Check if the market status is OPEN or ACTIVE."""
    status = str(ctx.market.get("status", "")).strip().lower()
    if status not in (MarketStatus.OPEN.value, MarketStatus.ACTIVE.value):
        return f"market_not_open_{status or 'unknown'}"
    return None


def rule_is_binary_market(ctx: RiskContext) -> Optional[str]:
    """Check if the market is a standalone 2-outcome binary market (not ladder or range)."""
    if not is_binary_market(ctx.market):
        return "not_standalone_binary_market"
    return None


def rule_not_past_close(ctx: RiskContext) -> Optional[str]:
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
            return "market_already_past_close_time"
    except (ValueError, TypeError):
        pass
    return None


def rule_no_existing_position(ctx: RiskContext) -> Optional[str]:
    """Check if we already placed a bet on this market for this strategy (one bet per market)."""
    ticker = ctx.market.get("ticker")
    if not ticker:
        return "missing_ticker"
    if ctx.db is not None:
        positioned = ctx.db.get_positioned_tickers(strategy=ctx.strategy_name)
        if ticker in positioned:
            return "position_already_exists"
    return None


def rule_under_max_open(ctx: RiskContext) -> Optional[str]:
    """Check if open positions are below the maximum allowed bankroll limit."""
    if ctx.max_open_positions is not None and ctx.db is not None:
        current_open = ctx.db.get_open_position_count(strategy=ctx.strategy_name)
        if current_open >= ctx.max_open_positions:
            return f"max_open_positions_reached_{current_open}_of_{ctx.max_open_positions}"
    return None


def rule_under_daily_loss_cap(ctx: RiskContext) -> Optional[str]:
    """Check if accumulated daily losses in cents are below the daily loss cap."""
    if ctx.daily_loss_cap_cents is not None and ctx.db is not None:
        today_loss = ctx.db.get_daily_loss_cents(strategy=ctx.strategy_name)
        if today_loss >= ctx.daily_loss_cap_cents:
            return f"daily_loss_cap_exceeded_{today_loss}c_of_{ctx.daily_loss_cap_cents}c"
    return None


# Default list of risk rules executed in order.
# The first rule that returns a reason string will reject the market trade.
DEFAULT_RISK_RULES: list[Callable[[RiskContext], Optional[str]]] = [
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
    rules: Optional[list[Callable[[RiskContext], Optional[str]]]] = None,
) -> Optional[str]:
    """
    Run each risk rule in sequence.
    Returns None if all rules pass, or the first reject reason string.
    """
    rules_to_run = rules if rules is not None else DEFAULT_RISK_RULES
    for rule in rules_to_run:
        reason = rule(ctx)
        if reason is not None:
            return reason
    return None
