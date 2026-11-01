"""
SQLite Database Layer
=====================
Stores markets and orders/positions for the trading bot, differentiated by environment
('demo.kalshi.co' vs 'production').
"""

import os
import sqlite3


def normalize_environment(environment=None):
    # Normalize environment identifier to 'demo.kalshi.co' or 'production'
    if not environment:
        use_demo = os.getenv("KALSHI_USE_DEMO", "true").lower() in ("true", "1", "yes")
        base_url = os.getenv("KALSHI_BASE_URL", "")
        if use_demo or "demo" in base_url.lower():
            return "demo.kalshi.co"
        return "production"

    env_lower = str(environment).strip().lower()
    if "demo" in env_lower:
        return "demo.kalshi.co"
    if env_lower in ("prod", "production", "kalshi.com", "live") or "kalshi.com" in env_lower:
        return "production"
    return env_lower


class Database:
    def __init__(self, db_path="trading_bot.db", environment=None):
        # Connect to SQLite file (or ":memory:" for tests)
        self.db_path = db_path
        self.environment = normalize_environment(environment)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        # Create markets and positions tables if they do not exist
        cursor = self.conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS markets (
                ticker TEXT,
                environment TEXT NOT NULL DEFAULT 'demo.kalshi.co',
                event_ticker TEXT,
                market_type TEXT,
                status TEXT,
                close_time TEXT,
                yes_ask INTEGER,
                no_ask INTEGER,
                result TEXT,
                PRIMARY KEY (ticker, environment)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                client_order_id TEXT PRIMARY KEY,
                ticker TEXT,
                environment TEXT NOT NULL DEFAULT 'demo.kalshi.co',
                side TEXT,
                count INTEGER,
                price_cents INTEGER,
                status TEXT,
                close_time TEXT,
                pnl_cents INTEGER
            )
            """
        )

        # Ensure environment column exists if migrating an older SQLite database file
        for table in ("markets", "positions"):
            cursor.execute(f"PRAGMA table_info({table})")
            cols = {row["name"] for row in cursor.fetchall()}
            if "environment" not in cols:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN environment TEXT NOT NULL DEFAULT 'demo.kalshi.co'"
                )

        self.conn.commit()

    def _extract_cents(self, market, cents_key, dollars_key):
        # Helper to read integer cents or convert dollar string to cents
        if market.get(cents_key) is not None:
            return int(market[cents_key])
        if market.get(dollars_key) is not None:
            return int(round(float(market[dollars_key]) * 100))
        return 50

    def upsert_markets(self, markets, environment=None):
        # Insert or update fetched markets in SQLite for the target environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue

            yes_ask = self._extract_cents(m, "yes_ask", "yes_ask_dollars")
            no_ask = self._extract_cents(m, "no_ask", "no_ask_dollars")

            cursor.execute(
                """
                INSERT INTO markets (
                    ticker, environment, event_ticker, market_type, status, close_time, yes_ask, no_ask, result
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, environment) DO UPDATE SET
                    status = excluded.status,
                    close_time = excluded.close_time,
                    yes_ask = excluded.yes_ask,
                    no_ask = excluded.no_ask,
                    result = excluded.result
                """,
                (
                    ticker,
                    env,
                    m.get("event_ticker", ""),
                    m.get("market_type", "binary"),
                    m.get("status", ""),
                    m.get("close_time", ""),
                    yes_ask,
                    no_ask,
                    m.get("result", ""),
                ),
            )
        self.conn.commit()

    def get_open_markets(self, environment=None):
        # Return all open/active candidate markets from SQLite for the target environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM markets WHERE LOWER(status) IN ('open', 'active') AND environment = ?",
            (env,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_positioned_tickers(self, environment=None):
        # Return set of tickers where we already placed an order (OPEN or SETTLED) in this environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute("SELECT ticker FROM positions WHERE environment = ?", (env,))
        return {row["ticker"] for row in cursor.fetchall()}

    def insert_open_position(
        self, client_order_id, ticker, side, count, price_cents, close_time="", environment=None
    ):
        # Write a new OPEN order/position into SQLite for the target environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO positions (
                client_order_id, ticker, environment, side, count, price_cents, status, close_time, pnl_cents
            )
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, NULL)
            """,
            (client_order_id, ticker, env, side, count, price_cents, close_time or ""),
        )
        self.conn.commit()

    def get_positions_to_settle(self, now_iso, environment=None):
        # Return OPEN positions in this environment where close_time < now (or close_time is empty)
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM positions
            WHERE status = 'OPEN' AND environment = ? AND (close_time = '' OR close_time < ?)
            """,
            (env, now_iso),
        )
        return [dict(row) for row in cursor.fetchall()]

    def settle_position(self, client_order_id, pnl_cents, result="", environment=None):
        # Mark a position as SETTLED and record its PnL in cents for the target environment
        env = normalize_environment(environment or self.environment)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE positions
            SET status = 'SETTLED', pnl_cents = ?
            WHERE client_order_id = ? AND environment = ?
            """,
            (pnl_cents, client_order_id, env),
        )
        if result:
            cursor.execute(
                "UPDATE markets SET status = 'finalized', result = ? WHERE ticker = ("
                "SELECT ticker FROM positions WHERE client_order_id = ? AND environment = ?"
                ") AND environment = ?",
                (result, client_order_id, env, env),
            )
        self.conn.commit()

    def get_all_positions(self, environment=None):
        # Return positions as dicts for the target environment (or all if environment='*')
        cursor = self.conn.cursor()
        if environment == "*":
            cursor.execute("SELECT * FROM positions")
        else:
            env = normalize_environment(environment or self.environment)
            cursor.execute("SELECT * FROM positions WHERE environment = ?", (env,))
        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        # Close database connection
        self.conn.close()
