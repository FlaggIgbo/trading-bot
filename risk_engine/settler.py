"""
Step 3: Settler
===============
Checks OPEN positions where close_time < now, queries Kalshi for the market resolution,
computes PnL in cents, and marks the position SETTLED in SQLite.
"""

from datetime import datetime, timezone


class Settler:
    def __init__(self, client, db):
        # Kalshi REST API client and SQLite database
        self.client = client
        self.db = db

    def compute_pnl_cents(self, side, price_cents, count, result):
        # Binary contract pays 100 cents if our side matches the result, or 0 cents if we lost
        won = side.strip().lower() == result.strip().lower()
        if won:
            return (100 - int(price_cents)) * int(count)
        else:
            return -int(price_cents) * int(count)

    def is_market_resolved(self, market):
        # In Kalshi API v2, resolved markets have status in ('determined', 'finalized', 'settled')
        # and a populated result ('yes' or 'no')
        if not isinstance(market, dict):
            return False
        status = market.get("status", "").lower()
        result = market.get("result", "").lower()
        return status in ("determined", "finalized", "settled") and result in ("yes", "no")

    def settle(self, now_iso=None):
        # Default to current UTC time in ISO 8601 format (e.g. '2026-11-01T19:45:00Z')
        if now_iso is None:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Read OPEN positions where close_time < now from SQLite
        open_positions = self.db.get_positions_to_settle(now_iso)
        settled_positions = []

        for pos in open_positions:
            ticker = pos["ticker"]
            client_order_id = pos["client_order_id"]

            # 2. GET market resolution from Kalshi
            market = self.client.get_market(ticker)
            if not self.is_market_resolved(market):
                continue

            result = market.get("result", "").lower()
            pnl_cents = self.compute_pnl_cents(
                side=pos["side"],
                price_cents=pos["price_cents"],
                count=pos["count"],
                result=result,
            )

            # 3. Write SETTLED + PnL to SQLite
            self.db.settle_position(
                client_order_id=client_order_id,
                pnl_cents=pnl_cents,
                result=result,
            )
            print(f"Settled {ticker} ({pos['side']}): result={result.upper()}, PnL={pnl_cents} cents.")

            settled_positions.append({
                "client_order_id": client_order_id,
                "ticker": ticker,
                "side": pos["side"],
                "result": result.upper(),
                "pnl_cents": pnl_cents,
                "status": "SETTLED",
            })

        return settled_positions
