"""
Step 3: Settler
===============
Checks OPEN positions where close_time < now, queries Kalshi for the market resolution,
computes PnL in cents, and marks the position SETTLED in SQLite.
"""

from datetime import datetime, timezone

from db import MarketResult, MarketStatus, PositionStatus, normalize_market_result
from logging.analytics import analytics


class Settler:
    def __init__(self, client, db):
        # Kalshi REST API client and SQLite database
        self.client = client
        self.db = db

    def compute_pnl_cents(self, side, price_cents, count, result):
        # Binary contract pays 100 cents if our side matches the result, or 0 cents if we lost
        won = str(side).strip().lower() == str(result).strip().lower()
        if won:
            return (100 - int(price_cents)) * int(count)
        else:
            return -int(price_cents) * int(count)

    def is_market_resolved(self, market):
        # In Kalshi API v2, resolved markets have status in ('determined', 'finalized', 'settled')
        # and a populated result ('yes' or 'no')
        if not isinstance(market, dict):
            return False
        status = str(market.get("status", "")).lower()
        result = str(market.get("result", "")).lower()
        resolved_statuses = (
            MarketStatus.DETERMINED.value,
            MarketStatus.FINALIZED.value,
            MarketStatus.SETTLED.value,
        )
        resolved_results = (MarketResult.YES.value, MarketResult.NO.value)
        return status in resolved_statuses and result in resolved_results

    def settle(self, now_iso=None):
        # Default to current UTC time in ISO 8601 format (e.g. '2026-11-01T19:45:00Z')
        if now_iso is None:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Read OPEN positions where close_time < now from SQLite
        open_positions = self.db.get_positions_to_settle(now_iso)
        settled_positions = []

        # If running in live mode, fetch actual settlement records from Kalshi portfolio API
        live_settlements = {}
        if getattr(self.client, "live_orders", False) and hasattr(self.client, "get_settlements"):
            try:
                for s in self.client.get_settlements():
                    t = s.get("ticker")
                    if t:
                        live_settlements[t] = s
            except Exception as e:
                analytics.warning(f"Could not fetch live settlements from Kalshi: {e}")

        for pos in open_positions:
            ticker = pos["ticker"]
            client_order_id = pos["client_order_id"]

            # Check if Kalshi has settled this market in portfolio
            if ticker in live_settlements:
                s_info = live_settlements[ticker]
                result = normalize_market_result(s_info.get("market_result", ""))
                if "revenue" in s_info and s_info["revenue"] is not None:
                    rev_cents = int(s_info["revenue"])
                    side_key = "yes_total_cost_dollars" if str(pos["side"]).upper() in ("YES", "POSITIONSIDE.YES") else "no_total_cost_dollars"
                    cost_val = s_info.get(side_key)
                    if cost_val is not None:
                        try:
                            cost_cents = int(round(float(cost_val) * 100))
                        except (ValueError, TypeError):
                            cost_cents = int(pos["price_cents"]) * int(pos["count"])
                    else:
                        cost_cents = int(pos["price_cents"]) * int(pos["count"])
                    pnl_cents = rev_cents - cost_cents
                else:
                    pnl_cents = self.compute_pnl_cents(
                        side=pos["side"],
                        price_cents=pos["price_cents"],
                        count=pos["count"],
                        result=result,
                    )
            else:
                # 2. GET market resolution from Kalshi
                market = self.client.get_market(ticker)
                if not self.is_market_resolved(market):
                    continue

                result = normalize_market_result(market.get("result", ""))
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
            analytics.track(
                "settlement",
                message=f"Settled {ticker} ({pos['side']}): result={result.upper()}, PnL={pnl_cents} cents.",
                ticker=ticker,
                side=str(pos["side"]),
                result=result.upper(),
                pnl_cents=pnl_cents,
            )

            settled_positions.append({
                "client_order_id": client_order_id,
                "ticker": ticker,
                "side": pos["side"],
                "result": result.upper(),
                "pnl_cents": pnl_cents,
                "status": PositionStatus.SETTLED,
            })

        return settled_positions
