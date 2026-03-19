"""
yfinance client for S&P 500 (primary) and backup for crypto/stocks.
No API key required. Tickers: ^GSPC (S&P 500), BTC-USD, ETH-USD, SOL-USD, etc.
Falls back to CoinGecko for crypto when Yahoo is blocked (proxy, PiHole, etc.).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")

_DEBUG = os.getenv("YFINANCE_DEBUG", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# CoinGecko fallback (when Yahoo is blocked)
# ---------------------------------------------------------------------------
# CoinGecko id -> yfinance ticker (fallback when Yahoo fails)
_COINGECKO_IDS: dict[str, str] = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "solana": "SOL-USD",
}


def _coingecko_snapshot(ticker: str) -> float | None:
    """Fallback: get crypto price from CoinGecko (no key, free tier)."""
    id_map = {v: k for k, v in _COINGECKO_IDS.items()}
    cg_id = id_map.get(ticker)
    if not cg_id:
        return None
    try:
        import requests

        r = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and cg_id in data:
            price = (data[cg_id] or {}).get("usd")
            if price is not None:
                return float(price)
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] CoinGecko fallback failed for {ticker}: {e}")
    return None


def get_snapshot(ticker: str) -> float | None:
    """
    Get current/latest price. Returns None on failure.
    Crypto: CoinGecko first (often works when Yahoo is blocked), else yfinance.
    S&P 500 (^GSPC): yfinance, fallback to SPY × 10.
    """
    # ^GSPC: try yfinance, then SPY. S&P: yfinance.
    to_try: list[tuple[str, bool]] = []
    if ticker in _COINGECKO_IDS.values():
        to_try = [(ticker, True), (ticker, False)]  # CoinGecko first for crypto
    elif ticker in ("^GSPC", "GSPC"):
        to_try = [(ticker, False), ("SPY", False)]
    else:
        to_try = [(ticker, False)]

    for t, use_coingecko in to_try:
        if use_coingecko:
            p = _coingecko_snapshot(t)
            if p is not None:
                return p
            continue
        try:
            yf_t = yf.Ticker(t)
            hist = yf_t.history(period="5d", interval="1d")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                price = float(hist["Close"].iloc[-1])
                if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                    price *= 10.0
                return price
            info = yf_t.info
            if isinstance(info, dict):
                for key in ("regularMarketPrice", "previousClose", "open"):
                    if info.get(key) is not None:
                        price = float(info[key])
                        if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                            price *= 10.0
                        return price
        except Exception as e:
            if _DEBUG:
                print(f"  [yfinance_client] yfinance failed for {t}: {e}")
            continue
    return None


# Fallback tickers when primary fails (e.g. ^GSPC blocked by proxy, SPY often works)
_TICKER_FALLBACKS: dict[str, list[str]] = {
    "^GSPC": ["SPY"],   # S&P 500 index -> SPY ETF (same index, different scale ~1/10)
    "GSPC": ["^GSPC", "SPY"],
}


def _coingecko_aggs(ticker: str, days_back: int) -> list[float]:
    """Fallback: get crypto daily closes from CoinGecko (no key)."""
    id_map = {v: k for k, v in _COINGECKO_IDS.items()}
    cg_id = id_map.get(ticker)
    if not cg_id:
        return []
    try:
        import requests
        import time
        end_ts = int(time.time())
        start_ts = end_ts - (days_back * 86400)
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart/range"
            f"?vs_currency=usd&from={start_ts}&to={end_ts}",
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        prices = (data or {}).get("prices") or []
        if not prices:
            return []
        # prices = [[ts, price], ...]; take last price per day for daily closes
        by_day: dict[int, float] = {}
        for ts, p in prices:
            day = int(ts) // 86400
            by_day[day] = float(p)
        days = sorted(by_day.keys())
        return [by_day[d] for d in days]
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] CoinGecko aggs failed for {ticker}: {e}")
        return []


def get_aggs(ticker: str, days_back: int = 30, config: dict | None = None) -> list[float]:
    """
    Get historical daily closes. Returns list of close prices (oldest first).
    Tries fallback tickers if primary fails (e.g. ^GSPC -> SPY for S&P 500).
    For crypto, tries CoinGecko when Yahoo fails.
    Uses local storage cache when enabled (faster runs, fewer API calls).
    """
    cfg = config
    if cfg is None:
        try:
            from config import get_config
            cfg = get_config()
        except Exception:
            cfg = {}

    # Check cache first (storage.storage_enabled and cache TTL)
    try:
        from storage import get_yf_closes
        cached = get_yf_closes(ticker, days_back, cfg)
        if cached:
            return cached
    except ImportError:
        pass

    result: list[float] = []
    # For crypto: try CoinGecko first (Yahoo often blocked)
    if ticker in _COINGECKO_IDS.values():
        result = _coingecko_aggs(ticker, days_back)
    if not result:
        tickers_to_try: list[str] = [ticker]
        tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
        for t in tickers_to_try:
            try:
                end = datetime.utcnow()
                start = end - timedelta(days=days_back)
                hist = yf.download(t, start=start, end=end, interval="1d", progress=False)
                if hist is None or hist.empty:
                    continue
                closes_raw = None
                if hasattr(hist.columns, "get_level_values") and "Close" in hist.columns.get_level_values(0):
                    closes = hist["Close"]
                    if hasattr(closes, "ndim") and closes.ndim > 1:
                        closes = closes.iloc[:, 0]
                    closes_raw = closes.dropna()
                elif "Close" in hist.columns:
                    closes_raw = hist["Close"].dropna()
                if closes_raw is not None and len(closes_raw) > 0:
                    result = [float(x) for x in closes_raw.tolist()]
                    if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                        result = [x * 10.0 for x in result]
                    break
            except Exception:
                continue

    if result:
        try:
            from storage import set_yf_closes
            set_yf_closes(ticker, days_back, result, cfg)
        except ImportError:
            pass
    return result


UTC = ZoneInfo("UTC")


def get_intraday_aggs_at_hour(
    ticker: str,
    days_back: int,
    resolution_hour_et: int,
    config: dict | None = None,
) -> list[float]:
    """
    Get historical prices at a specific hour (ET) for each day. For intraday empirical P.
    Uses yfinance 1h data. Falls back to daily closes if intraday fails.
    resolution_hour_et: 0-23 (e.g. 17 = 5pm ET).
    Returns list of prices (oldest first), one per day. Fewer days than days_back if 1h data limited.
    """
    cfg = config
    if cfg is None:
        try:
            from config import get_config
            cfg = get_config()
        except Exception:
            cfg = {}

    # Check storage cache (keyed by ticker, days_back, resolution_hour)
    try:
        from storage import get_yf_intraday_at_hour
        cached = get_yf_intraday_at_hour(ticker, days_back, resolution_hour_et, cfg)
        if cached:
            return cached
    except ImportError:
        pass

    # yfinance 1h: period up to ~60d for most tickers (Yahoo limits intraday range)
    period_days = min(60, max(7, days_back))
    result: list[float] = []
    tickers_to_try = [ticker] + _TICKER_FALLBACKS.get(ticker, [])

    for t in tickers_to_try:
        try:
            end = datetime.now(UTC)
            start = end - timedelta(days=period_days)
            hist = yf.download(t, start=start, end=end, interval="1h", progress=False, auto_adjust=True)
            if hist is None or hist.empty or "Close" not in hist.columns:
                continue
            closes_col = hist["Close"]
            if hasattr(closes_col, "ndim") and closes_col.ndim > 1:
                closes_col = closes_col.iloc[:, 0]
            closes_col = closes_col.dropna()
            if len(closes_col) < 24:
                continue

            # For each calendar day, find bar at resolution_hour_et (ET)
            by_date: dict[str, float] = {}
            for ts, row in hist.iterrows():
                if "Close" not in row.index:
                    continue
                try:
                    if hasattr(ts, "tz_localize") and ts.tzinfo is None:
                        ts_utc = ts.tz_localize(UTC)
                    elif hasattr(ts, "tz_convert"):
                        ts_utc = ts.tz_convert(UTC) if ts.tzinfo else ts.tz_localize(UTC)
                    else:
                        ts_utc = ts
                    ts_et = ts_utc.astimezone(ET)
                    if ts_et.hour == resolution_hour_et:
                        date_key = ts_et.strftime("%Y-%m-%d")
                        close_val = row["Close"]
                        price = float(close_val.iloc[0]) if hasattr(close_val, "iloc") else float(close_val)
                        if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                            price *= 10.0
                        by_date[date_key] = price
                except Exception:
                    continue

            if by_date:
                dates_sorted = sorted(by_date.keys())
                result = [by_date[d] for d in dates_sorted]
                break
        except Exception as e:
            if _DEBUG:
                print(f"  [yfinance_client] intraday failed for {t}: {e}")
            continue

    if not result:
        # Fallback to daily closes (CoinGecko doesn't have intraday)
        return get_aggs(ticker, days_back, cfg)

    try:
        from storage import set_yf_intraday_at_hour
        set_yf_intraday_at_hour(ticker, days_back, resolution_hour_et, result, cfg)
    except ImportError:
        pass
    return result


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | str | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (no lookahead).
    For backtesting: ensures we only use data that was available at end_date.
    end_date: datetime, ISO date string, or None (defaults to today).
    Returns list of close prices (oldest first).
    """
    if end_date is None:
        end = datetime.utcnow()
    elif isinstance(end_date, str):
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            end = datetime.strptime(end_date[:10], "%Y-%m-%d")
        if end.tzinfo:
            end = end.replace(tzinfo=None)
    else:
        end = end_date
    start = end - timedelta(days=days_back)
    result: list[float] = []
    if ticker in _COINGECKO_IDS.values():
        result = _coingecko_aggs(ticker, days_back)
    if not result:
        tickers_to_try: list[str] = [ticker]
        tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
        for t in tickers_to_try:
            try:
                hist = yf.download(t, start=start, end=end, interval="1d", progress=False)
                if hist is None or hist.empty:
                    continue
                closes_raw = None
                if hasattr(hist.columns, "get_level_values") and "Close" in hist.columns.get_level_values(0):
                    closes = hist["Close"]
                    if hasattr(closes, "ndim") and closes.ndim > 1:
                        closes = closes.iloc[:, 0]
                    closes_raw = closes.dropna()
                elif "Close" in hist.columns:
                    closes_raw = hist["Close"].dropna()
                if closes_raw is not None and len(closes_raw) > 0:
                    result = [float(x) for x in closes_raw.tolist()]
                    if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                        result = [x * 10.0 for x in result]
                    break
            except Exception:
                continue
    return result
