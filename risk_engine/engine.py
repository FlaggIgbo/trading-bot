"""
Risk Engine & Trade Engine
==========================
Unified orchestrator for evaluating markets, managing exits, enforcing risk rules,
and executing orders.

Architecture:
- Strategy dataclass: Every strategy is just a signal function plus parameters.
- Pure Signals: trend_signal, regime_signal, cheapest_side, random_signal.
- Single home for risk checks: reject(market, strategy, ctx).
- Exit management: manage_exits(strategy, ctx) handles take-profit and stop-loss.
- Execution: broker.buy() owns write-ahead intent (PENDING), polling, and transition to OPEN.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import random
import re
import sys
import time
from typing import Any, Callable, Optional, Protocol, Union

from db import (
    Database,
    MarketResolution,
    MarketResult,
    MarketStatus,
    PositionSide,
    PositionStatus,
    Strategy as DBStrategy,
    normalize_strategy,
)
from ingestion.api.client import is_binary_market
from logging.analytics import analytics
from risk_engine.broker import Broker, KalshiBroker, SimulatedBroker
from risk_engine.decision import Decision
from risk_engine.reconciler import Reconciler
from risk_engine.rules import (
    DEFAULT_RISK_RULES,
    RiskContext,
    RiskRejectReason,
    evaluate_risk_rules,
    is_last_n_strategy,
    reject,
)


class StrategyProtocol(Protocol):
    """Protocol for pure strategy functions: recent history -> Side.YES, Side.NO, or None."""
    def __call__(
        self,
        past_results: Optional[list[str]] = None,
        market: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[PositionSide]:
        ...


@dataclass(frozen=True)
class Strategy:
    """A strategy is just a pure signal function plus parameters."""
    name: str
    signal: Callable[..., Optional[PositionSide]]  # trend / fade / cheapest-side / random
    entry_window: tuple[float, float] = (0.0, 10.0)   # minutes into the market
    price_band: tuple[int, int] = (1, 99)             # min/max ask in cents
    take_profit: Union[int, float, None] = None       # int = absolute cents, float = multiple of entry
    stop_loss: bool = False
    check_spot: bool = False
    regime_filter: bool = False
    taker_ioc: bool = False
    entry_timeout: Optional[float] = None


@dataclass
class Config:
    """Configuration injected into TradeEngine."""
    env: str = "demo"
    max_open_positions: int = 10
    daily_loss_cap_cents: int = 5000  # $50 daily loss cap
    kill_switch_path: str = "KILL_SWITCH"
    shadow_mode: bool = False
    stop_loss: bool = False
    check_spot: bool = False
    min_entry_price: Optional[int] = None
    max_entry_price: Optional[int] = None
    default_contract_price: int = 50
    live_orders: bool = False
    take_profit: Optional[Union[int, float]] = None
    regime_filter: bool = False
    taker_ioc: bool = False
    entry_timeout: Optional[float] = None


# ---------------------------------------------------------------------------
# Helpers & Price Extraction
# ---------------------------------------------------------------------------

def extract_contract_price(market: Optional[dict], side: PositionSide, book_side: str = "ask") -> Optional[int]:
    """Extract ask or bid price in cents (1-99) for the given contract side."""
    if not isinstance(market, dict):
        return None
    side_str = "yes" if (side == PositionSide.YES or str(side).upper() == "YES") else "no"
    cents = None

    if book_side == "bid":
        cents = market.get(f"{side_str}_bid")
        if cents is None and market.get(f"{side_str}_bid_dollars") is not None:
            cents = int(round(float(market[f"{side_str}_bid_dollars"]) * 100))
    if cents is None:
        cents = market.get(f"{side_str}_ask")
        if cents is None and market.get(f"{side_str}_ask_dollars") is not None:
            cents = int(round(float(market[f"{side_str}_ask_dollars"]) * 100))
    if cents is None and market.get(f"{side_str}_price") is not None:
        cents = market.get(f"{side_str}_price")

    if cents is not None:
        try:
            val = int(cents)
            if 0 < val < 100:
                return val
        except (ValueError, TypeError):
            pass
    return None


extract_price = extract_contract_price


def in_band(price: Optional[int], band: tuple[int, int]) -> bool:
    """Return True if price falls within [min, max] band."""
    if price is None:
        return False
    return band[0] <= price <= band[1]


def _parse_time(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str):
        cleaned = val.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def get_market_minute(market_or_pos: dict, now: Any = None) -> Optional[float]:
    """Calculate elapsed minutes into the 15-minute market."""
    if not isinstance(market_or_pos, dict):
        return None
    if "minutes_past" in market_or_pos:
        return float(market_or_pos["minutes_past"])
    if "minute" in market_or_pos:
        return float(market_or_pos["minute"])

    now_dt = _parse_time(now) or datetime.now(timezone.utc)
    if market_or_pos.get("open_time"):
        open_dt = _parse_time(market_or_pos["open_time"])
        if open_dt:
            diff = (now_dt - open_dt).total_seconds() / 60.0
            if now is not None or (0 <= diff <= 30.0):
                return diff
    if market_or_pos.get("close_time"):
        close_dt = _parse_time(market_or_pos["close_time"])
        if close_dt:
            start_dt = close_dt - timedelta(minutes=15)
            diff = (now_dt - start_dt).total_seconds() / 60.0
            if now is not None or (0 <= diff <= 30.0):
                return diff
    if now is not None:
        return float(now_dt.minute % 15) + (now_dt.second / 60.0)
    return None


def calculate_weighted_trend(
    past_results: Any = None,
    limit: int = 21,
) -> tuple[Optional[PositionSide], float, float]:
    """Calculate magnitude-weighted trend from recent market resolutions."""
    if not past_results:
        return None, 0.0, 0.0

    recent = list(past_results)[:limit]
    up_weight = 0.0
    down_weight = 0.0

    for item in recent:
        clean_result = None
        weight = 1.0

        if isinstance(item, MarketResolution):
            clean_result = item.result
            weight = getattr(item, "weight", 1.0)
        elif isinstance(item, dict):
            clean_result = str(item.get("result", "")).strip().lower()
            if "weight" in item and item["weight"] is not None:
                try:
                    weight = float(item["weight"])
                except (ValueError, TypeError):
                    weight = 1.0
            else:
                fs = item.get("floor_strike")
                ev = item.get("expiration_value")
                if fs and ev:
                    try:
                        fs_f = float(fs)
                        ev_f = float(ev)
                        if fs_f > 0:
                            weight = abs(ev_f - fs_f) / fs_f * 100.0
                    except (ValueError, TypeError):
                        weight = 1.0
        else:
            val = getattr(item, "value", item)
            clean_result = str(val).strip().lower()

        if str(getattr(clean_result, "value", clean_result)).strip().lower() == MarketResult.YES.value:
            up_weight += weight
        elif str(getattr(clean_result, "value", clean_result)).strip().lower() == MarketResult.NO.value:
            down_weight += weight

    if up_weight > down_weight:
        candidate_side = PositionSide.YES
    elif down_weight > up_weight:
        candidate_side = PositionSide.NO
    else:
        candidate_side = PositionSide.NO

    return candidate_side, up_weight, down_weight


# ---------------------------------------------------------------------------
# Pure Signal Functions
# ---------------------------------------------------------------------------

def trend_signal(
    past_results: Any = None,
    market: Optional[dict] = None,
    check_spot: bool = False,
    spot_client: Optional[Any] = None,
    require_spot_agreement: bool = True,
    min_samples: int = 0,
    limit: int = 21,
    **kwargs: Any,
) -> Optional[PositionSide]:
    """Magnitude-weighted trend signal with optional spot price trend alignment."""
    if not past_results:
        return None if min_samples > 0 else PositionSide.NO

    valid = [
        r for r in past_results
        if str(getattr(r, "value", getattr(r, "result", r))).strip().lower() in ("yes", "no")
    ]
    if min_samples > 0 and len(valid) < min_samples:
        return None

    hist_side, _, _ = calculate_weighted_trend(valid, limit=limit)
    if hist_side is None:
        hist_side = PositionSide.NO if min_samples == 0 else None
        if hist_side is None:
            return None

    if check_spot and market:
        if spot_client is None:
            from ingestion.spot import SpotClient
            spot_client = SpotClient()
        spot_side, spot_val, strike_val = spot_client.get_spot_trend(market)
        if spot_side is not None:
            if require_spot_agreement and hist_side != spot_side:
                return None
            return spot_side

    return hist_side


def regime_signal(
    past_results: Any = None,
    market: Optional[dict] = None,
    check_spot: bool = False,
    spot_client: Optional[Any] = None,
    require_spot_agreement: bool = False,
    min_volatility: float = 0.03,
    min_edge_ratio: float = 0.20,
    max_trend_flip_ratio: float = 0.40,
    **kwargs: Any,
) -> Optional[PositionSide]:
    """Regime-aware signal fading streaks and gating chopped volatility."""
    from risk_engine.regime import detect_market_regime

    regime_res = detect_market_regime(
        past_results=past_results,
        min_volatility=min_volatility,
        min_edge_ratio=min_edge_ratio,
        max_trend_flip_ratio=max_trend_flip_ratio,
    )
    if not regime_res.should_trade:
        return None

    candidate_side = regime_res.recommended_side
    if check_spot and market and candidate_side:
        if spot_client is None:
            from ingestion.spot import SpotClient
            spot_client = SpotClient()
        spot_side, spot_val, strike_val = spot_client.get_spot_trend(market)
        if spot_side is not None and require_spot_agreement and spot_side != candidate_side:
            return None

    return candidate_side


def cheapest_side(
    past_results: Any = None,
    market: Optional[dict] = None,
    max_entry_price: Optional[int] = None,
    **kwargs: Any,
) -> Optional[PositionSide]:
    """Select whichever side is cheaper and under max_entry_price."""
    target_market = market
    if isinstance(past_results, dict) and target_market is None:
        target_market = past_results
    if not target_market:
        return None

    yes_p = extract_contract_price(target_market, PositionSide.YES)
    no_p = extract_contract_price(target_market, PositionSide.NO)
    max_p = max_entry_price or 100

    yes_valid = yes_p is not None and 0 < yes_p < max_p
    no_valid = no_p is not None and 0 < no_p < max_p

    if yes_valid and no_valid:
        return PositionSide.YES if yes_p <= no_p else PositionSide.NO
    if yes_valid:
        return PositionSide.YES
    if no_valid:
        return PositionSide.NO
    return None


cheapest_side_signal = cheapest_side


def random_signal(past_results: Any = None, market: Optional[dict] = None, **kwargs: Any) -> PositionSide:
    """Baseline strategy: randomly pick YES or NO."""
    return random.choice([PositionSide.YES, PositionSide.NO])


strategy_random = random_signal
choose_side_random = random_signal


# ---------------------------------------------------------------------------
# Strategy Registry
# ---------------------------------------------------------------------------

STRATEGIES: dict[str, Strategy] = {
    # Core simplified shapes
    "trend": Strategy("trend", trend_signal, price_band=(20, 65)),
    "fade": Strategy("fade", regime_signal, price_band=(20, 65), taker_ioc=True),
    "cheap2x": Strategy("cheap2x", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=2.0),
    "cheap1.5x": Strategy("cheap1.5x", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "random": Strategy("random", random_signal, price_band=(1, 99)),

    # Validated strategies & aliases
    "last_21": Strategy("last_21", trend_signal, price_band=(1, 99)),
    "last_21_spot": Strategy("last_21_spot", trend_signal, price_band=(20, 65), check_spot=True),
    "last_9": Strategy("last_9", trend_signal, price_band=(1, 99)),
    "last_9_spot": Strategy("last_9_spot", trend_signal, price_band=(20, 65), check_spot=True),
    "regime_aware": Strategy("regime_aware", regime_signal, price_band=(20, 65), taker_ioc=True),
    "regime_spot": Strategy("regime_spot", regime_signal, price_band=(20, 65), check_spot=True, taker_ioc=True),
    "regime": Strategy("regime", regime_signal, price_band=(20, 65), taker_ioc=True),
    "sell_3n": Strategy("sell_3n", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 33), take_profit=3.0),
    "sell_2n": Strategy("sell_2n", cheapest_side, entry_window=(10.0, 15.0), price_band=(1, 50), take_profit=2.0),
    "sell_2n_5m": Strategy("sell_2n_5m", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 50), take_profit=2.0),
    "sell_2n_5m_35p": Strategy("sell_2n_5m_35p", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=2.0),
    "sell_2_n_5m_35p": Strategy("sell_2n_5m_35p", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=2.0),
    "sell_1.5n": Strategy("sell_1.5n", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 66), take_profit=1.5),
    "sell_1_5n": Strategy("sell_1.5n", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 66), take_profit=1.5),
    "sell_1.5n_5m": Strategy("sell_1.5n_5m", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "sell_1_5n_5m": Strategy("sell_1.5n_5m", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "sell_1.5n_5m_35p": Strategy("sell_1.5n_5m_35p", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "sell_1_5n_5m_35p": Strategy("sell_1.5n_5m_35p", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "sell_1.5n_35p": Strategy("sell_1.5n_35p", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "sell_1_5n_35p": Strategy("sell_1.5n_35p", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 35), take_profit=1.5),
    "sell_2.5n": Strategy("sell_2.5n", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 40), take_profit=2.5),
    "sell_2_5n": Strategy("sell_2.5n", cheapest_side, entry_window=(5.0, 10.0), price_band=(1, 40), take_profit=2.5),
}


def get_strategy(spec: Any, **overrides: Any) -> Strategy:
    """Resolve a strategy by name, Strategy object, or callable, applying runtime overrides."""
    base: Optional[Strategy] = None

    if isinstance(spec, Strategy):
        base = spec
    elif isinstance(spec, str):
        cleaned = spec.strip().lower()
        if ("_sl" in cleaned or "stop_loss" in cleaned or "stoploss" in cleaned) and "stop_loss" not in overrides:
            overrides["stop_loss"] = True
        m_tp = re.search(r"tp_?(\d+)", cleaned)
        if m_tp and "take_profit" not in overrides:
            overrides["take_profit"] = int(m_tp.group(1))

        base = STRATEGIES.get(cleaned)
        if not base:
            for k in sorted(STRATEGIES.keys(), key=len, reverse=True):
                if cleaned.startswith(k):
                    base = STRATEGIES[k]
                    break
    elif callable(spec):
        name = getattr(spec, "__name__", "custom")
        entry_win = (getattr(spec, "y", 0.0), getattr(spec, "y", 0.0) + 5.0) if hasattr(spec, "y") else (0.0, 10.0)
        p_band = (1, int(getattr(spec, "z", 99)))
        tp = getattr(spec, "n", getattr(spec, "exit_multiplier", None))
        sl = bool(getattr(spec, "stop_loss", False))
        base = Strategy(name=name, signal=spec, entry_window=entry_win, price_band=p_band, take_profit=tp, stop_loss=sl)
    elif hasattr(spec, "value"):
        val_str = str(spec.value).lower()
        base = STRATEGIES.get(val_str)

    if base is None:
        base = STRATEGIES["trend"]

    filtered_overrides = {k: v for k, v in overrides.items() if v is not None and hasattr(base, k)}
    if filtered_overrides:
        from dataclasses import replace
        return replace(base, **filtered_overrides)
    return base


# ---------------------------------------------------------------------------
# Exit Management (Take-Profit & Stop-Loss)
# ---------------------------------------------------------------------------

def should_stop_loss(
    entry_price_cents: Optional[int] = None,
    current_price_cents: Optional[int] = None,
    minute: Optional[float] = None,
    stop_minute: Optional[float] = None,
    stop_price_threshold: int = 25,
    stop_ratio_threshold: float = 0.5,
    strategy: Any = None,
    **kwargs: Any,
) -> bool:
    """Check if an open position triggers the stop loss rule."""
    entry = kwargs.get("entry_price", entry_price_cents)
    current = kwargs.get("current_price", current_price_cents)
    strat = kwargs.get("strategy", strategy)

    if entry is None or current is None or entry <= 0:
        return False

    if stop_minute is None:
        strat_str = str(getattr(strat, "value", getattr(strat, "name", strat)) or "").lower().strip()
        if strat_str in ("random", "last_9", "last_21", "trend") or "random" in strat_str or "last" in strat_str:
            stop_minute = 5.0
        else:
            stop_minute = 10.0

    if minute is None or minute < stop_minute:
        return False
    if current <= stop_price_threshold:
        return True
    if current < (entry * stop_ratio_threshold):
        return True
    return False


def sell_position(
    position: dict[str, Any],
    current_price_cents: int,
    reason: str = "take_profit",
    ctx: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """Liquidate an open position via IOC order and settle it in SQLite if filled."""
    db = getattr(ctx, "db", getattr(ctx, "repo", None))
    broker = getattr(ctx, "broker", None)
    client = getattr(ctx, "client", getattr(broker, "client", None))
    ticker = position["ticker"]
    side = position["side"]
    count = int(position.get("count", 1))
    entry_price = position["price_cents"]
    resting_order_id = position.get("resting_order_id")

    # Cancel resting order if present to unlock contracts
    if resting_order_id:
        try:
            if broker:
                broker.cancel_order(resting_order_id)
            elif client and hasattr(client, "cancel_order"):
                client.cancel_order(resting_order_id)
        except Exception:
            pass

    client_sell_id = f"sell_{ticker}_{int(time.time() * 1000)}"[:64]
    order_result = None
    try:
        if broker:
            order_result = broker.place_order(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=current_price_cents,
                client_order_id=client_sell_id,
                action="sell",
                time_in_force="immediate_or_cancel",
                reduce_only=True,
            )
        elif client and hasattr(client, "place_order"):
            order_result = client.place_order(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=current_price_cents,
                client_order_id=client_sell_id,
                action="sell",
                time_in_force="immediate_or_cancel",
                reduce_only=True,
            )
        else:
            order_result = {"status": "executed"}
    except Exception as e:
        analytics.warning(f"Failed to submit sell order for {ticker}: {e}")
        return None

    if not order_result or (isinstance(order_result, dict) and "error" in order_result):
        return None

    order_data = order_result.get("order") if isinstance(order_result, dict) and "order" in order_result else order_result
    status = str(order_data.get("status") if isinstance(order_data, dict) else "executed").lower()
    fill_count = 0.0
    if isinstance(order_data, dict):
        try:
            fill_count = float(order_data.get("fill_count_fp", 1.0))
        except (ValueError, TypeError):
            fill_count = 1.0

    if status == "canceled" or fill_count == 0.0:
        return None

    pnl_cents = (current_price_cents - entry_price) * count
    if db is not None:
        db.settle_position(client_order_id=position["client_order_id"], pnl_cents=pnl_cents)

    return {
        "client_order_id": position["client_order_id"],
        "ticker": ticker,
        "side": side,
        "entry_price": entry_price,
        "sell_price": current_price_cents,
        "pnl_cents": pnl_cents,
        "status": PositionStatus.SETTLED,
        "reason": reason,
        "order": order_result,
    }


def manage_exits(
    strategy: Any,
    ctx: Any,
    markets: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Inspect all open positions; exit via take-profit or stop-loss if conditions are met."""
    db = getattr(ctx, "db", getattr(ctx, "repo", None))
    if db is None:
        return []
    broker = getattr(ctx, "broker", None)
    client = getattr(ctx, "client", getattr(broker, "client", None))
    is_live = getattr(broker, "is_live", getattr(client, "live_orders", False))

    strat_obj = get_strategy(strategy)
    strat_name = getattr(strategy, "value", getattr(strat_obj, "name", str(strategy)))

    open_positions = [
        p for p in db.get_all_positions()
        if p.get("status") == PositionStatus.OPEN and (
            not strat_name or p.get("strategy") == strat_name or getattr(p.get("strategy"), "value", None) == strat_name
        )
    ]

    market_map = {m.get("ticker"): m for m in markets} if markets else {}
    sold_positions = []

    for pos in open_positions:
        ticker = pos["ticker"]
        resting_order_id = pos.get("resting_order_id")
        entry_price = pos["price_cents"]

        # Calculate target sell price
        tp = strat_obj.take_profit
        if tp is not None:
            target_sell_price = min(99, int(round(tp * entry_price))) if isinstance(tp, float) else min(99, int(tp))
        else:
            target_sell_price = None

        # 1. Live reconciliation: Check if resting limit order filled on Kalshi
        if is_live and resting_order_id:
            order_info = broker.get_order(resting_order_id) if broker else (
                client.get_order(resting_order_id) if hasattr(client, "get_order") else None
            )
            if order_info and isinstance(order_info, dict):
                status = str(order_info.get("status", "")).lower()
                if status == "executed":
                    sell_px = target_sell_price or 85
                    pnl_cents = (sell_px - entry_price) * int(pos.get("count", 1))
                    db.settle_position(client_order_id=pos["client_order_id"], pnl_cents=pnl_cents)
                    sold_positions.append({
                        "client_order_id": pos["client_order_id"],
                        "ticker": ticker,
                        "side": pos["side"],
                        "sell_price": sell_px,
                        "pnl_cents": pnl_cents,
                        "reason": "take_profit",
                    })
                    continue
                elif status == "canceled":
                    pos["resting_order_id"] = None
                    resting_order_id = None
                    db.update_position_status(pos["client_order_id"], PositionStatus.OPEN, resting_order_id="")

        market = market_map.get(ticker)
        if market is None and hasattr(client, "get_market") and client:
            try:
                market = client.get_market(ticker)
            except Exception:
                pass
        if not market:
            continue

        current_bid = extract_contract_price(market, pos["side"], book_side="bid")
        if current_bid is None:
            current_bid = extract_contract_price(market, pos["side"], book_side="ask")

        minute = get_market_minute(market, now=now)
        if minute is None and pos.get("open_time"):
            minute = get_market_minute(pos, now=now)

        # 2. Safety net: Place missing resting order or immediate exit if bid reached
        if target_sell_price is not None and not pos.get("resting_order_id"):
            if current_bid is not None and current_bid >= target_sell_price:
                sold_info = sell_position(pos, current_bid, reason="take_profit", ctx=ctx)
                if sold_info:
                    sold_positions.append(sold_info)
                    continue
            else:
                side_val = pos["side"]
                safe_ticker = re.sub(r"[^a-zA-Z0-9_-]", "_", ticker)
                rest_client_id = f"rest_{safe_ticker}_{int(time.time() * 1000)}"[:64]
                try:
                    if broker:
                        rest_res = broker.place_order(
                            ticker=ticker,
                            side=side_val,
                            count=int(pos.get("count", 1)),
                            price_cents=target_sell_price,
                            client_order_id=rest_client_id,
                            action="sell",
                            time_in_force="good_till_canceled",
                        )
                    elif client and hasattr(client, "place_order"):
                        rest_res = client.place_order(
                            ticker=ticker,
                            side=side_val,
                            count=int(pos.get("count", 1)),
                            price_cents=target_sell_price,
                            client_order_id=rest_client_id,
                            action="sell",
                            time_in_force="good_till_canceled",
                        )
                    else:
                        rest_res = {"order_id": rest_client_id, "status": "resting"}

                    if rest_res and isinstance(rest_res, dict) and "error" not in rest_res:
                        actual_id = (
                            rest_res.get("order_id")
                            or (rest_res.get("order", {}).get("order_id") if isinstance(rest_res.get("order"), dict) else None)
                            or rest_client_id
                        )
                        pos["resting_order_id"] = actual_id
                        db.update_position_status(pos["client_order_id"], PositionStatus.OPEN, resting_order_id=actual_id)
                except Exception as e:
                    analytics.warning(f"Could not place resting order for {ticker}: {e}")

        # 3. Take-profit evaluation (simulated / instant exit)
        if not is_live and target_sell_price is not None and current_bid is not None and current_bid >= target_sell_price:
            sold_info = sell_position(pos, target_sell_price, reason="take_profit", ctx=ctx)
            if sold_info:
                sold_positions.append(sold_info)
                continue

        # 4. Stop-loss evaluation
        if strat_obj.stop_loss and current_bid is not None:
            if should_stop_loss(entry_price, current_bid, minute=minute, strategy=strat_name):
                sold_info = sell_position(pos, current_bid, reason="stop_loss", ctx=ctx)
                if sold_info:
                    sold_positions.append(sold_info)
                    continue

    return sold_positions


# ---------------------------------------------------------------------------
# TradeEngine (Unified Single Engine)
# ---------------------------------------------------------------------------

class TradeEngine:
    """
    Pure orchestrator for evaluating candidate markets, checking risk,
    managing positions, and executing orders.
    """
    def __init__(
        self,
        repo: Optional[Database] = None,
        broker: Optional[Broker] = None,
        strategy_fn: Any = None,
        strategy_name: Union[str, Strategy, Any] = DBStrategy.LAST_9,
        config: Optional[Config] = None,
        rules: Optional[list[Callable[[RiskContext], Optional[str]]]] = None,
        # Legacy parameter compatibility:
        client: Optional[Any] = None,
        db: Optional[Database] = None,
        strategy: Union[str, Strategy, Any] = None,
        stop_loss: Optional[bool] = None,
        check_spot: Optional[bool] = None,
        min_entry_price: Optional[int] = None,
        max_entry_price: Optional[int] = None,
        take_profit: Optional[Union[int, float]] = None,
        entry_timeout_seconds: Optional[float] = None,
        spot_client: Optional[Any] = None,
        **kwargs: Any,
    ):
        self.repo = repo or db
        self.db = self.repo
        self.client = client or getattr(broker, "client", None)
        self.config = config or Config()
        self.rules = rules if rules is not None else DEFAULT_RISK_RULES

        if broker is not None:
            self.broker = broker
        elif self.client is not None and getattr(self.client, "live_orders", False):
            self.broker = KalshiBroker(client=self.client, db=self.repo)
        else:
            self.broker = SimulatedBroker(client=self.client, db=self.repo)

        # Build runtime overrides
        overrides: dict[str, Any] = {}
        if stop_loss is not None:
            overrides["stop_loss"] = stop_loss
        if check_spot is not None:
            overrides["check_spot"] = check_spot
        if take_profit is not None:
            overrides["take_profit"] = take_profit
        if min_entry_price is not None or max_entry_price is not None:
            low = min_entry_price if min_entry_price is not None else 1
            high = max_entry_price if max_entry_price is not None else 99
            overrides["price_band"] = (low, high)
        if entry_timeout_seconds is not None:
            overrides["entry_timeout"] = entry_timeout_seconds

        target_strat = strategy if strategy is not None else (strategy_name if strategy_name is not None else strategy_fn)
        self.strategy = get_strategy(target_strat or DBStrategy.LAST_9, **overrides)
        if strategy_fn is not None and callable(strategy_fn):
            from dataclasses import replace
            self.strategy = replace(self.strategy, signal=strategy_fn)
        self.strategy_fn = self.strategy.signal
        self.strategy_name = (
            normalize_strategy(self.strategy.name)
            if hasattr(DBStrategy, str(self.strategy.name).upper())
            else self.strategy.name
        )

        # Compatibility fields
        self.stop_loss = self.strategy.stop_loss
        self.check_spot = self.strategy.check_spot
        self.min_entry_price = self.strategy.price_band[0] if self.strategy.price_band else None
        self.max_entry_price = self.strategy.price_band[1] if self.strategy.price_band else None
        self.take_profit = self.strategy.take_profit
        self.entry_timeout_seconds = self.strategy.entry_timeout
        self.taker_ioc = self.strategy.taker_ioc
        self.spot_client = spot_client
        self.bet_markets: set[str] = set()
        self.open_contract_tickers: set[str] = set()
        self.reconciler = Reconciler(broker=self.broker, db=self.repo)

    def run_cycle(self, markets: Optional[list[dict]] = None, now: Optional[datetime] = None) -> list[Decision]:
        """Run one complete trading cycle: reconcile -> manage exits -> evaluate candidates."""
        try:
            self.reconciler.reconcile()
        except Exception as e:
            analytics.warning(f"Reconciliation error in run_cycle: {e}")

        # 1. Manage exits on open positions
        self.manage_exits(markets=markets, now=now)

        # 2. Query candidate markets
        candidates = markets
        if candidates is None and self.repo:
            candidates = self.repo.get_candidates(
                environment=self.config.env,
                strategy=self.strategy_name,
            )
        if not candidates:
            return []

        decisions: list[Decision] = []
        for market in candidates:
            try:
                decision = self._process(market, now=now)
                if decision:
                    decisions.append(decision)
            except Exception as e:
                ticker = market.get("ticker", "unknown")
                strat_val = getattr(self.strategy_name, "value", str(self.strategy_name))
                analytics.exception(
                    f"Market {ticker} failed during cycle: {e}",
                    extra={"ticker": ticker, "strategy": strat_val},
                )
        return decisions

    def _process(self, market: dict[str, Any], now: Optional[datetime] = None) -> Optional[Decision]:
        """Evaluate a single market through risk rules, signal, and execution."""
        ticker = market.get("ticker")
        if not ticker:
            return None

        strat_val = getattr(self.strategy_name, "value", str(self.strategy_name))
        ctx = RiskContext(
            market=market,
            strategy_name=strat_val,
            db=self.repo,
            max_open_positions=self.config.max_open_positions,
            daily_loss_cap_cents=self.config.daily_loss_cap_cents,
            kill_switch_path=self.config.kill_switch_path,
            now=now,
        )

        reject_reason = reject(market, self.strategy, ctx)
        if reject_reason is not None:
            if self.repo:
                self.repo.log_event(
                    stage="risk_check",
                    level="INFO",
                    message=f"Market {ticker} skipped: {reject_reason}",
                    ticker=ticker,
                    strategy=strat_val,
                )
            return Decision(
                ticker=ticker,
                market=market,
                strategy=strat_val,
                side=None,
                reason=reject_reason,
            )

        if self.config.shadow_mode:
            # Evaluate side for shadow logging
            series = ticker.split("-")[0]
            past_results = self.repo.get_recent_results(series, limit=21) if self.repo else []
            side = self.strategy.signal(past_results=past_results, market=market)
            if side and self.repo:
                self.repo.log_event(
                    stage="trader",
                    level="INFO",
                    message=f"[SHADOW] Would bet 1 contract on {side.value} for {ticker}",
                    ticker=ticker,
                    strategy=strat_val,
                )
            return Decision(ticker=ticker, market=market, strategy=strat_val, side=side, shadow=True)

        order_res = self.process_market(market, now=now, check_timing=False)
        if not order_res or order_res.get("status") == "unfilled" or "error" in order_res:
            return Decision(ticker=ticker, market=market, strategy=strat_val, reason="order_not_filled")

        return Decision(
            ticker=ticker,
            market=market,
            strategy=strat_val,
            side=order_res.get("side"),
            price_cents=order_res.get("price", order_res.get("price_cents")),
        )

    def process_market(
        self,
        market: dict[str, Any],
        event_market_count: Any = None,
        now: Any = None,
        check_timing: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Process a single market and return the resulting order dict (or None)."""
        if self.repo is not None:
            self.bet_markets.update(self.repo.get_positioned_tickers(strategy=self.strategy_name))

        ticker = market.get("ticker")
        if not ticker or ticker in self.bet_markets or self._is_contract_held(ticker):
            return None

        strat_val = getattr(self.strategy_name, "value", str(self.strategy_name))
        ctx = RiskContext(
            market=market,
            strategy_name=strat_val,
            db=self.repo,
            max_open_positions=self.config.max_open_positions,
            daily_loss_cap_cents=self.config.daily_loss_cap_cents,
            kill_switch_path=self.config.kill_switch_path,
            now=now,
        )

        if check_timing and reject(market, self.strategy, ctx):
            return None

        if self._check_margin_guard(market, price_cents=50):
            return None

        series = ticker.split("-")[0]
        past_results = self.get_last_results(ticker, limit=21)
        side = None
        if callable(self.strategy.signal):
            try:
                side = self.strategy.signal(
                    past_results=past_results,
                    market=market,
                    check_spot=self.strategy.check_spot,
                    spot_client=self.spot_client,
                    min_entry_price=self.strategy.price_band[0],
                    max_entry_price=self.strategy.price_band[1],
                )
            except TypeError:
                side = self.strategy.signal(past_results=past_results)

        if side is None:
            return None

        ask_cents = extract_contract_price(market, side, book_side="ask")
        if ask_cents is None:
            if self.strategy.take_profit is not None:
                return None
            price_cents = 50
        else:
            price_cents = ask_cents

        if not in_band(price_cents, self.strategy.price_band):
            return None

        if self._check_margin_guard(market, price_cents=price_cents):
            return None

        resting_px = self._get_resting_sell_price(price_cents)
        tif = "immediate_or_cancel" if self.strategy.taker_ioc else None
        order_res = self.broker.buy(
            market=market,
            side=side,
            price_cents=price_cents,
            count=1,
            time_in_force=tif,
            resting_price_cents=resting_px,
            strategy_name=self.strategy_name,
            db=self.repo,
            entry_timeout=self.strategy.entry_timeout,
        )

        self.bet_markets.add(ticker)
        self.open_contract_tickers.add(ticker)

        if resting_px is not None and order_res and isinstance(order_res, dict):
            order_res["resting_order"] = {
                "action": "sell",
                "price": resting_px,
                "order_id": order_res.get("resting_order_id", ""),
            }

        return order_res

    def trade(
        self,
        markets: Optional[list[dict]] = None,
        now: Any = None,
        check_timing: bool = True,
    ) -> list[dict[str, Any]]:
        """Run exit management and evaluate candidates, returning list of placed orders."""
        self.manage_exits(markets=markets, now=now)
        candidates = markets
        if candidates is None and self.repo:
            candidates = self.repo.get_candidates(
                environment=self.config.env,
                strategy=self.strategy_name,
            )
        orders = []
        for m in (candidates or []):
            res = self.process_market(m, now=now, check_timing=check_timing)
            if res and res.get("status") != "unfilled" and "error" not in res:
                orders.append(res)
        return orders

    evaluate_all = trade

    def manage_exits(self, markets: Optional[list[dict]] = None, now: Any = None) -> list[dict]:
        """Manage take-profit and stop-loss exits on open positions."""
        return manage_exits(self.strategy, self, markets=markets, now=now)

    check_open_positions = manage_exits
    check_and_sell_positions = manage_exits

    def sell_position(
        self,
        position: dict[str, Any],
        current_price_cents: int,
        reason: str = "take_profit",
    ) -> Optional[dict[str, Any]]:
        """Liquidate an open position."""
        return sell_position(position, current_price_cents, reason=reason, ctx=self)

    def _get_resting_sell_price(self, entry_price: int) -> Optional[int]:
        tp = self.strategy.take_profit
        if tp is None:
            return None
        if isinstance(tp, float):
            return min(99, int(round(tp * entry_price)))
        return min(99, int(tp))

    def _check_margin_guard(self, market: dict[str, Any], price_cents: int) -> bool:
        if not getattr(self.broker, "is_live", False) or not self.client:
            return False
        if not hasattr(self.client, "base_url"):
            return False
        try:
            now_t = time.time()
            if not hasattr(self, "_last_bal_check") or (now_t - getattr(self, "_last_bal_check", 0)) > 15:
                self._last_bal_check = now_t
                url = f"{self.client.base_url}/portfolio/balance"
                headers = self.client._build_auth_headers("GET", url)
                bal_res = self.client._request("GET", url, headers=headers, timeout=5)
                if bal_res.status_code == 200:
                    bd = bal_res.json()
                    crypto_cash = 0.0
                    for b in bd.get("balance_breakdown", []):
                        if b.get("exchange_index") == 2:
                            crypto_cash = float(b.get("balance", 0.0))
                    total_cash = float(bd.get("balance_dollars", 0.0))
                    self._cached_avail_cash = crypto_cash if crypto_cash > 0 else total_cash
            cached_cash = getattr(self, "_cached_avail_cash", None)
            if cached_cash is not None and cached_cash < (price_cents / 100.0):
                analytics.warning(
                    f"[MARGIN CHECK] Skipping {market.get('ticker')}: available cash ${cached_cash:.2f} < contract cost ${price_cents/100:.2f}."
                )
                return True
        except Exception:
            pass
        return False

    def is_market_open(self, market: dict[str, Any]) -> bool:
        return str(market.get("status", "")).strip().lower() in ("open", "active")

    def is_binary_market(self, market: dict[str, Any], event_market_count: Any = None) -> bool:
        return is_binary_market(market, event_market_count=event_market_count)

    def pick_random_side(self) -> PositionSide:
        return random_signal()

    def choose_side(self, ticker: str, market: Optional[dict] = None) -> Optional[PositionSide]:
        series = ticker.split("-")[0]
        past = self.repo.get_recent_results(series, limit=21) if self.repo else []
        return self.strategy.signal(
            past_results=past,
            market=market,
            check_spot=self.strategy.check_spot,
            spot_client=self.spot_client,
        )

    def get_last_results(self, ticker: str, limit: int = 21) -> list[Any]:
        series = ticker.split("-")[0]
        # Continuously sync the freshest settled resolutions from Kalshi API (throttled to once every 30s)
        # Avoid overriding in-memory or isolated test fixtures during test runs
        if "pytest" not in sys.modules and self.client and hasattr(self.client, "get_settled_markets"):
            now_t = time.time()
            last_fetch = getattr(self, f"_last_settled_fetch_{series}", 0)
            if (now_t - last_fetch) > 30:
                setattr(self, f"_last_settled_fetch_{series}", now_t)
                try:
                    settled = self.client.get_settled_markets(series, limit=10)
                    if settled and self.repo:
                        self.repo.upsert_markets(settled)
                except Exception:
                    pass
        return self.repo.get_recent_results(series, limit=limit) if self.repo else []

    def _extract_ask_cents(self, market: dict, side: PositionSide) -> Optional[int]:
        return extract_contract_price(market, side, book_side="ask")

    def _extract_bid_cents(self, market: dict, side: PositionSide) -> Optional[int]:
        return extract_contract_price(market, side, book_side="bid")

    def _is_contract_held(self, ticker: str) -> bool:
        if self.repo and hasattr(self.repo, "has_open_position"):
            return self.repo.has_open_position(ticker, strategy=self.strategy_name)
        return ticker in self.open_contract_tickers


# Unified aliases: Trader and RiskEngine point directly to TradeEngine
Trader = TradeEngine
RiskEngine = TradeEngine


# ---------------------------------------------------------------------------
# Linear Cycle Function
# ---------------------------------------------------------------------------

def run_cycle(
    s: Strategy,
    ctx: Any,
    markets: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
) -> list[Decision]:
    """
    Linear 15-minute trading cycle pipeline:
    Scanner -> Settler -> Exits -> Candidate Loop (reject -> signal -> in_band -> buy)
    """
    if hasattr(ctx, "scanner") and ctx.scanner:
        ctx.scanner.scan()
    if hasattr(ctx, "settler") and ctx.settler:
        ctx.settler.settle()

    manage_exits(s, ctx, markets=markets, now=now)

    db = getattr(ctx, "db", getattr(ctx, "repo", None))
    open_mkts = markets
    if open_mkts is None and db:
        env = getattr(ctx, "env", "demo")
        open_mkts = db.get_candidates(environment=env, strategy=s.name) if hasattr(db, "get_candidates") else []

    decisions: list[Decision] = []
    broker = getattr(ctx, "broker", None)

    for m in (open_mkts or []):
        ticker = m.get("ticker", "")
        risk_ctx = RiskContext(
            market=m,
            strategy_name=s.name,
            db=db,
            max_open_positions=getattr(ctx, "max_open_positions", 10),
            daily_loss_cap_cents=getattr(ctx, "daily_loss_cap_cents", 5000),
            kill_switch_path=getattr(ctx, "kill_switch_path", "KILL_SWITCH"),
            now=now,
        )
        if (reason := reject(m, s, risk_ctx)):
            decisions.append(Decision(ticker=ticker, market=m, strategy=s.name, reason=reason))
            continue

        series = ticker.split("-")[0]
        results = db.get_recent_results(series, limit=21) if db else []
        side = s.signal(past_results=results, market=m, check_spot=s.check_spot)
        if not side:
            decisions.append(Decision(ticker=ticker, market=m, strategy=s.name, reason="strategy_no_opinion"))
            continue

        ask_cents = extract_contract_price(m, side, book_side="ask")
        if not in_band(ask_cents, s.price_band):
            decisions.append(Decision(ticker=ticker, market=m, strategy=s.name, side=side, price_cents=ask_cents, reason="price_outside_band"))
            continue

        if broker and hasattr(broker, "buy"):
            resting_px = None
            if s.take_profit is not None:
                resting_px = min(99, int(round(s.take_profit * ask_cents))) if isinstance(s.take_profit, float) else min(99, int(s.take_profit))
            tif = "immediate_or_cancel" if s.taker_ioc else None
            broker.buy(
                market=m,
                side=side,
                price_cents=ask_cents,
                count=1,
                time_in_force=tif,
                resting_price_cents=resting_px,
                strategy_name=s.name,
                db=db,
                entry_timeout=s.entry_timeout,
            )
            decisions.append(Decision(ticker=ticker, market=m, strategy=s.name, side=side, price_cents=ask_cents))

    return decisions


# ---------------------------------------------------------------------------
# Compatibility Shims for Legacy Function Signatures
# ---------------------------------------------------------------------------

def strategy_last_9(
    past_results: Any = None,
    market: Optional[dict] = None,
    min_samples: int = 9,
    limit: int = 21,
    **kwargs: Any,
) -> Optional[PositionSide]:
    return trend_signal(past_results=past_results, market=market, min_samples=min_samples, limit=limit, **kwargs)


strategy_last_21 = strategy_last_9


def choose_side_last_21(
    past_results: Any = None,
    market: Optional[dict] = None,
    spot_client: Optional[Any] = None,
    check_spot: bool = False,
    min_entry_price: Optional[int] = None,
    max_entry_price: Optional[int] = None,
    require_spot_agreement: bool = True,
    limit: int = 21,
    **kwargs: Any,
) -> Optional[PositionSide]:
    side = trend_signal(
        past_results=past_results,
        market=market,
        spot_client=spot_client,
        check_spot=check_spot,
        require_spot_agreement=require_spot_agreement,
        min_samples=0,
        limit=limit,
    )
    if market and (min_entry_price is not None or max_entry_price is not None) and side:
        ask = extract_contract_price(market, side)
        if not in_band(ask, (min_entry_price or 1, max_entry_price or 99)):
            return None
    return side


choose_side_last_9 = choose_side_last_21


def last_21_spot(
    past_results_or_market: Any = None,
    market: Optional[dict] = None,
    past_results: Any = None,
    exit_multiplier: Optional[Union[int, float]] = None,
    exit_price_cents: Optional[int] = None,
    n: Optional[Union[int, float]] = None,
    exit_price: Optional[int] = None,
    **kwargs: Any,
) -> Any:
    target_market = market
    target_past = past_results
    if isinstance(past_results_or_market, dict):
        target_market = past_results_or_market
    elif isinstance(past_results_or_market, (list, tuple)):
        target_past = past_results_or_market

    eff_n = exit_multiplier if exit_multiplier is not None else n
    eff_p = exit_price_cents if exit_price_cents is not None else exit_price

    if target_market is None and target_past is None:
        def _bound(past_res=None, m=None, **kw):
            return last_21_spot(market=(m or target_market), past_results=(past_res or target_past), **kw)
        _bound.exit_multiplier = eff_n
        _bound.exit_price_cents = eff_p
        _bound.n = eff_n
        _bound.check_spot = True
        return _bound

    return choose_side_last_21(
        past_results=target_past,
        market=target_market,
        check_spot=True,
        min_entry_price=kwargs.pop("min_entry_price", 20),
        max_entry_price=kwargs.pop("max_entry_price", 65),
        **kwargs,
    )


last_9_spot = last_21_spot


def choose_side_regime_aware(
    past_results: Any = None,
    market: Optional[dict] = None,
    spot_client: Optional[Any] = None,
    check_spot: bool = False,
    min_entry_price: Optional[int] = 20,
    max_entry_price: Optional[int] = 65,
    **kwargs: Any,
) -> Optional[PositionSide]:
    side = regime_signal(past_results=past_results, market=market, check_spot=check_spot, spot_client=spot_client, **kwargs)
    if market and (min_entry_price is not None or max_entry_price is not None) and side:
        ask = extract_contract_price(market, side)
        if not in_band(ask, (min_entry_price or 1, max_entry_price or 99)):
            return None
    return side


def regime_aware(past_results_or_market: Any = None, market: Optional[dict] = None, past_results: Any = None, **kwargs: Any) -> Any:
    target_market = market
    target_past = past_results
    if isinstance(past_results_or_market, dict):
        target_market = past_results_or_market
    elif isinstance(past_results_or_market, (list, tuple)):
        target_past = past_results_or_market

    if target_market is None and target_past is None:
        def _bound(past_res=None, m=None, **kw):
            return regime_aware(market=(m or target_market), past_results=(past_res or target_past), **kw)
        _bound.taker_ioc = True
        return _bound
    return choose_side_regime_aware(past_results=target_past, market=target_market, **kwargs)


def regime_spot(past_results_or_market: Any = None, market: Optional[dict] = None, **kwargs: Any) -> Any:
    return regime_aware(past_results_or_market=past_results_or_market, market=market, check_spot=True, **kwargs)


def sell_Xn_Ym(
    past_results_or_market: Any = None,
    n_or_market: Any = None,
    y_val: Optional[Union[int, float]] = None,
    z_val: Optional[Union[int, float]] = None,
    *,
    n: Optional[float] = None,
    y: Optional[float] = None,
    z: Optional[Union[int, float]] = None,
    zp: Optional[Union[int, float]] = None,
    p: Optional[Union[int, float]] = None,
    max_entry_price: Optional[Union[int, float]] = None,
    market: Optional[dict] = None,
    now: Any = None,
    check_timing: bool = False,
    yes_ask: Optional[int] = None,
    no_ask: Optional[int] = None,
    stop_loss: bool = False,
    **kwargs: Any,
) -> Any:
    target_market = market
    if isinstance(past_results_or_market, dict):
        target_market = past_results_or_market
        if isinstance(n_or_market, (int, float)):
            n = float(n_or_market)
        if isinstance(y_val, (int, float)):
            y = float(y_val)
        if isinstance(z_val, (int, float)):
            z = float(z_val)
    elif isinstance(past_results_or_market, (int, float)):
        n = float(past_results_or_market)
        if isinstance(n_or_market, (int, float)):
            y = float(n_or_market)
        if isinstance(y_val, (int, float)):
            z = float(y_val)
    elif isinstance(n_or_market, dict):
        target_market = n_or_market

    n_num = float(n) if n is not None else 3.0
    y_num = float(y) if y is not None else 5.0
    z_in = z if z is not None else (zp if zp is not None else (p if p is not None else max_entry_price))
    eff_z = int(z_in) if z_in is not None else (int(100 / n_num) if n_num > 0 else 33)

    if target_market is None and yes_ask is None and no_ask is None:
        def _bound_strat(past_or_m=None, m=None, **kw):
            tm = past_or_m if isinstance(past_or_m, dict) else (m or target_market)
            return sell_Xn_Ym(tm, n=n_num, y=y_num, z=eff_z, **kw)
        _bound_strat.n = n_num
        _bound_strat.y = y_num
        _bound_strat.z = eff_z
        _bound_strat.stop_loss = stop_loss
        return _bound_strat

    if check_timing and target_market:
        if not is_entry_time_ym(target_market, y_minutes=y_num, now=now):
            return None

    m_to_check = target_market or {}
    if yes_ask is not None or no_ask is not None:
        m_to_check = {"yes_ask": yes_ask, "no_ask": no_ask}

    return cheapest_side(market=m_to_check, max_entry_price=eff_z)


sell_xn_ym = sell_Xn_Ym
sell_Xn_Ym_Zp = sell_Xn_Ym
sell_xn_ym_zp = sell_Xn_Ym

sell_3n = lambda *args, **kw: sell_Xn_Ym(*args, n=3, y=5, **kw)
sell_2n = lambda *args, **kw: sell_Xn_Ym(*args, n=2, y=10, **kw)
sell_2n_5m = lambda *args, **kw: sell_Xn_Ym(*args, n=2, y=5, **kw)
sell_2n_5m_35p = lambda *args, **kw: sell_Xn_Ym(*args, n=2, y=5, z=35, **kw)
sell_1_5n = lambda *args, **kw: sell_Xn_Ym(*args, n=1.5, y=5, **kw)
sell_1_5n_5m = lambda *args, **kw: sell_Xn_Ym(*args, n=1.5, y=5, z=kw.pop("z", 35), **kw)
sell_1_5n_5m_35p = lambda *args, **kw: sell_Xn_Ym(*args, n=1.5, y=5, z=35, **kw)
sell_1_5n_35p = lambda *args, **kw: sell_Xn_Ym(*args, n=1.5, y=5, z=35, **kw)
sell_2_5n = lambda *args, **kw: sell_Xn_Ym(*args, n=2.5, y=5, **kw)

choose_sell_if_3n = sell_3n
choose_sell_if_2n = sell_2n
choose_sell_if_2n_5m = sell_2n_5m
choose_sell_if_1_5n = sell_1_5n
choose_sell_if_2_5n = sell_2_5n
choose_sell_if_2n_5m_35p = sell_2n_5m_35p
choose_sell_if_1_5n_35p = sell_1_5n_35p
choose_side_sell_if_3n = sell_3n
choose_side_sell_if_2n = sell_2n
choose_side_sell_2n_5m = sell_2n_5m
choose_side_sell_1_5n = sell_1_5n
choose_side_sell_2_5n = sell_2_5n
choose_side_sell_2n_5m_35p = sell_2n_5m_35p
choose_side_sell_1_5n_35p = sell_1_5n_35p


def is_sell_strategy(strategy: Any) -> bool:
    return get_strategy_resting_multiplier(strategy) is not None


def get_strategy_resting_multiplier(strategy: Any) -> Optional[Union[int, float]]:
    if hasattr(strategy, "n"):
        n = getattr(strategy, "n")
        return int(n) if float(n).is_integer() else float(n)
    s = str(getattr(strategy, "value", getattr(strategy, "name", strategy)) or "").lower().strip()
    if not s or s in ("random", "last_9", "last_21", "trend", "fade"):
        return None
    if "1.5" in s or "1_5" in s:
        return 1.5
    if "2.5" in s or "2_5" in s:
        return 2.5
    if "3n" in s or "3x" in s:
        return 3
    if "2n" in s or "2x" in s:
        return 2
    match = re.search(r"sell_?([0-9.]+)n", s)
    if match:
        val = float(match.group(1))
        return int(val) if val.is_integer() else val
    return None


def get_strategy_entry_minute(strategy: Any) -> Optional[Union[int, float]]:
    if hasattr(strategy, "y"):
        y = getattr(strategy, "y")
        return int(y) if float(y).is_integer() else float(y)
    s = str(getattr(strategy, "value", getattr(strategy, "name", strategy)) or "").lower().strip()
    if s in ("sell_2n", "sell2n", "sell_if_2n", "sell_2n_10m") or "10m" in s:
        return 10
    if "5m" in s or "sell" in s:
        return 5
    return None


def get_strategy_max_entry_price(strategy: Any) -> int:
    if hasattr(strategy, "z") and getattr(strategy, "z") is not None:
        return int(getattr(strategy, "z"))
    s = str(getattr(strategy, "value", getattr(strategy, "name", strategy)) or "").lower().strip()
    m = re.search(r"(\d+)p\b", s)
    if m:
        return int(m.group(1))
    if "sell_1.5n_5m" in s or "sell_1_5n_5m" in s or "sell_1.5n_35p" in s:
        return 35
    n = get_strategy_resting_multiplier(strategy)
    if n and n > 0:
        return int(100 / n)
    return 33


def is_entry_time_ym(market: dict, y_minutes: float = 5.0, window_minutes: float = 5.0, now: Any = None) -> bool:
    minute = get_market_minute(market, now=now)
    if minute is None:
        return True
    return float(y_minutes) <= minute < float(y_minutes + window_minutes)


def is_entry_time_5m(market: dict, now: Any = None) -> bool:
    return is_entry_time_ym(market, y_minutes=5.0, now=now)


def is_entry_time_10m(market: dict, now: Any = None) -> bool:
    return is_entry_time_ym(market, y_minutes=10.0, now=now)


def is_entry_time(market: dict, strategy: Any = None, now: Any = None) -> bool:
    if is_last_n_strategy(strategy):
        minute = get_market_minute(market, now=now)
        return minute is None or (0.0 <= minute < 10.0)
    y = get_strategy_entry_minute(strategy) or 5.0
    return is_entry_time_ym(market, y_minutes=float(y), now=now)


def is_checkin_time(market_or_pos: dict, now: Any = None) -> bool:
    minute = get_market_minute(market_or_pos, now=now)
    return minute is None or minute >= 10.0


def should_sell(entry_price_cents: int, current_price_cents: Optional[int], multiplier: float = 3.0) -> bool:
    if current_price_cents is None or entry_price_cents <= 0:
        return False
    return current_price_cents >= multiplier * entry_price_cents
