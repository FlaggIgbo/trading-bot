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
    PRICE_REJECTED = "🚫 [PRICE REJECTED]"            # Candidate price rejected due to low EV middle zone
    OPEN_POSITION_GATE = "🛡️ [OPEN POSITION GATE]"     # Skipping buy order because open position or order exists
    STOP_LOSS_EVALUATION = "📊 [STOP-LOSS EVALUATION]"    # Evaluating 10m stop-loss condition
    STOP_LOSS_TRIGGERED = "🛑 [STOP-LOSS TRIGGERED]"      # Liquidating position immediately (loss >= 10¢ or price <= 30¢)
    DRY_RUN_STOP_LOSS = "🛑 [DRY-RUN STOP-LOSS]"        # Preview stop-loss liquidation in dry-run mode
    EARLY_CUT_LOSS_TRIGGERED = "✂️ [5-MIN CUT-LOSS TRIGGERED]"  # Liquidating 00m entry below 40¢ at 5m mark
    DRY_RUN_EARLY_CUT_LOSS = "✂️ [DRY-RUN 5-MIN CUT-LOSS]"      # Preview 5m cut-loss liquidation in dry-run mode
    HOLDING_POSITION = "🛡️ [HOLDING WINNING POSITION]"         # Holding healthy winning position above threshold
    POSITION_CHECK = "📊 [5-MIN POSITION CHECK]"      # Inspecting 5m open position status
    ENDGAME_CHECK = "⏳ [12.5-MIN ENDGAME CHECK]"     # Inspecting 12.5m endgame position status (2.5m before expiration)
    ENDGAME_CUT_LOSS_TRIGGERED = "🛑 [12.5-MIN CUT-LOSS TRIGGERED]"  # Liquidating losing position at 12.5m mark
    DRY_RUN_ENDGAME_CUT_LOSS = "🛑 [DRY-RUN 12.5-MIN CUT-LOSS]"      # Preview 12.5m cut-loss liquidation in dry-run mode
    WINNER_ROLLED = "🔄 [WINNER PROFIT LOCKED & RE-BOUGHT]"  # Selling winning position at 10m to bank profit & re-buying at ask to run to 100¢
    DRY_RUN_WINNER_ROLLED = "🔄 [DRY-RUN WINNER ROLLED]"      # Preview 10m winner roll in dry-run mode
    LET_IT_RUN = "🚀 [LET IT RUN TO 100¢]"             # Letting winner run to 100¢ settlement payout
    PROFIT_FLOOR_ARMED = "🛡️ [PROFIT FLOOR ARMED]"     # Armed trailing profit floor (current_price - 10¢) on let-it-run winner
    PROFIT_FLOOR_TRIGGERED = "💰 [PROFIT FLOOR TRIGGERED]"  # Liquidating position when price hits trailing profit floor
    TRADE_EXECUTION = "🎯 [TRADE EXECUTION]"          # Submitting Maker buy order to exchange API
    MAKER_BUY_RESTING = "⏳ [MAKER BUY RESTING]"       # Maker buy order submitted and resting on book
    AUTO_SELL_SET = "💰 [AUTO-SELL SET]"              # Placing resting 82¢ limit sell order for take-profit
    POSITION_EXISTS = "🛡️ [POSITION EXISTS]"          # Active market position found in portfolio
    ORDER_EXISTS = "🛡️ [ORDER EXISTS]"                # Resting order found in portfolio
    PRUNE_STALE_BUY = "🧹 [PRUNED STALE BUY]"         # Canceled unfilled resting buy order from prior pass
    PORTFOLIO_PNL = "💰 [PORTFOLIO PNL]"              # Real-time portfolio balance and realized PnL summary
    HOURLY_MULTI_STRIKE = "🎯 [HOURLY MULTI-STRIKE SCAN]"  # Scanning hourly multi-strike options
    TIEBREAKER_CHOSEN = "⚖️ [TIEBREAKER CHOSEN]"       # Resolved tiebreaker between YES and NO on same contract
    DUAL_BUY_MATCH = "⚡ [DUAL-STRIKE BUY MATCH]"     # Executing dual buy on different strike contracts
    HOURLY_CHECK = "⏱️ [HOURLY POSITION CHECK]"       # Hourly lifecycle position check (20m, 40m, 50m)


class BaseTradingClient(ABC):
    """
    Abstract base class providing shared trading strategy rules, risk management evaluations,
    and common helper methods for prediction market exchange clients.
    """

    exchange_name: str = "Exchange"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keeps track of armed trailing profit floors in memory (e.g. floor at 62¢ for a position winning at 72¢)
        self._profit_floors: dict[str, int] = {}
        # Keeps track of the exact price we paid when entering a contract (e.g. 52¢)
        self._entry_prices: dict[str, int] = {}

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

    @property
    def entry_prices(self) -> dict[str, int]:
        if not hasattr(self, "_entry_prices") or self._entry_prices is None:
            self._entry_prices = {}
        return self._entry_prices

    def get_entry_price(self, ticker: str) -> int | None:
        """Returns recorded buy entry price in cents for ticker, or None if unknown."""
        return self.entry_prices.get(ticker)

    def set_entry_price(self, ticker: str, price_cents: int) -> None:
        """Records the exact entry price in cents when we buy a contract."""
        self.entry_prices[ticker] = price_cents

    def clear_entry_price(self, ticker: str) -> None:
        """Clears recorded entry price for ticker once the position is closed."""
        self.entry_prices.pop(ticker, None)

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

    @abstractmethod
    def execute_buy(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        """Executes a buy order for ticker at target price."""
        pass

    def evaluate_and_execute_10min_strategy(
        self,
        ticker: str,
        current_yes_bid: int | None,
        current_no_bid: int | None,
        current_yes_ask: int | None = None,
        current_no_ask: int | None = None,
        dry_run: bool = False,
        stop_loss_threshold_cents: int = 10,
        price_floor: int = 30,
        win_threshold_cents: int = 70,
    ) -> dict:
        """
        Evaluates active positions at the 10-minute mark (:10:30, :25:30, :40:30, :55:30):
        1. Winning Positions (Bid >= 70¢ or profit >= 15¢):
           - Sells the winning position at the current Bid to LOCK IN / BANK REALIZED PROFIT in cash!
           - Immediately re-buys 1 contract at the current Ask price to reset cost basis and let THAT re-bought position run to 100¢ ($1.00)!
        2. Losing Positions:
           - If loss >= stop_loss_threshold_cents OR price <= price_floor:
             Executes immediate taker liquidation to cut losses.
        """
        pos = self.get_market_position(ticker)
        if pos is None:
            if not dry_run:
                self.cancel_and_verify_resting_orders(ticker)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            return {"triggered": False}

        side = pos["side"]
        position_count = pos.get("count", 1)
        entry_price = pos.get("entry_price_cents") or self.get_entry_price(ticker) or 40

        bid_price = current_yes_bid if side == "yes" else current_no_bid
        ask_price = current_yes_ask if side == "yes" else current_no_ask
        if ask_price is None and bid_price is not None:
            ask_price = min(99, bid_price + 1)

        if bid_price is None:
            log.warning(f"⚠️ Cannot evaluate 10-min strategy for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        loss_cents = entry_price - bid_price
        profit_cents = bid_price - entry_price

        pnl_label = f"Gain: +{profit_cents}¢ (In Profit 🟢)" if profit_cents > 0 else f"Loss: -{loss_cents}¢ (In Loss 🔴)" if loss_cents > 0 else "Breakeven: 0¢ (Flat ⚪)"

        log.info(
            f"{LogTag.STOP_LOSS_EVALUATION.value} [{self.exchange_name}] Market: {ticker} | Side: {side.upper()} | "
            f"Entry: ~{entry_price}¢ | Bid: {bid_price}¢ | Ask: {ask_price}¢ | Status: {pnl_label}"
        )

        # 1. Winning Position Roll: If position reached >= 70¢ or profit >= 15¢
        # Sell at Bid to bank realized profit, then re-buy at Ask to let the new position run to 100¢!
        if bid_price >= win_threshold_cents or profit_cents >= 15:
            rebuy_price = ask_price if ask_price is not None else min(99, bid_price + 1)

            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_WINNER_ROLLED.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"WOULD SELL AT {bid_price}¢ (BANK +{profit_cents}¢ PROFIT) & RE-BUY AT {rebuy_price}¢ TO RUN TO 100¢ SETTLEMENT."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "action": "winner_rolled_rebuy",
                    "exit_price": bid_price,
                    "profit_locked_cents": profit_cents,
                    "rebuy_price_cents": rebuy_price,
                }

            # Step 1: Cancel any resting TP orders
            self.cancel_and_verify_resting_orders(ticker)
            # Step 2: Liquidate current position to bank profit in cash
            self.execute_liquidation(ticker, side, position_count, bid_price)
            self.clear_profit_floor(ticker)

            # Step 3: Re-buy at Ask to let new position run to 100¢
            try:
                self.execute_buy(ticker, side, position_count, rebuy_price)
                self.set_entry_price(ticker, rebuy_price)
                log.info(
                    f"{LogTag.WINNER_ROLLED.value} [{self.exchange_name}] Successfully rolled winner for {ticker} ({side.upper()}): "
                    f"Sold at {bid_price}¢ (+{profit_cents}¢ profit banked!) and re-bought at {rebuy_price}¢ to run to 100¢ ($1.00 payout)!"
                )
            except Exception as buy_err:
                log.error(f"❌ [{self.exchange_name}] Failed to re-buy {ticker} after banking profit: {buy_err}")

            return {
                "triggered": True,
                "side": side,
                "action": "winner_rolled_rebuy",
                "exit_price": bid_price,
                "profit_locked_cents": profit_cents,
                "rebuy_price_cents": rebuy_price,
            }

        # 2. Stop-Loss Check: If loss >= stop_loss_threshold_cents OR bid_price <= price_floor
        is_loss_exceeded = loss_cents >= stop_loss_threshold_cents
        is_below_floor = bid_price <= price_floor

        if is_loss_exceeded or is_below_floor:
            reason = f"loss is {loss_cents}¢ (>= {stop_loss_threshold_cents}¢ limit)" if is_loss_exceeded else f"price is {bid_price}¢ (<= {price_floor}¢ floor)"

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
                    "exit_price": bid_price,
                }

            self.cancel_and_verify_resting_orders(ticker)
            self.execute_liquidation(ticker, side, position_count, bid_price)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            log.info(f"{LogTag.STOP_LOSS_TRIGGERED.value} [{self.exchange_name}] Executed stop-loss liquidation for {ticker} at {bid_price}¢ ({reason}).")
            return {
                "triggered": True,
                "side": side,
                "action": "stop_loss_sold",
                "loss_cents": loss_cents,
                "exit_price": bid_price,
            }

        return {"triggered": False}

    def evaluate_and_execute_stop_loss(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        dry_run: bool = False,
        stop_loss_threshold_cents: int = 10,
        price_floor: int = 30,
    ) -> dict:
        """Alias for 10-minute strategy evaluation."""
        return self.evaluate_and_execute_10min_strategy(
            ticker=ticker,
            current_yes_bid=current_yes_price,
            current_no_bid=current_no_price,
            dry_run=dry_run,
            stop_loss_threshold_cents=stop_loss_threshold_cents,
            price_floor=price_floor,
        )

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
            self.clear_entry_price(ticker)
            return {"triggered": False}

        side = pos["side"]
        position_count = pos.get("count", 1)
        entry_price = pos.get("entry_price_cents") or self.get_entry_price(ticker) or 40

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
            self.clear_entry_price(ticker)
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

    def evaluate_and_execute_12_5min_endgame_check(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        dry_run: bool = False,
        endgame_floor: int = 25,
    ) -> dict:
        """
        Evaluates active positions at the 12.5-minute mark (:12:30, :27:30, :42:30, :57:30):
        With only 2.5 minutes remaining before contract settlement:
        - If we are losing (current price < entry price / loss > 0, or current price < endgame_floor e.g. < 25¢),
          liquidates immediately so we don't lose all capital gains or suffer a 100% loss at 0¢ expiration.
        - If winning (current price >= entry price and >= endgame_floor), holds position into the final 2.5m for 100¢ ($1.00) settlement payout!
        """
        pos = self.get_market_position(ticker)
        if pos is None:
            if not dry_run:
                self.cancel_and_verify_resting_orders(ticker)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            return {"triggered": False}

        side = pos["side"]
        position_count = pos.get("count", 1)
        entry_price = pos.get("entry_price_cents") or self.get_entry_price(ticker) or 40
        current_price = current_yes_price if side == "yes" else current_no_price

        if current_price is None:
            log.warning(f"⚠️ Cannot evaluate 12.5-min endgame check for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        loss_cents = entry_price - current_price
        pnl_label = f"Gain: +{abs(loss_cents)}¢ (In Profit 🟢)" if loss_cents < 0 else f"Loss: -{loss_cents}¢ (In Loss 🔴)" if loss_cents > 0 else "Breakeven: 0¢ (Flat ⚪)"

        log.info(
            f"{LogTag.ENDGAME_CHECK.value} [{self.exchange_name}] Market: {ticker} | Side: {side.upper()} | "
            f"Entry: ~{entry_price}¢ | Current: {current_price}¢ | Floor: {endgame_floor}¢ | Status: {pnl_label}"
        )

        # Endgame Loss Check: If price is below endgame floor (e.g. < 25¢) or losing on entry/rebuy (loss_cents > 0)
        is_losing = (loss_cents > 0) or (current_price < endgame_floor)
        if is_losing:
            reason = f"losing at 12.5m mark (Entry: ~{entry_price}¢, Current: {current_price}¢, Floor: {endgame_floor}¢, Loss: -{loss_cents}¢)"
            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_ENDGAME_CUT_LOSS.value} [{self.exchange_name}] {ticker} ({side.upper()}): {reason}. "
                    f"WOULD LIQUIDATE IMMEDIATELY TO SALVAGE CAPITAL BEFORE EXPIRATION."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "action": "endgame_loss_cut",
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                }

            self.cancel_and_verify_resting_orders(ticker)
            self.execute_liquidation(ticker, side, position_count, current_price)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            log.info(
                f"{LogTag.ENDGAME_CUT_LOSS_TRIGGERED.value} [{self.exchange_name}] Liquidated losing position {ticker} "
                f"({side.upper()}) at {current_price}¢ to prevent total loss at expiration ({reason})."
            )
            return {
                "triggered": True,
                "side": side,
                "action": "endgame_loss_cut",
                "loss_cents": loss_cents,
                "exit_price": current_price,
            }

        # Holding Winning Position (in profit into final 2.5 minutes)
        log.info(
            f"{LogTag.HOLDING_POSITION.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
            f"Position is winning at {current_price}¢ ({pnl_label})! Holding into final 2.5m for 100¢ ($1.00) settlement."
        )
        return {"triggered": False, "action": "holding_winner"}
