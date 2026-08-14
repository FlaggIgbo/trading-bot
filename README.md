# Trading Bot (Clean Slate)

A minimal, clean-slate prediction market trading bot centered around:
- **Kalshi API**: Authentication (RSA-PSS), balance queries, market data, order placement/cancellation, and 15-minute crypto market retrieval (`signals/kalshi_client.py`).
- **Gemini API**: Kept in the repository, but **commented out** for now (`signals/gemini_client.py`).
- **15-Minute Crypto Markets**: Dedicated retrieval for 15-minute prediction market contracts (e.g. `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`).

---

## Repository Structure

```
trading-bot/
├── main.py                  # Main entry point for testing/running Kalshi calls
├── kalshi_client.py         # Root shim re-exporting KalshiClient
├── gemini_client.py         # Root shim (commented out Gemini client)
├── requirements.txt         # Essential Python dependencies
├── .env.example             # Template for API credentials
└── signals/
    ├── __init__.py          # Module exports
    ├── kalshi_client.py     # Active Kalshi API implementation & 15m crypto retrieval
    └── gemini_client.py     # Commented-out Gemini API client
```

---

## Setup & Environment

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Configure your Kalshi API credentials in `.env`:
   - `KALSHI_API_KEY_ID`: Your Key ID
   - `KALSHI_PRIVATE_KEY_PATH`: Path to your RSA private key file
   - `KALSHI_USE_DEMO`: Set `true` for demo API or `false` for production

3. Run the bot entry point:
   ```bash
   python main.py
   ```

---

## Core Features

- **`KalshiClient.get_balance()`**: Fetches current portfolio balance in cents.
- **`KalshiClient.get_15m_crypto_markets()`**: Retrieves active 15-minute crypto markets across all supported 15m tickers (`KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXDOGE15M`, `KXBNB15M`, `KXHYPE15M`).
- **`KalshiClient.create_order_safe()` / `cancel_order_safe()`**: Place and cancel limit orders with balance checks and price tick rounding.
