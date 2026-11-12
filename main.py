"""
Main Trading Bot (One Process, Aligned 15-Minute Cycle)
=======================================================
System design:
1. Wait until the next :00:30, :15:30, :30:30, or :45:30 mark on the clock.
2. Scanner: Fetch + filter open binary 15-minute markets from Kalshi -> upsert into SQLite.
3. Settler: Read OPEN positions where close_time < now -> GET resolution from Kalshi -> write SETTLED + PnL to SQLite.
4. Trader: Read candidates + open positions from SQLite -> place 1-contract bet -> write OPEN to SQLite.
"""

import argparse
import os
import time
from dotenv import load_dotenv

from db import Database
from ingestion.api.client import KalshiClient
from ingestion.scanner import Scanner
from logging.analytics import analytics
from risk_engine.broker import KalshiBroker, SimulatedBroker
from risk_engine.engine import Config, TradeEngine, Trader
from risk_engine.reconciler import Reconciler
from risk_engine.settler import Settler

# Load environment variables from .env file
load_dotenv()


def seconds_until_next_slot(now=None, interval=900, offset=30):
    # Every 15 minutes (900s) is :00, :15, :30, :45.
    # Adding offset=30 targets :00:30, :15:30, :30:30, and :45:30.
    if now is None:
        now = time.time()
    elapsed = (now - offset) % interval
    wait = interval - elapsed
    return 0.0 if wait == interval else wait


def main(
    single_cycle=False,
    sleep_seconds=900,
    offset_seconds=30,
    db_path="trading_bot.db",
    strategy="last_9",
    env="demo",
    live=False,
    stop_loss=False,
    check_spot=False,
    min_entry_price=None,
    max_entry_price=None,
    take_profit=None,
    shadow_mode=False,
    max_open_positions=10,
    daily_loss_cap_cents=5000,
    kill_switch_path="KILL_SWITCH",
    entry_timeout=None,
    regime_filter=False,
    taker_ioc=False,
):
    analytics.info(
        f"Starting trading bot (strategy={strategy}, env={env}, live={live}, stop_loss={stop_loss}, check_spot={check_spot}, take_profit={take_profit}, shadow={shadow_mode}, regime_filter={regime_filter}, taker_ioc={taker_ioc}, schedule=every {sleep_seconds}s at +{offset_seconds}s)...",
        event="bot_start",
        strategy=strategy,
        env=env,
        live=live,
        stop_loss=stop_loss,
        check_spot=check_spot,
        take_profit=take_profit,
        shadow=shadow_mode,
        regime_filter=regime_filter,
        taker_ioc=taker_ioc,
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        max_open_positions=max_open_positions,
        daily_loss_cap_cents=daily_loss_cap_cents,
        sleep_seconds=sleep_seconds,
        offset_seconds=offset_seconds,
    )

    # Support DB_PATH env var for persistent disk mount on Render (/var/data/trading_bot.db)
    effective_db_path = os.getenv("DB_PATH", db_path)

    # Initialize Kalshi REST API client and SQLite database (tagged by environment)
    base_url = "https://api.elections.kalshi.com/trade-api/v2" if str(env).lower() in ("prod", "production") else "https://demo-api.kalshi.co/trade-api/v2"
    client = KalshiClient(base_url=base_url, live_orders=live)
    db = Database(db_path=effective_db_path, environment=client.environment)
    analytics.info(
        f"Environment: {client.environment} ({client.base_url}) [DB: {effective_db_path}]",
        event="environment_init",
        environment=client.environment,
        base_url=client.base_url,
        db_path=effective_db_path,
    )

    # Pick broker once: KalshiBroker for live exchange orders, SimulatedBroker for simulation
    broker = KalshiBroker(client=client) if live else SimulatedBroker(client=client)

    # Startup reconciliation: check any PENDING positions from previous runs/crashes
    reconciler = Reconciler(broker=broker, db=db)
    reconciled = reconciler.reconcile()
    if reconciled:
        analytics.info(
            f"Startup reconciler resolved {len(reconciled)} pending positions.",
            event="startup_reconciliation",
            count=len(reconciled),
        )

    # Build injected Config
    config = Config(
        env=client.environment.value,
        max_open_positions=max_open_positions,
        daily_loss_cap_cents=daily_loss_cap_cents,
        kill_switch_path=kill_switch_path,
        shadow_mode=shadow_mode,
        stop_loss=stop_loss,
        check_spot=check_spot,
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        live_orders=live,
        take_profit=take_profit,
        regime_filter=regime_filter,
        taker_ioc=taker_ioc,
    )

    # Initialize pipeline steps with injected dependencies
    scanner = Scanner(client=client, db=db)
    settler = Settler(client=client, db=db)
    trader = Trader(
        client=client,
        db=db,
        strategy=strategy,
        stop_loss=stop_loss,
        check_spot=check_spot,
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        take_profit=take_profit,
        broker=broker,
        config=config,
        entry_timeout_seconds=entry_timeout,
    )

    # Main loop aligned to :00:30, :15:30, :30:30, :45:30
    try:
        while True:
            # Check local kill switch every cycle
            if (kill_switch_path and os.path.exists(kill_switch_path)) or os.getenv("KILL_SWITCH", "").lower() in ("1", "true", "yes"):
                analytics.warning(
                    "Kill switch is ACTIVE! Pausing cycle for 10 seconds.",
                    event="kill_switch_active",
                )
                time.sleep(10)
                continue

            if not single_cycle and sleep_seconds > 0:
                wait_sec = seconds_until_next_slot(interval=sleep_seconds, offset=offset_seconds)
                analytics.info(
                    f"Waiting {wait_sec:.1f}s until next :{offset_seconds:02d}s slot...",
                    event="wait_slot",
                    wait_seconds=round(wait_sec, 1),
                )
                time.sleep(wait_sec)

            # 1. Scanner: fetch + filter open 15-minute binary markets and save into SQLite
            markets = scanner.scan()
            analytics.track(
                "markets_scanned",
                message=f"1. Scanner: Fetched and upserted {len(markets)} open binary markets.",
                count=len(markets),
            )

            # 2. Settler: settle the previous 15-minute positions that just closed
            settled = settler.settle()
            analytics.track(
                "positions_settled",
                message=f"2. Settler: Settled {len(settled)} expired positions.",
                count=len(settled),
            )

            # 3. Trader: place 1-contract bet on each new 15-minute market and let it ride
            orders = trader.trade()
            analytics.track(
                "orders_placed",
                message=f"3. Trader: Placed {len(orders)} new orders.",
                count=len(orders),
            )

            # If user passed --once, exit after one cycle
            if single_cycle:
                break

            # Pause 1s so we move past the current :30s boundary before computing the next slot
            time.sleep(1)

    except KeyboardInterrupt:
        analytics.info("Stopping bot. Bye!", event="bot_stop")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple 15-minute prediction market trading bot")
    parser.add_argument("--once", action="store_true", help="Run once immediately and stop")
    parser.add_argument("--interval", type=int, default=900, help="Window length in seconds (default: 900 = 15 mins)")
    parser.add_argument("--offset", type=int, default=30, help="Seconds past each window start (default: 30)")
    parser.add_argument("--db", type=str, default="trading_bot.db", help="Path to SQLite database file")
    parser.add_argument(
        "--env",
        type=str,
        default="demo",
        choices=["demo", "prod", "production"],
        help="Environment to target ('demo' for demo.kalshi.co, 'prod'/'production' for kalshi.com)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="last_9",
        choices=[
            "trend",
            "fade",
            "cheap2x",
            "cheap1.5x",
            "regime_aware",
            "regime",
            "regime_spot",
            "regimespot",
            "last_21",
            "last_21_spot",
            "last_9",
            "last_9_spot",
            "random",
            "sell_3n",
            "sell_2n",
            "sell_2n_5m",
            "sell_2n_5m_35p",
            "sell_2_n_5m_35p",
            "sell_1.5n",
            "sell_1_5n",
            "sell_1.5n_5m",
            "sell_1_5n_5m",
            "sell_1.5n_5m_35p",
            "sell_1_5n_5m_35p",
            "sell_1.5n_35p",
            "sell_1_5n_35p",
            "sell_2.5n",
            "sell_2_5n",
        ],
        help="Strategy to run: core signals ('trend', 'fade', 'cheap2x', 'cheap1.5x', 'random') or parameterized variants ('last_21_spot', 'last_9', etc.)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live order execution against Kalshi API (requires active credentials)",
    )
    parser.add_argument(
        "--stop-loss",
        action="store_true",
        help="Enable minute-10 stop loss for sell_Xn_5m strategies (exits if price <= 25c or < 50%% entry)",
    )
    parser.add_argument(
        "--check-spot",
        action="store_true",
        help="Enable real-time spot price trend alignment (Coinbase/CF Benchmarks) for last_9",
    )
    parser.add_argument(
        "--regime-filter",
        action="store_true",
        help="Enable market regime detection & volatility gate filter",
    )
    parser.add_argument(
        "--taker-ioc",
        action="store_true",
        help="Use Immediate-Or-Cancel (IOC) taker market orders crossing spread when edge is high",
    )
    parser.add_argument(
        "--min-price",
        type=int,
        default=None,
        help="Minimum contract ask price in cents to enter (avoids low-probability longshots)",
    )
    parser.add_argument(
        "--max-price",
        type=int,
        default=None,
        help="Maximum contract ask price in cents to enter (avoids poor risk/reward)",
    )
    parser.add_argument(
        "--take-profit",
        type=int,
        default=None,
        help="Take profit limit price in cents (e.g. 85 to place resting sell order at 85c)",
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Run strategy in shadow mode (evaluate and log decisions, but do not place orders)",
    )
    parser.add_argument(
        "--max-open",
        type=int,
        default=10,
        help="Bankroll limit: maximum simultaneous open positions allowed (default: 10)",
    )
    parser.add_argument(
        "--daily-loss-cap",
        type=int,
        default=5000,
        help="Bankroll limit: daily loss cap in cents (default: 5000 = $50)",
    )
    parser.add_argument(
        "--kill-switch",
        type=str,
        default="KILL_SWITCH",
        help="File path to check for local kill switch trigger (default: KILL_SWITCH)",
    )
    parser.add_argument(
        "--entry-timeout",
        type=float,
        default=None,
        help="Timeout in seconds for resting entry orders before canceling (default: 60s for last_N, 2.5s for others)",
    )
    args = parser.parse_args()

    main(
        single_cycle=args.once,
        sleep_seconds=args.interval,
        offset_seconds=args.offset,
        db_path=args.db,
        strategy=args.strategy,
        env=args.env,
        live=args.live,
        stop_loss=args.stop_loss,
        check_spot=args.check_spot,
        min_entry_price=args.min_price,
        max_entry_price=args.max_price,
        take_profit=args.take_profit,
        shadow_mode=args.shadow,
        max_open_positions=args.max_open,
        daily_loss_cap_cents=args.daily_loss_cap,
        kill_switch_path=args.kill_switch,
        entry_timeout=args.entry_timeout,
        regime_filter=args.regime_filter,
        taker_ioc=args.taker_ioc,
    )
