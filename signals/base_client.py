"""
Base Prediction Market Trading Client & Helper Utilities
=========================================================

Provides abstract base trading client functionality, log tag definitions, and shared
risk management logic (stop-loss evaluation, early cut-loss evaluation, order state checking)
shared between KalshiClient and GeminiClient.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Log Tag Enum Definitions (Human-Readable Info Brackets)
# ---------------------------------------------------------------------------
class LogTag(str, Enum):
    """
    Log Tag Enums representing info log event categories:
    """
    CLOCK_ALIGNMENT = "[CLOCK ALIGNMENT] ⏱️"          # Sleep timer waiting for 30s past next 5-min mark
    BUY_MATCH = "⚡ [STRATEGY MATCH]"                 # Dynamic buy strategy criteria met (price range + spot momentum)
    DRY_RUN_MATCH = "🎯 [DRY-RUN MATCH]"              # Preview buy match in dry-run mode
    DRY_RUN_PREVIEW = "🎯 [DRY-RUN PREVIEW]"          # Dry-run pipeline mode indicator
    OPEN_POSITION_GATE = "🛡️ [OPEN POSITION GATE]"     # Skipping buy order because open position or order exists
    STOP_LOSS_EVALUATION = "📊 [STOP-LOSS EVALUATION]"    # Evaluating 10m stop-loss condition
    STOP_LOSS_TRIGGERED = "🛑 [STOP-LOSS TRIGGERED]"      # Liquidating position immediately (loss >= 10¢ or price <= 30¢)
    DRY_RUN_STOP_LOSS = "🛑 [DRY-RUN STOP-LOSS]"        # Preview stop-loss liquidation in dry-run mode
    EARLY_CUT_LOSS_TRIGGERED = "✂️ [5-MIN CUT-LOSS TRIGGERED]"  # Liquidating 00m entry below 40¢ at 5m mark
    DRY_RUN_EARLY_CUT_LOSS = "✂️ [DRY-RUN 5-MIN CUT-LOSS]"      # Preview 5m cut-loss liquidation in dry-run mode
    HOLDING_POSITION = "🛡️ [HOLDING WINNING POSITION]"         # Holding healthy winning position above threshold
    POSITION_CHECK = "📊 [5-MIN POSITION CHECK]"      # Inspecting 5m open position status
    LET_IT_RUN = "🚀 [LET IT RUN TO 100¢]"             # Canceling 82¢ TP at 10m for strong winners (>= 70¢) to get 100¢
    PROFIT_FLOOR_ARMED = "🛡️ [PROFIT FLOOR ARMED]"     # Armed trailing profit floor (current_price - 10¢) on let-it-run winner
    PROFIT_FLOOR_TRIGGERED = "💰 [PROFIT FLOOR TRIGGERED]"  # Liquidating position when price hits trailing profit floor
    TRADE_EXECUTION = "🎯 [TRADE EXECUTION]"          # Submitting Maker buy order to exchange API
    MAKER_BUY_RESTING = "⏳ [MAKER BUY RESTING]"       # Maker buy order submitted and resting on book
    AUTO_SELL_SET = "💰 [AUTO-SELL SET]"              # Placing resting 82¢ limit sell order for take-profit
    POSITION_EXISTS = "🛡️ [POSITION EXISTS]"          # Active market position found in portfolio
    ORDER_EXISTS = "🛡️ [ORDER EXISTS]"                # Resting order found in portfolio
    PRUNE_STALE_BUY = "🧹 [PRUNED STALE BUY]"         # Canceled unfilled resting buy order from prior pass
    PORTFOLIO_PNL = "💰 [PORTFOLIO PNL]"              # Real-time portfolio balance and realized PnL summary


class BaseTradingClient(ABC):
    """
    Abstract base class providing shared trading strategy rules, risk management evaluations,
    and common helper methods for prediction market exchange clients.
    """

    exchange_name: str = "Exchange"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._profit_floors: dict[str, int] = {}

    @property
    def profit_floors(self) -> dict[str, int]:
        if not hasattr(self, "_profit_floors") or self._profit_floors is None:
            self._profit_floors = {}
        return self._profit_floors

    def get_profit_floor(self, ticker: str) -> int | None:
        """Returns active profit floor in cents for ticker, or None if not set."""
        return self.profit_floors.get(ticker)

    def set_profit_floor(self, ticker: str, floor_cents: int) -> None:
        """Sets or updates the trailing profit floor in cents for ticker."""
        self.profit_floors[ticker] = floor_cents

    def clear_profit_floor(self, ticker: str) -> None:
        """Clears any active profit floor for ticker (e.g. after liquidation or position close)."""
        self.profit_floors.pop(ticker, None)

    def has_profit_floor(self, ticker: str) -> bool:
        """Returns True if ticker has an active profit floor armed."""
        return ticker in self.profit_floors

    @abstractmethod
    def get_market_position(self, ticker: str) -> dict | None:
        """Retrieves current open position for ticker: {"side": "yes"/"no", "count": int, "entry_price_cents": int} or None."""
        pass

    @abstractmethod
    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        """Cancels resting orders for ticker."""
        pass

    @abstractmethod
    def execute_liquidation(self, ticker: str, side: str, count: int, current_price: int) -> dict:
        """Executes market/limit liquidation order for a position."""
        pass

    def evaluate_and_execute_stop_loss(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        dry_run: bool = False,
        stop_loss_threshold_cents: int = 10,
    ) -> dict:
        """
        Evaluates active positions for ticker at the 10-minute mark (:10, :25, :40, :55):
        1. Checks actual position from exchange portfolio.
        2. Trailing Profit Floor Trigger: If an armed profit floor exists and price dropped to/below it,
           immediately liquidates to lock in gains.
        3. Let Winners Run with Trailing Profit Floor: If position price >= 70¢, cancels resting TP
           order and arms/ratchets a trailing profit floor at (current_price - 10¢) to capture 100¢ settlement.
        4. Tightened Stop-Loss: If loss >= 10¢ OR price <= 30¢, sells position immediately.
        """
        pos = self.get_market_position(ticker)
        if pos is None:
            if not dry_run:
                self.cancel_and_verify_resting_orders(ticker)
            self.clear_profit_floor(ticker)
            return {"triggered": False}

        side = pos["side"]
        position_count = pos.get("count", 1)
        entry_price = pos.get("entry_price_cents") or 40

        current_price = current_yes_price if side == "yes" else current_no_price

        if current_price is None:
            log.warning(f"⚠️ Cannot evaluate stop-loss for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        loss_cents = entry_price - current_price
        pnl_label = f"Gain: +{abs(loss_cents)}¢ (Profit)" if loss_cents < 0 else f"Loss: -{loss_cents}¢"

        log.info(
            f"{LogTag.STOP_LOSS_EVALUATION.value} [{self.exchange_name}] Market: {ticker} | Side: {side.upper()} | "
            f"Entry: ~{entry_price}¢ | Current: {current_price}¢ | Status: {pnl_label}"
        )

        existing_floor = self.get_profit_floor(ticker)

        # 1. Trailing Profit Floor Trigger Check (if already armed on a winning position)
        if existing_floor is not None and current_price <= existing_floor:
            profit_locked = max(0, current_price - entry_price)
            reason = f"price dropped to {current_price}¢ (<= {existing_floor}¢ trailing profit floor)"

            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_STOP_LOSS.value} [{self.exchange_name}] {ticker} ({side.upper()}) {reason}. "
                    f"WOULD CANCEL RESTING ORDERS & LIQUIDATE TO LOCK IN +{profit_locked}¢ PROFIT."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "action": "profit_floor_sold",
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                    "profit_locked_cents": profit_locked,
                    "profit_floor_cents": existing_floor,
                }

            self.cancel_and_verify_resting_orders(ticker)
            self.execute_liquidation(ticker, side, position_count, current_price)
            self.clear_profit_floor(ticker)
            log.info(
                f"{LogTag.PROFIT_FLOOR_TRIGGERED.value} [{self.exchange_name}] Executed profit-floor liquidation for {ticker} "
                f"at {current_price}¢ (Floor: {existing_floor}¢ | Locked in +{profit_locked}¢ profit)."
            )
            return {
                "triggered": True,
                "side": side,
                "action": "profit_floor_sold",
                "loss_cents": loss_cents,
                "exit_price": current_price,
                "profit_locked_cents": profit_locked,
                "profit_floor_cents": existing_floor,
            }

        # 2. Let Winners Run with Trailing Profit Floor (>= 70¢)
        if current_price >= 70:
            candidate_floor = current_price - 10
            new_floor = max(existing_floor, candidate_floor) if existing_floor is not None else candidate_floor
            self.set_profit_floor(ticker, new_floor)

            log.info(
                f"{LogTag.LET_IT_RUN.value} [{self.exchange_name}] {ticker} ({side.upper()}) performs strongly at {current_price}¢ (>= 70¢)! "
                f"Canceling resting TP order to capture 100¢ settlement with armed trailing profit floor at {new_floor}¢."
            )
            if not dry_run:
                self.cancel_and_verify_resting_orders(ticker)
            return {
                "triggered": False,
                "action": "canceled_tp_let_run",
                "profit_floor_cents": new_floor,
            }

        # 3. Standard Tightened Stop-Loss Check
        is_loss_exceeded = loss_cents >= stop_loss_threshold_cents
        is_below_floor = current_price <= 30

        if is_loss_exceeded or is_below_floor:
            reason = f"loss is {loss_cents}¢ (>= {stop_loss_threshold_cents}¢ limit)" if is_loss_exceeded else f"price is {current_price}¢ (<= 30¢ floor)"

            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_STOP_LOSS.value} [{self.exchange_name}] {ticker} ({side.upper()}) {reason}. "
                    f"WOULD CANCEL RESTING ORDERS & SELL POSITION IMMEDIATELY."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "action": "stop_loss_sold",
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                }

            self.cancel_and_verify_resting_orders(ticker)
            self.execute_liquidation(ticker, side, position_count, current_price)
            self.clear_profit_floor(ticker)
            log.info(f"{LogTag.STOP_LOSS_TRIGGERED.value} [{self.exchange_name}] Executed stop-loss liquidation for {ticker} at {current_price}¢.")
            return {
                "triggered": True,
                "side": side,
                "action": "stop_loss_sold",
                "loss_cents": loss_cents,
                "exit_price": current_price,
            }

        return {"triggered": False}

    def evaluate_and_execute_5min_cut_loss(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        min_price_threshold: int = 40,
        dry_run: bool = False,
    ) -> dict:
        """
        Evaluates active positions for ticker at the 5-minute mark (:05, :20, :35, :50):
        If an asset bought at the 00-minute mark drops below 40¢,
        cancels resting take-profit limit orders and immediately liquidates the position to cut losses early.
        """
        pos = self.get_market_position(ticker)
        if pos is None:
            self.clear_profit_floor(ticker)
            return {"triggered": False}

        side = pos["side"]
        position_count = pos.get("count", 1)
        entry_price = pos.get("entry_price_cents") or 40

        current_price = current_yes_price if side == "yes" else current_no_price

        if current_price is None:
            log.warning(f"⚠️ Cannot evaluate 5-min cut-loss for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        loss_cents = entry_price - current_price
        if loss_cents < 0:
            pnl_label = f"Gain: +{abs(loss_cents)}¢ (In Profit 🟢)"
        elif loss_cents > 0:
            pnl_label = f"Loss: -{loss_cents}¢ (In Loss 🔴)"
        else:
            pnl_label = "Breakeven: 0¢ (Flat ⚪)"

        log.info(
            f"{LogTag.POSITION_CHECK.value} [{self.exchange_name}] Market: {ticker} | Side: {side.upper()} | "
            f"Entry: ~{entry_price}¢ | Current: {current_price}¢ | PnL: {pnl_label}"
        )

        if current_price < min_price_threshold:
            reason = f"Current price {current_price}¢ is below safety cut threshold ({min_price_threshold}¢)"

            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_EARLY_CUT_LOSS.value} [{self.exchange_name}] {ticker} ({side.upper()}): {reason}. "
                    f"WOULD CANCEL RESTING ORDERS & SELL POSITION IMMEDIATELY TO CUT LOSSES."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "action": "early_loss_cut",
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                }

            self.cancel_and_verify_resting_orders(ticker)
            self.execute_liquidation(ticker, side, position_count, current_price)
            self.clear_profit_floor(ticker)
            log.info(f"{LogTag.EARLY_CUT_LOSS_TRIGGERED.value} [{self.exchange_name}] Liquidated {ticker} ({side.upper()}) position early at {current_price}¢ ({reason}).")
            return {
                "triggered": True,
                "side": side,
                "action": "early_loss_cut",
                "loss_cents": loss_cents,
                "exit_price": current_price,
            }
        else:
            log.info(
                f"{LogTag.HOLDING_POSITION.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
                f"Current price is {current_price}¢ (>= {min_price_threshold}¢ threshold, {pnl_label}). "
                f"Position is healthy and winning! Holding for Take-Profit auto-sell or $1.00 settlement."
            )

        return {"triggered": False}
