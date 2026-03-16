"""
Trading bot for Kalshi prediction markets.
Watches selected markets and decides when to trade based on configurable strategies.

Run: python scripts/bot.py   (from project root)
"""
import logging
import sys
import time
from pathlib import Path

# Add project root to path so imports work when run from scripts/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config
from kalshi_client import KalshiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    cfg = get_config()
    client = KalshiClient()

    # Quick connectivity check
    try:
        bal = client.get_balance()
        log.info("Connected. Balance (cents): %s", bal.get("balance", "?"))
    except Exception as e:
        log.error("Connection check failed: %s", e)
        return 1

    # Load watched markets (tickers or series)
    markets_to_watch = cfg.get("markets", [])
    if not markets_to_watch:
        log.warning(
            "No markets configured. Add tickers or series in config.yaml or MARKETS env."
        )
        log.info("Example: KALSHI_YES, KXHIGHNY, or event tickers like KXBTC-25MAR15")
        return 0

    log.info("Watching %s: %s", len(markets_to_watch), markets_to_watch)

    poll_interval = int(cfg.get("poll_interval_seconds", 60))

    while True:
        try:
            for item in markets_to_watch:
                item = item.strip()
                if not item:
                    continue

                # Resolve to market tickers
                tickers = _resolve_markets(client, item)
                for ticker in tickers:
                    _check_market(client, ticker, cfg)

        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            log.exception("Loop error: %s", e)

        log.debug("Sleeping %ds", poll_interval)
        time.sleep(poll_interval)

    return 0


def _resolve_markets(client: KalshiClient, item: str) -> list[str]:
    """Turn a ticker or series into list of market tickers."""
    # Single market ticker: try direct fetch first
    if "/" not in item and len(item) > 10:
        try:
            client.get_market(item)
            return [item]
        except Exception:
            pass

    # Treat as series: fetch open markets for this series
    try:
        r = client.get_markets(series_ticker=item.upper(), status="open")
        return [m["ticker"] for m in r.get("markets", [])]
    except Exception:
        return []


def _check_market(client: KalshiClient, ticker: str, cfg: dict) -> None:
    """Fetch market data and apply simple strategy placeholder."""
    try:
        market = client.get_market(ticker)
    except Exception as e:
        log.warning("Could not fetch %s: %s", ticker, e)
        return

    m = market.get("market", market)
    title = m.get("title", ticker)
    yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
    no_bid = float(m.get("no_bid_dollars", 0) or 0)
    status = m.get("status", "?")

    log.info(
        "[%s] %s | YES: %.2f NO: %.2f | %s",
        ticker[:20],
        title[:50] + "..." if len(title) > 50 else title,
        yes_bid,
        no_bid,
        status,
    )

    strategy = cfg.get("strategy", {})
    trading = cfg.get("trading", {})
    buy_yes_threshold = strategy.get("buy_yes_below")
    buy_no_threshold = strategy.get("buy_no_below")
    dry_run = trading.get("dry_run", True)
    max_pct = float(trading.get("max_pct_of_balance", 0.90))
    min_contracts = int(trading.get("min_contracts", 1))

    if buy_yes_threshold is not None and yes_bid < buy_yes_threshold:
        log.info("  -> Strategy: buy YES below %.2f (current %.2f)", buy_yes_threshold, yes_bid)
        if not dry_run:
            result = client.create_order_safe(
                ticker, "yes", "buy",
                count=10,  # strategy can add max_contracts
                yes_price_dollars=f"{yes_bid:.4f}",  # Kalshi requires 4 decimal places
                max_pct_of_balance=max_pct,
            )
            if result:
                log.info("  -> Placed BUY YES order")
    if buy_no_threshold is not None and no_bid < buy_no_threshold:
        log.info("  -> Strategy: buy NO below %.2f (current %.2f)", buy_no_threshold, no_bid)
        if not dry_run:
            result = client.create_order_safe(
                ticker, "no", "buy",
                count=10,
                no_price_dollars=f"{no_bid:.4f}",  # Kalshi requires 4 decimal places
                max_pct_of_balance=max_pct,
            )
            if result:
                log.info("  -> Placed BUY NO order")


if __name__ == "__main__":
    raise SystemExit(main())
