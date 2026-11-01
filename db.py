"""
SQLite Database Layer
=====================
Stores markets and orders/positions for the trading bot.
"""

import sqlite3


class Database:
    def __init__(self, db_path="trading_bot.db"):
        # Connect to SQLite file (or ":memory:" for tests)
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        # Create markets and positions tables if they do not exist
        cursor = self.conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS markets (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT,
                market_type TEXT,
                status TEXT,
                close_time TEXT,
                yes_ask INTEGER,
                no_ask INTEGER,
                result TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                client_order_id TEXT PRIMARY KEY,
                ticker TEXT,
                side TEXT,
                count INTEGER,
                price_cents INTEGER,
                status TEXT,
                close_time TEXT,
                pnl_cents INTEGER
            )
            """
        )
        self.conn.commit()

    def _extract_cents(self, market, cents_key, dollars_key):
        # Helper to read integer cents or convert dollar string to cents
        if market.get(cents_key) is not None:
            return int(market[cents_key])
        if market.get(dollars_key) is not None:
            return int(round(float(market[dollars_key]) * 100))
        return 50

    def upsert_markets(self, markets):
        # Insert or update fetched markets in SQLite
        cursor = self.conn.cursor()
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue

            yes_ask = self._extract_cents(m, "yes_ask", "yes_ask_dollars")
            no_ask = self._extract_cents(m, "no_ask", "no_ask_dollars")

            cursor.execute(
                """
                INSERT INTO markets (ticker, event_ticker, market_type, status, close_time, yes_ask, no_ask, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    close_time = excluded.close_time,
                    yes_ask = excluded.yes_ask,
                    no_ask = excluded.no_ask,
                    result = excluded.result
                """,
                (
                    ticker,
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

    def get_open_markets(self):
        # Return all open/active candidate markets from SQLite
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM markets WHERE LOWER(status) IN ('open', 'active')")
        return [dict(row) for row in cursor.fetchall()]

    def get_positioned_tickers(self):
        # Return set of tickers where we already placed an order (OPEN or SETTLED)
        cursor = self.conn.cursor()
        cursor.execute("SELECT ticker FROM positions")
        return {row["ticker"] for row in cursor.fetchall()}

    def insert_open_position(self, client_order_id, ticker, side, count, price_cents, close_time=""):
        # Write a new OPEN order/position into SQLite
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO positions (
                client_order_id, ticker, side, count, price_cents, status, close_time, pnl_cents
            )
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, NULL)
            """,
            (client_order_id, ticker, side, count, price_cents, close_time or ""),
        )
        self.conn.commit()

    def get_positions_to_settle(self, now_iso):
        # Return OPEN positions where close_time < now (or close_time is empty so we still check)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM positions
            WHERE status = 'OPEN' AND (close_time = '' OR close_time < ?)
            """,
            (now_iso,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def settle_position(self, client_order_id, pnl_cents, result=""):
        # Mark a position as SETTLED and record its PnL in cents
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE positions
            SET status = 'SETTLED', pnl_cents = ?
            WHERE client_order_id = ?
            """,
            (pnl_cents, client_order_id),
        )
        if result:
            cursor.execute(
                "UPDATE markets SET status = 'finalized', result = ? WHERE ticker = ("
                "SELECT ticker FROM positions WHERE client_order_id = ?"
                ")",
                (result, client_order_id),
            )
        self.conn.commit()

    def get_all_positions(self):
        # Return all positions as dicts (helpful for inspection and tests)
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM positions")
        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        # Close database connection
        self.conn.close()
