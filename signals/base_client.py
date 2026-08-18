"""
Base Prediction Market Trading Client & Risk Architecture
==========================================================

Provides abstract base trading client functionality, human-readable log tags, and
defensive risk management engines shared across prediction market exchange clients:

Key Robustness & Safety Engines:
--------------------------------
1. Order Cancellation Verification:
   - Verifies 0 resting orders before executing liquidations to prevent locked position errors.
2. Crash-Proof Persistent Cost Basis:
   - Reads/writes entry prices from MongoDB Atlas `active_positions` to survive worker restarts.
3. Winner Profit Banking (≥70¢ or ≥+15¢ Gain):
   - Sells winning positions at Bid to lock realized profit directly into cash balance without high-price re-buys.
4. Synchronized Checkpoint Lifecycle:
   - Evaluates early cut-loss (5m/20m), winner profit banking (10m/40m/55m), stop-loss exits, and endgame checks (12.5m/50m/57.5m).
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
    WINNER_ROLLED = "💰 [WINNER PROFIT BANKED]"        # Selling winning position at 10m/40m to bank realized cash profit
    DRY_RUN_WINNER_ROLLED = "💰 [DRY-RUN WINNER PROFIT BANKED]"  # Preview 10m/40m winner profit banking in dry-run mode
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
    HOURLY_CHECK = "⏱️ [HOURLY POSITION CHECK]"       # Hourly lifecycle position check (20m, 30m, 40m, 50m, 55m, 57.5m)


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
        # Keeps track of the intended take-profit price for this position (e.g. 88¢ or 62¢)
        self._target_tps: dict[str, int] = {}
        # Keeps track of strategy tags (e.g. 'endgame_favorite_maker', 'discount_longshot', 'momentum')
        self._strategy_tags: dict[str, str] = {}
        # Strict Single-Side Market Lock: Maps ticker -> side ('yes' or 'no') to prevent dual buys
        self._traded_markets: dict[str, str] = {}

    @property
    def strategy_tags(self) -> dict[str, str]:
        if not hasattr(self, "_strategy_tags") or self._strategy_tags is None:
            self._strategy_tags = {}
        return self._strategy_tags

    def get_strategy_tag(self, ticker: str) -> str | None:
        """Returns the strategy tag for ticker (e.g. 'endgame_favorite_maker')."""
        if ticker in self.strategy_tags:
            return self.strategy_tags[ticker]
        try:
            from database import get_active_position_strategy_tag
            db_tag = get_active_position_strategy_tag(ticker, getattr(self, "exchange_name", "kalshi"))
            if db_tag is not None:
                self.strategy_tags[ticker] = db_tag
                return db_tag
        except Exception as err:
            log.debug(f"Could not load strategy tag for {ticker} from DB: {err}")
        return None

    def set_strategy_tag(self, ticker: str, tag: str) -> None:
        """Sets or updates the strategy tag for ticker."""
        self.strategy_tags[ticker] = tag

    def clear_strategy_tag(self, ticker: str) -> None:
        """Clears strategy tag for ticker."""
        self.strategy_tags.pop(ticker, None)

    @property
    def traded_markets(self) -> dict[str, str]:
        if not hasattr(self, "_traded_markets") or self._traded_markets is None:
            self._traded_markets = {}
        return self._traded_markets

    def record_traded_market(self, ticker: str, side: str) -> None:
        """Records that ticker has been traded on side during the current market lifecycle."""
        self.traded_markets[ticker] = side.lower()

    def get_traded_market_side(self, ticker: str) -> str | None:
        """Returns the side already traded for this ticker, or None."""
        return self.traded_markets.get(ticker)

    def is_side_allowed(self, ticker: str, side: str) -> bool:
        """
        Strict Single-Side Market Lock:
        If ticker was already traded on the opposing side during this lifecycle, returns False.
        Prevents buying YES and NO on the same contract (synthetic loss).
        """
        side_clean = side.lower()
        existing_side = self.get_traded_market_side(ticker)
        if existing_side is not None and existing_side != side_clean:
            return False
        pos = self.get_market_position(ticker)
        if pos is not None and pos.get("side", "").lower() != side_clean:
            return False
        try:
            from database import get_active_position_cost
            db_pos = get_active_position_cost(ticker)
            if db_pos is not None:
                db_side = str(db_pos.get("side", "")).lower()
                if db_side and db_side != side_clean:
                    return False
        except Exception:
            pass
        return True

    def clear_traded_market(self, ticker: str) -> None:
        """Clears traded market record for ticker (e.g. on new contract rollover)."""
        self.traded_markets.pop(ticker, None)

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

    @property
    def target_tps(self) -> dict[str, int]:
        if not hasattr(self, "_target_tps") or self._target_tps is None:
            self._target_tps = {}
        return self._target_tps

    def get_entry_price(self, ticker: str) -> int | None:
        """
        Returns recorded buy entry price in cents for ticker.
        Checks in-memory cache first, then MongoDB Atlas active_positions collection upon restart.
        """
        if ticker in self.entry_prices:
            return self.entry_prices[ticker]
        try:
            from database import get_active_position_cost
            db_price = get_active_position_cost(ticker, getattr(self, "exchange_name", "kalshi"))
            if db_price is not None:
                self._entry_prices[ticker] = db_price
                return db_price
        except Exception as err:
            log.debug(f"Could not load entry price for {ticker} from DB: {err}")
        return None

    def get_target_tp(self, ticker: str) -> int | None:
        """
        Returns intended take-profit price in cents for ticker.
        Checks in-memory cache first, then MongoDB Atlas active_positions collection upon restart.
        """
        if ticker in self.target_tps:
            return self.target_tps[ticker]
        try:
            from database import get_active_position_target_tp
            db_tp = get_active_position_target_tp(ticker, getattr(self, "exchange_name", "kalshi"))
            if db_tp is not None:
                self._target_tps[ticker] = db_tp
                return db_tp
        except Exception as err:
            log.debug(f"Could not load target TP for {ticker} from DB: {err}")
        return None

    def set_target_tp(self, ticker: str, tp_cents: int) -> None:
        """Sets or updates the intended take-profit target in cents for ticker."""
        self.target_tps[ticker] = tp_cents

    def set_entry_price(
        self,
        ticker: str,
        price_cents: int,
        side: str = "yes",
        count: int = 1,
        client_order_id: str | None = None,
        tp_order_id: str | None = None,
        take_profit_cents: int | None = None,
        strategy_tag: str | None = None,
        spread_saved_cents: int | None = None,
        order_role: str | None = None,
    ) -> None:
        """Records the exact entry price, strategy tag, and target TP in cents in memory and MongoDB Atlas."""
        self.entry_prices[ticker] = price_cents
        self.record_traded_market(ticker, side)
        if take_profit_cents is not None:
            self.target_tps[ticker] = take_profit_cents
        if strategy_tag is not None:
            self.strategy_tags[ticker] = strategy_tag
        try:
            from database import save_active_position_cost
            save_active_position_cost(
                ticker=ticker,
                side=side,
                count=count,
                entry_price_cents=price_cents,
                ex=getattr(self, "exchange_name", "kalshi"),
                client_order_id=client_order_id,
                tp_order_id=tp_order_id,
                target_tp_cents=take_profit_cents,
                strategy_tag=strategy_tag,
                spread_saved_cents=spread_saved_cents,
                order_role=order_role,
            )
        except Exception as err:
            log.debug(f"Could not persist entry price for {ticker} to DB: {err}")

    def clear_entry_price(self, ticker: str) -> None:
        """Clears recorded entry price, strategy tag, and target TP from memory and MongoDB Atlas."""
        self.entry_prices.pop(ticker, None)
        self.target_tps.pop(ticker, None)
        self.strategy_tags.pop(ticker, None)
        try:
            from database import remove_active_position_cost
            remove_active_position_cost(ticker, getattr(self, "exchange_name", "kalshi"))
        except Exception as err:
            log.debug(f"Could not remove entry price for {ticker} from DB: {err}")

    def get_balance_cents(self) -> int | None:
        """
        Retrieves current available cash balance in cents.
        Subclasses (e.g. KalshiClient) can override or BaseTradingClient falls back to calling get_balance().
        """
        if hasattr(self, "get_balance"):
            try:
                b = self.get_balance()
                if isinstance(b, dict) and "balance" in b:
                    return int(b["balance"])
            except Exception:
                pass
        return None

    @abstractmethod
    def get_market_position(self, ticker: str) -> dict | None:
        """Retrieves current open position for ticker: {"side": "yes"/"no", "count": int, "entry_price_cents": int} or None."""
        pass

    @abstractmethod
    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        """Cancels resting orders for ticker and verifies that 0 resting orders remain."""
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
        stop_loss_threshold_cents: int = 25,
        price_floor: int = 15,
        win_threshold_cents: int = 70,
    ) -> dict:
        """
        Evaluates active positions at the 10-minute / 40-minute / 55-minute mark:
        1. Winning Positions (Bid >= 70¢ or profit >= 15¢):
           - Cancels resting TP orders with strict verification.
           - Sells the winning position at current Bid to LOCK IN / BANK REALIZED PROFIT directly in cash balance.
           - Keeps profit safely in balance without high-price re-buys.
        2. Losing Positions:
           - If loss >= stop_loss_threshold_cents OR price <= price_floor:
             Cancels resting orders with strict verification, then executes immediate taker liquidation.
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
        bid_price = current_yes_bid if side == "yes" else current_no_bid
        ask_price = current_yes_ask if side == "yes" else current_no_ask
        if ask_price is None and bid_price is not None:
            ask_price = min(99, bid_price + 1)

        # Dynamic entry price resolution: 1) Native position exposure, 2) DB/Memory, 3) Dynamic bid fallback
        entry_price = pos.get("entry_price_cents") or self.get_entry_price(ticker) or bid_price or 50

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

        # 1. Favorite Maker EV-Preservation Guard (Reddit Empirical Finding):
        # Dipped favorites recover 70%+ of the time. Cutting dipped favorites costs ~20¢ of EV.
        # Positions entered under the Favorite Maker strategy (entry_price >= 70¢ or strategy_tag == 'endgame_favorite_maker')
        # are held through to 100¢ settlement rather than being cut prematurely at depressed bids.
        is_favorite_maker = (self.get_strategy_tag(ticker) == "endgame_favorite_maker") or (entry_price >= 70)
        if is_favorite_maker:
            log.info(
                f"🛡️ [FAVORITE MAKER HOLD] [{self.exchange_name}] {ticker} ({side.upper()}): "
                f"Holding favorite position into settlement (entry: {entry_price}¢, bid: {bid_price}¢). "
                f"Bypassing intermediate panic stop-loss / early profit banking to capture full 100¢ payout."
            )
            return {"triggered": False, "action": "favorite_maker_holding"}

        # 2. Winning Position Profit Bank: If position reached >= 70¢ or profit >= 15¢
        # Sell at Bid to bank realized profit in cash. Keep the cash in balance (DO NOT re-buy at 85¢-98¢ into negative EV).
        if bid_price >= win_threshold_cents or profit_cents >= 15:
            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_WINNER_ROLLED.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"WOULD SELL AT {bid_price}¢ (BANK +{profit_cents}¢ PROFIT IN CASH). Keeping profit safely in balance."
                )
                return {
                    "triggered": True,
                    "side": side,
                    "action": "winner_profit_banked",
                    "exit_price": bid_price,
                    "profit_locked_cents": profit_cents,
                }

            # Step 1: Cancel resting TP orders with strict verification
            cleared = self.cancel_and_verify_resting_orders(ticker)
            if not cleared:
                log.error(
                    f"❌ [{self.exchange_name}] Cannot bank profit for {ticker}: "
                    f"Failed to clear resting orders. Aborting liquidation to avoid locked position errors."
                )
                return {"triggered": False, "error": "resting_orders_not_cleared"}

            # Step 2: Liquidate current position to bank profit in cash
            self.execute_liquidation(ticker, side, position_count, bid_price)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)

            log.info(
                f"💰 [WINNER PROFIT BANKED] [{self.exchange_name}] Successfully banked profit for {ticker} ({side.upper()}): "
                f"Sold at {bid_price}¢ (+{profit_cents}¢ profit banked!). Kept cash safely in balance without high-price re-buy."
            )

            return {
                "triggered": True,
                "side": side,
                "action": "winner_profit_banked",
                "exit_price": bid_price,
                "profit_locked_cents": profit_cents,
            }

        # 3. Stop-Loss Check: If loss >= stop_loss_threshold_cents OR bid_price <= price_floor
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

            # Step 1: Cancel resting TP orders with strict verification
            cleared = self.cancel_and_verify_resting_orders(ticker)
            if not cleared:
                log.error(
                    f"❌ [{self.exchange_name}] Cannot execute stop-loss liquidation for {ticker}: "
                    f"Failed to clear resting orders. Aborting liquidation to avoid locked position errors."
                )
                return {"triggered": False, "error": "resting_orders_not_cleared"}

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
        stop_loss_threshold_cents: int = 25,
        price_floor: int = 15,
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
        min_price_threshold: int = 15,
        dry_run: bool = False,
    ) -> dict:
        """
        Evaluates active positions for ticker at the 5-minute / 20-minute mark:
        If an asset drops below the dynamic safety cut threshold,
        verifies resting order cancellation and immediately liquidates the position to cut losses early.
        """
        pos = self.get_market_position(ticker)
        if pos is None:
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            return {"triggered": False}

        side = pos["side"]
        position_count = pos.get("count", 1)
        current_price = current_yes_price if side == "yes" else current_no_price

        # Dynamic entry price resolution
        entry_price = pos.get("entry_price_cents") or self.get_entry_price(ticker) or current_price or 50

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

        is_favorite_maker = (self.get_strategy_tag(ticker) == "endgame_favorite_maker") or (entry_price >= 70)
        if is_favorite_maker:
            log.info(
                f"🛡️ [FAVORITE MAKER HOLD] [{self.exchange_name}] {ticker} ({side.upper()}): "
                f"Favorite maker holding (entry: {entry_price}¢, price: {current_price}¢). "
                f"Bypassing early cut-loss to preserve positive EV into expiration."
            )
            return {"triggered": False, "action": "favorite_maker_holding"}

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

            cleared = self.cancel_and_verify_resting_orders(ticker)
            if not cleared:
                log.error(
                    f"❌ [{self.exchange_name}] Cannot execute early cut-loss liquidation for {ticker}: "
                    f"Failed to clear resting orders. Aborting liquidation to avoid locked position errors."
                )
                return {"triggered": False, "error": "resting_orders_not_cleared"}

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
            if loss_cents > 0:
                log.info(
                    f"{LogTag.HOLDING_POSITION.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Current price is {current_price}¢ (>= {min_price_threshold}¢ safety threshold, {pnl_label}). "
                    f"Position is currently trailing entry but above safety cut floor ({min_price_threshold}¢). Holding to allow price recovery before Stop-Loss / Endgame marks."
                )
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
        endgame_floor: int = 15,
    ) -> dict:
        """
        Evaluates active positions at the endgame mark (2.5m before 15m expiry, or 10m / 2.5m before hourly expiry e.g. 50m / 57.5m):
        - If we are losing (current price < entry price / loss > 0, or current price < endgame_floor e.g. < 25¢),
          verifies resting order cancellation and liquidates immediately to salvage capital before 0¢ expiration.
        - If winning (current price >= entry price and >= endgame_floor), holds position for 100¢ ($1.00) settlement payout!
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
        current_price = current_yes_price if side == "yes" else current_no_price

        # Dynamic entry price resolution
        entry_price = pos.get("entry_price_cents") or self.get_entry_price(ticker) or current_price or 50

        if current_price is None:
            log.warning(f"⚠️ Cannot evaluate endgame check for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        loss_cents = entry_price - current_price
        pnl_label = f"Gain: +{abs(loss_cents)}¢ (In Profit 🟢)" if loss_cents < 0 else f"Loss: -{loss_cents}¢ (In Loss 🔴)" if loss_cents > 0 else "Breakeven: 0¢ (Flat ⚪)"

        log.info(
            f"{LogTag.ENDGAME_CHECK.value} [{self.exchange_name}] Market: {ticker} | Side: {side.upper()} | "
            f"Entry: ~{entry_price}¢ | Current: {current_price}¢ | Floor: {endgame_floor}¢ | Status: {pnl_label}"
        )

        # Favorite Maker Endgame Evaluation (Hold to Settlement):
        is_favorite_maker = (self.get_strategy_tag(ticker) == "endgame_favorite_maker") or (entry_price >= 70)
        if is_favorite_maker:
            if current_price >= 25:
                log.info(
                    f"🚀 [FAVORITE MAKER SETTLEMENT RUN] [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Favorite holding at {current_price}¢ (entry: ~{entry_price}¢). Holding into resolution for 100¢ settlement payout!"
                )
                return {"triggered": False, "action": "favorite_maker_holding_to_settle"}

        # Endgame Loss Check: If price is below endgame floor (e.g. < 25¢) or losing on entry/rebuy (loss_cents > 0)
        is_losing = (loss_cents > 0) or (current_price < endgame_floor)
        if is_losing:
            reason = f"losing at endgame mark (Entry: ~{entry_price}¢, Current: {current_price}¢, Floor: {endgame_floor}¢, Loss: -{loss_cents}¢)"
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

            cleared = self.cancel_and_verify_resting_orders(ticker)
            if not cleared:
                log.error(
                    f"❌ [{self.exchange_name}] Cannot execute endgame liquidation for {ticker}: "
                    f"Failed to clear resting orders. Aborting liquidation to avoid locked position errors."
                )
                return {"triggered": False, "error": "resting_orders_not_cleared"}

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

        # Holding Winning Position (in profit into final minutes)
        log.info(
            f"{LogTag.HOLDING_POSITION.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
            f"Position is winning at {current_price}¢ ({pnl_label})! Holding into expiration for 100¢ ($1.00) settlement."
        )
        return {"triggered": False, "action": "holding_winner"}
