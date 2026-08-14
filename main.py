"""
Clean Slate - Trading Bot Main Script
======================================

Purpose:
--------
This script demonstrates how to:
1. Initialize the Kalshi API Client with RSA authentication.
2. Fetch account balance from Kalshi.
3. Retrieve active 15-minute crypto prediction markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE).
4. Shows how Gemini API calls would be structured (commented out).

How to run:
-----------
$ python main.py
"""

import logging
from signals.kalshi_client import KalshiClient, SERIES_15M_CRYPTO

# Set up clean logging output format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_kalshi_demo(client: KalshiClient):
    """
    Demonstrates fetching balance and 15-minute crypto markets from Kalshi.
    """
    log.info("=========================================")
    log.info("1. KALSHI API DEMO")
    log.info("=========================================")

    # Step 1: Query Portfolio Balance
    try:
        balance_info = client.get_balance()
        balance_cents = balance_info.get("balance", 0)
        dollar_balance = balance_cents / 100.0
        log.info(f"Account Balance: ${dollar_balance:.2f} USD ({balance_cents} cents)")
    except Exception as error:
        log.error(f"Failed to fetch balance: {error}")

    # Step 2: Fetch 15-Minute Crypto Markets
    log.info("\nFetching active 15-minute crypto markets from Kalshi...")
    try:
        markets = client.get_15m_crypto_markets(status="open")
        log.info(
            f"Successfully retrieved {len(markets)} active 15m crypto markets "
            f"across series: {SERIES_15M_CRYPTO}\n"
        )

        # Print details for retrieved 15-minute crypto markets
        for index, market in enumerate(markets, start=1):
            ticker = market.get("ticker", "Unknown Ticker")
            title = market.get("title", "No Title")
            yes_bid = market.get("yes_bid", "N/A")
            yes_ask = market.get("yes_ask", "N/A")

            log.info(
                f"  [{index}] Market Ticker: {ticker}\n"
                f"      Title: {title}\n"
                f"      Current Market Prices -> Yes Bid: {yes_bid}¢ | Yes Ask: {yes_ask}¢"
            )
    except Exception as error:
        log.error(f"Failed to fetch 15-minute crypto markets: {error}")


def run_gemini_demo():
    """
    Demonstrates how Gemini API calls would be structured (Commented Out).
    """
    log.info("\n=========================================")
    log.info("2. GEMINI API DEMO (COMMENTED OUT)")
    log.info("=========================================")
    # # Step 1: Initialize public Gemini client
    # gemini_client = GeminiPredictionClient()
    #
    # # Step 2: Fetch active 15m crypto events
    # gemini_events = gemini_client.get_15m_crypto_events()
    # log.info(f"Retrieved {len(gemini_events)} Gemini 15m crypto events.")


def main():
    log.info("Starting Prediction Market Trading Bot...")

    # Initialize Kalshi client (uses .env file for credentials)
    try:
        kalshi_client = KalshiClient()
        run_kalshi_demo(kalshi_client)
    except Exception as error:
        log.error(f"Could not initialize Kalshi client: {error}")

    # Run Gemini demo (commented out)
    run_gemini_demo()

    log.info("\nTrading Bot execution complete.")


if __name__ == "__main__":
    main()
