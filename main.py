r"""
15-Minute & Hourly Multi-Asset Prediction Trading & Data Pipeline Bot
======================================================================

What is this bot?
------------------------------------------------------
A high-performance algorithmic trading and empirical microstructure data engine for
Kalshi prediction markets across 15-minute and Hourly multi-strike events spanning:
- Crypto: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ADA, TON, BCH, NEAR, ZEC
- Commodities: Gold (KXGOLD15M, KXGOLDH), Silver (KXSILVER15M, KXSILVERH), WTI Oil (KXWTI15M, KXWTIH)
- Equity Indices: S&P 500 (KXINXU), Nasdaq-100 (KXNASDAQ100U)
Contracts settle at either $1.00 (100¢) if YES occurs, or $0.00 if NO occurs.

Core Architecture & Risk Engines:
----------------------------------
1. Synchronized Checkpoint Execution (:00:30, :02:30, :05:00, :07:30, :10:00, :12:30, :20:00, :30:00, :40:00, :50:00):
   - Waits past clock intervals for market books to establish depth and liquidity.
2. Concurrent Multi-Source Spot Oracle Ingestion:
   - Queries Coinbase (crypto), Pyth Network Hermes (commodities), and Yahoo Finance (indices) in parallel (<150ms total).
3. Dynamic 6-Hour Database Calibration Engine:
   - Dynamically computes volatility ($\sigma$), empirical win rates, and optimal price corridors from MongoDB Atlas.
4. Hourly Multi-Strike Selection & Tiebreakers:
   - Scans all strike options, picking cheapest YES and cheapest NO within valid corridors.
   - Dual buys on different strikes; resolves ties on same contract by price and spot momentum.
5. 15-Minute Empirical Discovery Window (05:00–05:15 UTC):
   - Brief 15-minute discovery window with a strict global trade cap (max 3 live exploration trades per session).
6. Risk-Free Counterfactual Empirical Tracking:
   - Records non-traded mid-zone opportunities (37¢–55¢) and rejected candidates as dry-run snapshots for statistical modeling without risking live capital.
7. Crash-Proof Persistent Trade State:
   - Stores active position cost bases in MongoDB Atlas `active_positions` to survive container restarts.
8. Error-Isolated Market Boundaries:
   - Wraps each individual market and symbol group in defensive error handlers so one payload glitch never halts the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

from database import (
    calculate_dynamic_contracts_count,
    evaluate_ensemble_consensus,
    get_asset_settlement_streak,
    get_dynamic_filter_for_market,
    get_dynamic_price_filter_map,
    is_default_exploration_window,
    is_mid_zone_exploration_window,
    save_market_snapshots,
    save_portfolio_pnl,
    save_settlements,
)
from signals.base_client import BaseTradingClient, LogTag

# from signals.gemini_client import GeminiClient
from signals.kalshi_client import CFBENCHMARKS_CRYPTO_INDEX_MAP, KalshiClient

# Set up clean, human-readable logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# In-memory price oscillation / lead change tracker for market choppiness filtering
_MARKET_LEAD_CHANGES: dict[str, list[dict]] = defaultdict(list)


def record_market_price_snapshot(ticker: str, yes_price: int, timestamp: float | None = None) -> None:
    """Records top-of-book YES price to track price oscillation / lead changes over the 15m lifecycle."""
    ts = timestamp or time.time()
    snapshots = _MARKET_LEAD_CHANGES[ticker]
    # Prune snapshots older than 20 minutes (1200s)
    _MARKET_LEAD_CHANGES[ticker] = [s for s in snapshots if ts - s["ts"] <= 1200]
    _MARKET_LEAD_CHANGES[ticker].append({"p": yes_price, "ts": ts})


def get_market_lead_changes(ticker: str) -> int:
    """
    Counts how many times YES crossed 50¢ in its lifecycle (measuring whipsaw / chop).
    A lead change occurs when YES transitions between >=50¢ and <50¢.
    """
    snapshots = _MARKET_LEAD_CHANGES.get(ticker, [])
    if len(snapshots) < 2:
        return 0
    lead_changes = 0
    last_state = None
    for s in snapshots:
        p = s.get("p")
        if p is not None:
            curr_state = (p >= 50)
            if last_state is not None and curr_state != last_state:
                lead_changes += 1
            last_state = curr_state
    return lead_changes


def is_market_choppy(ticker: str, max_lead_changes: int = 2) -> bool:
    """Returns True if the market has flipped direction more than max_lead_changes times."""
    return get_market_lead_changes(ticker) > max_lead_changes

# Supported symbols across exchanges (Crypto, Commodities, Equity Indices)
SUPPORTED_SYMBOLS = (
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC",
    "GOLD", "SILVER", "WTI", "INX", "NASDAQ100",
)

# Pyth Hermes Price Feeds for Kalshi Commodities (Gold, Silver, WTI Oil)
# Kalshi contract settlement rules officially specify Pyth feeds:
PYTH_COMMODITY_FEEDS: dict[str, str] = {
    "GOLD": "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",    # Metal.XAU/USD (Spot Gold)
    "SILVER": "f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e",  # Metal.XAG/USD (Spot Silver)
    "WTI": "925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0a154887b3e6",     # Commodities.USOILSPOT (WTI Oil)
}

# Yahoo Finance ticker mapping for Equity Indices
INDEX_TICKER_MAP: dict[str, str] = {
    "INX": "^GSPC",          # S&P 500 Index
    "SPX": "^GSPC",
    "NASDAQ100": "^NDX",     # Nasdaq-100 Index
    "NDX": "^NDX",
}

COMMODITY_SYMBOLS = set(PYTH_COMMODITY_FEEDS.keys())
INDEX_SYMBOLS = set(INDEX_TICKER_MAP.keys())

# Default expiration in seconds for Maker limit buy orders to protect against adverse price drops
BUY_ORDER_EXPIRATION_SECONDS = int(os.getenv("BUY_ORDER_EXPIRATION_SECONDS", "45"))

# Maximum number of live discovery/exploration trades permitted per exploration session (05:00-05:15 UTC)
MAX_EXPLORATION_TRADES_PER_SESSION = int(
    os.getenv("MAX_EXPLORATION_TRADES_PER_SESSION", "3")
)
_EXPLORATION_TRADES_COUNT: int = 0
_LAST_EXPLORATION_SESSION_ID: str = ""


def get_exploration_trades_count() -> int:
    """Returns current number of executed live exploration trades in this session."""
    return _EXPLORATION_TRADES_COUNT


def reset_exploration_trades_count():
    """Resets the exploration trades counter."""
    global _EXPLORATION_TRADES_COUNT, _LAST_EXPLORATION_SESSION_ID
    _EXPLORATION_TRADES_COUNT = 0
    _LAST_EXPLORATION_SESSION_ID = ""


def can_execute_exploration_trade(now_utc: datetime | None = None) -> bool:
    """
    Checks if an exploration trade is permitted under the session cap.
    Automatically resets the counter when a new calendar day / session begins.
    """
    global _EXPLORATION_TRADES_COUNT, _LAST_EXPLORATION_SESSION_ID
    now = now_utc or datetime.now(timezone.utc)
    session_id = now.strftime("%Y-%m-%d")
    if _LAST_EXPLORATION_SESSION_ID != session_id:
        _LAST_EXPLORATION_SESSION_ID = session_id
        _EXPLORATION_TRADES_COUNT = 0
    return _EXPLORATION_TRADES_COUNT < MAX_EXPLORATION_TRADES_PER_SESSION


def record_exploration_trade_executed(now_utc: datetime | None = None):
    """Increments the exploration trade counter for this session."""
    global _EXPLORATION_TRADES_COUNT, _LAST_EXPLORATION_SESSION_ID
    now = now_utc or datetime.now(timezone.utc)
    session_id = now.strftime("%Y-%m-%d")
    if _LAST_EXPLORATION_SESSION_ID != session_id:
        _LAST_EXPLORATION_SESSION_ID = session_id
        _EXPLORATION_TRADES_COUNT = 0
    _EXPLORATION_TRADES_COUNT += 1


# Persistent HTTP session with connection pooling sized for parallel thread queries
http_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)


def calculate_seconds_until_next_5min_mark(offset_seconds: float = 30.0) -> float:
    """
    Calculates exact seconds to sleep until 30 seconds past the NEXT 5-minute mark (:00:30, :05:30, etc.).

    Safety Guards:
    - Computes target datetime explicitly based on UTC clock.
    - If the calculated wait is under 15 seconds (e.g. if a run finished right around :05:30),
      it advances to the following 5-minute bucket so the bot never double-fires in the same interval.
    """
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    next_5min_bucket = (current_minute // 5 + 1) * 5

    target_minute = next_5min_bucket % 60
    hour_increment = next_5min_bucket // 60

    target_dt = now.replace(
        minute=target_minute,
        second=int(offset_seconds),
        microsecond=0,
    )
    if hour_increment > 0:
        target_dt = target_dt + timedelta(hours=hour_increment)

    wait_seconds = (target_dt - now).total_seconds()

    # Guarantee at least 15 seconds sleep to avoid same-interval re-execution on timing drift
    if wait_seconds < 15.0:
        wait_seconds += 300.0

    return max(15.0, wait_seconds)


def calculate_seconds_until_next_checkpoint(open_delay_seconds: float = 30.0, offset_seconds: float | None = None) -> float:
    """
    Calculates exact seconds to sleep until the NEXT scheduled checkpoint:
    
    15-Minute Market Checkpoints (Every 15m block):
    - Minute :00:30, :15:30, :30:30, :45:30 (Open Entry, +30s open delay to allow bets to settle)
    - Minute :02:30, :17:30, :32:30, :47:30 (2.5m Disaster Cut-Loss & 2nd Chance Entry)
    - Minute :05:00, :20:00, :35:00, :50:00 (5m Cut-Loss & Tight Entry, exact timestamp without buffer)
    - Minute :07:30, :22:30, :37:30, :52:30 (7.5m Mid-Candle Cut-Loss & Tight Entry)
    - Minute :10:00, :25:00, :40:00, :55:00 (10m Stop-Loss & Winner-Roll, exact timestamp without buffer)
    - Minute :12:30, :27:30, :42:30, :57:30 (12.5m Endgame Check: 2.5m before expiration)

    Hourly Multi-Strike Market Checkpoints (Every 10m past the hour):
    - Minute :00:30 (Open Multi-Strike Entry, +30s open delay)
    - Minute :10:00 (10m Disaster Cut-Loss & 2nd Chance Entry, exact timestamp)
    - Minute :20:00 (20m Cut-Loss & Tighter Entry, exact timestamp)
    - Minute :30:00 (30m Mid-Hour Cut-Loss & Tighter Entry, exact timestamp without delay)
    - Minute :40:00 (40m Winner-Roll & Stop-Loss, exact timestamp)
    - Minute :50:00 (50m Endgame Check, exact timestamp)
    """
    if offset_seconds is not None:
        open_delay_seconds = offset_seconds

    now = datetime.now(timezone.utc)

    # Complete 60-minute scheduled checkpoints (minute, second)
    open_delay = int(open_delay_seconds)
    hourly_checkpoints = (
        (0, open_delay),   # 15M :00 open (+30s), 1H :00 open (+30s)
        (2, 30),           # 15M :02:30
        (5, 0),            # 15M :05:00
        (7, 30),           # 15M :07:30
        (10, 0),           # 15M :10:00, 1H :10:00
        (12, 30),          # 15M :12:30
        (15, open_delay),  # 15M :15:30 open (+30s)
        (17, 30),          # 15M :17:30
        (20, 0),           # 15M :20:00, 1H :20:00
        (22, 30),          # 15M :22:30
        (25, 0),           # 15M :25:00
        (27, 30),          # 15M :27:30
        (30, 0),           # 1H :30:00 mid-hour check (exact timestamp, NO delay)
        (30, open_delay),  # 15M :30:30 open (+30s)
        (32, 30),          # 15M :32:30
        (35, 0),           # 15M :35:00
        (37, 30),          # 15M :37:30
        (40, 0),           # 15M :40:00, 1H :40:00
        (42, 30),          # 15M :42:30
        (45, open_delay),  # 15M :45:30 open (+30s)
        (47, 30),          # 15M :47:30
        (50, 0),           # 15M :50:00, 1H :50:00
        (52, 30),          # 15M :52:30
        (55, 0),           # 15M :55:00
        (57, 30),          # 15M :57:30
    )

    candidates = []
    # Generate candidate timestamps across current hour and next hour
    for hour_offset in (0, 1):
        base_hour_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour_offset)
        for cp_min, cp_sec in hourly_checkpoints:
            dt = base_hour_dt.replace(minute=cp_min, second=cp_sec)
            candidates.append(dt)

    # Pick the earliest candidate that is at least 10 seconds in the future
    future_candidates = [c for c in candidates if (c - now).total_seconds() >= 10.0]
    next_dt = min(future_candidates) if future_candidates else candidates[-1]

    wait_seconds = (next_dt - now).total_seconds()
    return max(10.0, wait_seconds)


# Alias for backward compatibility
calculate_seconds_until_next_check = calculate_seconds_until_next_checkpoint


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """
    Extracts the short asset symbol (e.g. 'BTC', 'ETH', 'GOLD', 'WTI', 'SILVER', 'INX', 'NASDAQ100') from full ticker strings.
    Example:
      'KXBTC15M-26AUG141315-15' -> 'BTC'
      'KXBTCD-26AUG1521-T69399.99' -> 'BTC'
      'KXSOLD-26AUG1617-T99.9999' -> 'SOL'
      'KXGOLD15M-26AUG162345-45' -> 'GOLD'
      'KXGOLDH-26AUG1701-T4427.99' -> 'GOLD'
      'KXINXU-26AUG17H1000-T7919.9999' -> 'INX'
      'KXNASDAQ100U-26AUG17H1000-T31989.99' -> 'NASDAQ100'
    """
    # 1. Check ticker prefix if hyphen-separated (e.g. btc-updown-15m-...)
    ticker_prefix = ticker.split("-")[0].upper() if "-" in ticker else ""
    if ticker_prefix in SUPPORTED_SYMBOLS:
        return ticker_prefix

    # 2. Check series ticker or ticker prefix against supported symbols
    series_upper = (series_ticker or "").upper()
    for sym in SUPPORTED_SYMBOLS:
        if (
            series_upper == f"KX{sym}"
            or series_upper.startswith(f"KX{sym}15M")
            or series_upper.startswith(f"KX{sym}H")
            or series_upper.startswith(f"KX{sym}D")
            or series_upper.startswith(f"KX{sym}U")
            or series_upper.startswith(f"KX{sym}")
            or series_upper.startswith(sym)
            or ticker_prefix.startswith(f"KX{sym}")
        ):
            return sym

    # 3. Clean known prefixes/suffixes (KX, 15M, D, H, W, U) and match
    series_clean = series_upper.replace("15M", "").replace("KX", "").rstrip("DHWEU").upper()
    if series_clean in SUPPORTED_SYMBOLS:
        return series_clean

    # 4. Search with word boundary regex
    combined = f"{series_ticker} {ticker}".upper()
    for sym in SUPPORTED_SYMBOLS:
        if re.search(rf"\b{sym}\b", combined):
            return sym

    return next((sym for sym in SUPPORTED_SYMBOLS if sym in combined), series_ticker or "ASSET")


def extract_target_strike(market: dict) -> float | None:
    """
    Safely extracts the strike / target price from a market dict across 15M and hourly formats.
    Checks:
    1. 'floor_strike', 'cap_strike', 'strike_price'
    2. 'subtitle', 'yes_sub_title', 'no_sub_title' (e.g. '$72,250 or above', 'Target Price: $4,393.04', '7,920 or above')
    3. 'ticker' (e.g. 'KXBTCD-26AUG1617-T72249.99' or 'KXINXU-26AUG17H1000-T7919.9999')
    """
    for k in ("floor_strike", "cap_strike", "strike_price"):
        val = market.get(k)
        if val is not None:
            try:
                f_val = float(val)
                if f_val > 0:
                    return f_val
            except (ValueError, TypeError):
                pass

    for sub_k in ("subtitle", "yes_sub_title", "no_sub_title"):
        if sub_text := market.get(sub_k):
            if m := re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", str(sub_text)):
                try:
                    return float(m.group(1).replace(",", ""))
                except (ValueError, TypeError):
                    pass
            if m := re.search(r"([0-9,]+(?:\.[0-9]+)?)\s+or\s+above", str(sub_text), re.I):
                try:
                    return float(m.group(1).replace(",", ""))
                except (ValueError, TypeError):
                    pass

    if ticker := market.get("ticker"):
        if m := re.search(r"-T([0-9]+(?:\.[0-9]+)?)", str(ticker)):
            try:
                return float(m.group(1))
            except (ValueError, TypeError):
                pass

    return None


def parse_cents(val_dollars: str | float | None, val_cents: int | str | None = None) -> int | None:
    """
    Safely converts string dollars (e.g. '0.3500') or cent integers (e.g. 35) to an integer cent value (1-99).
    Returns None if the price is missing, 0, or invalid.
    """
    for val, multiplier in ((val_dollars, 100), (val_cents, 1)):
        if val is not None:
            try:
                res = int(round(float(val) * multiplier))
                if 1 <= res <= 99:
                    return res
            except (ValueError, TypeError):
                continue
    return None


def get_pyth_commodity_spot_prices(symbols: list[str]) -> dict[str, float | None]:
    """
    Fetches real-time commodity spot prices directly from Pyth Network Hermes API.
    Matches Kalshi's official settlement source for Gold, Silver, and WTI Oil.
    """
    feeds_to_fetch = {sym: PYTH_COMMODITY_FEEDS[sym] for sym in symbols if sym in PYTH_COMMODITY_FEEDS}
    result: dict[str, float | None] = {sym: None for sym in symbols}
    if not feeds_to_fetch:
        return result

    try:
        params = [("ids[]", fid) for fid in feeds_to_fetch.values()]
        resp = http_session.get(
            "https://hermes.pyth.network/v2/updates/price/latest",
            params=params,
            timeout=3.5,
        )
        if resp.status_code == 200 and resp.text:
            data = resp.json()
            id_to_sym = {v: k for k, v in feeds_to_fetch.items()}
            for p in data.get("parsed", []):
                sym = id_to_sym.get(p.get("id"))
                if sym:
                    p_info = p.get("price", {})
                    raw_price = int(p_info.get("price", 0))
                    expo = int(p_info.get("expo", 0))
                    result[sym] = round(raw_price * (10 ** expo), 4)
    except Exception as error:
        log.debug(f"Could not batch-fetch Pyth commodity spot prices: {error}")

    return result


def get_index_spot_price(symbol: str) -> float | None:
    """
    Fetches real-time equity index spot price (e.g. S&P 500, Nasdaq-100) from Yahoo Finance chart API.
    """
    ticker = INDEX_TICKER_MAP.get(symbol.upper(), symbol)
    headers = {"User-Agent": "Mozilla/5.0"}
    for base_url in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        try:
            resp = http_session.get(
                f"{base_url}/v8/finance/chart/{ticker}?interval=1m&range=1d",
                headers=headers,
                timeout=3.5,
            )
            if resp.status_code == 200 and resp.text:
                data = resp.json()
                results = data.get("chart", {}).get("result", [])
                if results:
                    meta = results[0].get("meta", {})
                    price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
                    if price is not None:
                        return round(float(price), 2)
        except Exception as error:
            log.debug(f"Could not fetch index spot price for {symbol} from {base_url}: {error}")
    return None


def get_kalshi_cfbenchmarks_spot_price(
    symbol: str,
    client: BaseTradingClient | None = None,
    calc_twap_seconds: int = 0,
) -> float | None:
    """
    Fetches real-time underlying crypto price directly from Kalshi's official CF Benchmarks feed.
    If calc_twap_seconds > 0, calculates the 60-second TWAP matching Kalshi settlement specification.
    """
    sym_upper = symbol.upper()
    if client and hasattr(client, "get_cfbenchmarks_price"):
        try:
            val = client.get_cfbenchmarks_price(sym_upper, calc_twap_seconds=calc_twap_seconds)
            if val is not None and val > 0:
                return val
        except Exception as err:
            log.debug(f"KalshiClient CF Benchmarks query failed for {sym_upper}: {err}")
    return None


def get_market_settlement_source(market: dict, client: BaseTradingClient | None = None) -> str:
    """
    Dynamically identifies the official settlement source for a given market.
    - Crypto series -> 'CF Benchmarks'
    - Commodities -> 'Pyth Network'
    - Indices -> 'Yahoo Finance'
    """
    series_ticker = market.get("series_ticker", "")
    ticker = market.get("ticker", "")
    asset_symbol = extract_asset_symbol(series_ticker, ticker)

    if client and hasattr(client, "get_series_settlement_sources") and series_ticker:
        try:
            sources = client.get_series_settlement_sources(series_ticker)
            if sources and isinstance(sources, list):
                src_names = [s.get("name", "") for s in sources if isinstance(s, dict)]
                if src_names:
                    return src_names[0]
        except Exception:
            pass

    if asset_symbol in COMMODITY_SYMBOLS:
        return "Pyth Network"
    elif asset_symbol in INDEX_SYMBOLS:
        return "Yahoo Finance"
    else:
        return "CF Benchmarks"


def get_crypto_spot_price(
    symbol: str,
    client: BaseTradingClient | None = None,
    calc_twap_seconds: int = 0,
) -> float | None:
    """
    Fetches real-time underlying crypto price.
    Prioritizes Kalshi's official CF Benchmarks index feed, with defensive fallback to Coinbase Spot API.
    """
    # 1. Try Kalshi's official CF Benchmarks RTI feed first
    cfb_price = get_kalshi_cfbenchmarks_spot_price(symbol, client=client, calc_twap_seconds=calc_twap_seconds)
    if cfb_price is not None and cfb_price > 0:
        return cfb_price

    # 2. Fallback to Coinbase public spot API
    try:
        resp = http_session.get(
            f"https://api.coinbase.com/v2/prices/{symbol.upper()}-USD/spot",
            timeout=3.5,
        )
        if resp.status_code == 200 and resp.text:
            data = resp.json()
            if isinstance(data, dict) and "data" in data and "amount" in data["data"]:
                return round(float(data["data"]["amount"]), 4)
    except (requests.RequestException, ValueError, json.JSONDecodeError, KeyError) as error:
        log.debug(f"Could not fetch spot price for {symbol} from Coinbase: {error}")
    return None


def get_spot_price(symbol: str, client: BaseTradingClient | None = None) -> float | None:
    """
    Unified single spot price lookup router:
    - Commodities (GOLD, SILVER, WTI) -> Pyth Hermes API
    - Indices (INX, NASDAQ100) -> Yahoo Finance Chart API
    - Crypto (BTC, ETH, SOL, etc.) -> CF Benchmarks API / Coinbase Fallback
    """
    sym_upper = symbol.upper()
    if sym_upper in COMMODITY_SYMBOLS:
        res = get_pyth_commodity_spot_prices([sym_upper])
        return res.get(sym_upper)
    elif sym_upper in INDEX_SYMBOLS:
        return get_index_spot_price(sym_upper)
    else:
        return get_crypto_spot_price(sym_upper, client=client)


def batch_fetch_spot_prices(
    symbols: tuple[str, ...] | list[str],
    max_workers: int = 16,
    client: BaseTradingClient | None = None,
    calc_twap_seconds: int = 0,
) -> dict[str, float | None]:
    """
    Fetches spot prices for all assets (Crypto, Commodities, Equity Indices) concurrently in parallel.
    - Commodities (GOLD, SILVER, WTI) batch-queried in 1 call via Pyth Hermes API (~80-100ms)
    - Indices (INX, NASDAQ100) queried concurrently via Yahoo Finance
    - Cryptos queried concurrently via CF Benchmarks (or Coinbase fallback)
    """
    unique_symbols = list(set(symbols))
    spot_cache: dict[str, float | None] = {sym: None for sym in unique_symbols}

    if not unique_symbols:
        return spot_cache

    commodities = [s for s in unique_symbols if s in COMMODITY_SYMBOLS]
    indices = [s for s in unique_symbols if s in INDEX_SYMBOLS]
    cryptos = [s for s in unique_symbols if s not in COMMODITY_SYMBOLS and s not in INDEX_SYMBOLS]

    with ThreadPoolExecutor(max_workers=min(len(unique_symbols) + 2, max_workers)) as executor:
        futures = []
        if commodities:
            futures.append(executor.submit(lambda: ("__COMMODITIES__", get_pyth_commodity_spot_prices(commodities))))
        for idx_sym in indices:
            futures.append(executor.submit(lambda s=idx_sym: (s, get_index_spot_price(s))))
        for cry_sym in cryptos:
            if client is not None or calc_twap_seconds > 0:
                futures.append(executor.submit(lambda s=cry_sym: (s, get_crypto_spot_price(s, client=client, calc_twap_seconds=calc_twap_seconds))))
            else:
                futures.append(executor.submit(lambda s=cry_sym: (s, get_crypto_spot_price(s))))

        for future in as_completed(futures):
            try:
                k, v = future.result()
                if k == "__COMMODITIES__":
                    spot_cache.update(v)
                else:
                    spot_cache[k] = v
            except Exception as err:
                log.debug(f"Batch spot fetch worker error: {err}")

    return spot_cache


# Alias for backward compatibility with existing tests
batch_fetch_crypto_spot_prices = batch_fetch_spot_prices


def is_spot_momentum_aligned(
    side: str,
    spot_price: float | None,
    target_price: float | None,
    otm_pct_threshold: float = 0.05,
) -> bool:
    """
    Spot Momentum Reality Check (Coinbase Spot vs. Exchange Strike Target):
    -----------------------------------------------------------------------
    Prediction contract prices show betting sentiment, but Coinbase spot shows real-world truth.
    We check the real asset price against the strike target before making any trade:
      - Dead-Heat Zone (|pct_dist| <= 0.05%): The price is right on the line; allow either YES or NO.
      - Bullish Momentum (spot >= target): Only permit YES buys (avoids buying cheap NO on a rallying asset).
      - Bearish Momentum (spot < target): Only permit NO buys (avoids buying cheap YES on a falling asset).
    """
    if spot_price is None or target_price is None or target_price <= 0:
        return True

    pct_dist = ((spot_price - target_price) / target_price) * 100.0
    if abs(pct_dist) <= otm_pct_threshold:
        return True

    return (pct_dist >= 0) if side.lower() == "yes" else (pct_dist < 0)


def extract_price_cents(market: dict, side: str, client: BaseTradingClient | None = None) -> int | None:
    """
    Extracts the Ask price in cents (1-99) for either 'yes' or 'no'.
    Falls back to querying the market book if the payload lacks top-of-book quotes.
    """
    prefix = side.lower()
    dollar_str = market.get(f"{prefix}_ask_dollars")
    cent_val = market.get(f"{prefix}_ask")

    # Fallback to market detail / orderbook if quotes are missing from the list response
    if dollar_str is None and cent_val is None and client and market.get("ticker"):
        try:
            detail = client.get_market(market["ticker"]).get("market", {})
            dollar_str = detail.get(f"{prefix}_ask_dollars")
            cent_val = detail.get(f"{prefix}_ask")
        except Exception as err:
            log.debug(f"Fallback get_market failed for {market.get('ticker')}: {err}")

    return parse_cents(dollar_str, cent_val)


def determine_counterfactual_outcome(
    trade_side: str,
    action_type: str,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None = None,
) -> str:
    """
    Determines post-mortem trade outcomes for data analytics and strategy evaluation.
    Tells us if our Stop-Loss or Take-Profit decisions helped or hurt the final result.
    """
    if market_result in ("yes", "no"):
        winning_side = market_result.lower()
    elif spot_price is not None and target_price is not None:
        winning_side = "yes" if spot_price >= target_price else "no"
    else:
        return "pending_settlement"

    won = (trade_side.lower() == winning_side)
    if action_type in ("stop_loss_sold", "early_loss_cut", "profit_floor_sold", "endgame_loss_cut"):
        return "sold_early_would_have_won" if won else "sl_cap_saved"
    if action_type in ("winner_profit_banked", "winner_rolled_rebuy"):
        return "winner_profit_banked" if won else "winner_banked_saved_reversal"
    if action_type == "take_profit_sold":
        return "tp_full_win" if won else "sold_early_saved_from_loss"

    return "open_position"


def _execute_buy_flow(
    ticker: str,
    side: str,
    price_cents: int,
    maker_bid: int | None,
    min_buy: int,
    max_buy: int,
    take_profit_cents: int,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None,
    client: BaseTradingClient,
    dry_run: bool,
    is_exploration: bool = False,
    corridor_mode: str = "discount",
    count: int = 1,
) -> tuple[bool, dict | None]:
    """Helper function to execute Maker Buy order and setup resting Take-Profit Auto-Sell with dynamic contract sizing."""
    side_lower = side.lower()
    side_upper = side.upper()
    ex_name = getattr(client, "exchange_name", "Exchange")
    momentum_label = "Bullish" if side_lower == "yes" else "Bearish"
    corridor_tag = ""
    if corridor_mode == "momentum":
        corridor_tag = " [MOMENTUM BREAKOUT]"
    elif corridor_mode == "mid_zone":
        corridor_tag = " [MID-ZONE EXPLORATION]"

    log_prefix = LogTag.DRY_RUN_MATCH.value if dry_run else LogTag.BUY_MATCH.value
    regime_tag = " [EXPLORATION REGIME]" if is_exploration else ""
    contracts_str = f"{count} CONTRACT{'S' if count != 1 else ''}"
    log_action = (
        f"WOULD BUY {contracts_str} OF [{side_upper}] + SET {take_profit_cents}¢ AUTO-SELL."
        if dry_run
        else f"Executing Maker Buy ({contracts_str}) + {take_profit_cents}¢ Auto-Sell!"
    )
    log.info(
        f"{log_prefix}{regime_tag}{corridor_tag} [{ex_name}] {ticker} -> {side_upper} price is {price_cents}¢ "
        f"(between {min_buy}¢ and {max_buy}¢ & {momentum_label} Spot Momentum!). {log_action}"
    )

    effective_buy = price_cents
    if not dry_run:
        try:
            buy_res = client.buy_and_set_take_profit(
                ticker=ticker,
                side=side_lower,
                buy_price_cents=price_cents,
                take_profit_cents=take_profit_cents,
                count=count,
                maker_bid_cents=maker_bid,
                buy_order_timeout_seconds=BUY_ORDER_EXPIRATION_SECONDS,
            )
            effective_buy = buy_res.get("buy_price_cents", price_cents)
        except Exception as error:
            log.error(f"❌ [{ex_name}] Failed to execute {side_upper} trade on {ticker}: {error}")
            return False, None

    trade_details = {
        **({"dry_run": True} if dry_run else {}),
        "ex": ex_name.lower(),
        "action": "buy_opened",
        "regime": "default_window" if is_exploration else "calibrated",
        "corridor": corridor_mode,
        "side": side_lower,
        "buy": effective_buy,
        "count": count,
        "tp": take_profit_cents,
        "sl": 25,
        "outcome": determine_counterfactual_outcome(side_lower, "buy_opened", spot_price, target_price, market_result),
        "entry_ts": int(time.time()),
        "spot_at_entry": spot_price,
        "target_at_entry": target_price,
    }
    if not dry_run and isinstance(buy_res, dict):
        if cid := buy_res.get("client_order_id"):
            trade_details["client_order_id"] = cid
        if oid := buy_res.get("order_id"):
            trade_details["order_id"] = oid
        if tp_oid := buy_res.get("tp_order_id"):
            trade_details["tp_order_id"] = tp_oid
        if is_exploration or corridor_mode == "mid_zone":
            record_exploration_trade_executed()

    # Record traded market on client to strictly enforce single-side lock per lifecycle
    if getattr(client, "record_traded_market", None) is not None:
        client.record_traded_market(ticker, side_lower)

    return True, trade_details


def resolve_safe_take_profit_for_position(
    ticker: str,
    client: BaseTradingClient,
    dynamic_filters: dict,
) -> int:
    """
    Resolves the intended and safe Take-Profit target (cents) for an active position:
    1. Checks recorded target_tp from order execution / MongoDB.
    2. Falls back to momentum_tp (88¢) if entry price was in momentum corridor (>= momentum_min_buy or >= 50¢).
    3. Falls back to discount tp (62¢).
    4. Strict invariant: safe_tp must ALWAYS be strictly greater than entry_price.
    """
    pos_entry = client.get_entry_price(ticker)
    intended_tp = getattr(client, "get_target_tp", lambda t: None)(ticker)

    if intended_tp is not None:
        effective_tp = intended_tp
    elif pos_entry is not None and pos_entry >= dynamic_filters.get("momentum_min_buy", 56):
        effective_tp = int(dynamic_filters.get("momentum_tp", 88))
    else:
        effective_tp = int(dynamic_filters.get("tp", 62))

    if pos_entry is not None and effective_tp <= pos_entry:
        effective_tp = min(99, max(pos_entry + 5, int(dynamic_filters.get("momentum_tp", 88))))

    return effective_tp


def evaluate_favorite_maker_opportunity(
    market: dict,
    quotes: dict,
    rem_mins: float | None,
    dynamic_filters: dict,
    client: BaseTradingClient,
    min_fav_price: int = 74,
    max_fav_price: int = 93,
    min_rem_mins: float = 1.0,
    max_rem_mins: float = 6.0,
) -> dict | None:
    """
    Identifies high-probability favorites in the endgame window and calculates
    an optimal resting maker bid inside the spread to capture the spread discount.

    Rules (from Reddit findings):
    - Confirmed favorite sits between 74¢ and 93¢ (crowd expects high win probability).
    - Window: Final 1.0m to 6.0m of 15m lifecycle.
    - Maker Limit Bid: Places resting bid inside the spread (1¢-2¢ below ask, or 1¢ above bid).
    - Avoids crossing the spread (-6¢ taker friction) and captures zero maker fees on Kalshi crypto.
    - Gated by choppiness filter (lead_changes <= 2) and portfolio directional cap (Comment p3ok6ft).
    """
    ticker = market.get("ticker", "")
    if rem_mins is not None and not (min_rem_mins <= rem_mins <= max_rem_mins):
        return None

    # Choppiness check: Reject hyper-bouncing markets
    lead_changes = get_market_lead_changes(ticker)
    max_lead = int(dynamic_filters.get("max_lead_changes", 2))
    if lead_changes > max_lead:
        log.info(f"⚠️ [CHOPPY MARKET SKIP] {ticker} has {lead_changes} lead changes (> {max_lead}). Skipping Favorite Maker entry.")
        return None

    yes_bid = quotes.get("yes_bid")
    yes_ask = quotes.get("yes_ask")
    no_bid = quotes.get("no_bid")
    no_ask = quotes.get("no_ask")

    # 1. Check YES as favorite (Ask between 74¢ and 93¢)
    if yes_ask is not None and min_fav_price <= yes_ask <= max_fav_price:
        if not client.is_side_allowed(ticker, "yes"):
            return None
        max_conc = int(dynamic_filters.get("max_crypto_directional_concurrent", 3))
        if hasattr(client, "check_crypto_portfolio_directional_cap"):
            if not client.check_crypto_portfolio_directional_cap("yes", max_concurrent=max_conc):
                return None

        maker_bid = max(yes_bid + 1, yes_ask - 2) if yes_bid is not None else (yes_ask - 2)
        maker_bid = max(min_fav_price, min(yes_ask - 1, maker_bid))
        return {
            "side": "yes",
            "maker_bid": maker_bid,
            "target_ask": yes_ask,
            "spread_saved": max(1, yes_ask - maker_bid),
            "favorite_type": "heavy_favorite_yes",
        }

    # 2. Check NO as favorite (Ask between 74¢ and 93¢ or YES Ask <= 26¢)
    if no_ask is not None and min_fav_price <= no_ask <= max_fav_price:
        if not client.is_side_allowed(ticker, "no"):
            return None
        max_conc = int(dynamic_filters.get("max_crypto_directional_concurrent", 3))
        if hasattr(client, "check_crypto_portfolio_directional_cap"):
            if not client.check_crypto_portfolio_directional_cap("no", max_concurrent=max_conc):
                return None

        maker_bid = max(no_bid + 1, no_ask - 2) if no_bid is not None else (no_ask - 2)
        maker_bid = max(min_fav_price, min(no_ask - 1, maker_bid))
        return {
            "side": "no",
            "maker_bid": maker_bid,
            "target_ask": no_ask,
            "spread_saved": max(1, no_ask - maker_bid),
            "favorite_type": "heavy_favorite_no",
        }

    return None


def _execute_favorite_maker_flow(
    ticker: str,
    side: str,
    maker_bid: int,
    target_ask: int,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None,
    client: BaseTradingClient,
    dry_run: bool,
    count: int = 1,
) -> tuple[bool, dict | None]:
    """Helper function to execute resting Favorite Maker limit bid inside the spread."""
    side_lower = side.lower()
    side_upper = side.upper()
    ex_name = getattr(client, "exchange_name", "Exchange")
    spread_saved = max(0, target_ask - maker_bid)

    log_prefix = LogTag.DRY_RUN_MATCH.value if dry_run else LogTag.BUY_MATCH.value
    log_action = (
        f"WOULD REST MAKER BID AT {maker_bid}¢ (Ask: {target_ask}¢, Spread Saved: +{spread_saved}¢). Hold to 100¢ settlement."
        if dry_run
        else f"Executing Resting Maker Bid at {maker_bid}¢ (Ask: {target_ask}¢, Spread Saved: +{spread_saved}¢)!"
    )
    log.info(
        f"{log_prefix} [FAVORITE MAKER] [{ex_name}] {ticker} -> Confirmed {side_upper} Favorite! {log_action}"
    )

    effective_buy = maker_bid
    buy_res = None
    if not dry_run:
        try:
            if hasattr(client, "buy_favorite_maker_order"):
                buy_res = client.buy_favorite_maker_order(
                    ticker=ticker,
                    side=side_lower,
                    maker_bid_cents=maker_bid,
                    target_ask_cents=target_ask,
                    count=count,
                    buy_order_timeout_seconds=60,
                )
                effective_buy = buy_res.get("buy_price_cents", maker_bid)
            else:
                buy_res = client.buy_and_set_take_profit(
                    ticker=ticker,
                    side=side_lower,
                    buy_price_cents=maker_bid,
                    take_profit_cents=99,
                    count=count,
                    maker_bid_cents=maker_bid,
                    buy_order_timeout_seconds=60,
                )
                effective_buy = buy_res.get("buy_price_cents", maker_bid)
        except Exception as error:
            log.error(f"❌ [{ex_name}] Failed to execute Favorite Maker trade on {ticker}: {error}")
            return False, None

    trade_details = {
        **({"dry_run": True} if dry_run else {}),
        "ex": ex_name.lower(),
        "action": "favorite_maker_opened",
        "strategy_tag": "endgame_favorite_maker",
        "order_role": "maker",
        "spread_saved_cents": spread_saved,
        "corridor": "favorite_maker",
        "side": side_lower,
        "buy": effective_buy,
        "count": count,
        "tp": 100,
        "sl": None,
        "outcome": determine_counterfactual_outcome(side_lower, "favorite_maker_opened", spot_price, target_price, market_result),
        "entry_ts": int(time.time()),
        "spot_at_entry": spot_price,
        "target_at_entry": target_price,
    }
    if not dry_run and isinstance(buy_res, dict):
        if cid := buy_res.get("client_order_id"):
            trade_details["client_order_id"] = cid
        if oid := buy_res.get("order_id"):
            trade_details["order_id"] = oid

    if getattr(client, "record_traded_market", None) is not None:
        client.record_traded_market(ticker, side_lower)

    return True, trade_details


def process_single_market(
    market: dict,
    is_trading_interval: bool,
    client: BaseTradingClient,
    now_utc: datetime,
    minute_past_hour: int,
    spot_cache: dict[str, float | None] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Evaluates pricing, risk rules, trade opportunities, and generates snapshot documents
    for a single 15-minute crypto prediction market.
    """
    ticker = market.get("ticker", "UNKNOWN")
    series_ticker = market.get("series_ticker", "UNKNOWN")
    asset_symbol = extract_asset_symbol(series_ticker, ticker)

    # 1. Parse top-of-book prices and bid-ask spread
    yes_price_cents = extract_price_cents(market, "yes", client=client)
    no_price_cents = extract_price_cents(market, "no", client=client)

    quotes = {
        k: parse_cents(market.get(f"{k}_dollars"), market.get(k))
        for k in ("yes_ask", "yes_bid", "no_ask", "no_bid")
    }
    sprd = (
        (quotes["yes_ask"] - quotes["yes_bid"])
        if (quotes["yes_ask"] is not None and quotes["yes_bid"] is not None)
        else None
    )

    # 2. Extract spot price from cache & strike target metrics
    spot_price = spot_cache.get(asset_symbol) if spot_cache is not None else get_spot_price(asset_symbol)
    target_price = extract_target_strike(market)

    price_diff, pct_dist = None, None
    if spot_price is not None and target_price is not None:
        try:
            p_float, tgt_float = float(spot_price), float(target_price)
            price_diff = round(p_float - tgt_float, 4)
            if tgt_float > 0:
                pct_dist = round((price_diff / tgt_float) * 100.0, 4)
        except (ValueError, TypeError):
            pass

    # 3. Time decay and market volume
    rem_mins = None
    if close_iso := (market.get("close_time") or market.get("expected_expiration_time")):
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            rem_mins = round(max(0.0, (close_dt - now_utc).total_seconds() / 60.0), 1)
        except Exception:
            pass

    vol_count = None
    if (vol_raw := (market.get("volume_fp") or market.get("volume_24h_fp"))) is not None:
        try:
            vol_count = int(round(float(vol_raw)))
        except (ValueError, TypeError):
            pass

    # 4. Strategy Rules & Dynamic Price Filter Lookup
    # Strategy Concept: Instead of hardcoded one-size-fits-all numbers, we ask
    # our in-memory engine for the optimal buy & sell targets for THIS specific coin and timing mark!
    minute_into_market = minute_past_hour % 15
    is_12_5min_mark = (minute_into_market == 12)
    is_stop_loss_interval = (minute_into_market == 10)
    is_7_5min_mark = (minute_into_market == 7)
    is_5min_mark = (minute_into_market == 5)
    is_2_5min_mark = (minute_into_market == 2)
    is_00_mark = (minute_into_market == 0)

    # :05 and :07.5 use tighter dynamic price filters (is_5min_mark=True); :00 and :02.5 use broad open filters
    is_tight_corridor = (minute_into_market in (5, 7))
    dynamic_filters = get_dynamic_filter_for_market(
        asset_symbol,
        is_5min_mark=is_tight_corridor,
        timeframe="15m",
        now_utc=now_utc,
    )
    streak_data = get_asset_settlement_streak(asset_symbol, timeframe="15m", now_utc=now_utc)
    min_buy_price = dynamic_filters["min_buy"]
    max_buy_price = dynamic_filters["max_buy"]
    take_profit_cents = dynamic_filters["tp"]
    is_exploration = dynamic_filters.get("is_exploration", False)
    is_restricted = dynamic_filters.get("is_restricted", False)
    base_contracts = int(dynamic_filters.get("contracts_per_trade", 1))

    trade_executed = False
    trade_details = None
    counterfactual_details = None
    fav_opp = None
    chosen_consensus = None

    # -----------------------------------------------------------------------
    # Step 4A: 12.5-Minute Endgame Safety Check (:12.5 mark - 2.5m before expiration)
    # At minute 12.5: If we are losing (loss > 0 or price < dynamic endgame floor e.g. < 25¢),
    # liquidate immediately so we don't lose all capital gains or suffer a 0¢ expiration loss!
    # If winning, let it run to 100¢ ($1.00 settlement).
    # -----------------------------------------------------------------------
    if is_12_5min_mark:
        sellable_yes_price = quotes["yes_bid"] if quotes["yes_bid"] is not None else yes_price_cents
        sellable_no_price = quotes["no_bid"] if quotes["no_bid"] is not None else no_price_cents
        endgame_floor = dynamic_filters.get("endgame_12_5_floor", 25)

        eg_res = client.evaluate_and_execute_12_5min_endgame_check(
            ticker=ticker,
            current_yes_price=sellable_yes_price,
            current_no_price=sellable_no_price,
            dry_run=dry_run,
            endgame_floor=endgame_floor,
        )
        if isinstance(eg_res, dict) and eg_res.get("triggered"):
            trade_executed = True
            eg_side = eg_res.get("side", "yes")
            ex_name = getattr(client, "exchange_name", "Exchange")
            action_name = eg_res.get("action", "endgame_loss_cut")
            trade_details = {
                "ex": ex_name.lower(),
                "action": action_name,
                "side": eg_side,
                "exit_price": eg_res.get("exit_price"),
                "loss_cents": eg_res.get("loss_cents"),
                "outcome": determine_counterfactual_outcome(
                    eg_side, action_name, spot_price, target_price, market.get("result")
                ),
            }
            if "profit_locked_cents" in eg_res:
                trade_details["profit_locked_cents"] = eg_res["profit_locked_cents"]

    # -----------------------------------------------------------------------
    # Step 4B: 10-Minute Winner-Bank & Stop-Loss Evaluation (:10 mark)
    # At minute 10:
    # 1. Winning positions (>= 70¢ / in profit): SELL at Bid to bank realized profit directly in cash!
    # 2. Losing positions: Execute immediate stop-loss liquidation.
    # -----------------------------------------------------------------------
    elif is_stop_loss_interval:
        sellable_yes_price = quotes["yes_bid"] if quotes["yes_bid"] is not None else yes_price_cents
        sellable_no_price = quotes["no_bid"] if quotes["no_bid"] is not None else no_price_cents

        sl_threshold = dynamic_filters.get("sl_threshold", 10)
        sl_floor = dynamic_filters.get("sl_floor", 30)

        sl_res = client.evaluate_and_execute_10min_strategy(
            ticker=ticker,
            current_yes_bid=sellable_yes_price,
            current_no_bid=sellable_no_price,
            current_yes_ask=quotes.get("yes_ask"),
            current_no_ask=quotes.get("no_ask"),
            dry_run=dry_run,
            stop_loss_threshold_cents=sl_threshold,
            price_floor=sl_floor,
        )
        if isinstance(sl_res, dict) and sl_res.get("triggered"):
            trade_executed = True
            sl_side = sl_res.get("side", "yes")
            ex_name = getattr(client, "exchange_name", "Exchange")
            action_name = sl_res.get("action", "stop_loss_sold")
            trade_details = {
                "ex": ex_name.lower(),
                "action": action_name,
                "side": sl_side,
                "exit_price": sl_res.get("exit_price"),
                "outcome": determine_counterfactual_outcome(
                    sl_side, action_name, spot_price, target_price, market.get("result")
                ),
            }
            if "loss_cents" in sl_res:
                trade_details["loss_cents"] = sl_res["loss_cents"]
            if "profit_locked_cents" in sl_res:
                trade_details["profit_locked_cents"] = sl_res["profit_locked_cents"]
            if "rebuy_price_cents" in sl_res:
                trade_details["rebuy_price_cents"] = sl_res["rebuy_price_cents"]

    # -----------------------------------------------------------------------
    # Step 4C: Buy Strategy & Early Cut-Loss Check (:00, :02.5, :05, :07.5...)
    # - :00 & :02.5: Deep discount buy entries (18¢-30¢) + :02.5 disaster cut-loss.
    # - :05 & :07.5: Clamped discount entries (18¢-24¢) + early cut-loss checks (<15¢).
    # Exclusively gated: We do NOT evaluate new buys if Stop-Loss just liquidated this market.
    # -----------------------------------------------------------------------
    elif is_trading_interval:
        if not dry_run:
            client.cancel_unfilled_buy_orders(ticker)

        cut_loss_executed = False
        pos = client.get_market_position(ticker)

        # Early Cut-Loss Check: Evaluate active positions at :02.5, :05, and :07.5 marks
        if pos is not None and (is_2_5min_mark or is_5min_mark or is_7_5min_mark):
            side = pos["side"]
            if is_2_5min_mark:
                cut_threshold = max(20, dynamic_filters.get("cut_5min", 40) - 5)
                action_tag = "2_5min_loss_cut"
            elif is_7_5min_mark:
                cut_threshold = dynamic_filters.get("cut_5min", 40)
                action_tag = "7_5min_loss_cut"
            else:
                cut_threshold = dynamic_filters.get("cut_5min", 40)
                action_tag = "early_loss_cut"

            cut_res = client.evaluate_and_execute_5min_cut_loss(
                ticker=ticker,
                current_yes_price=yes_price_cents,
                current_no_price=no_price_cents,
                min_price_threshold=cut_threshold,
                dry_run=dry_run,
            )
            if isinstance(cut_res, dict) and cut_res.get("triggered"):
                trade_executed = True
                ex_name = getattr(client, "exchange_name", "Exchange")
                trade_details = {
                    "ex": ex_name.lower(),
                    "action": action_tag,
                    "side": side,
                    "exit_price": cut_res.get("exit_price"),
                    "loss_cents": cut_res.get("loss_cents"),
                    "outcome": determine_counterfactual_outcome(
                        side, action_tag, spot_price, target_price, market.get("result")
                    ),
                }
                pos = None
                cut_loss_executed = True

        # Position Gates & Entry Evaluation
        ex_name = getattr(client, "exchange_name", "Exchange")
        has_floor = getattr(client, "has_profit_floor", lambda t: False)(ticker)
        if cut_loss_executed:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Early Cut-Loss executed for {ticker}. Skipping buy evaluation.")
        elif pos is not None:
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Already hold active {pos['side'].upper()} pos of {pos['count']} contract(s) for {ticker}. Skipping buy.")
            if not is_stop_loss_interval and not client.has_resting_take_profit_order(ticker) and not has_floor and not dry_run:
                safe_tp = resolve_safe_take_profit_for_position(
                    ticker=ticker,
                    client=client,
                    dynamic_filters=dynamic_filters,
                )
                client.place_take_profit_order(
                    ticker=ticker,
                    side=pos["side"],
                    count=pos["count"],
                    take_profit_cents=safe_tp,
                )
        elif client.has_existing_position_or_order(ticker):
            log.info(f"{LogTag.OPEN_POSITION_GATE.value} [{ex_name}] Already hold an active position or resting order for {ticker}. Skipping buy.")
        else:
            # Record current price snapshot to track market oscillation / lead changes
            if yes_price_cents is not None:
                record_market_price_snapshot(ticker, yes_price_cents, timestamp=now_utc.timestamp())

            # Priority 1: Endgame Favorite Maker Strategy (74¢–93¢ resting maker bid)
            fav_opp = evaluate_favorite_maker_opportunity(
                market=market,
                quotes=quotes,
                rem_mins=rem_mins,
                dynamic_filters=dynamic_filters,
                client=client,
                min_fav_price=int(dynamic_filters.get("favorite_maker_min_buy", 74)),
                max_fav_price=int(dynamic_filters.get("favorite_maker_max_buy", 93)),
            )
            if fav_opp is not None and dynamic_filters.get("enable_favorite_maker", True):
                fav_side = fav_opp["side"]
                fav_maker_bid = fav_opp["maker_bid"]
                fav_target_ask = fav_opp["target_ask"]
                trade_executed, trade_details = _execute_favorite_maker_flow(
                    ticker=ticker,
                    side=fav_side,
                    maker_bid=fav_maker_bid,
                    target_ask=fav_target_ask,
                    spot_price=spot_price,
                    target_price=target_price,
                    market_result=market.get("result"),
                    client=client,
                    dry_run=dry_run,
                    count=1,
                )

            # Priority 2: Screen for candidates across General Corridors if Favorite Maker not triggered
            if not trade_executed:
                avoid_mid_zone = dynamic_filters.get("avoid_mid_zone", True)
                mid_min = int(dynamic_filters.get("mid_zone_min", 31))
                mid_max = int(dynamic_filters.get("mid_zone_max", 61))

                pct_dist_val = 0.0
                if spot_price and target_price and target_price > 0:
                    pct_dist_val = ((spot_price - target_price) / target_price) * 100.0

                candidate_sides = []
                counterfactual_details = None
                for side, p_cents, aligned in [
                    ("yes", yes_price_cents, is_spot_momentum_aligned("yes", spot_price, target_price)),
                    ("no", no_price_cents, is_spot_momentum_aligned("no", spot_price, target_price)),
                ]:
                    if p_cents is None or not aligned:
                        continue

                    if not getattr(client, "is_side_allowed", lambda t, s: True)(ticker, side):
                        log.info(
                            f"🛡️ [SINGLE-SIDE LOCK] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                            f"Opposing side already traded for this contract lifecycle. Strictly blocking dual buy."
                        )
                        continue

                    # Corridor 1: Deep Discount Corridor
                    if min_buy_price <= p_cents <= max_buy_price:
                        candidate_sides.append({
                            "side": side,
                            "price": p_cents,
                            "tp": take_profit_cents,
                            "min_buy": min_buy_price,
                            "max_buy": max_buy_price,
                            "mode": "discount",
                        })

                    # Dead Coin-Flip Middle Zone
                    elif mid_min <= p_cents <= mid_max:
                        # Record counterfactual sample for empirical discovery in MongoDB without capital risk
                        counterfactual_details = {
                            "dry_run": True,
                            "ex": market.get("exchange", "kalshi"),
                            "action": "counterfactual_mid_zone",
                            "regime": "exploration_sim" if is_exploration else "empirical_tracking",
                            "corridor": "mid_zone",
                            "side": side,
                            "buy": p_cents,
                            "tp": take_profit_cents,
                            "sl": 25,
                            "outcome": determine_counterfactual_outcome(side, "buy_opened", spot_price, target_price, market.get("result")),
                            "entry_ts": int(time.time()),
                            "spot_at_entry": spot_price,
                            "target_at_entry": target_price,
                        }

                        if avoid_mid_zone:
                            log.info(
                                f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢) "
                                f"falls in low-EV coin-flip middle zone ({mid_min}¢-{mid_max}¢). Skipping live order (saved as empirical snapshot)."
                            )
                        elif not can_execute_exploration_trade(now_utc):
                            log.info(
                                f"🎯 [EXPLORATION CAP REACHED] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                                f"Session exploration trade cap ({MAX_EXPLORATION_TRADES_PER_SESSION} trades) reached. Skipping live order (saved as empirical snapshot)."
                            )
                        else:
                            log.info(
                                f"🎯 [MID-ZONE EXPLORATION] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                                f"Allowing coin-flip middle zone trade during discovery window (05:00-05:15 UTC, {get_exploration_trades_count()}/{MAX_EXPLORATION_TRADES_PER_SESSION} used) for empirical data collection."
                            )
                            candidate_sides.append({
                                "side": side,
                                "price": p_cents,
                                "tp": take_profit_cents,
                                "min_buy": mid_min,
                                "max_buy": mid_max,
                                "mode": "mid_zone",
                            })

                    # Corridor 2: Momentum / Trend Breakout Corridor
                    elif dynamic_filters.get("enable_momentum_buys", False):
                        mom_min = int(dynamic_filters.get("momentum_min_buy", 62))
                        mom_max = int(dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65))
                        min_lead = float(dynamic_filters.get("momentum_min_pct_dist", 0.15))
                        has_breakout_lead = (pct_dist_val >= min_lead) if side == "yes" else (pct_dist_val <= -min_lead)

                        if mom_min <= p_cents <= mom_max:
                            if has_breakout_lead:
                                mom_tp = int(dynamic_filters.get("momentum_tp", 88))
                                log.info(
                                    f"🚀 [MOMENTUM BREAKOUT SIGNAL] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                                    f"Spot lead {pct_dist_val:+.2f}% exceeds {min_lead}%. Targeting {mom_tp}¢ TP!"
                                )
                                candidate_sides.append({
                                    "side": side,
                                    "price": p_cents,
                                    "tp": mom_tp,
                                    "min_buy": mom_min,
                                    "max_buy": mom_max,
                                    "mode": "momentum",
                                })
                            else:
                                log.info(
                                    f"🚫 [MOMENTUM LEAD REJECTED] [{ex_name}] {ticker} ({side.upper()} @ {p_cents}¢): "
                                    f"Spot lead {pct_dist_val:+.2f}% is below required {min_lead}% breakout lead threshold. Skipping."
                                )

                if candidate_sides:
                    # -----------------------------------------------------------
                    # Ensemble Consensus & Settlement Streak Filter Evaluation
                    # (DeweyMarco Consensus Model + MongoDB Settlement Autocorrelation)
                    # -----------------------------------------------------------
                    # TODO (Improvement 2): Scan for intra-market synthetic arbitrage opportunities (yes_ask + no_ask <= 96¢)
                    # TODO (Improvement 3): Check portfolio R-risk budget caps (daily -3R / weekly -8R) and break-even hibernation
                    streak_data = get_asset_settlement_streak(asset_symbol, timeframe="15m", now_utc=now_utc)
                    lead_changes_count = get_market_lead_changes(ticker)

                    approved_candidates = []
                    for cand in candidate_sides:
                        cons = evaluate_ensemble_consensus(
                            symbol=asset_symbol,
                            proposed_side=cand["side"],
                            price_cents=cand["price"],
                            spot_price=spot_price,
                            target_price=target_price,
                            timeframe="15m",
                            dynamic_filters=dynamic_filters,
                            lead_changes=lead_changes_count,
                            streak_data=streak_data,
                            now_utc=now_utc,
                        )
                        cand["consensus"] = cons
                        if cons["is_approved"]:
                            approved_candidates.append(cand)
                        else:
                            log.info(
                                f"🛡️ [ENSEMBLE CONSENSUS BLOCKED] [{ex_name}] {ticker} ({cand['side'].upper()} @ {cand['price']}¢): "
                                f"{cons['reason']}"
                            )

                    if approved_candidates:
                        chosen_cand = min(approved_candidates, key=lambda x: x["price"])
                        chosen_side = chosen_cand["side"]
                        chosen_price = chosen_cand["price"]
                        chosen_tp = chosen_cand["tp"]
                        chosen_min = chosen_cand["min_buy"]
                        chosen_max = chosen_cand["max_buy"]
                        chosen_mode = chosen_cand["mode"]
                        chosen_consensus = chosen_cand.get("consensus", {})
                        chosen_bid = quotes["yes_bid"] if chosen_side == "yes" else quotes["no_bid"]

                        log.info(
                            f"🚀 [ENSEMBLE CONSENSUS APPROVED] [{ex_name}] {ticker} ({chosen_side.upper()} @ {chosen_price}¢) | "
                            f"Streak: {chosen_consensus.get('streak_length', 0)}x {chosen_consensus.get('streak_side') or 'none'} | "
                            f"Score: {chosen_consensus.get('consensus_score', 0):+.2f} | Regime: {chosen_consensus.get('regime')}"
                        )

                        # Dynamic bankroll sizing
                        balance_cents = None
                        if hasattr(client, "get_balance_cents"):
                            try:
                                b_val = client.get_balance_cents()
                                if isinstance(b_val, (int, float)):
                                    balance_cents = int(b_val)
                            except Exception:
                                pass

                        contracts_count = calculate_dynamic_contracts_count(
                            balance_cents=balance_cents,
                            buy_price_cents=chosen_price,
                            base_count=base_contracts,
                            is_restricted=is_restricted,
                        )

                        if is_restricted:
                            log.info(
                                f"⚠️ [DYNAMIC LOSS RESTRICTION] [{ex_name}] {ticker}: Asset {asset_symbol} is in restricted regime. "
                                f"Enforcing tight ceiling ({chosen_max}¢) and 1 contract sniper sizing."
                            )

                        if contracts_count <= 0 and balance_cents is not None:
                            log.info(
                                f"⚠️ [INSUFFICIENT BALANCE] [{ex_name}] {ticker}: Available balance {balance_cents}¢ "
                                f"is below contract price {chosen_price}¢. Skipping buy."
                            )
                        else:
                            contracts_to_buy = max(1, contracts_count)
                            trade_executed, trade_details = _execute_buy_flow(
                                ticker=ticker,
                                side=chosen_side,
                                price_cents=chosen_price,
                                maker_bid=chosen_bid,
                                min_buy=chosen_min,
                                max_buy=chosen_max,
                                take_profit_cents=chosen_tp,
                                spot_price=spot_price,
                                target_price=target_price,
                                market_result=market.get("result"),
                                client=client,
                                dry_run=dry_run,
                                is_exploration=is_exploration,
                                corridor_mode=chosen_mode,
                                count=contracts_to_buy,
                            )
                            if trade_executed and trade_details and chosen_consensus:
                                trade_details["consensus_score"] = chosen_consensus.get("consensus_score")
                                trade_details["consensus_regime"] = chosen_consensus.get("regime")
                                trade_details["streak_length"] = chosen_consensus.get("streak_length")

    # 5. Build Compact Snapshot Document
    cfb_spot = get_kalshi_cfbenchmarks_spot_price(asset_symbol, client=client) if asset_symbol not in COMMODITY_SYMBOLS and asset_symbol not in INDEX_SYMBOLS else None
    settle_src = get_market_settlement_source(market, client=client)
    lead_changes_count = get_market_lead_changes(ticker)
    fav_side = "yes" if (yes_price_cents or 0) >= 60 else "no" if (no_price_cents or 0) >= 60 else "coin_flip"

    compact_document = {
        "ts": int(now_utc.timestamp()),
        "m": minute_past_hour,
        "tf": "15m",
        "sym": asset_symbol,
        "tick": ticker,
        "ex": market.get("exchange", "kalshi"),
        "y": yes_price_cents,
        "n": no_price_cents,
        "y_bid": quotes["yes_bid"],
        "y_ask": quotes["yes_ask"],
        "n_bid": quotes["no_bid"],
        "n_ask": quotes["no_ask"],
        "y_bid_sz": parse_cents(None, market.get("yes_bid_size") or market.get("yes_bid_count")),
        "y_ask_sz": parse_cents(None, market.get("yes_ask_size") or market.get("yes_ask_count")),
        "n_bid_sz": parse_cents(None, market.get("no_bid_size") or market.get("no_bid_count")),
        "n_ask_sz": parse_cents(None, market.get("no_ask_size") or market.get("no_ask_count")),
        "sprd": sprd,
        "p": spot_price,
        "cfb": cfb_spot,
        "src": settle_src,
        "fav": fav_side,
        "fav_maker": bool(fav_opp is not None),
        "lead_chg": lead_changes_count,
        "tgt": target_price,
        "diff": price_diff,
        "pct": pct_dist,
        "rem": rem_mins,
        "vol": vol_count,
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time") or market.get("expected_expiration_time"),
        "imp_prob": round(yes_price_cents / 100.0, 4) if yes_price_cents is not None else None,
        "strk": streak_data.get("streak_length") if streak_data else None,
        "strk_side": streak_data.get("streak_side") if streak_data else None,
        "c_score": chosen_consensus.get("consensus_score") if chosen_consensus else None,
        "c_regime": chosen_consensus.get("regime") if chosen_consensus else None,
    }

    if trade_executed and trade_details:
        compact_document["trd"] = trade_details
    elif counterfactual_details is not None:
        compact_document["trd"] = counterfactual_details

    return compact_document


def process_hourly_market_group(
    symbol: str,
    markets: list[dict],
    client: BaseTradingClient,
    now_utc: datetime,
    minute_past_hour: int,
    spot_cache: dict[str, float | None] | None = None,
    dry_run: bool = False,
    force_trade_mark: bool = False,
    circuit_breaker_tripped: bool = False,
) -> list[dict]:
    """
    Evaluates pricing, risk rules, multi-strike selection, tiebreaker resolution,
    and lifecycle checkpoints for hourly crypto prediction markets belonging to a single asset.
    """
    if not markets:
        return []

    ex_name = getattr(client, "exchange_name", "Exchange")
    spot_price = spot_cache.get(symbol) if spot_cache is not None else get_spot_price(symbol)

    # Checkpoint interval flags
    is_hourly_00_or_10_entry = ((minute_past_hour in (0, 10)) or force_trade_mark) and not circuit_breaker_tripped
    is_hourly_20_or_30_entry = ((minute_past_hour in (20, 30)) or force_trade_mark) and not circuit_breaker_tripped
    is_hourly_trading_interval = is_hourly_00_or_10_entry or is_hourly_20_or_30_entry
    is_hourly_cut_loss = (minute_past_hour in (10, 20, 30)) and not force_trade_mark
    is_hourly_40min_stop_loss = (minute_past_hour == 40) and not force_trade_mark
    is_hourly_50min_endgame = (minute_past_hour == 50) and not force_trade_mark

    # Dynamic filter parameters (20-min and 30-min use tighter filter, 00-min and 10-min use broad open filter)
    is_tight_corridor = (minute_past_hour in (20, 30))
    dynamic_filters = get_dynamic_filter_for_market(
        symbol,
        is_5min_mark=is_tight_corridor,
        timeframe="1h",
        now_utc=now_utc,
    )
    streak_data = get_asset_settlement_streak(symbol, timeframe="1h", now_utc=now_utc)
    min_buy_price = dynamic_filters["min_buy"]
    max_buy_price = dynamic_filters["max_buy"]
    take_profit_cents = dynamic_filters["tp"]
    is_exploration = dynamic_filters.get("is_exploration", False)
    avoid_mid_zone = dynamic_filters.get("avoid_mid_zone", True)
    is_restricted = dynamic_filters.get("is_restricted", False)
    base_contracts = int(dynamic_filters.get("contracts_per_trade", 1 if is_restricted else 3))

    snapshots: list[dict] = []
    trade_executed_by_ticker: dict[str, dict] = {}
    counterfactual_by_ticker: dict[str, dict] = {}
    fav_opp_by_ticker: dict[str, dict] = {}

    # Pre-parse quote & strike metrics for all strikes in the group
    parsed_markets = []
    for market in markets:
        ticker = market.get("ticker", "UNKNOWN")
        series_ticker = market.get("series_ticker", "UNKNOWN")
        target_price = extract_target_strike(market)

        yes_price_cents = extract_price_cents(market, "yes", client=client)
        no_price_cents = extract_price_cents(market, "no", client=client)

        quotes = {
            k: parse_cents(market.get(f"{k}_dollars"), market.get(k))
            for k in ("yes_ask", "yes_bid", "no_ask", "no_bid")
        }
        sprd = (
            (quotes["yes_ask"] - quotes["yes_bid"])
            if (quotes["yes_ask"] is not None and quotes["yes_bid"] is not None)
            else None
        )

        price_diff, pct_dist = None, None
        if spot_price is not None and target_price is not None:
            try:
                p_float, tgt_float = float(spot_price), float(target_price)
                price_diff = round(p_float - tgt_float, 4)
                if tgt_float > 0:
                    pct_dist = round((price_diff / tgt_float) * 100.0, 4)
            except (ValueError, TypeError):
                pass

        rem_mins = None
        if close_iso := (market.get("close_time") or market.get("expected_expiration_time")):
            try:
                close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
                rem_mins = round(max(0.0, (close_dt - now_utc).total_seconds() / 60.0), 1)
            except Exception:
                pass

        vol_count = None
        if (vol_raw := (market.get("volume_fp") or market.get("volume_24h_fp"))) is not None:
            try:
                vol_count = int(round(float(vol_raw)))
            except (ValueError, TypeError):
                pass

        parsed_markets.append({
            "market": market,
            "ticker": ticker,
            "series_ticker": series_ticker,
            "target_price": target_price,
            "yes_price_cents": yes_price_cents,
            "no_price_cents": no_price_cents,
            "quotes": quotes,
            "sprd": sprd,
            "price_diff": price_diff,
            "pct_dist": pct_dist,
            "rem_mins": rem_mins,
            "vol_count": vol_count,
        })

    # Record live price snapshots for lead-change & choppiness tracking
    for item in parsed_markets:
        if item["yes_price_cents"] is not None:
            record_market_price_snapshot(item["ticker"], item["yes_price_cents"], timestamp=now_utc.timestamp())

    # -----------------------------------------------------------------------
    # Step 1: Hourly 50-Minute Endgame Safety Check (:50 mark)
    # -----------------------------------------------------------------------
    if is_hourly_50min_endgame:
        for item in parsed_markets:
            ticker = item["ticker"]
            pos = client.get_market_position(ticker)
            if pos is not None:
                quotes = item["quotes"]
                sellable_yes = quotes["yes_bid"] if quotes["yes_bid"] is not None else item["yes_price_cents"]
                sellable_no = quotes["no_bid"] if quotes["no_bid"] is not None else item["no_price_cents"]
                endgame_floor = dynamic_filters.get("endgame_12_5_floor", 15)

                eg_res = client.evaluate_and_execute_12_5min_endgame_check(
                    ticker=ticker,
                    current_yes_price=sellable_yes,
                    current_no_price=sellable_no,
                    dry_run=dry_run,
                    endgame_floor=endgame_floor,
                )
                if isinstance(eg_res, dict) and eg_res.get("triggered"):
                    trade_executed_by_ticker[ticker] = {
                        "ex": ex_name.lower(),
                        "action": "50min_loss_cut",
                        "side": eg_res.get("side", "yes"),
                        "exit_price": eg_res.get("exit_price"),
                        "loss_cents": eg_res.get("loss_cents"),
                        "outcome": determine_counterfactual_outcome(
                            eg_res.get("side", "yes"),
                            "50min_loss_cut",
                            spot_price,
                            item["target_price"],
                            item["market"].get("result"),
                        ),
                    }
                    if "profit_locked_cents" in eg_res:
                        trade_executed_by_ticker[ticker]["profit_locked_cents"] = eg_res["profit_locked_cents"]

    # -----------------------------------------------------------------------
    # Step 2: Hourly 40-Minute Stop-Loss & Winner-Roll Check (:40 mark)
    # -----------------------------------------------------------------------
    elif is_hourly_40min_stop_loss:
        for item in parsed_markets:
            ticker = item["ticker"]
            pos = client.get_market_position(ticker)
            if pos is not None:
                quotes = item["quotes"]
                sellable_yes = quotes["yes_bid"] if quotes["yes_bid"] is not None else item["yes_price_cents"]
                sellable_no = quotes["no_bid"] if quotes["no_bid"] is not None else item["no_price_cents"]

                sl_threshold = dynamic_filters.get("sl_threshold", 25)
                sl_floor = dynamic_filters.get("sl_floor", 15)

                sl_res = client.evaluate_and_execute_10min_strategy(
                    ticker=ticker,
                    current_yes_bid=sellable_yes,
                    current_no_bid=sellable_no,
                    current_yes_ask=quotes.get("yes_ask"),
                    current_no_ask=quotes.get("no_ask"),
                    dry_run=dry_run,
                    stop_loss_threshold_cents=sl_threshold,
                    price_floor=sl_floor,
                )
                if isinstance(sl_res, dict) and sl_res.get("triggered"):
                    sl_side = sl_res.get("side", "yes")
                    action_name = sl_res.get("action", "stop_loss_sold")
                    trade_details = {
                        "ex": ex_name.lower(),
                        "action": action_name,
                        "side": sl_side,
                        "exit_price": sl_res.get("exit_price"),
                        "outcome": determine_counterfactual_outcome(
                            sl_side, action_name, spot_price, item["target_price"], item["market"].get("result")
                        ),
                    }
                    if "loss_cents" in sl_res:
                        trade_details["loss_cents"] = sl_res["loss_cents"]
                    if "profit_locked_cents" in sl_res:
                        trade_details["profit_locked_cents"] = sl_res["profit_locked_cents"]
                    if "rebuy_price_cents" in sl_res:
                        trade_details["rebuy_price_cents"] = sl_res["rebuy_price_cents"]
                    trade_executed_by_ticker[ticker] = trade_details

    # -----------------------------------------------------------------------
    # Step 3: Hourly Buy Entry & Early Cut-Loss (:00, :10, :20, :30)
    # -----------------------------------------------------------------------
    elif is_hourly_trading_interval:
        # Step 3A: Hourly Early Cut-Loss Check (:10, :20, :30 marks)
        cut_loss_tickers = set()
        if is_hourly_cut_loss:
            if minute_past_hour == 10:
                cut_threshold = max(15, dynamic_filters.get("cut_5min", 15) - 5)
                action_tag = "10min_loss_cut"
            elif minute_past_hour == 20:
                cut_threshold = dynamic_filters.get("cut_5min", 15)
                action_tag = "20min_loss_cut"
            elif minute_past_hour == 30:
                cut_threshold = dynamic_filters.get("cut_5min", 15)
                action_tag = "30min_loss_cut"
            else:
                cut_threshold = dynamic_filters.get("cut_5min", 15)
                action_tag = "early_loss_cut"

            for item in parsed_markets:
                ticker = item["ticker"]
                pos = client.get_market_position(ticker)
                if pos is not None:
                    side = pos["side"]
                    cut_res = client.evaluate_and_execute_5min_cut_loss(
                        ticker=ticker,
                        current_yes_price=item["yes_price_cents"],
                        current_no_price=item["no_price_cents"],
                        min_price_threshold=cut_threshold,
                        dry_run=dry_run,
                    )
                    if isinstance(cut_res, dict) and cut_res.get("triggered"):
                        cut_loss_tickers.add(ticker)
                        trade_details = {
                            "ex": ex_name.lower(),
                            "action": action_tag,
                            "side": side,
                            "exit_price": cut_res.get("exit_price"),
                            "loss_cents": cut_res.get("loss_cents"),
                            "outcome": determine_counterfactual_outcome(
                                side, action_tag, spot_price, item["target_price"], item["market"].get("result")
                            ),
                        }
                        trade_executed_by_ticker[ticker] = trade_details

        # Step 3B: Prune stale unfilled buy orders
        if not dry_run:
            for item in parsed_markets:
                client.cancel_unfilled_buy_orders(item["ticker"])

        # Step 3C-0: Priority 1 - Scan for Endgame Favorite Maker opportunities across all strikes (74¢–93¢)
        # Empirical finding: Resting maker bids on confirmed favorites capture spread with 0% maker fees and positive EV.
        favorite_maker_executed = False
        fav_opp_by_ticker = {}
        if dynamic_filters.get("enable_favorite_maker", True):
            for item in parsed_markets:
                ticker = item["ticker"]
                if ticker in cut_loss_tickers or client.has_existing_position_or_order(ticker):
                    continue
                fav_opp = evaluate_favorite_maker_opportunity(
                    market=item["market"],
                    quotes=item["quotes"],
                    rem_mins=item["rem_mins"],
                    dynamic_filters=dynamic_filters,
                    client=client,
                    min_fav_price=int(dynamic_filters.get("favorite_maker_min_buy", 74)),
                    max_fav_price=int(dynamic_filters.get("favorite_maker_max_buy", 93)),
                    min_rem_mins=1.0,
                    max_rem_mins=25.0,
                )
                if fav_opp is not None:
                    fav_opp_by_ticker[ticker] = fav_opp

            if fav_opp_by_ticker:
                best_fav_ticker, best_fav = min(fav_opp_by_ticker.items(), key=lambda x: x[1]["maker_bid"])
                best_item = next(it for it in parsed_markets if it["ticker"] == best_fav_ticker)
                fav_executed, fav_details = _execute_favorite_maker_flow(
                    ticker=best_fav_ticker,
                    side=best_fav["side"],
                    maker_bid=best_fav["maker_bid"],
                    target_ask=best_fav["target_ask"],
                    spot_price=spot_price,
                    target_price=best_item["target_price"],
                    market_result=best_item["market"].get("result"),
                    client=client,
                    dry_run=dry_run,
                    count=1,
                )
                if fav_executed and fav_details:
                    trade_executed_by_ticker[best_fav_ticker] = fav_details
                    favorite_maker_executed = True

        # Step 3C: Collect valid BUY_YES and BUY_NO candidates across all strike options (if Favorite Maker not executed)
        valid_yes_candidates = []
        valid_no_candidates = []

        if not favorite_maker_executed:
            for item in parsed_markets:
                ticker = item["ticker"]
                target_price = item["target_price"]
                quotes = item["quotes"]
                pct_dist = item["pct_dist"]
                pct_dist_val = pct_dist if pct_dist is not None else 0.0

                # Position check: skip if we already hold an active position or resting order
                if ticker in cut_loss_tickers:
                    continue
                if client.has_existing_position_or_order(ticker):
                    pos = client.get_market_position(ticker)
                    has_floor = getattr(client, "has_profit_floor", lambda t: False)(ticker)
                    if pos is not None and not client.has_resting_take_profit_order(ticker) and not has_floor and not dry_run:
                        safe_tp = resolve_safe_take_profit_for_position(
                            ticker=ticker,
                            client=client,
                            dynamic_filters=dynamic_filters,
                        )
                        client.place_take_profit_order(
                            ticker=ticker,
                            side=pos["side"],
                            count=pos["count"],
                            take_profit_cents=safe_tp,
                        )
                    continue

                # Market Choppiness Filter: Skip markets with > 2 lead changes across 50¢
                max_lead = int(dynamic_filters.get("max_lead_changes", 2))
                if is_market_choppy(ticker, max_lead_changes=max_lead):
                    log.info(
                        f"⚠️ [CHOPPY MARKET SKIP] [{ex_name}] Hourly {ticker} has {get_market_lead_changes(ticker)} "
                        f"lead changes (> {max_lead} max). Skipping candidate evaluation to prevent whipsaw losses."
                    )
                    continue

                # Evaluate YES
                yes_p = item["yes_price_cents"]
                mid_min = int(dynamic_filters.get("mid_zone_min", 37))
                mid_max = int(dynamic_filters.get("mid_zone_max", 55))
                if yes_p is not None and getattr(client, "is_side_allowed", lambda t, s: True)(ticker, "yes"):
                    if is_spot_momentum_aligned("yes", spot_price, target_price):
                        # 1. Discount Corridor (18¢ - 44¢)
                        if min_buy_price <= yes_p <= max_buy_price:
                            if avoid_mid_zone and (37 <= yes_p <= max_buy_price) and pct_dist_val < 0.08:
                                log.info(
                                    f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (YES @ {yes_p}¢) "
                                    f"falls in conditional band (37¢-{max_buy_price}¢) without strong spot edge. Skipping."
                                )
                            else:
                                valid_yes_candidates.append({
                                    "item": item,
                                    "ticker": ticker,
                                    "side": "yes",
                                    "price": yes_p,
                                    "bid": quotes["yes_bid"],
                                    "target_price": target_price,
                                    "pct_dist": pct_dist,
                                    "tp": take_profit_cents,
                                    "min_buy": min_buy_price,
                                    "max_buy": max_buy_price,
                                    "mode": "discount",
                                })
                        # 2. Dead Coin-Flip Middle Zone
                        elif mid_min <= yes_p <= mid_max:
                            counterfactual_by_ticker[ticker] = {
                                "dry_run": True,
                                "ex": item["market"].get("exchange", "kalshi"),
                                "action": "counterfactual_mid_zone",
                                "regime": "exploration_sim" if is_exploration else "empirical_tracking",
                                "corridor": "mid_zone",
                                "side": "yes",
                                "buy": yes_p,
                                "tp": take_profit_cents,
                                "sl": 25,
                                "outcome": determine_counterfactual_outcome("yes", "buy_opened", spot_price, target_price, item["market"].get("result")),
                                "entry_ts": int(time.time()),
                                "spot_at_entry": spot_price,
                                "target_at_entry": target_price,
                            }
                            if avoid_mid_zone:
                                log.info(
                                    f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (YES @ {yes_p}¢) "
                                    f"falls in low-EV coin-flip middle zone ({mid_min}¢-{mid_max}¢). Skipping live order (saved as empirical snapshot)."
                                )
                            elif not can_execute_exploration_trade(now_utc):
                                log.info(
                                    f"🎯 [EXPLORATION CAP REACHED] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                    f"Session exploration trade cap ({MAX_EXPLORATION_TRADES_PER_SESSION} trades) reached. Skipping live order (saved as empirical snapshot)."
                                )
                            else:
                                log.info(
                                    f"🎯 [MID-ZONE EXPLORATION] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                    f"Allowing coin-flip middle zone trade during discovery window (05:00-05:15 UTC, {get_exploration_trades_count()}/{MAX_EXPLORATION_TRADES_PER_SESSION} used) for empirical data collection."
                                )
                                valid_yes_candidates.append({
                                    "item": item,
                                    "ticker": ticker,
                                    "side": "yes",
                                    "price": yes_p,
                                    "bid": quotes["yes_bid"],
                                    "target_price": target_price,
                                    "pct_dist": pct_dist,
                                    "tp": take_profit_cents,
                                    "min_buy": mid_min,
                                    "max_buy": mid_max,
                                    "mode": "mid_zone",
                                })
                        # 3. Momentum Breakout Corridor (56¢ - 68¢ at open, 56¢ - 65¢ at :20)
                        elif dynamic_filters.get("enable_momentum_buys", False):
                            mom_min = int(dynamic_filters.get("momentum_min_buy", 56))
                            mom_max = int(dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65))
                            min_lead = float(dynamic_filters.get("momentum_min_pct_dist", 0.10))

                            if mom_min <= yes_p <= mom_max:
                                if pct_dist_val >= min_lead:
                                    mom_tp = int(dynamic_filters.get("momentum_tp", 88))
                                    log.info(
                                        f"🚀 [MOMENTUM BREAKOUT SIGNAL] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                        f"Spot lead {pct_dist_val:+.2f}% exceeds {min_lead}%. Targeting {mom_tp}¢ TP!"
                                    )
                                    valid_yes_candidates.append({
                                        "item": item,
                                        "ticker": ticker,
                                        "side": "yes",
                                        "price": yes_p,
                                        "bid": quotes["yes_bid"],
                                        "target_price": target_price,
                                        "pct_dist": pct_dist,
                                        "tp": mom_tp,
                                        "min_buy": mom_min,
                                        "max_buy": mom_max,
                                        "mode": "momentum",
                                        "pct_dist": pct_dist,
                                    })
                                else:
                                    log.info(
                                        f"🚫 [MOMENTUM LEAD REJECTED] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                        f"Spot lead {pct_dist_val:+.2f}% is below required {min_lead}% breakout lead threshold. Skipping."
                                    )
                            else:
                                max_eval_price = dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65) if dynamic_filters.get("enable_momentum_buys", False) else max_buy_price
                                if min_buy_price <= yes_p <= max_eval_price:
                                    log.info(
                                        f"🚫 [SPOT MOMENTUM REJECTED] [{ex_name}] {ticker} (YES @ {yes_p}¢): "
                                        f"Spot ${spot_price} < Target ${target_price} (Diff: {item['price_diff']}). Bearish spot momentum."
                                    )

                # Evaluate NO
                no_p = item["no_price_cents"]
                if no_p is not None and getattr(client, "is_side_allowed", lambda t, s: True)(ticker, "no"):
                    if is_spot_momentum_aligned("no", spot_price, target_price):
                        # 1. Discount Corridor (18¢ - 44¢)
                        if min_buy_price <= no_p <= max_buy_price:
                            if avoid_mid_zone and (37 <= no_p <= max_buy_price) and pct_dist_val > -0.08:
                                log.info(
                                    f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (NO @ {no_p}¢) "
                                    f"falls in conditional band (37¢-{max_buy_price}¢) without strong spot edge. Skipping."
                                )
                            else:
                                valid_no_candidates.append({
                                    "item": item,
                                    "ticker": ticker,
                                    "side": "no",
                                    "price": no_p,
                                    "bid": quotes["no_bid"],
                                    "target_price": target_price,
                                    "pct_dist": pct_dist,
                                    "tp": take_profit_cents,
                                    "min_buy": min_buy_price,
                                    "max_buy": max_buy_price,
                                    "mode": "discount",
                                })
                        # 2. Dead Coin-Flip Middle Zone
                        elif mid_min <= no_p <= mid_max:
                            counterfactual_by_ticker[ticker] = {
                                "dry_run": True,
                                "ex": item["market"].get("exchange", "kalshi"),
                                "action": "counterfactual_mid_zone",
                                "regime": "exploration_sim" if is_exploration else "empirical_tracking",
                                "corridor": "mid_zone",
                                "side": "no",
                                "buy": no_p,
                                "tp": take_profit_cents,
                                "sl": 25,
                                "outcome": determine_counterfactual_outcome("no", "buy_opened", spot_price, target_price, item["market"].get("result")),
                                "entry_ts": int(time.time()),
                                "spot_at_entry": spot_price,
                                "target_at_entry": target_price,
                            }
                            if avoid_mid_zone:
                                log.info(
                                    f"{LogTag.PRICE_REJECTED.value} [{ex_name}] {ticker} (NO @ {no_p}¢) "
                                    f"falls in low-EV coin-flip middle zone ({mid_min}¢-{mid_max}¢). Skipping live order (saved as empirical snapshot)."
                                )
                            elif not can_execute_exploration_trade(now_utc):
                                log.info(
                                    f"🎯 [EXPLORATION CAP REACHED] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                    f"Session exploration trade cap ({MAX_EXPLORATION_TRADES_PER_SESSION} trades) reached. Skipping live order (saved as empirical snapshot)."
                                )
                            else:
                                log.info(
                                    f"🎯 [MID-ZONE EXPLORATION] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                    f"Allowing coin-flip middle zone trade during discovery window (05:00-05:15 UTC, {get_exploration_trades_count()}/{MAX_EXPLORATION_TRADES_PER_SESSION} used) for empirical data collection."
                                )
                                valid_no_candidates.append({
                                    "item": item,
                                    "ticker": ticker,
                                    "side": "no",
                                    "price": no_p,
                                    "bid": quotes["no_bid"],
                                    "target_price": target_price,
                                    "pct_dist": pct_dist,
                                    "tp": take_profit_cents,
                                    "min_buy": mid_min,
                                    "max_buy": mid_max,
                                    "mode": "mid_zone",
                                })
                        # 3. Momentum Breakout Corridor (56¢ - 68¢ at open, 56¢ - 65¢ at :20)
                        elif dynamic_filters.get("enable_momentum_buys", False):
                            mom_min = int(dynamic_filters.get("momentum_min_buy", 56))
                            mom_max = int(dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65))
                            min_lead = float(dynamic_filters.get("momentum_min_pct_dist", 0.10))

                            if mom_min <= no_p <= mom_max:
                                if pct_dist_val <= -min_lead:
                                    mom_tp = int(dynamic_filters.get("momentum_tp", 88))
                                    log.info(
                                        f"🚀 [MOMENTUM BREAKOUT SIGNAL] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                        f"Spot lead {pct_dist_val:+.2f}% exceeds {min_lead}%. Targeting {mom_tp}¢ TP!"
                                    )
                                    valid_no_candidates.append({
                                        "item": item,
                                        "ticker": ticker,
                                        "side": "no",
                                        "price": no_p,
                                        "bid": quotes["no_bid"],
                                        "target_price": target_price,
                                        "pct_dist": pct_dist,
                                        "tp": mom_tp,
                                        "min_buy": mom_min,
                                        "max_buy": mom_max,
                                        "mode": "momentum",
                                    })
                                else:
                                    log.info(
                                        f"🚫 [MOMENTUM LEAD REJECTED] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                        f"Spot lead {pct_dist_val:+.2f}% is below required {min_lead}% breakout lead threshold. Skipping."
                                    )
                        else:
                            max_eval_price = dynamic_filters.get("momentum_max_buy", 68 if not is_tight_corridor else 65) if dynamic_filters.get("enable_momentum_buys", False) else max_buy_price
                            if min_buy_price <= no_p <= max_eval_price:
                                log.info(
                                    f"🚫 [SPOT MOMENTUM REJECTED] [{ex_name}] {ticker} (NO @ {no_p}¢): "
                                    f"Spot ${spot_price} >= Target ${target_price} (Diff: {item['price_diff']}). Bullish spot momentum."
                                )

        # Step 3D: Multi-Strike Selection & Tiebreaker Execution
        best_yes = min(valid_yes_candidates, key=lambda c: c["price"]) if valid_yes_candidates else None
        best_no = min(valid_no_candidates, key=lambda c: c["price"]) if valid_no_candidates else None

        candidates_to_execute = []

        if best_yes and best_no:
            # Pick single best directional opportunity to eliminate dual-buy synthetic loss risk
            if best_yes["price"] < best_no["price"]:
                chosen = best_yes
                log.info(
                    f"🎯 [SINGLE-DIRECTION OPPORTUNITY CHOSEN] [{ex_name}] {symbol} Hourly: "
                    f"BUY_YES on {best_yes['ticker']} ({best_yes['price']}¢) is cheaper than BUY_NO on {best_no['ticker']} ({best_no['price']}¢). Picking BUY_YES."
                )
            elif best_no["price"] < best_yes["price"]:
                chosen = best_no
                log.info(
                    f"🎯 [SINGLE-DIRECTION OPPORTUNITY CHOSEN] [{ex_name}] {symbol} Hourly: "
                    f"BUY_NO on {best_no['ticker']} ({best_no['price']}¢) is cheaper than BUY_YES on {best_yes['ticker']} ({best_yes['price']}¢). Picking BUY_NO."
                )
            else:
                pct_val = best_yes.get("pct_dist") or 0.0
                chosen = best_yes if pct_val >= 0 else best_no
                log.info(
                    f"🎯 [SINGLE-DIRECTION OPPORTUNITY CHOSEN] [{ex_name}] {symbol} Hourly: "
                    f"YES and NO tied at {chosen['price']}¢. Picking {chosen['side'].upper()} on {chosen['ticker']} based on spot momentum ({pct_val:+.2f}%)."
                )
            candidates_to_execute.append(chosen)
        elif best_yes:
            candidates_to_execute.append(best_yes)
        elif best_no:
            candidates_to_execute.append(best_no)

        # -------------------------------------------------------------------
        # Ensemble Consensus & Settlement Streak Gating for Hourly Markets
        # (DeweyMarco Consensus Model + MongoDB Settlement Autocorrelation)
        # -------------------------------------------------------------------
        # TODO (Improvement 2): Scan for intra-market synthetic arbitrage opportunities across strikes (yes_ask + no_ask <= 96¢)
        # TODO (Improvement 3): Check portfolio R-risk budget caps (daily -3R / weekly -8R) and break-even hibernation
        streak_data = get_asset_settlement_streak(symbol, timeframe="1h", now_utc=now_utc)

        approved_hourly_candidates = []
        for cand in candidates_to_execute:
            cand_ticker = cand["ticker"]
            cand_side = cand["side"]
            cand_price = cand["price"]
            cand_target = cand.get("target_price")
            lead_changes_count = get_market_lead_changes(cand_ticker)

            cons = evaluate_ensemble_consensus(
                symbol=symbol,
                proposed_side=cand_side,
                price_cents=cand_price,
                spot_price=spot_price,
                target_price=cand_target,
                timeframe="1h",
                dynamic_filters=dynamic_filters,
                lead_changes=lead_changes_count,
                streak_data=streak_data,
                now_utc=now_utc,
            )
            cand["consensus"] = cons
            if cons["is_approved"]:
                approved_hourly_candidates.append(cand)
                log.info(
                    f"🚀 [HOURLY ENSEMBLE CONSENSUS APPROVED] [{ex_name}] {cand_ticker} ({cand_side.upper()} @ {cand_price}¢) | "
                    f"Streak: {cons.get('streak_length', 0)}x {cons.get('streak_side') or 'none'} | "
                    f"Score: {cons.get('consensus_score', 0):+.2f} | Regime: {cons.get('regime')}"
                )
            else:
                log.info(
                    f"🛡️ [HOURLY ENSEMBLE CONSENSUS BLOCKED] [{ex_name}] {cand_ticker} ({cand_side.upper()} @ {cand_price}¢): "
                    f"{cons['reason']}"
                )

        # Execute approved candidate orders
        for cand in approved_hourly_candidates:
            item = cand["item"]
            ticker = cand["ticker"]
            side = cand["side"]
            price_cents = cand["price"]
            maker_bid = cand["bid"]
            chosen_consensus = cand.get("consensus", {})

            # Dynamic bankroll sizing
            balance_cents = None
            if hasattr(client, "get_balance_cents"):
                try:
                    b_val = client.get_balance_cents()
                    if isinstance(b_val, (int, float)):
                        balance_cents = int(b_val)
                except Exception:
                    pass

            contracts_count = calculate_dynamic_contracts_count(
                balance_cents=balance_cents,
                buy_price_cents=price_cents,
                base_count=base_contracts,
                is_restricted=is_restricted,
            )

            if is_restricted:
                log.info(
                    f"⚠️ [DYNAMIC LOSS RESTRICTION] [{item['market'].get('exchange', 'kalshi')}] {ticker}: "
                    f"Asset {symbol} is in restricted regime. Enforcing tight corridor and 1 contract sizing."
                )

            if contracts_count <= 0 and balance_cents is not None:
                log.info(
                    f"⚠️ [INSUFFICIENT BALANCE] [{item['market'].get('exchange', 'kalshi')}] {ticker}: "
                    f"Available balance {balance_cents}¢ is below contract price {price_cents}¢. Skipping buy."
                )
                continue

            # Cross-Asset Portfolio Directional Risk Cap (Comment p3ok6ft):
            # Caps total concurrent crypto directional exposure across BTC/ETH/SOL to avoid correlated wipeouts
            max_conc = int(dynamic_filters.get("max_crypto_directional_concurrent", 3))
            if hasattr(client, "check_crypto_portfolio_directional_cap") and symbol not in COMMODITY_SYMBOLS and symbol not in INDEX_SYMBOLS:
                if not client.check_crypto_portfolio_directional_cap(side, max_concurrent=max_conc):
                    log.info(
                        f"🛡️ [HOURLY PORTFOLIO CAP REACHED] [{ex_name}] Max concurrent {side.upper()} crypto directional cap "
                        f"({max_conc}) reached. Skipping hourly buy on {ticker}."
                    )
                    continue

            contracts_to_buy = max(1, contracts_count)
            executed, details = _execute_buy_flow(
                ticker=ticker,
                side=side,
                price_cents=price_cents,
                maker_bid=maker_bid,
                min_buy=cand.get("min_buy", min_buy_price),
                max_buy=cand.get("max_buy", max_buy_price),
                take_profit_cents=cand.get("tp", take_profit_cents),
                spot_price=spot_price,
                target_price=cand["target_price"],
                market_result=item["market"].get("result"),
                client=client,
                dry_run=dry_run,
                is_exploration=is_exploration,
                corridor_mode=cand.get("mode", "discount"),
                count=contracts_to_buy,
            )
            if executed and details:
                if chosen_consensus:
                    details["consensus_score"] = chosen_consensus.get("consensus_score")
                    details["consensus_regime"] = chosen_consensus.get("regime")
                    details["streak_length"] = chosen_consensus.get("streak_length")
                trade_executed_by_ticker[ticker] = details

    # -----------------------------------------------------------------------
    # Step 4: Build Compact Snapshot Documents for all strikes in the group
    # -----------------------------------------------------------------------
    cfb_spot = get_kalshi_cfbenchmarks_spot_price(symbol, client=client) if symbol not in COMMODITY_SYMBOLS and symbol not in INDEX_SYMBOLS else None

    for item in parsed_markets:
        ticker = item["ticker"]
        quotes = item["quotes"]
        settle_src = get_market_settlement_source(item["market"], client=client)
        lead_changes_count = get_market_lead_changes(ticker)
        fav_side = "yes" if (item["yes_price_cents"] or 0) >= 60 else "no" if (item["no_price_cents"] or 0) >= 60 else "coin_flip"

        trd_info = trade_executed_by_ticker.get(ticker) or {}
        compact_doc = {
            "ts": int(now_utc.timestamp()),
            "m": minute_past_hour,
            "tf": "1h",
            "sym": symbol,
            "tick": ticker,
            "ex": item["market"].get("exchange", "kalshi"),
            "y": item["yes_price_cents"],
            "n": item["no_price_cents"],
            "y_bid": quotes["yes_bid"],
            "y_ask": quotes["yes_ask"],
            "n_bid": quotes["no_bid"],
            "n_ask": quotes["no_ask"],
            "y_bid_sz": parse_cents(None, item["market"].get("yes_bid_size") or item["market"].get("yes_bid_count")),
            "y_ask_sz": parse_cents(None, item["market"].get("yes_ask_size") or item["market"].get("yes_ask_count")),
            "n_bid_sz": parse_cents(None, item["market"].get("no_bid_size") or item["market"].get("no_bid_count")),
            "n_ask_sz": parse_cents(None, item["market"].get("no_ask_size") or item["market"].get("no_ask_count")),
            "sprd": item["sprd"],
            "p": spot_price,
            "cfb": cfb_spot,
            "src": settle_src,
            "fav": fav_side,
            "fav_maker": bool(ticker in fav_opp_by_ticker),
            "lead_chg": lead_changes_count,
            "tgt": item["target_price"],
            "diff": item["price_diff"],
            "pct": item["pct_dist"],
            "rem": item["rem_mins"],
            "vol": item["vol_count"],
            "open_time": item["market"].get("open_time"),
            "close_time": item["market"].get("close_time") or item["market"].get("expected_expiration_time"),
            "imp_prob": round(item["yes_price_cents"] / 100.0, 4) if item["yes_price_cents"] is not None else None,
            "strk": streak_data.get("streak_length") if streak_data else None,
            "strk_side": streak_data.get("streak_side") if streak_data else None,
            "c_score": trd_info.get("consensus_score"),
            "c_regime": trd_info.get("consensus_regime"),
        }
        if ticker in trade_executed_by_ticker:
            compact_doc["trd"] = trade_executed_by_ticker[ticker]
        elif ticker in counterfactual_by_ticker:
            compact_doc["trd"] = counterfactual_by_ticker[ticker]

        snapshots.append(compact_doc)

    return snapshots


# Session Trading Loss Circuit Breaker ($5.00 max trading loss, immune to cash withdrawals)
_SESSION_INITIAL_REALIZED_PNL: float | None = None
MAX_SESSION_LOSS_DOLLARS: float = 5.00


def run_pipeline_pass(
    clients: list[tuple[str, BaseTradingClient]] | None = None,
    dry_run: bool = False,
    force_trade_mark: bool = False,
):
    """
    Executes a single market processing & trading pass across all active exchanges
    for BOTH 15-minute and Hourly multi-strike crypto prediction markets.
    Accepts long-lived exchange clients to prevent socket leaks and connection overhead.
    """
    global _SESSION_INITIAL_REALIZED_PNL
    
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute

    minute_into_market_15m = current_minute % 15
    is_15m_open_ready = (minute_into_market_15m == 0) and (now_utc.second >= 20 or force_trade_mark)
    is_15m_trading_interval = is_15m_open_ready or (minute_into_market_15m in (2, 5, 7)) or force_trade_mark
    is_15m_10min_interval = (minute_into_market_15m == 10) and not force_trade_mark
    is_15m_12_5min_interval = (minute_into_market_15m == 12) and not force_trade_mark

    is_hourly_00_or_10_entry = (current_minute in (0, 10)) or force_trade_mark
    is_hourly_20_or_30_mark = (current_minute in (20, 30)) and not force_trade_mark
    is_hourly_40_mark = (current_minute == 40) and not force_trade_mark
    is_hourly_50_mark = (current_minute == 50) and not force_trade_mark

    # Human-readable interval descriptions
    intervals_active = []
    if is_15m_trading_interval:
        if minute_into_market_15m == 2:
            intervals_active.append("15M :02.5 2ND CHANCE BUY & DISASTER CUT")
        elif minute_into_market_15m == 5:
            intervals_active.append("15M :05 BUY ENTRY / 5M CUT")
        elif minute_into_market_15m == 7:
            intervals_active.append("15M :07.5 MID-CANDLE BUY ENTRY / CUT")
        else:
            intervals_active.append("15M :00 BUY ENTRY")
    elif is_15m_10min_interval:
        intervals_active.append("15M WINNER ROLL / SL")
    elif is_15m_12_5min_interval:
        intervals_active.append("15M ENDGAME SAFETY CHECK")

    if current_minute == 0 or force_trade_mark:
        intervals_active.append("HOURLY 00M MULTI-STRIKE ENTRY")
    elif current_minute == 10:
        intervals_active.append("HOURLY 10M 2ND CHANCE ENTRY & DISASTER CUT")
    elif current_minute == 20:
        intervals_active.append("HOURLY 20M CUT-LOSS & 3RD ENTRY")
    elif current_minute == 30:
        intervals_active.append("HOURLY 30M MID-HOUR CUT-LOSS & ENTRY")
    elif current_minute == 40:
        intervals_active.append("HOURLY 40M WINNER ROLL & SL")
    elif current_minute == 50:
        intervals_active.append("HOURLY 50M ENDGAME SAFETY CHECK")

    interval_summary = " + ".join(intervals_active) if intervals_active else "DATA COLLECTION ONLY MARK"

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {now_utc.isoformat()} (Minute :{current_minute:02d}:{now_utc.second:02d})")
    if dry_run:
        log.info(f"--> MODE: {LogTag.DRY_RUN_PREVIEW.value} (No real orders will be placed)")
    if is_default_exploration_window(now_utc):
        mid_zone_note = " (including 45¢–55¢ mid-zone)" if is_mid_zone_exploration_window(now_utc) else " (45¢–55¢ mid-zone avoided)"
        cap_note = f" [Cap: {get_exploration_trades_count()}/{MAX_EXPLORATION_TRADES_PER_SESSION} live trades used]"
        log.info(f"--> REGIME: 🌍 [EXPLORATION REGIME] Active UTC window 05:00–05:15 (1:00AM–1:15AM ET): Trading on broad corridors (:00 -> 18¢–44¢, :05/20m -> 18¢–30¢){mid_zone_note}{cap_note} for empirical discovery.")
    log.info(f"--> INTERVAL: {interval_summary}")
    log.info("=================================================================")

    # Ensure dynamic price filters are in memory (auto-refreshes every 6 hours, at 11:00 UTC anchor, or on cold start)
    get_dynamic_price_filter_map(now_utc=now_utc)

    # Initialize fallback clients if persistent instances were not passed
    active_clients = []
    if clients is None:
        try:
            active_clients.append(("Kalshi", KalshiClient()))
        except Exception as error:
            log.error(f"Could not initialize KalshiClient: {error}")

        # Gemini trading disabled / commented out:
        # try:
        #     active_clients.append(("Gemini", GeminiClient()))
        # except Exception as error:
        #     log.error(f"Could not initialize GeminiClient: {error}")
    else:
        active_clients = clients

    if not active_clients:
        log.error("No active exchange clients initialized. Exiting pipeline pass.")
        return

    # Batch-fetch all underlying spot prices across Crypto, Commodities (Pyth), and Indices (Yahoo)
    spot_price_cache = batch_fetch_spot_prices(SUPPORTED_SYMBOLS)

    snapshots_to_save = []
    for exchange_name, client in active_clients:
        log.info(f"\n--- [{exchange_name.upper()} EXCHANGE PIPELINE] ---")

        # Fetch real-time Portfolio PnL & Balance
        circuit_breaker_tripped = False
        try:
            pnl = client.get_portfolio_pnl_summary()
            pnl["ts"] = int(now_utc.timestamp())
            pnl["exchange"] = exchange_name.lower()
            sign = "+" if pnl.get("total_realized_pnl_dollars", 0) >= 0 else ""

            # Circuit Breaker Evaluation (Trading loss >= $5.00 limit, immune to cash withdrawals)
            current_realized_pnl = float(pnl.get("total_realized_pnl_dollars") or 0.0)
            if _SESSION_INITIAL_REALIZED_PNL is None:
                _SESSION_INITIAL_REALIZED_PNL = current_realized_pnl

            session_trading_loss = round(_SESSION_INITIAL_REALIZED_PNL - current_realized_pnl, 2)
            circuit_breaker_tripped = (session_trading_loss >= MAX_SESSION_LOSS_DOLLARS)

            log.info(
                f"{LogTag.PORTFOLIO_PNL.value} [{exchange_name}] Cash: ${pnl.get('balance_dollars', 0.0):.2f} | "
                f"Port Value: ${pnl.get('portfolio_value_dollars', 0.0):.2f} | "
                f"Settled PnL: {sign}${pnl.get('total_realized_pnl_dollars', 0.0):.2f} "
                f"({sign}{pnl.get('total_realized_pnl_cents', 0)}¢ across {pnl.get('settlements_count', 0)} mkts) | "
                f"Active: {pnl.get('open_positions_count', 0)} pos (${pnl.get('open_exposure_dollars', 0.0):.2f})"
            )

            if circuit_breaker_tripped and not force_trade_mark:
                log.warning(
                    f"🛑 [LOSS CIRCUIT BREAKER ACTIVE] Session trading loss is -${session_trading_loss:.2f} "
                    f"(exceeds ${MAX_SESSION_LOSS_DOLLARS:.2f} limit). New buy entries are halted to preserve capital."
                )
                is_15m_trading_interval = False

            if not dry_run:
                save_portfolio_pnl(pnl)
                if pnl.get("settlements"):
                    save_settlements(pnl["settlements"])
        except Exception as err:
            log.warning(f"Could not retrieve portfolio PnL summary from {exchange_name}: {err}")

        # -------------------------------------------------------------------
        # 1. 15-Minute Markets Pipeline (Crypto & Commodities)
        # -------------------------------------------------------------------
        log.info(f"Fetching active 15-minute markets from {exchange_name}...")
        try:
            markets_15m = client.get_15m_crypto_markets(status="open")
            log.info(f"Retrieved {len(markets_15m)} active 15-minute markets from {exchange_name}.")
        except Exception as error:
            log.error(f"Failed to fetch 15-minute markets from {exchange_name}: {error}")
            markets_15m = []

        for market in markets_15m:
            tick = market.get("ticker", "UNKNOWN")
            try:
                snapshot = process_single_market(
                    market,
                    is_15m_trading_interval,
                    client,
                    now_utc,
                    current_minute,
                    spot_cache=spot_price_cache,
                    dry_run=dry_run,
                )
                snapshot["ex"] = exchange_name.lower()
                snapshots_to_save.append(snapshot)

                diff_val = snapshot.get("diff")
                diff_str = f"+{diff_val}" if (diff_val is not None and diff_val > 0) else str(diff_val)
                log.info(
                    f"  • [{exchange_name}] [15M] [{snapshot['sym']} | {snapshot['tick']}] -> "
                    f"YES: {snapshot['y']}¢ | NO: {snapshot['n']}¢ | "
                    f"Price: ${snapshot.get('p')} | Target: ${snapshot.get('tgt')} | Diff: {diff_str}"
                )
            except Exception as mkt_err:
                log.error(f"❌ [{exchange_name}] Unhandled error processing 15M market {tick}: {mkt_err}", exc_info=True)

        # -------------------------------------------------------------------
        # 2. Hourly Multi-Strike Markets Pipeline (Crypto, Commodities, Equity Indices)
        # -------------------------------------------------------------------
        if hasattr(client, "get_hourly_crypto_markets"):
            log.info(f"Fetching active hourly multi-strike markets from {exchange_name}...")
            try:
                markets_1h = client.get_hourly_crypto_markets(
                    status="open", current_hour_only=True, now_utc=now_utc
                )
                log.info(f"Retrieved {len(markets_1h)} active hourly strike markets from {exchange_name}.")
            except Exception as error:
                log.error(f"Failed to fetch hourly markets from {exchange_name}: {error}")
                markets_1h = []

            # Group hourly markets by symbol (e.g. BTC, GOLD, WTI, INX, NASDAQ100, etc.)
            hourly_by_symbol = defaultdict(list)
            for m in markets_1h:
                series_tick = m.get("series_ticker", "UNKNOWN")
                tick = m.get("ticker", "UNKNOWN")
                sym = extract_asset_symbol(series_tick, tick)
                hourly_by_symbol[sym].append(m)

            for sym, sym_markets in hourly_by_symbol.items():
                try:
                    hourly_snapshots = process_hourly_market_group(
                        symbol=sym,
                        markets=sym_markets,
                        client=client,
                        now_utc=now_utc,
                        minute_past_hour=current_minute,
                        spot_cache=spot_price_cache,
                        dry_run=dry_run,
                        force_trade_mark=force_trade_mark,
                        circuit_breaker_tripped=circuit_breaker_tripped,
                    )
                    for s in hourly_snapshots:
                        s["ex"] = exchange_name.lower()
                        snapshots_to_save.append(s)

                        # Render Log Optimization:
                        # Only console.log strikes where YES or NO is in the broad tradeable corridor (20¢ - 80¢)
                        # or where a trade action occurred. This eliminates 300+ lines of out-of-the-money 1¢ strikes.
                        y_val = s.get("y")
                        n_val = s.get("n")
                        has_trade = s.get("trd") is not None
                        in_broad_corridor = (
                            (y_val is not None and 20 <= y_val <= 80)
                            or (n_val is not None and 20 <= n_val <= 80)
                        )

                        if has_trade or in_broad_corridor:
                            diff_val = s.get("diff")
                            diff_str = f"+{diff_val}" if (diff_val is not None and diff_val > 0) else str(diff_val)
                            log.info(
                                f"  • [{exchange_name}] [1H] [{s['sym']} | {s['tick']}] -> "
                                f"YES: {s['y']}¢ | NO: {s['n']}¢ | "
                                f"Price: ${s.get('p')} | Target: ${s.get('tgt')} | Diff: {diff_str}"
                            )
                except Exception as sym_err:
                    log.error(f"❌ [{exchange_name}] Unhandled error processing hourly group for {sym}: {sym_err}", exc_info=True)

    # Save valid market snapshots to MongoDB Atlas
    # We save snapshots on regular checkpoints (:00, :02.5, :05, :07.5, :10, :12.5, :20, :30, :40, :50) OR whenever a trade occurs
    is_standard_save_mark = (minute_into_market_15m in (0, 2, 5, 7, 10, 12)) or (current_minute in (0, 10, 20, 30, 40, 50))
    valid_snapshots = [
        s for s in snapshots_to_save
        if (s.get("y") is not None or s.get("n") is not None) and (is_standard_save_mark or s.get("trd") is not None)
    ]
    if valid_snapshots and not dry_run:
        log.info("\nSaving ultra-compact market snapshot data to MongoDB Atlas...")
        save_market_snapshots(valid_snapshots)
    elif dry_run:
        log.info(f"\n{LogTag.DRY_RUN_PREVIEW.value} Skipped saving snapshots to MongoDB Atlas in Dry-Run mode.")
    elif not is_standard_save_mark:
        log.info("\nInterim data-collection pass complete (compact storage policy: snapshots saved at :00, :02.5, :05, :07.5, :10, :12.5, :20, :30, :40, :50 marks).")
    else:
        log.warning("\nNo valid price snapshots retrieved in this pass. Skipping MongoDB Atlas save.")
    log.info("Pass complete.")


def main():
    """Main continuous background worker loop."""
    args = sys.argv[1:]
    dry_run = "--dry-run" in args or "-d" in args
    force_trade_mark = "--force-trade" in args or "-f" in args
    run_once = "--once" in args or "-o" in args

    # Initialize long-lived persistent exchange clients ONCE at startup
    persistent_clients = []
    try:
        persistent_clients.append(("Kalshi", KalshiClient()))
    except Exception as error:
        log.error(f"Could not initialize persistent KalshiClient: {error}")

    # Gemini trading disabled / commented out:
    # try:
    #     persistent_clients.append(("Gemini", GeminiClient()))
    # except Exception as error:
    #     log.error(f"Could not initialize persistent GeminiClient: {error}")

    if run_once or dry_run:
        run_pipeline_pass(clients=persistent_clients, dry_run=dry_run, force_trade_mark=force_trade_mark)
        return

    log.info("Starting Trading Bot in Continuous Render Background Worker Mode...")

    while True:
        try:
            run_pipeline_pass(clients=persistent_clients, dry_run=dry_run, force_trade_mark=force_trade_mark)
        except Exception as error:
            log.error(f"Unexpected error in loop pass: {error}")

        sleep_seconds = calculate_seconds_until_next_checkpoint(offset_seconds=30.0)
        target_utc = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
        log.info(
            f"\n{LogTag.CLOCK_ALIGNMENT.value} Sleeping {sleep_seconds:.1f}s until next checkpoint at UTC {target_utc.strftime('%H:%M:%S')}...\n"
        )
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
