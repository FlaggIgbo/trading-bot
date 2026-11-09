"""
SQLite Database Layer
=====================
Stores markets and orders/positions for the trading bot, differentiated by environment
('demo.kalshi.co' vs 'production') and typed with StrEnums for categorical columns.
"""

from enum import StrEnum
import os
import sqlite3
from typing import Any, Optional


class Environment(StrEnum):
    DEMO = "demo.kalshi.co"
    PRODUCTION = "production"


class MarketType(StrEnum):
    BINARY = "binary"
    SCALAR = "scalar"


class MarketStatus(StrEnum):
    INITIALIZED = "initialized"
    UNOPENED = "unopened"
    OPEN = "open"
    ACTIVE = "active"
    CLOSED = "closed"
    DETERMINED = "determined"
    FINALIZED = "finalized"
    SETTLED = "settled"
    INACTIVE = "inactive"


class MarketResult(StrEnum):
    PENDING = ""
    YES = "yes"
    NO = "no"
    VOID = "void"


class PositionSide(StrEnum):
    YES = "YES"
    NO = "NO"


OrderSide = PositionSide


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    SETTLED = "SETTLED"


class Strategy(StrEnum):
    RANDOM = "random"
    LAST_3 = "last_3"
    LAST_5 = "last_5"
    LAST_9 = "last_9"
    SELL_3N = "sell_3n"
    SELL_2N = "sell_2n"
    SELL_2N_5M = "sell_2n_5m"
    SELL_2N_5M_35P = "sell_2n_5m_35p"
    SELL_1_5N = "sell_1.5n"
    SELL_1_5N_5M = "sell_1.5n_5m"
    SELL_1_5N_5M_35P = "sell_1.5n_5m_35p"
    SELL_1_5N_35P = "sell_1.5n_35p"
    SELL_2_5N = "sell_2.5n"


def normalize_environment(environment: Optional[str] = None) -> Environment:
    # Normalize environment identifier to Environment.DEMO ('demo.kalshi.co') or Environment.PRODUCTION ('production')
    if isinstance(environment, Environment):
        return environment

    if not environment:
        use_demo = os.getenv("KALSHI_USE_DEMO", "true").lower() in ("true", "1", "yes")
        base_url = os.getenv("KALSHI_BASE_URL", "")
        if use_demo or "demo" in base_url.lower():
            return Environment.DEMO
        return Environment.PRODUCTION

    env_lower = str(environment).strip().lower()
    if "demo" in env_lower:
        return Environment.DEMO
    if env_lower in ("prod", "production", "kalshi.com", "live") or "kalshi.com" in env_lower:
        return Environment.PRODUCTION
    return Environment(env_lower)


def normalize_market_type(market_type: Optional[str] = None) -> MarketType:
    if isinstance(market_type, MarketType):
        return market_type
    if not market_type:
        return MarketType.BINARY
    return MarketType(str(market_type).strip().lower())


def normalize_market_status(status: Optional[str] = None) -> MarketStatus:
    if isinstance(status, MarketStatus):
        return status
    if not status:
        return MarketStatus.OPEN
    return MarketStatus(str(status).strip().lower())


def normalize_market_result(result: Optional[str] = None) -> MarketResult:
    if isinstance(result, MarketResult):
        return result
    if not result:
        return MarketResult.PENDING
    return MarketResult(str(result).strip().lower())


def normalize_position_side(side: str) -> PositionSide:
    if isinstance(side, PositionSide):
        return side
    cleaned = str(side).strip().upper()
    if cleaned in ("YES", "BUY_YES"):
        return PositionSide.YES
    if cleaned in ("NO", "BUY_NO"):
        return PositionSide.NO
    return PositionSide(cleaned)


def normalize_position_status(status: Optional[str] = None) -> PositionStatus:
    if isinstance(status, PositionStatus):
        return status
    if not status:
        return PositionStatus.OPEN
    return PositionStatus(str(status).strip().upper())


def normalize_strategy(strategy: Optional[str] = None) -> Strategy:
    if isinstance(strategy, Strategy):
        return strategy
    if not strategy:
        return Strategy.LAST_9
    strat_clean = str(strategy).strip().lower()
    if strat_clean in ("last_5", "last5", "last_3", "last3"):
        return Strategy.LAST_9
    if strat_clean in ("sell_if_3n", "sell3n"):
        return Strategy.SELL_3N
    if strat_clean in ("sell_if_2n", "sell2n"):
        return Strategy.SELL_2N
    if strat_clean in ("sell_2n_5m", "sell_5m_2n", "sell_2x_5m", "sell_3n_2x"):
        return Strategy.SELL_2N_5M
    if strat_clean in ("sell_1.5n_5m_35p", "sell_1_5n_5m_35p", "sell1.5n_5m_35p", "sell1_5n_5m_35p"):
        return Strategy.SELL_1_5N_5M_35P
    if strat_clean in ("sell_1.5n_35p", "sell_1_5n_35p", "sell1.5n_35p", "sell1_5n_35p"):
        return Strategy.SELL_1_5N_35P
    if strat_clean in ("sell_1.5n_5m", "sell_1_5n_5m", "sell1.5n_5m", "sell1_5n_5m"):
        return Strategy.SELL_1_5N_5M
    if strat_clean in ("sell_1.5n", "sell_1_5n", "sell1.5n", "sell1_5n"):
        return Strategy.SELL_1_5N
    if strat_clean in ("sell_2.5n", "sell_2_5n", "sell2.5n", "sell2_5n", "sell_2.5n_5m", "sell_2_5n_5m"):
        return Strategy.SELL_2_5N
    return Strategy(strat_clean)


class Database:
    def __init__(self, db_path: str = "trading_bot.db", environment: Optional[str] = None) -> None:
        # Connect to SQLite file (or ":memory:" for tests)
        self.db_path: str = db_path
        self.environment: Environment = normalize_environment(environment)
        self.conn: sqlite3.Connection = sqlite3.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.create_tables()

    def create_tables(self) -> None:
        # Create markets and positions tables if they do not exist
        cursor = self.conn.cursor()

        env_values = ", ".join(f"'{e.value}'" for e in Environment)
        mtype_values = ", ".join(f"'{t.value}'" for t in MarketType)
        mstatus_values = ", ".join(f"'{s.value}'" for s in MarketStatus)
        mresult_values = ", ".join(f"'{r.value}'" for r in MarketResult)
        pside_values = ", ".join(f"'{s.value}'" for s in PositionSide)
        pstatus_values = ", ".join(f"'{s.value}'" for s in PositionStatus)
        strat_values = ", ".join(f"'{s.value}'" for s in Strategy)

        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS markets (
                ticker TEXT,
                environment TEXT NOT NULL DEFAULT '{Environment.DEMO.value}' CHECK (environment IN ({env_values})),
                event_ticker TEXT,
                market_type TEXT NOT NULL DEFAULT '{MarketType.BINARY.value}' CHECK (market_type IN ({mtype_values})),
                status TEXT NOT NULL DEFAULT '{MarketStatus.OPEN.value}' CHECK (status IN ({mstatus_values})),
                close_time TEXT,
                yes_ask INTEGER,
                no_ask INTEGER,
                result TEXT NOT NULL DEFAULT '{MarketResult.PENDING.value}' CHECK (result IN ({mresult_values})),
                PRIMARY KEY (ticker, environment)
            )
            """
        )

        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS positions (
                client_order_id TEXT PRIMARY KEY,
                ticker TEXT,
                environment TEXT NOT NULL DEFAULT '{Environment.DEMO.value}' CHECK (environment IN ({env_values})),
                strategy TEXT NOT NULL DEFAULT '{Strategy.LAST_9.value}' CHECK (strategy IN ({strat_values})),
                side TEXT NOT NULL CHECK (side IN ({pside_values})),
                count INTEGER,
                price_cents INTEGER,
                status TEXT NOT NULL DEFAULT '{PositionStatus.OPEN.value}' CHECK (status IN ({pstatus_values})),
                close_time TEXT,
                pnl_cents INTEGER,
                resting_order_id TEXT
            )
            """
        )

        # Ensure environment, strategy, and resting_order_id columns exist if migrating an older SQLite database file
        for table in ("markets", "positions"):
            cursor.execute(f"PRAGMA table_info({table})")
            cols = {row["name"] for row in cursor.fetchall()}
            if "environment" not in cols:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN environment TEXT NOT NULL DEFAULT '{Environment.DEMO.value}'"
                )
            if table == "positions" and "strategy" not in cols:
                cursor.execute(
                    f"ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT '{Strategy.RANDOM.value}'"
                )
            if table == "positions" and "resting_order_id" not in cols:
                cursor.execute(
                    "ALTER TABLE positions ADD COLUMN resting_order_id TEXT"
                )

        self.conn.commit()

    def _hydrate_market_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["environment"] = normalize_environment(data.get("environment"))
        data["market_type"] = normalize_market_type(data.get("market_type"))
        data["status"] = normalize_market_status(data.get("status"))
        data["result"] = normalize_market_result(data.get("result"))
        return data

    def _hydrate_position_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["environment"] = normalize_environment(data.get("environment"))
        if "strategy" in data and data["strategy"]:
            data["strategy"] = normalize_strategy(data.get("strategy"))
        data["side"] = normalize_position_side(data.get("side", PositionSide.YES))
        data["status"] = normalize_position_status(data.get("status"))
        return data

    def _extract_cents(self, market, cents_key, dollars_key):
        # Helper to read integer cents or convert dollar string to cents.
        # In Kalshi, <= 0 or >= 100 means there is no resting order on the book -> default to 50 cents.
        cents = None
        if market.get(cents_key) is not None:
            cents = int(market[cents_key])
        elif market.get(dollars_key) is not None:
            cents = int(round(float(market[dollars_key]) * 100))

        if cents is None or cents <= 0 or cents >= 100:
            return 50
        return cents

    def upsert_markets(self, markets, environment=None):
        # Insert or update fetched markets in SQLite for the target environment using enums.
        # Keeps the initial opening yes_ask / no_ask instead of overwriting them on later polls.
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue

            market_type = normalize_market_type(m.get("market_type", MarketType.BINARY))
            status = normalize_market_status(m.get("status", MarketStatus.OPEN))
            result = normalize_market_result(m.get("result", MarketResult.PENDING))
            yes_ask = self._extract_cents(m, "yes_ask", "yes_ask_dollars")
            no_ask = self._extract_cents(m, "no_ask", "no_ask_dollars")

            cursor.execute(
                """
                INSERT INTO markets (
                    ticker, environment, event_ticker, market_type, status, close_time, yes_ask, no_ask, result
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, environment) DO UPDATE SET
                    market_type = excluded.market_type,
                    status = excluded.status,
                    close_time = excluded.close_time,
                    yes_ask = CASE WHEN markets.yes_ask <= 0 OR markets.yes_ask >= 100 THEN excluded.yes_ask ELSE markets.yes_ask END,
                    no_ask = CASE WHEN markets.no_ask <= 0 OR markets.no_ask >= 100 THEN excluded.no_ask ELSE markets.no_ask END,
                    result = excluded.result
                """,
                (
                    ticker,
                    env.value,
                    m.get("event_ticker", ""),
                    market_type.value,
                    status.value,
                    m.get("close_time", ""),
                    yes_ask,
                    no_ask,
                    result.value,
                ),
            )
        self.conn.commit()

    def get_open_markets(self, environment=None):
        # Return all open/active candidate markets from SQLite for the target environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM markets WHERE status IN (?, ?) AND environment = ?",
            (MarketStatus.OPEN.value, MarketStatus.ACTIVE.value, env.value),
        )
        return [self._hydrate_market_row(row) for row in cursor.fetchall()]

    def get_recent_results(self, series_ticker: str, limit: int = 3, environment=None) -> list[MarketResult]:
        # Return the last N resolved results ('yes' or 'no') for a market series (e.g. 'KXBTC15M')
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT result FROM markets
            WHERE ticker LIKE ? AND environment = ? AND result IN (?, ?)
            ORDER BY close_time DESC, ticker DESC
            LIMIT ?
            """,
            (f"{series_ticker}-%", env.value, MarketResult.YES.value, MarketResult.NO.value, int(limit)),
        )
        return [normalize_market_result(row["result"]) for row in cursor.fetchall()]

    def get_positioned_tickers(self, environment=None, strategy=None):
        # Return set of tickers where we already placed an order (OPEN or SETTLED) in this environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        if strategy is not None:
            strat = normalize_strategy(strategy)
            cursor.execute(
                "SELECT ticker FROM positions WHERE environment = ? AND strategy = ?",
                (env.value, strat.value),
            )
        else:
            cursor.execute("SELECT ticker FROM positions WHERE environment = ?", (env.value,))
        return {row["ticker"] for row in cursor.fetchall()}

    def insert_open_position(
        self,
        client_order_id,
        ticker,
        side,
        count,
        price_cents,
        close_time="",
        environment=None,
        status: PositionStatus = PositionStatus.OPEN,
        strategy: Strategy = Strategy.LAST_5,
        resting_order_id: Optional[str] = None,
    ):
        # Write a new OPEN order/position into SQLite for the target environment using enums
        env = normalize_environment(environment or self.environment)
        pos_side = normalize_position_side(side)
        pos_status = normalize_position_status(status)
        pos_strategy = normalize_strategy(strategy)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO positions (
                client_order_id, ticker, environment, strategy, side, count, price_cents, status, close_time, pnl_cents, resting_order_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                client_order_id,
                ticker,
                env.value,
                pos_strategy.value,
                pos_side.value,
                int(count),
                int(price_cents),
                pos_status.value,
                close_time or "",
                resting_order_id or "",
            ),
        )
        self.conn.commit()

    def get_positions_to_settle(self, now_iso, environment=None):
        # Return OPEN positions in this environment where close_time < now (or close_time is empty)
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM positions
            WHERE status = ? AND environment = ? AND (close_time = '' OR close_time < ?)
            """,
            (PositionStatus.OPEN.value, env.value, now_iso),
        )
        return [self._hydrate_position_row(row) for row in cursor.fetchall()]

    def settle_position(self, client_order_id, pnl_cents, result="", environment=None):
        # Mark a position as SETTLED and record its PnL in cents for the target environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE positions
            SET status = ?, pnl_cents = ?
            WHERE client_order_id = ? AND environment = ?
            """,
            (PositionStatus.SETTLED.value, int(pnl_cents), client_order_id, env.value),
        )
        if result:
            market_result = normalize_market_result(result)
            cursor.execute(
                "UPDATE markets SET status = ?, result = ? WHERE ticker = ("
                "SELECT ticker FROM positions WHERE client_order_id = ? AND environment = ?"
                ") AND environment = ?",
                (
                    MarketStatus.FINALIZED.value,
                    market_result.value,
                    client_order_id,
                    env.value,
                    env.value,
                ),
            )
        self.conn.commit()

    def get_all_positions(self, environment=None, strategy=None):
        # Return positions as dicts for the target environment (or all if environment='*')
        cursor = self.conn.cursor()
        if environment == "*":
            cursor.execute("SELECT * FROM positions")
        elif strategy is not None:
            env = normalize_environment(environment or self.environment)
            strat = normalize_strategy(strategy)
            cursor.execute(
                "SELECT * FROM positions WHERE environment = ? AND strategy = ?",
                (env.value, strat.value),
            )
        else:
            env = normalize_environment(environment or self.environment)
            cursor.execute("SELECT * FROM positions WHERE environment = ?", (env.value,))
        return [self._hydrate_position_row(row) for row in cursor.fetchall()]

    def close(self):
        # Close database connection
        self.conn.close()
