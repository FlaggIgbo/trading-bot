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
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from enum import Enum

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Market Closure Time, Expiration & Timeframe Validation Helpers
# ---------------------------------------------------------------------------

def is_5min_market(text: str | None) -> bool:
    """
    Returns True if the given ticker, series, or instrument symbol indicates a 5-minute prediction market.
    Matches '05M', '5M' (not preceded by digit), '05MIN', '5MIN', '5-MIN', '05-MIN', '5_MIN', '05_MIN', '5MINUTE', '05MINUTE'.
    """
    if not text:
        return False
    return bool(re.search(r"(?:05M|(?<![0-9])5M|05MIN|5MIN|05-MIN|5-MIN|5_MIN|05_MIN|5MINUTE|05MINUTE)", str(text), re.IGNORECASE))


def is_15m_market(text: str | None) -> bool:
    """
    Returns True if the given ticker, series, or instrument symbol indicates a 15-minute prediction market.
    Matches '15M', '15MIN', '15-MIN', '15_MIN', '15MINUTE'.
    """
    if not text:
        return False
    return bool(re.search(r"(?:15M|15MIN|15-MIN|15_MIN|15MINUTE)", str(text), re.IGNORECASE))


def parse_market_close_datetime(
    close_time_raw: str | int | float | datetime | None,
    ticker: str | None = None,
) -> datetime | None:
    """
    Parses market closure / expiration time into an aware UTC datetime object.
    Supports ISO 8601 strings, UNIX epoch timestamps (seconds or ms),
    and exchange ticker pattern fallbacks (Kalshi & Gemini).
    """
    if close_time_raw is None and not ticker:
        return None

    if isinstance(close_time_raw, datetime):
        if close_time_raw.tzinfo is None:
            return close_time_raw.replace(tzinfo=timezone.utc)
        return close_time_raw.astimezone(timezone.utc)

    if isinstance(close_time_raw, (int, float)):
        val = float(close_time_raw)
        if val > 1e11:  # Milliseconds
            val /= 1000.0
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except Exception:
            pass

    if isinstance(close_time_raw, str) and close_time_raw.strip():
        s = close_time_raw.strip()
        # Check if numeric string
        try:
            val = float(s)
            if val > 1e11:
                val /= 1000.0
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (ValueError, TypeError):
            pass

        # Try ISO 8601 parsing
        iso_clean = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    # Fallback to parsing expiration from ticker string if available
    tick = str(ticker or close_time_raw or "").upper()

    # Kalshi 15M: e.g. KXBTC15M-26AUG181215-15 (YYMMMDDHHMM)
    m_k15 = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", tick)
    if m_k15:
        yy, mmm, dd, hh, mm = m_k15.groups()
        try:
            return datetime.strptime(f"20{yy}-{mmm}-{dd} {hh}:{mm}:00", "%Y-%b-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    # Kalshi Hourly: e.g. KXBTCD-26AUG1813-T60000 or KXINXU-26AUG18H1000-T7919.9999
    m_kh = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:H)?(\d{2})(?:(\d{2}))?", tick)
    if m_kh:
        yy, mmm, dd, hh, mm = m_kh.groups()
        mm_str = mm or "00"
        try:
            return datetime.strptime(f"20{yy}-{mmm}-{dd} {hh}:{mm_str}:00", "%Y-%b-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    # Gemini 15M / Hourly: e.g. GEMI-BTC15M2608181215-UP or GEMI-BTCH2608181300-UP (YYMMDDHHMM)
    m_gem = re.search(r"(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", tick)
    if m_gem:
        yy, mo, dd, hh, mm = m_gem.groups()
        try:
            return datetime.strptime(f"20{yy}-{mo}-{dd} {hh}:{mm}:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None


def validate_market_closure_time(
    close_time_raw: str | int | float | datetime | None,
    now_utc: datetime,
    timeframe: str = "15m",
    ticker: str | None = None,
    max_drift_seconds: float = 90.0,
) -> tuple[bool, str, float | None]:
    """
    Validates that a market's expiration / closure time precisely aligns with the
    current active cycle window ('15m' or '1h') and checks the time distance to market closure.

    Args:
        close_time_raw: Raw close/expiry timestamp, ISO string, or None.
        now_utc: Current reference UTC datetime.
        timeframe: '15m' or '1h'.
        ticker: Optional market ticker string for fallback date parsing.
        max_drift_seconds: Allowable tolerance in seconds (default 90s) between market close and target boundary.

    Returns:
        tuple[bool, str, float | None]: (is_valid, reason, remaining_minutes)
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    if close_time_raw is None:
        return True, "no_close_time_specified", None

    close_dt = parse_market_close_datetime(close_time_raw, ticker=ticker)
    if close_dt is None:
        return False, "unparseable_close_time", None

    remaining_seconds = (close_dt - now_utc).total_seconds()
    remaining_minutes = remaining_seconds / 60.0

    if remaining_seconds <= 0:
        return False, "market_already_closed", remaining_minutes

    tf_clean = str(timeframe).lower()

    if tf_clean == "15m":
        # 15-minute markets strictly expire on 15-minute boundaries (:00, :15, :30, :45)
        # We also allow second=59 or second=00
        if close_dt.minute % 15 != 0 and (close_dt.minute != 59 or (close_dt.minute + 1) % 15 != 0):
            return False, f"invalid_15m_expiry_boundary (minute :{close_dt.minute:02d})", remaining_minutes

        # Compute exact target expiration for the current active 15m cycle
        cur_minute = now_utc.minute
        next_15m_mark = (cur_minute // 15 + 1) * 15
        target_close_dt = now_utc.replace(second=0, microsecond=0) + timedelta(minutes=(next_15m_mark - cur_minute))
        expected_remaining_seconds = (target_close_dt - now_utc).total_seconds()

        drift = abs((close_dt - target_close_dt).total_seconds())
        if drift > max_drift_seconds:
            return (
                False,
                f"time_distance_mismatch (expected {expected_remaining_seconds/60.0:.1f}m away, got {remaining_minutes:.1f}m away)",
                remaining_minutes,
            )

        if remaining_seconds > (16 * 60):
            return False, f"market_beyond_active_15m_cycle ({remaining_minutes:.1f}m away)", remaining_minutes

        return True, "valid_15m_market", remaining_minutes

    elif tf_clean in ("1h", "hourly", "60m"):
        # Hourly markets strictly expire on the hour (:00 or :59)
        if close_dt.minute not in (0, 59):
            return False, f"invalid_1h_expiry_boundary (minute :{close_dt.minute:02d})", remaining_minutes

        # Compute exact target expiration for the current active 1h cycle (upcoming top of the hour)
        target_close_dt = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        expected_remaining_seconds = (target_close_dt - now_utc).total_seconds()

        drift = abs((close_dt - target_close_dt).total_seconds())
        if drift > max_drift_seconds:
            return (
                False,
                f"time_distance_mismatch (expected {expected_remaining_seconds/60.0:.1f}m away, got {remaining_minutes:.1f}m away)",
                remaining_minutes,
            )

        if remaining_seconds > (65 * 60):
            return False, f"market_beyond_active_hourly_cycle ({remaining_minutes:.1f}m away)", remaining_minutes

        return True, "valid_hourly_market", remaining_minutes

    return True, f"unvalidated_timeframe_{timeframe}", remaining_minutes


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
    CRASH_CHECK = "🚨 [LAST-MINUTE CRASH CHECK]"       # Inspecting :14/:59 last-minute crash / selloff status (1m before expiration)
    CRASH_CUT_LOSS_TRIGGERED = "🚨 [CRASH SELLOFF TRIGGERED]"  # Liquidating crashing/losing position at :14/:59 mark
    DRY_RUN_CRASH_CUT_LOSS = "🚨 [DRY-RUN CRASH SELLOFF]"      # Preview :14/:59 crash liquidation in dry-run mode
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

    def __init__(self, *args, allow_live_orders: bool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if allow_live_orders is not None:
            self.allow_live_orders = allow_live_orders
        else:
            self.allow_live_orders = (
                os.getenv("ENABLE_LIVE_ORDERS", "true").lower() == "true"
                or os.getenv("LIVE_TRADING", "true").lower() == "true"
            )
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
        # Paper Trading / Simulated Active Positions Map: Maps ticker -> position dict
        self._paper_positions: dict[str, dict] = {}

    @property
    def paper_positions(self) -> dict[str, dict]:
        if not hasattr(self, "_paper_positions") or self._paper_positions is None:
            self._paper_positions = {}
        return self._paper_positions

    def record_paper_position(
        self,
        ticker: str,
        side: str,
        count: int,
        entry_price_cents: int,
        target_tp_cents: int | None = None,
        strategy_tag: str | None = None,
        entry_ts: int | None = None,
        expiration_ts: int | None = None,
    ) -> dict:
        """
        Records a simulated active position for paper trading / dry-run execution.
        Enables tracking what we would have bought, following its price lifecycle,
        and evaluating what we would have sold at subsequent checkpoints.
        """
        side_clean = side.lower()
        now_ts = entry_ts or int(datetime.now(timezone.utc).timestamp())
        pos = {
            "ticker": ticker,
            "side": side_clean,
            "count": count,
            "entry_price_cents": entry_price_cents,
            "target_tp_cents": target_tp_cents,
            "strategy_tag": strategy_tag,
            "entry_ts": now_ts,
            "expiration_ts": expiration_ts,
            "is_paper": True,
        }
        self.paper_positions[ticker] = pos
        self.set_entry_price(ticker, entry_price_cents)
        if target_tp_cents is not None:
            self.set_target_tp(ticker, target_tp_cents)
        if strategy_tag is not None:
            self.set_strategy_tag(ticker, strategy_tag)
        self.record_traded_market(ticker, side_clean)
        return pos

    def get_paper_position(self, ticker: str) -> dict | None:
        """Returns the active paper position for ticker, or None."""
        return self.paper_positions.get(ticker)

    def remove_paper_position(self, ticker: str) -> dict | None:
        """Removes and returns active paper position for ticker."""
        return self.paper_positions.pop(ticker, None)

    def get_all_paper_positions(self) -> dict[str, dict]:
        """Returns a copy of all active paper positions."""
        return dict(self.paper_positions)

    def clear_expired_paper_positions(self, now_utc: datetime | None = None) -> int:
        """Prunes paper positions whose expiration time has passed."""
        now = now_utc or datetime.now(timezone.utc)
        now_ts = int(now.timestamp())
        expired = []
        for ticker, pos in list(self.paper_positions.items()):
            exp_ts = pos.get("expiration_ts")
            if exp_ts and now_ts >= exp_ts:
                expired.append(ticker)
        for ticker in expired:
            self.remove_paper_position(ticker)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            self.clear_strategy_tag(ticker)
        return len(expired)

    def check_simulated_take_profit_fill(
        self,
        ticker: str,
        current_yes_bid: int | None,
        current_no_bid: int | None,
        current_yes_ask: int | None = None,
        current_no_ask: int | None = None,
    ) -> dict | None:
        """
        Checks if an active paper position's resting take-profit limit order would have filled.
        For YES: If current_yes_bid >= target_tp or current_yes_ask >= target_tp.
        For NO: If current_no_bid >= target_tp or current_no_ask >= target_tp.
        """
        pos = self.get_paper_position(ticker)
        if pos is None:
            return None

        side = pos["side"]
        entry_price = pos.get("entry_price_cents", 50)
        target_tp = pos.get("target_tp_cents") or self.get_target_tp(ticker)
        if target_tp is None:
            return None

        bid_p = current_yes_bid if side == "yes" else current_no_bid
        ask_p = current_yes_ask if side == "yes" else current_no_ask
        market_p = ask_p or bid_p

        if market_p is not None and market_p >= target_tp:
            profit_cents = target_tp - entry_price
            log.info(
                f"💰 [DRY-RUN TAKE-PROFIT FILLED] [{self.exchange_name}] {ticker} ({side.upper()}): "
                f"Market price reached {market_p}¢ (>= {target_tp}¢ TP target). "
                f"WOULD SELL AT {target_tp}¢ (+{profit_cents}¢ PROFIT BANKED IN CASH)."
            )
            self.remove_paper_position(ticker)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            return {
                "triggered": True,
                "side": side,
                "action": "take_profit_sold",
                "exit_price": target_tp,
                "profit_locked_cents": profit_cents,
            }

        return None

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
            from database import get_active_position_doc
            ex_name = getattr(self, "exchange_name", "kalshi")
            db_pos = get_active_position_doc(ticker, str(ex_name).lower())
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
        spot_price_at_entry: float | None = None,
        target_strike_price: float | None = None,
        strike_distance_diff: float | None = None,
        strike_distance_pct: float | None = None,
        minute_into_candle: int | None = None,
        seconds_remaining: float | None = None,
        spread_cents_at_entry: int | None = None,
        sym: str | None = None,
        tf: str | None = None,
        regime: str | None = None,
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
                tf=tf,
                client_order_id=client_order_id,
                tp_order_id=tp_order_id,
                target_tp_cents=take_profit_cents,
                strategy_tag=strategy_tag,
                spread_saved_cents=spread_saved_cents,
                order_role=order_role,
                spot_price_at_entry=spot_price_at_entry,
                target_strike_price=target_strike_price,
                strike_distance_diff=strike_distance_diff,
                strike_distance_pct=strike_distance_pct,
                minute_into_candle=minute_into_candle,
                seconds_remaining=seconds_remaining,
                spread_cents_at_entry=spread_cents_at_entry,
                sym=sym,
                regime=regime,
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

    def get_all_active_positions(self, force_refresh: bool = False) -> dict[str, dict]:
        """
        Fetches/returns all active open positions in the portfolio: {ticker: position_dict}.
        Subclasses (KalshiClient, GeminiClient) implement optimized batch caching.
        """
        if hasattr(self, "_cached_positions_map") and self._cached_positions_map:
            return self._cached_positions_map
        if hasattr(self, "_positions_cache"):
            return self._positions_cache
        if hasattr(self, "_positions"):
            return self._positions
        return {}

    def get_market(self, ticker: str) -> dict:
        """Fetches detailed market metadata for a single ticker."""
        return {"market": {"ticker": ticker}}

    @abstractmethod
    def get_market_position(self, ticker: str) -> dict | None:
        """Retrieves current open position for ticker: {"side": "yes"/"no", "count": int, "entry_price_cents": int} or None."""
        pass

    @abstractmethod
    def cancel_and_verify_resting_orders(self, ticker: str) -> bool:
        """Cancels resting orders for ticker and verifies that 0 resting orders remain."""
        pass

    def cancel_all_stale_resting_buy_orders(self, max_age_seconds: int = 45) -> int:
        """
        Sweeps all resting orders across the portfolio and cancels any stale unfilled BUY orders.
        Preserves resting Take-Profit SELL orders.
        """
        return 0

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
        # Dipped favorites recover 70%+ of the time, so we allow reasonable fluctuations.
        # However, if a favorite severely breaks down (bid_price < 35¢ or loss > 40¢),
        # we trigger a hard stop-loss to prevent a total 100% loss at 0¢ settlement.
        is_favorite_maker = (self.get_strategy_tag(ticker) == "endgame_favorite_maker") or (entry_price >= 70)
        if is_favorite_maker:
            if bid_price >= 35 and loss_cents <= 40:
                log.info(
                    f"🛡️ [FAVORITE MAKER HOLD] [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Holding viable favorite position into settlement (entry: {entry_price}¢, bid: {bid_price}¢). "
                    f"Bypassing intermediate noise to capture full 100¢ payout."
                )
                return {"triggered": False, "action": "favorite_maker_holding"}
            else:
                log.warning(
                    f"⚠️ [FAVORITE HARD STOP] [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Favorite entered at {entry_price}¢ has severely collapsed to {bid_price}¢ (loss: -{loss_cents}¢). "
                    f"Executing hard stop-loss liquidation to salvage capital!"
                )

        # 2. Winning Position Profit Bank: If position reached >= 70¢ or profit >= 15¢
        # Sell at Bid to bank realized profit in cash. Keep the cash in balance (DO NOT re-buy at 85¢-98¢ into negative EV).
        if bid_price >= win_threshold_cents or profit_cents >= 15:
            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_WINNER_ROLLED.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"WOULD SELL AT {bid_price}¢ (BANK +{profit_cents}¢ PROFIT IN CASH). Keeping profit safely in balance."
                )
                self.remove_paper_position(ticker)
                self.clear_profit_floor(ticker)
                self.clear_entry_price(ticker)
                self.clear_strategy_tag(ticker)
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
                self.remove_paper_position(ticker)
                self.clear_profit_floor(ticker)
                self.clear_entry_price(ticker)
                self.clear_strategy_tag(ticker)
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

    def evaluate_and_execute_10min_stop_loss(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        dry_run: bool = False,
        sl_threshold: int = 25,
        sl_floor: int = 15,
        winner_take_profit_cents: int = 82,
    ) -> dict:
        """Alias for 10-minute stop-loss strategy evaluation."""
        return self.evaluate_and_execute_10min_strategy(
            ticker=ticker,
            current_yes_bid=current_yes_price,
            current_no_bid=current_no_price,
            dry_run=dry_run,
            stop_loss_threshold_cents=sl_threshold,
            price_floor=sl_floor,
            win_threshold_cents=winner_take_profit_cents,
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
            if current_price >= 35 and loss_cents <= 40:
                log.info(
                    f"🛡️ [FAVORITE MAKER HOLD] [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Favorite maker holding (entry: {entry_price}¢, price: {current_price}¢). "
                    f"Bypassing early cut-loss to preserve positive EV into expiration."
                )
                return {"triggered": False, "action": "favorite_maker_holding"}
            else:
                log.warning(
                    f"⚠️ [FAVORITE HARD STOP] [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Favorite entered at {entry_price}¢ has broken down to {current_price}¢ (loss: -{loss_cents}¢). "
                    f"Cutting collapsed position early!"
                )

        if current_price < min_price_threshold:
            reason = f"Current price {current_price}¢ is below safety cut threshold ({min_price_threshold}¢)"

            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_EARLY_CUT_LOSS.value} [{self.exchange_name}] {ticker} ({side.upper()}): {reason}. "
                    f"WOULD CANCEL RESTING ORDERS & SELL POSITION IMMEDIATELY TO CUT LOSSES."
                )
                self.remove_paper_position(ticker)
                self.clear_profit_floor(ticker)
                self.clear_entry_price(ticker)
                self.clear_strategy_tag(ticker)
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
                if not dry_run:
                    self.cancel_and_verify_resting_orders(ticker)
                log.info(
                    f"🚀 [FAVORITE MAKER SETTLEMENT RUN] [{self.exchange_name}] {ticker} ({side.upper()}): "
                    f"Favorite holding at {current_price}¢ (entry: ~{entry_price}¢). Cleared resting TP orders; holding into resolution for 100¢ settlement payout!"
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
                self.remove_paper_position(ticker)
                self.clear_profit_floor(ticker)
                self.clear_entry_price(ticker)
                self.clear_strategy_tag(ticker)
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

    def evaluate_and_execute_crash_detection(
        self,
        ticker: str,
        current_yes_price: int | None,
        current_no_price: int | None,
        dry_run: bool = False,
        crash_floor_cents: int = 35,
        action_name: str = "crash_selloff_cut",
    ) -> dict:
        """
        Evaluates active positions at the final 1-minute mark (:14 for 15-minute markets, :59 for hourly markets).
        Protects against last-minute market dumps, flash crashes, or adverse price reversals that risk a full $1.00 wipeout.

        Rules:
        - If severely broken down below crash floor (< crash_floor_cents e.g. < 35¢) AND deeply losing (loss_cents >= 20),
          verifies resting order cancellation and immediately liquidates the position at market Bid to salvage capital.
        - If securely winning or in minor normal fluctuation (current_price >= crash_floor_cents), holds into expiration for 100¢ settlement.
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
            log.warning(f"⚠️ Cannot evaluate crash detection / selloff check for {ticker}: current {side.upper()} price is unavailable.")
            return {"triggered": False}

        loss_cents = entry_price - current_price
        profit_cents = current_price - entry_price
        pnl_label = f"Gain: +{profit_cents}¢ (In Profit 🟢)" if profit_cents > 0 else f"Loss: -{loss_cents}¢ (In Loss 🔴)" if loss_cents > 0 else "Breakeven: 0¢ (Flat ⚪)"

        log.info(
            f"{LogTag.CRASH_CHECK.value} [{self.exchange_name}] Market: {ticker} | Side: {side.upper()} | "
            f"Entry: ~{entry_price}¢ | Current: {current_price}¢ | Floor: {crash_floor_cents}¢ | Status: {pnl_label}"
        )

        # Crash & Selloff check:
        # 1. Severe Breakdown: Deep loss (loss_cents >= 20) AND dropped below crash floor (< crash_floor_cents)
        is_severe_crash = (current_price < crash_floor_cents) and (loss_cents >= 20)
        # 2. Speculative Profit Lock: Low-price entry with profit (profit_cents >= 10) but still under crash floor
        is_speculative_profit_lock = (entry_price < crash_floor_cents) and (profit_cents >= 10) and (current_price < crash_floor_cents)

        is_crash = is_severe_crash or is_speculative_profit_lock
        if is_crash:
            reason = (
                f"last-minute crash/selloff detected at 1m mark "
                f"(Entry: ~{entry_price}¢, Current: {current_price}¢, Floor: {crash_floor_cents}¢, PnL: {pnl_label})"
            )
            if dry_run:
                log.info(
                    f"{LogTag.DRY_RUN_CRASH_CUT_LOSS.value} [{self.exchange_name}] {ticker} ({side.upper()}): {reason}. "
                    f"WOULD LIQUIDATE IMMEDIATELY TO SALVAGE CAPITAL BEFORE EXPIRATION."
                )
                self.remove_paper_position(ticker)
                self.clear_profit_floor(ticker)
                self.clear_entry_price(ticker)
                self.clear_strategy_tag(ticker)
                res = {
                    "triggered": True,
                    "side": side,
                    "action": action_name,
                    "loss_cents": loss_cents,
                    "exit_price": current_price,
                }
                if profit_cents > 0:
                    res["profit_locked_cents"] = profit_cents
                return res

            cleared = self.cancel_and_verify_resting_orders(ticker)
            if not cleared:
                log.error(
                    f"❌ [{self.exchange_name}] Cannot execute crash liquidation for {ticker}: "
                    f"Failed to clear resting orders. Aborting liquidation to avoid locked position errors."
                )
                return {"triggered": False, "error": "resting_orders_not_cleared"}

            self.execute_liquidation(ticker, side, position_count, current_price)
            self.clear_profit_floor(ticker)
            self.clear_entry_price(ticker)
            log.info(
                f"{LogTag.CRASH_CUT_LOSS_TRIGGERED.value} [{self.exchange_name}] Liquidated crashing/losing position {ticker} "
                f"({side.upper()}) at {current_price}¢ to prevent full loss at expiration ({reason})."
            )
            res = {
                "triggered": True,
                "side": side,
                "action": action_name,
                "loss_cents": loss_cents,
                "exit_price": current_price,
            }
            if profit_cents > 0:
                res["profit_locked_cents"] = profit_cents
            return res

        # Holding Winning Position (in profit into final 60 seconds)
        if not dry_run and current_price >= 75:
            # Clear resting TP orders so high-probability winner resolves at 100¢ settlement
            self.cancel_and_verify_resting_orders(ticker)
        log.info(
            f"{LogTag.HOLDING_POSITION.value} [{self.exchange_name}] {ticker} ({side.upper()}): "
            f"Position is securely winning at {current_price}¢ (>= {crash_floor_cents}¢ floor, {pnl_label})! "
            f"Holding into expiration for 100¢ ($1.00) settlement."
        )
        return {"triggered": False, "action": "holding_winner"}
