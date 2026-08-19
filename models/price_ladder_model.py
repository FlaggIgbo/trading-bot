"""
Price Ladder & Timing Matrix Empirical Modeling Engine
======================================================

Enables quantitative backtesting and forward modeling of hypothetical trade entries
at any price X (where X is anywhere between 1¢ and 99¢ / $0.01 and $0.99) at any time T
(minute marks across 15M and 1H prediction market lifecycles).

Key Capabilities:
-----------------
1. Microstructure Price Ladder Simulation:
   - Evaluates fill feasibility (Taker vs Maker resting limit bid).
   - Simulates hold-to-settlement PnL, ROI, Win Rates, and Expected Value (EV).
   - Tracks path metrics: Max Favorable Excursion (MFE), Max Adverse Excursion (MAE),
     and Dipped Favorite Recovery Rates.
   - Evaluates dynamic exit strategies (Fixed Take-Profit, Trailing Profit Floor, Stop-Loss).

2. Price & Timing Matrix Analysis:
   - Aggregates empirical performance across all prices (1¢–99¢) and time checkpoints.
   - Identifies high-conviction positive-EV corridors and dead coin-flip zones.
   - Detects statistical market patterns across asset classes (BTC, ETH, SPX, etc.).

3. Flexible Export & Reporting:
   - ASCII visual heatmaps and tables for CLI display.
   - JSON / CSV export for quantitative model refinement.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# Standard time checkpoints for 15-minute and 1-hour prediction markets
CHECKPOINTS_15M = [0, 2, 5, 7, 10, 12, 14]
CHECKPOINTS_1H = [0, 10, 20, 30, 40, 50, 52, 55, 57, 59]


@dataclass
class HypotheticalTrade:
    """Represents the simulated outcome of entering a contract at price X at time T."""
    ticker: str
    symbol: str
    timeframe: str
    side: str
    entry_price_cents: int
    entry_minute: int
    entry_ts: int
    fill_type: str  # 'taker' or 'maker'
    filled: bool
    fill_price_cents: int
    fill_minute: int
    settlement_result: str  # 'yes' or 'no'
    settlement_won: bool
    hold_pnl_cents: int
    hold_roi_pct: float
    max_price_cents: int
    min_price_cents: int
    max_favorable_excursion_cents: int  # MFE
    max_adverse_excursion_cents: int    # MAE
    dipped_and_recovered: bool
    strategy_pnl_cents: int
    strategy_exit_action: str
    strategy_exit_price_cents: int
    spot_lead_pct: float | None = None


@dataclass
class PriceLadderCell:
    """Aggregated statistical metrics for a specific (price X, timing T, side S) bucket."""
    price_cents: int
    minute: int
    side: str
    sample_count: int
    fill_count: int
    fill_rate: float
    win_count: int
    win_rate: float
    hold_ev_cents: float
    hold_roi_pct: float
    total_hold_pnl_cents: int
    strategy_ev_cents: float
    total_strategy_pnl_cents: int
    avg_mfe_cents: float
    avg_mae_cents: float
    recovery_rate: float
    profit_factor: float


def simulate_hypothetical_entry(
    market_snapshots: list[dict],
    settlement_result: str,
    target_price_cents: int,
    entry_minute: int,
    side: str = "yes",
    fill_mode: str = "maker",
    take_profit_target_cents: int | None = None,
    stop_loss_threshold_cents: int = 25,
    stop_loss_floor_cents: int = 15,
    symbol: str = "BTC",
    timeframe: str = "15m",
) -> HypotheticalTrade | None:
    """
    Simulates entering a contract at target_price_cents (1¢–99¢) at entry_minute.

    Args:
        market_snapshots: Chronologically sorted snapshot documents for a single market lifecycle.
        settlement_result: Final resolution ('yes' or 'no').
        target_price_cents: Hypothetical entry price X in cents (1 to 99).
        entry_minute: Target minute mark T to enter (e.g. 0, 2, 5, 7, 10, 12, 14).
        side: Contract side to enter ('yes' or 'no').
        fill_mode: 'maker' (resting limit bid at X) or 'taker' (buy if ask <= X).
        take_profit_target_cents: Optional TP target (e.g. 62¢, 88¢, or None for hold-to-settlement).
        stop_loss_threshold_cents: Maximum loss in cents before stop-loss trigger.
        stop_loss_floor_cents: Absolute price floor in cents for stop-loss trigger.
        symbol: Underlying asset symbol.
        timeframe: Market timeframe ('15m' or '1h').

    Returns:
        HypotheticalTrade or None if market data insufficient.
    """
    if not market_snapshots or not settlement_result:
        return None

    side_clean = side.lower()
    settle_clean = settlement_result.lower()
    clamped_price = max(1, min(99, int(target_price_cents)))

    # Sort snapshots chronologically by timestamp
    sorted_snaps = sorted(market_snapshots, key=lambda s: s.get("ts", 0))
    if not sorted_snaps:
        return None

    ticker = sorted_snaps[0].get("tick", "UNKNOWN")

    # Find the snapshot closest to or matching entry_minute
    entry_snap_idx = None
    for idx, snap in enumerate(sorted_snaps):
        m_val = snap.get("m")
        if m_val is None:
            ts = snap.get("ts", 0)
            if ts:
                m_val = datetime.fromtimestamp(ts, tz=timezone.utc).minute
        if m_val is not None:
            mod_m = m_val % (15 if timeframe == "15m" else 60)
            if mod_m >= entry_minute:
                entry_snap_idx = idx
                break

    if entry_snap_idx is None:
        entry_snap_idx = 0

    entry_snap = sorted_snaps[entry_snap_idx]
    entry_ts = entry_snap.get("ts", 0)

    # Check fill feasibility
    filled = False
    fill_minute = entry_minute
    fill_price = clamped_price

    def get_quote_for_snap(s: dict) -> tuple[int | None, int | None, int | None]:
        """Returns (price, bid, ask) in cents for the target side."""
        if side_clean == "yes":
            p = s.get("y")
            bid = s.get("y_bid")
            ask = s.get("y_ask")
        else:
            p = s.get("n")
            bid = s.get("n_bid")
            ask = s.get("n_ask")
        if p is None and ask is not None:
            p = ask
        elif p is None and bid is not None:
            p = bid
        return p, bid, ask

    # Evaluate fill condition at entry snapshot and subsequent snapshots
    for idx in range(entry_snap_idx, len(sorted_snaps)):
        snap = sorted_snaps[idx]
        p, bid, ask = get_quote_for_snap(snap)
        s_min = snap.get("m", entry_minute)

        if fill_mode == "taker":
            effective_market_price = ask if ask is not None else p
            if effective_market_price is not None and effective_market_price <= clamped_price:
                filled = True
                fill_price = effective_market_price
                fill_minute = s_min
                entry_snap_idx = idx
                break
        else:
            if ask is not None and ask <= clamped_price:
                filled = True
                fill_price = clamped_price
                fill_minute = s_min
                entry_snap_idx = idx
                break
            elif p is not None and p <= clamped_price:
                filled = True
                fill_price = clamped_price
                fill_minute = s_min
                entry_snap_idx = idx
                break

    if not filled:
        return HypotheticalTrade(
            ticker=ticker,
            symbol=symbol,
            timeframe=timeframe,
            side=side_clean,
            entry_price_cents=clamped_price,
            entry_minute=entry_minute,
            entry_ts=entry_ts,
            fill_type=fill_mode,
            filled=False,
            fill_price_cents=clamped_price,
            fill_minute=entry_minute,
            settlement_result=settle_clean,
            settlement_won=(side_clean == settle_clean),
            hold_pnl_cents=0,
            hold_roi_pct=0.0,
            max_price_cents=clamped_price,
            min_price_cents=clamped_price,
            max_favorable_excursion_cents=0,
            max_adverse_excursion_cents=0,
            dipped_and_recovered=False,
            strategy_pnl_cents=0,
            strategy_exit_action="unfilled",
            strategy_exit_price_cents=clamped_price,
        )

    # Track price trajectory from fill point forward to settlement
    subsequent_prices: list[int] = []
    for idx in range(entry_snap_idx, len(sorted_snaps)):
        snap = sorted_snaps[idx]
        p, bid, ask = get_quote_for_snap(snap)
        if p is not None:
            subsequent_prices.append(p)
        elif bid is not None:
            subsequent_prices.append(bid)
        elif ask is not None:
            subsequent_prices.append(ask)

    if not subsequent_prices:
        subsequent_prices = [fill_price]

    max_p = max(subsequent_prices)
    min_p = min(subsequent_prices)
    mfe = max(0, max_p - fill_price)
    mae = max(0, fill_price - min_p)

    # Settlement outcome (Hold to 100¢ payout)
    settlement_won = (side_clean == settle_clean)
    if settlement_won:
        hold_pnl = 100 - fill_price
        hold_roi = ((100 - fill_price) / fill_price) * 100.0 if fill_price > 0 else 0.0
    else:
        hold_pnl = -fill_price
        hold_roi = -100.0

    dipped_and_recovered = (mae > 0) and settlement_won

    # Dynamic Strategy Simulation (TP / Stop Loss / Cut-Loss exits)
    strategy_pnl = hold_pnl
    strategy_action = "held_to_settlement"
    strategy_exit_price = 100 if settlement_won else 0

    resolved_tp = take_profit_target_cents
    if resolved_tp is None:
        if fill_price < 50:
            resolved_tp = 62
        elif fill_price < 70:
            resolved_tp = 88
        else:
            resolved_tp = 100

    for idx in range(entry_snap_idx + 1, len(sorted_snaps)):
        snap = sorted_snaps[idx]
        p, bid, ask = get_quote_for_snap(snap)
        s_min = snap.get("m", 0) % (15 if timeframe == "15m" else 60)
        curr_p = bid if bid is not None else p

        if curr_p is None:
            continue

        # 1. Take-Profit Check
        if resolved_tp and resolved_tp < 100 and curr_p >= resolved_tp:
            strategy_pnl = resolved_tp - fill_price
            strategy_action = "take_profit_hit"
            strategy_exit_price = resolved_tp
            break

        # 2. Favorite Maker Guard
        if fill_price >= 70:
            continue

        # 3. 10-Minute Stop-Loss Check
        if s_min >= 10:
            loss = fill_price - curr_p
            if loss >= stop_loss_threshold_cents or curr_p <= stop_loss_floor_cents:
                strategy_pnl = curr_p - fill_price
                strategy_action = "stop_loss_hit"
                strategy_exit_price = curr_p
                break

        # 4. 12.5-Minute / 14-Minute Endgame Cut Check
        if s_min >= 12 and curr_p < 25:
            strategy_pnl = curr_p - fill_price
            strategy_action = "endgame_cut_hit"
            strategy_exit_price = curr_p
            break

    spot_p = entry_snap.get("p")
    spot_lead_val = None
    if spot_p:
        spot_lead_val = 0.0

    return HypotheticalTrade(
        ticker=ticker,
        symbol=symbol,
        timeframe=timeframe,
        side=side_clean,
        entry_price_cents=clamped_price,
        entry_minute=entry_minute,
        entry_ts=entry_ts,
        fill_type=fill_mode,
        filled=True,
        fill_price_cents=fill_price,
        fill_minute=fill_minute,
        settlement_result=settle_clean,
        settlement_won=settlement_won,
        hold_pnl_cents=hold_pnl,
        hold_roi_pct=hold_roi,
        max_price_cents=max_p,
        min_price_cents=min_p,
        max_favorable_excursion_cents=mfe,
        max_adverse_excursion_cents=mae,
        dipped_and_recovered=dipped_and_recovered,
        strategy_pnl_cents=strategy_pnl,
        strategy_exit_action=strategy_action,
        strategy_exit_price_cents=strategy_exit_price,
        spot_lead_pct=spot_lead_val,
    )


def model_price_ladder_matrix(
    markets_data: list[dict],
    min_price_cents: int = 1,
    max_price_cents: int = 99,
    price_step: int = 1,
    time_checkpoints: list[int] | None = None,
    timeframe: str = "15m",
    fill_mode: str = "maker",
) -> dict[tuple[int, int, str], PriceLadderCell]:
    """
    Builds the complete empirical Price Ladder & Timing Matrix across historical markets.
    """
    checkpoints = time_checkpoints or (CHECKPOINTS_15M if timeframe == "15m" else CHECKPOINTS_1H)
    price_range = range(min_price_cents, max_price_cents + 1, price_step)

    cell_trades: dict[tuple[int, int, str], list[HypotheticalTrade]] = defaultdict(list)

    for mkt in markets_data:
        snaps = mkt.get("snapshots", [])
        result = mkt.get("result")
        sym = mkt.get("sym", "BTC")
        if not snaps or not result:
            continue

        for price_x in price_range:
            for minute_t in checkpoints:
                for side in ("yes", "no"):
                    trade = simulate_hypothetical_entry(
                        market_snapshots=snaps,
                        settlement_result=result,
                        target_price_cents=price_x,
                        entry_minute=minute_t,
                        side=side,
                        fill_mode=fill_mode,
                        symbol=sym,
                        timeframe=timeframe,
                    )
                    if trade is not None:
                        cell_trades[(price_x, minute_t, side)].append(trade)

    matrix: dict[tuple[int, int, str], PriceLadderCell] = {}

    for (price_x, minute_t, side), trades in cell_trades.items():
        sample_count = len(trades)
        filled_trades = [t for t in trades if t.filled]
        fill_count = len(filled_trades)
        fill_rate = (fill_count / sample_count) if sample_count > 0 else 0.0

        if fill_count == 0:
            matrix[(price_x, minute_t, side)] = PriceLadderCell(
                price_cents=price_x,
                minute=minute_t,
                side=side,
                sample_count=sample_count,
                fill_count=0,
                fill_rate=0.0,
                win_count=0,
                win_rate=0.0,
                hold_ev_cents=0.0,
                hold_roi_pct=0.0,
                total_hold_pnl_cents=0,
                strategy_ev_cents=0.0,
                total_strategy_pnl_cents=0,
                avg_mfe_cents=0.0,
                avg_mae_cents=0.0,
                recovery_rate=0.0,
                profit_factor=0.0,
            )
            continue

        win_count = sum(1 for t in filled_trades if t.settlement_won)
        win_rate = win_count / fill_count

        total_hold_pnl = sum(t.hold_pnl_cents for t in filled_trades)
        hold_ev = total_hold_pnl / fill_count
        hold_roi = (hold_ev / price_x) * 100.0 if price_x > 0 else 0.0

        total_strat_pnl = sum(t.strategy_pnl_cents for t in filled_trades)
        strat_ev = total_strat_pnl / fill_count

        avg_mfe = sum(t.max_favorable_excursion_cents for t in filled_trades) / fill_count
        avg_mae = sum(t.max_adverse_excursion_cents for t in filled_trades) / fill_count

        dipped_count = sum(1 for t in filled_trades if t.max_adverse_excursion_cents > 0)
        recovered_count = sum(1 for t in filled_trades if t.dipped_and_recovered)
        recovery_rate = (recovered_count / dipped_count) if dipped_count > 0 else (1.0 if win_rate > 0.5 else 0.0)

        gross_profit = sum(t.hold_pnl_cents for t in filled_trades if t.hold_pnl_cents > 0)
        gross_loss = abs(sum(t.hold_pnl_cents for t in filled_trades if t.hold_pnl_cents < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        matrix[(price_x, minute_t, side)] = PriceLadderCell(
            price_cents=price_x,
            minute=minute_t,
            side=side,
            sample_count=sample_count,
            fill_count=fill_count,
            fill_rate=round(fill_rate, 3),
            win_count=win_count,
            win_rate=round(win_rate, 4),
            hold_ev_cents=round(hold_ev, 2),
            hold_roi_pct=round(hold_roi, 2),
            total_hold_pnl_cents=total_hold_pnl,
            strategy_ev_cents=round(strat_ev, 2),
            total_strategy_pnl_cents=total_strat_pnl,
            avg_mfe_cents=round(avg_mfe, 1),
            avg_mae_cents=round(avg_mae, 1),
            recovery_rate=round(recovery_rate, 3),
            profit_factor=round(profit_factor, 2),
        )

    return matrix


def find_profitable_patterns(
    matrix: dict[tuple[int, int, str], PriceLadderCell],
    min_samples: int = 5,
    min_ev_cents: float = 3.0,
    min_win_rate: float = 0.52,
) -> dict[str, list[dict[str, Any]]]:
    """
    Analyzes the price ladder matrix to extract actionable statistical patterns.
    """
    sweet_spots: list[dict[str, Any]] = []
    high_win_rate: list[dict[str, Any]] = []
    recovery_zones: list[dict[str, Any]] = []
    negative_traps: list[dict[str, Any]] = []

    for (price_x, minute_t, side), cell in matrix.items():
        if cell.fill_count < min_samples:
            continue

        item = {
            "price_cents": price_x,
            "minute": minute_t,
            "side": side,
            "samples": cell.fill_count,
            "win_rate_pct": round(cell.win_rate * 100, 1),
            "hold_ev_cents": cell.hold_ev_cents,
            "strategy_ev_cents": cell.strategy_ev_cents,
            "roi_pct": cell.hold_roi_pct,
            "avg_mfe": cell.avg_mfe_cents,
            "avg_mae": cell.avg_mae_cents,
            "recovery_rate_pct": round(cell.recovery_rate * 100, 1),
            "profit_factor": cell.profit_factor,
        }

        if cell.hold_ev_cents >= min_ev_cents and cell.win_rate >= min_win_rate:
            sweet_spots.append(item)

        if cell.win_rate >= 0.70:
            high_win_rate.append(item)

        if price_x >= 70 and cell.recovery_rate >= 0.65:
            recovery_zones.append(item)

        if cell.hold_ev_cents <= -5.0 or (35 <= price_x <= 55 and cell.hold_ev_cents < 0):
            negative_traps.append(item)

    sweet_spots.sort(key=lambda x: x["hold_ev_cents"], reverse=True)
    high_win_rate.sort(key=lambda x: x["win_rate_pct"], reverse=True)
    recovery_zones.sort(key=lambda x: x["recovery_rate_pct"], reverse=True)
    negative_traps.sort(key=lambda x: x["hold_ev_cents"])

    return {
        "sweet_spots": sweet_spots[:25],
        "high_win_rate_corridors": high_win_rate[:25],
        "favorite_recovery_zones": recovery_zones[:25],
        "negative_ev_traps": negative_traps[:25],
    }


def format_price_matrix_report(
    matrix: dict[tuple[int, int, str], PriceLadderCell],
    timeframe: str = "15m",
    symbol: str | None = None,
    price_bands: list[tuple[int, int]] | None = None,
) -> str:
    """
    Renders an ASCII report and price band heatmap summarizing the model.
    """
    checkpoints = CHECKPOINTS_15M if timeframe == "15m" else CHECKPOINTS_1H
    bands = price_bands or [
        (1, 15),
        (16, 25),
        (26, 35),
        (36, 45),
        (46, 55),
        (56, 65),
        (66, 75),
        (76, 85),
        (86, 95),
        (96, 99),
    ]

    lines = []
    sym_label = f" ({symbol})" if symbol else ""
    lines.append("=" * 88)
    lines.append(f"📊 PRICE LADDER & TIMING MATRIX EMPIRICAL MODEL{sym_label} [{timeframe.upper()}]")
    lines.append("=" * 88)
    lines.append(
        "Shows Win Rate (%) / Hold Expected Value (EV in ¢) for entering at price X at minute T:"
    )
    lines.append("-" * 88)

    header = f"{'Price Band':<14} | " + " | ".join(f":{m:02d}m EV (Win%)" for m in checkpoints)
    lines.append(header)
    lines.append("-" * len(header))

    for min_p, max_p in bands:
        band_label = f"{min_p:02d}¢ - {max_p:02d}¢"
        row_cells = []
        for m in checkpoints:
            band_filled = 0
            band_wins = 0
            band_pnl = 0

            for p in range(min_p, max_p + 1):
                for side in ("yes", "no"):
                    cell = matrix.get((p, m, side))
                    if cell and cell.fill_count > 0:
                        band_filled += cell.fill_count
                        band_wins += cell.win_count
                        band_pnl += cell.total_hold_pnl_cents

            if band_filled > 0:
                wr = (band_wins / band_filled) * 100.0
                ev = band_pnl / band_filled
                sign = "+" if ev > 0 else ""
                row_cells.append(f"{sign}{ev:4.1f}¢ ({wr:4.0f}%)")
            else:
                row_cells.append("   -- ( --%)")

        lines.append(f"{band_label:<14} | " + " | ".join(row_cells))

    lines.append("-" * 88)

    patterns = find_profitable_patterns(matrix)
    lines.append("\n🎯 TOP STATISTICAL PATTERNS DISCOVERED:")
    lines.append("-" * 88)

    if patterns["sweet_spots"]:
        lines.append("\n⭐ POSITIVE-EV SWEET SPOTS (Highest Expected Return):")
        for p in patterns["sweet_spots"][:5]:
            lines.append(
                f"  • Entry @ {p['price_cents']:02d}¢ at :{p['minute']:02d}m ({p['side'].upper()}): "
                f"EV = +{p['hold_ev_cents']:.1f}¢/contract | Win Rate = {p['win_rate_pct']}% | ROI = +{p['roi_pct']:.1f}% (N={p['samples']})"
            )

    if patterns["favorite_recovery_zones"]:
        lines.append("\n🛡️ FAVORITE RECOVERY ZONES (Dipped Favorites Recovering to 100¢):")
        for p in patterns["favorite_recovery_zones"][:5]:
            lines.append(
                f"  • Favorite @ {p['price_cents']:02d}¢ at :{p['minute']:02d}m ({p['side'].upper()}): "
                f"Recovery Rate = {p['recovery_rate_pct']}% | Win Rate = {p['win_rate_pct']}% | EV = +{p['hold_ev_cents']:.1f}¢ (N={p['samples']})"
            )

    if patterns["negative_ev_traps"]:
        lines.append("\n🚫 NEGATIVE-EV TRAPS (Avoid Buying into These Zones):")
        for p in patterns["negative_ev_traps"][:5]:
            lines.append(
                f"  • Trap @ {p['price_cents']:02d}¢ at :{p['minute']:02d}m ({p['side'].upper()}): "
                f"EV = {p['hold_ev_cents']:.1f}¢/contract | Win Rate = {p['win_rate_pct']}% | Loss Risk High (N={p['samples']})"
            )

    lines.append("=" * 88)
    return "\n".join(lines)


def export_matrix_to_json(
    matrix: dict[tuple[int, int, str], PriceLadderCell],
    filepath: str,
) -> bool:
    """Exports the entire price ladder matrix to a JSON file."""
    try:
        data = {
            f"{p}_{m}_{side}": asdict(cell)
            for (p, m, side), cell in matrix.items()
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(f"Successfully exported price ladder matrix to {filepath}")
        return True
    except Exception as error:
        log.error(f"Failed to export matrix to JSON: {error}")
        return False


def export_matrix_to_csv(
    matrix: dict[tuple[int, int, str], PriceLadderCell],
    filepath: str,
) -> bool:
    """Exports the price ladder matrix to a CSV file."""
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "price_cents",
                "minute",
                "side",
                "sample_count",
                "fill_count",
                "fill_rate",
                "win_count",
                "win_rate",
                "hold_ev_cents",
                "hold_roi_pct",
                "total_hold_pnl_cents",
                "strategy_ev_cents",
                "total_strategy_pnl_cents",
                "avg_mfe_cents",
                "avg_mae_cents",
                "recovery_rate",
                "profit_factor",
            ])
            for (p, m, side), cell in sorted(matrix.items()):
                writer.writerow([
                    cell.price_cents,
                    cell.minute,
                    cell.side,
                    cell.sample_count,
                    cell.fill_count,
                    cell.fill_rate,
                    cell.win_count,
                    cell.win_rate,
                    cell.hold_ev_cents,
                    cell.hold_roi_pct,
                    cell.total_hold_pnl_cents,
                    cell.strategy_ev_cents,
                    cell.total_strategy_pnl_cents,
                    cell.avg_mfe_cents,
                    cell.avg_mae_cents,
                    cell.recovery_rate,
                    cell.profit_factor,
                ])
        log.info(f"Successfully exported price ladder matrix to {filepath}")
        return True
    except Exception as error:
        log.error(f"Failed to export matrix to CSV: {error}")
        return False
