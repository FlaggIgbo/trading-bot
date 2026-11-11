#!/usr/bin/env python3
"""
Backtest Engine for Kalshi 15-Minute Crypto Markets
===================================================
Replays historical settled markets from Kalshi API over a specified time window
(e.g. past 24 hours) without lookahead bias.
"""

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
os.chdir(ROOT_DIR)

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
import math
import random
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from db import MarketResolution, MarketResult, PositionSide, normalize_market_result
from ingestion.api.client import KalshiClient
from risk_engine.engine import (
    calculate_weighted_trend,
    extract_contract_price,
    should_stop_loss,
    strategy_last_21,
    strategy_last_9,
    strategy_random,
)
from risk_engine.settler import Settler

CRYPTO_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXBNB15M"]


def compute_kalshi_fee(price_cents: int) -> int:
    """
    Kalshi taker fee calculation for 1 binary contract.
    Formula: ceil(0.07 * P * (1 - P) * 100) cents, minimum 1 cent if traded.
    At 50c: ceil(0.07 * 0.50 * 0.50 * 100) = ceil(1.75) = 2 cents.
    """
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    fee = math.ceil(0.07 * p * (1.0 - p) * 100.0)
    return max(1, int(fee))


@dataclass
class BacktestTrade:
    ticker: str
    series_ticker: str
    strategy: str
    side: PositionSide
    entry_price_cents: int
    exit_price_cents: int
    exit_reason: str  # 'settlement', 'take_profit', 'stop_loss'
    won: bool
    gross_pnl_cents: int
    fees_cents: int
    net_pnl_cents: int
    open_time: str
    close_time: str


@dataclass
class StrategyMetrics:
    name: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl_dollars: float = 0.0
    fees_dollars: float = 0.0
    net_pnl_dollars: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_dollars: float = 0.0
    avg_trade_cents: float = 0.0
    tp_exits: int = 0
    sl_exits: int = 0
    settled_exits: int = 0


class HistoricalDataManager:
    def __init__(self, db_path: str = "backtest.db", client: Optional[KalshiClient] = None):
        self.db_path = db_path
        self.client = client or KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2")
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS historical_markets (
                ticker TEXT PRIMARY KEY,
                series_ticker TEXT NOT NULL,
                open_time TEXT NOT NULL,
                close_time TEXT NOT NULL,
                result TEXT NOT NULL,
                floor_strike REAL,
                expiration_value REAL,
                settlement_value_dollars REAL,
                entry_price_cents INTEGER,
                candlesticks_json TEXT,
                fetched_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def fetch_and_cache(self, series_list: List[str] = CRYPTO_SERIES, hours: int = 24, refresh: bool = False):
        """Fetch historical settled markets and 1-minute candlesticks from Kalshi API."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        print(f"[*] Ingesting historical settled markets for {len(series_list)} crypto series...")
        print(f"[*] Time window: Last {hours} hours (since {cutoff.strftime('%Y-%m-%d %H:%M:%S UTC')})")

        total_markets = 0
        total_cached = 0
        markets_to_fetch_candlesticks = []

        for series in series_list:
            # Fetch up to 200 markets to ensure the history buffer is pre-warmed for last_21
            markets = self.client.get_settled_markets(series, limit=200)
            if not markets:
                print(f"    [-] No settled markets returned for {series}")
                continue

            for m in markets:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                open_time = m.get("open_time")
                close_time = m.get("close_time")
                result = normalize_market_result(m.get("result", ""))
                if not open_time or not close_time or result not in ("yes", "no"):
                    continue

                total_markets += 1

                if not refresh:
                    c.execute("SELECT ticker, candlesticks_json FROM historical_markets WHERE ticker = ?", (ticker,))
                    row = c.fetchone()
                    if row and row[1]:
                        continue  # Already fully cached

                floor_strike = m.get("floor_strike")
                expiration_value = m.get("expiration_value")
                settlement_value = m.get("settlement_value_dollars")

                # Insert or update basic record
                c.execute("""
                    INSERT INTO historical_markets (
                        ticker, series_ticker, open_time, close_time, result,
                        floor_strike, expiration_value, settlement_value_dollars,
                        entry_price_cents, candlesticks_json, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticker) DO UPDATE SET
                        result=excluded.result,
                        floor_strike=excluded.floor_strike,
                        expiration_value=excluded.expiration_value,
                        settlement_value_dollars=excluded.settlement_value_dollars
                """, (
                    ticker,
                    series,
                    open_time,
                    close_time,
                    result,
                    float(floor_strike) if floor_strike is not None else None,
                    float(expiration_value) if expiration_value is not None else None,
                    float(settlement_value) if settlement_value is not None else None,
                    50,
                    None,
                    now.isoformat(),
                ))
                markets_to_fetch_candlesticks.append(m)
                total_cached += 1

        conn.commit()

        if markets_to_fetch_candlesticks:
            print(f"[*] Fetching 1-minute candlesticks for {len(markets_to_fetch_candlesticks)} markets in parallel...")

            def _fetch_cs(m_dict):
                ticker = m_dict["ticker"]
                series = ticker.split("-")[0]
                ot_str = m_dict["open_time"].replace("Z", "+00:00")
                ot_dt = datetime.fromisoformat(ot_str)
                start_ts = int(ot_dt.timestamp())
                end_ts = start_ts + 900
                cs = self.client.get_candlesticks(series, ticker, start_ts, end_ts, period_interval=1)
                return ticker, cs

            with ThreadPoolExecutor(max_workers=12) as ex:
                cs_results = list(ex.map(_fetch_cs, markets_to_fetch_candlesticks))

            for ticker, cs in cs_results:
                if cs:
                    c.execute(
                        "UPDATE historical_markets SET candlesticks_json = ? WHERE ticker = ?",
                        (json.dumps(cs), ticker),
                    )
            conn.commit()
            print(f"    [+] Cached {len(cs_results)} candlestick records.")

        conn.close()
        print(f"[+] Historical ingestion complete. Database at: {self.db_path}\n")

    def load_markets(self) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT ticker, series_ticker, open_time, close_time, result,
                   floor_strike, expiration_value, settlement_value_dollars,
                   entry_price_cents, candlesticks_json
            FROM historical_markets
            ORDER BY open_time ASC, close_time ASC
        """)
        rows = c.fetchall()
        markets = []
        for r in rows:
            m = dict(r)
            if m.get("candlesticks_json"):
                try:
                    m["candlesticks"] = json.loads(m["candlesticks_json"])
                except Exception:
                    m["candlesticks"] = []
            else:
                m["candlesticks"] = []
            markets.append(m)
        conn.close()
        return markets


class BacktestEngine:
    def __init__(self, markets: List[Dict[str, Any]], hours: int = 24):
        self.all_markets = sorted(
            markets,
            key=lambda m: (m["open_time"], m["close_time"])
        )
        self.hours = hours
        self.settler = Settler(client=None, db=None)

    def _extract_candlestick_ask(self, cs_list: List[Dict], minute: int, side: PositionSide) -> Optional[int]:
        """Extract contract ask price in cents for YES or NO at a specific minute."""
        if not cs_list or minute >= len(cs_list):
            return None
        bar = cs_list[minute]
        if side == PositionSide.YES:
            ya = bar.get("yes_ask", {})
            val = ya.get("close_dollars") or ya.get("mean_dollars")
            if val is not None:
                cents = int(round(float(val) * 100))
                if 1 <= cents <= 99:
                    return cents
        else:
            yb = bar.get("yes_bid", {})
            val = yb.get("close_dollars") or yb.get("mean_dollars")
            if val is not None:
                cents = int(round((1.0 - float(val)) * 100))
                if 1 <= cents <= 99:
                    return cents
        return None

    def _extract_candlestick_bid(self, cs_list: List[Dict], minute: int, side: PositionSide) -> Optional[int]:
        """Extract contract bid price (what you could sell for) in cents at a specific minute."""
        if not cs_list or minute >= len(cs_list):
            return None
        bar = cs_list[minute]
        if side == PositionSide.YES:
            yb = bar.get("yes_bid", {})
            val = yb.get("high_dollars") or yb.get("close_dollars")
            if val is not None:
                cents = int(round(float(val) * 100))
                if 1 <= cents <= 99:
                    return cents
        else:
            ya = bar.get("yes_ask", {})
            val = ya.get("low_dollars") or ya.get("close_dollars")
            if val is not None:
                cents = int(round((1.0 - float(val)) * 100))
                if 1 <= cents <= 99:
                    return cents
        return None

    def run_strategy(self, strategy_type: str, random_seed: Optional[int] = None) -> List[BacktestTrade]:
        """
        Replay chronologically.
        Enforces STRICT NO-LOOKAHEAD BIAS:
        A market M can ONLY see resolutions whose close_time <= M.open_time.
        """
        if random_seed is not None:
            random.seed(random_seed)

        now = datetime.now(timezone.utc)
        test_window_start = now - timedelta(hours=self.hours)

        history: Dict[str, List[Dict[str, Any]]] = {}
        trades: List[BacktestTrade] = []

        for m in self.all_markets:
            series = m["series_ticker"]
            ot = datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
            ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))

            # Build candidate past results strictly before this market opened
            series_history = history.setdefault(series, [])
            valid_past = [
                h for h in series_history
                if datetime.fromisoformat(h["close_time"].replace("Z", "+00:00")) <= ot
            ]

            # Only trade and record metrics if market is within the backtest window
            is_in_test_window = ct >= test_window_start

            side: Optional[PositionSide] = None
            entry_price: int = 50
            exit_price: int = 50
            exit_reason: str = "settlement"
            tp_threshold: Optional[int] = None
            has_stop_loss: bool = False
            cs = m.get("candlesticks", [])

            if strategy_type in ("regime_aware", "regime_aware_tp85_sl"):
                # Regime-Aware strategy (fades streaks >= 2, trending breakout, chop abstain) with TP85 and SL
                from risk_engine.regime import detect_market_regime
                regime_res = detect_market_regime(valid_past)
                if regime_res.should_trade:
                    cand_side = regime_res.recommended_side
                    real_ask = self._extract_candlestick_ask(cs, minute=0, side=cand_side) if cand_side else None
                    if real_ask is not None:
                        if 20 <= real_ask <= 65:
                            side = cand_side
                            entry_price = real_ask
                        else:
                            side = None
                    else:
                        side = cand_side
                        entry_price = 50
                tp_threshold = 85
                has_stop_loss = True

            elif strategy_type == "regime_aware_hold":
                # Regime-Aware pure hold to expiry (benchmark without TP or SL)
                from risk_engine.regime import detect_market_regime
                regime_res = detect_market_regime(valid_past)
                if regime_res.should_trade:
                    side = regime_res.recommended_side
                entry_price = 50

            elif strategy_type == "last_21_hold":
                # Magnitude-weighted trend over past 21 resolutions, pure hold to expiry
                if len(valid_past) >= 21:
                    trend_side, _, _ = calculate_weighted_trend(valid_past, limit=21)
                    side = trend_side or PositionSide.NO
                entry_price = 50

            elif strategy_type == "last_21_spot_tp85_sl":
                # last_21 trend with TP at 85c and Stop Loss
                if len(valid_past) >= 21:
                    trend_side, _, _ = calculate_weighted_trend(valid_past, limit=21)
                    side = trend_side or PositionSide.NO
                tp_threshold = 85
                has_stop_loss = True
                # Minute 0-5 realistic entry
                real_ask = self._extract_candlestick_ask(cs, minute=0, side=side) if side else None
                entry_price = real_ask if real_ask and 20 <= real_ask <= 65 else 50

            elif strategy_type == "last_21_sl":
                # last_21 trend with Stop Loss only, hold to expiry
                if len(valid_past) >= 21:
                    trend_side, _, _ = calculate_weighted_trend(valid_past, limit=21)
                    side = trend_side or PositionSide.NO
                has_stop_loss = True
                real_ask = self._extract_candlestick_ask(cs, minute=0, side=side) if side else None
                entry_price = real_ask if real_ask and 20 <= real_ask <= 65 else 50

            elif strategy_type == "sell_1.5n":
                # Asymmetric momentum entry at minute 5: buys whichever side is < 35c
                # resting limit sell order at 1.5x entry price
                yes_ask = self._extract_candlestick_ask(cs, minute=5, side=PositionSide.YES)
                no_ask = self._extract_candlestick_ask(cs, minute=5, side=PositionSide.NO)

                yes_valid = yes_ask is not None and 0 < yes_ask <= 35
                no_valid = no_ask is not None and 0 < no_ask <= 35

                if yes_valid and no_valid:
                    if yes_ask <= no_ask:
                        side = PositionSide.YES
                        entry_price = yes_ask
                    else:
                        side = PositionSide.NO
                        entry_price = no_ask
                elif yes_valid:
                    side = PositionSide.YES
                    entry_price = yes_ask
                elif no_valid:
                    side = PositionSide.NO
                    entry_price = no_ask
                else:
                    side = None  # abstain

                if side is not None:
                    tp_threshold = min(99, int(round(entry_price * 1.5)))

            elif strategy_type == "last_9_hold":
                # Magnitude-weighted trend over past 9 resolutions benchmark, hold to expiry
                if len(valid_past) >= 9:
                    trend_side, _, _ = calculate_weighted_trend(valid_past, limit=9)
                    side = trend_side or PositionSide.NO
                entry_price = 50

            elif strategy_type == "random":
                # Pure 50/50 baseline
                side = random.choice([PositionSide.YES, PositionSide.NO])
                entry_price = 50

            # Execute trade if side chosen and within test window
            if side is not None and is_in_test_window:
                won = False
                gross_pnl = 0
                fees = compute_kalshi_fee(entry_price)
                exited_early = False

                # Evaluate intraday exits (TP / SL) minute by minute
                if cs and (tp_threshold is not None or has_stop_loss):
                    start_min = 5 if strategy_type == "sell_1.5n" else 1
                    for minute in range(start_min, min(15, len(cs))):
                        cur_bid = self._extract_candlestick_bid(cs, minute=minute, side=side)
                        if cur_bid is None:
                            continue

                        # Check Take-Profit
                        if tp_threshold is not None and cur_bid >= tp_threshold:
                            exit_price = tp_threshold
                            exit_reason = "take_profit"
                            won = True
                            gross_pnl = exit_price - entry_price
                            fees += compute_kalshi_fee(exit_price)
                            exited_early = True
                            break

                        # Check Stop-Loss
                        if has_stop_loss and should_stop_loss(
                            entry_price_cents=entry_price,
                            current_price_cents=cur_bid,
                            minute=float(minute),
                            strategy=strategy_type,
                        ):
                            exit_price = cur_bid
                            exit_reason = "stop_loss"
                            won = (exit_price > entry_price)
                            gross_pnl = exit_price - entry_price
                            fees += compute_kalshi_fee(exit_price)
                            exited_early = True
                            break

                # If not exited early, hold to expiry settlement
                if not exited_early:
                    exit_reason = "settlement"
                    won = (side.value.lower() == str(m["result"]).strip().lower())
                    gross_pnl = (100 - entry_price) if won else -entry_price
                    exit_price = 100 if won else 0

                net_pnl = gross_pnl - fees

                trades.append(BacktestTrade(
                    ticker=m["ticker"],
                    series_ticker=series,
                    strategy=strategy_type,
                    side=side,
                    entry_price_cents=entry_price,
                    exit_price_cents=exit_price,
                    exit_reason=exit_reason,
                    won=won,
                    gross_pnl_cents=gross_pnl,
                    fees_cents=fees,
                    net_pnl_cents=net_pnl,
                    open_time=m["open_time"],
                    close_time=m["close_time"],
                ))

            # Reveal this market's result to FUTURE markets only AFTER it has closed
            series_history.append(m)

        return trades

    def evaluate_metrics(self, name: str, trades: List[BacktestTrade]) -> StrategyMetrics:
        if not trades:
            return StrategyMetrics(name=name)

        wins = sum(1 for t in trades if t.won)
        losses = len(trades) - wins
        win_rate = (wins / len(trades)) * 100.0 if trades else 0.0

        gross_pnl_cents = sum(t.gross_pnl_cents for t in trades)
        fees_cents = sum(t.fees_cents for t in trades)
        net_pnl_cents = sum(t.net_pnl_cents for t in trades)

        gross_gains = sum(t.gross_pnl_cents for t in trades if t.gross_pnl_cents > 0)
        gross_losses = abs(sum(t.gross_pnl_cents for t in trades if t.gross_pnl_cents < 0))
        profit_factor = (gross_gains / gross_losses) if gross_losses > 0 else (99.0 if gross_gains > 0 else 1.0)

        # Max drawdown
        cum = 0
        peak = 0
        max_dd = 0
        for t in trades:
            cum += t.net_pnl_cents
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd

        tp_cnt = sum(1 for t in trades if t.exit_reason == "take_profit")
        sl_cnt = sum(1 for t in trades if t.exit_reason == "stop_loss")
        settle_cnt = sum(1 for t in trades if t.exit_reason == "settlement")

        return StrategyMetrics(
            name=name,
            total_trades=len(trades),
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            gross_pnl_dollars=gross_pnl_cents / 100.0,
            fees_dollars=fees_cents / 100.0,
            net_pnl_dollars=net_pnl_cents / 100.0,
            profit_factor=profit_factor,
            max_drawdown_dollars=max_dd / 100.0,
            avg_trade_cents=net_pnl_cents / len(trades) if trades else 0.0,
            tp_exits=tp_cnt,
            sl_exits=sl_cnt,
            settled_exits=settle_cnt,
        )


def main():
    parser = argparse.ArgumentParser(description="Kalshi 24-Hour Crypto Market Strategy Backtest")
    parser.add_argument("--hours", type=int, default=24, help="Historical time window in hours (default: 24)")
    parser.add_argument("--db-path", type=str, default="backtest.db", help="Path to SQLite historical database")
    parser.add_argument("--refresh", action="store_true", help="Force refresh data from Kalshi API")
    parser.add_argument("--runs", type=int, default=50, help="Monte Carlo runs for random baseline")
    args = parser.parse_args()

    print("=" * 80)
    print(" KALSHI HISTORICAL BACKTEST ENGINE (LAST 24 HOURS)")
    print("=" * 80)

    # 1. Ingest / Cache Historical Markets
    data_mgr = HistoricalDataManager(db_path=args.db_path)
    data_mgr.fetch_and_cache(hours=args.hours, refresh=args.refresh)

    markets = data_mgr.load_markets()
    print(f"[*] Loaded {len(markets)} total historical markets from database.")

    engine = BacktestEngine(markets, hours=args.hours)

    # 2. Run Backtests
    strategies = [
        ("regime_aware (TP85c, SL)", "regime_aware"),
        ("regime_aware (Hold to Exp)", "regime_aware_hold"),
        ("last_21_spot (TP85c, SL)", "last_21_spot_tp85_sl"),
        ("last_21 (SL)", "last_21_sl"),
        ("last_21 (Hold to Exp)", "last_21_hold"),
        ("sell_1.5n (5m Entry)", "sell_1.5n"),
        ("last_9 (Hold to Exp)", "last_9_hold"),
    ]

    results: List[StrategyMetrics] = []
    trade_maps: Dict[str, List[BacktestTrade]] = {}

    for label, strat_id in strategies:
        trades = engine.run_strategy(strat_id)
        metrics = engine.evaluate_metrics(label, trades)
        results.append(metrics)
        trade_maps[label] = trades

    # 3. Random Baseline (Monte Carlo over N runs)
    print(f"[*] Running {args.runs} Monte Carlo iterations for Random Baseline...")
    rand_trades_all = []
    rand_metrics_list = []
    for i in range(args.runs):
        t = engine.run_strategy("random", random_seed=i + 42)
        m = engine.evaluate_metrics(f"random_run_{i}", t)
        rand_metrics_list.append(m)
        if i == 0:
            rand_trades_all = t

    avg_random = StrategyMetrics(
        name="random (baseline, avg)",
        total_trades=int(sum(m.total_trades for m in rand_metrics_list) / len(rand_metrics_list)),
        wins=int(sum(m.wins for m in rand_metrics_list) / len(rand_metrics_list)),
        losses=int(sum(m.losses for m in rand_metrics_list) / len(rand_metrics_list)),
        win_rate=sum(m.win_rate for m in rand_metrics_list) / len(rand_metrics_list),
        gross_pnl_dollars=sum(m.gross_pnl_dollars for m in rand_metrics_list) / len(rand_metrics_list),
        fees_dollars=sum(m.fees_dollars for m in rand_metrics_list) / len(rand_metrics_list),
        net_pnl_dollars=sum(m.net_pnl_dollars for m in rand_metrics_list) / len(rand_metrics_list),
        profit_factor=sum(m.profit_factor for m in rand_metrics_list) / len(rand_metrics_list),
        max_drawdown_dollars=sum(m.max_drawdown_dollars for m in rand_metrics_list) / len(rand_metrics_list),
        avg_trade_cents=sum(m.avg_trade_cents for m in rand_metrics_list) / len(rand_metrics_list),
    )
    results.append(avg_random)

    # 4. Display Results Table
    print("\n" + "=" * 105)
    print(f"{'Strategy':<26} | {'Trades':<6} | {'W/L':<8} | {'WinRate':<7} | {'Gross PnL':<10} | {'Fees':<7} | {'Net PnL':<10} | {'PF':<5} | {'MaxDD':<7}")
    print("-" * 105)
    for m in results:
        wl = f"{m.wins}/{m.losses}"
        pnl_str = f"${m.net_pnl_dollars:+6.2f}"
        gross_str = f"${m.gross_pnl_dollars:+6.2f}"
        fees_str = f"${m.fees_dollars:5.2f}"
        dd_str = f"${m.max_drawdown_dollars:5.2f}"
        print(f"{m.name:<26} | {m.total_trades:<6} | {wl:<8} | {m.win_rate:>6.1f}% | {gross_str:>10} | {fees_str:>7} | {pnl_str:>10} | {m.profit_factor:>5.2f} | {dd_str:>7}")
    print("=" * 105)

    # 5. Asset Breakdown for Key Strategies
    print("\n[*] Asset Breakdown (Net PnL by Series):")
    series_keys = CRYPTO_SERIES
    header = f"{'Series':<10} | " + " | ".join(f"{s[0][:14]:<14}" for s in strategies[:5])
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    for s in series_keys:
        row = [f"{s:<10}"]
        for label, _ in strategies[:5]:
            t_list = [t for t in trade_maps[label] if t.series_ticker == s]
            net = sum(t.net_pnl_cents for t in t_list) / 100.0
            row.append(f"${net:+6.2f}".center(14))
        print(" | ".join(row))
    print("-" * len(header))

    # 6. Exit Distribution Analysis
    print("\n[*] Exit Reason Breakdown:")
    for label, _ in strategies[:5]:
        t_list = trade_maps[label]
        tp_cnt = sum(1 for t in t_list if t.exit_reason == "take_profit")
        sl_cnt = sum(1 for t in t_list if t.exit_reason == "stop_loss")
        settle_cnt = sum(1 for t in t_list if t.exit_reason == "settlement")
        print(f"  - {label:<26}: TP Exits={tp_cnt:<3} | SL Exits={sl_cnt:<3} | Settled to Expiry={settle_cnt:<3}")
    print()


if __name__ == "__main__":
    main()
