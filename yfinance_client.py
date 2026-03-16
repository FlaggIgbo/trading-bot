"""
yfinance client for S&P 500 (primary) and backup for crypto/stocks.
No API key required. Tickers: ^GSPC (S&P 500), BTC-USD, ETH-USD, SOL-USD, etc.
Falls back to CoinGecko for crypto when Yahoo is blocked (proxy, PiHole, etc.).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import yfinance as yf

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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of end_date (no lookahead). For backtesting.
    end_date: last trading day to include. Default: yesterday UTC.
    """
    end = end_date or (datetime.utcnow() - timedelta(days=1))
    start = end - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try = [ticker] + _TICKER_FALLBACKS.get(ticker, [])
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Historical daily closes as of end_date (no lookahead). For backtesting.
    Returns list of close prices (oldest first). end_date defaults to today.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Historical daily closes as of end_date (no lookahead). For backtesting.
    Returns list of close prices (oldest first), with last close on or before end_date.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try = [ticker]
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Historical daily closes as of end_date (no lookahead). For backtesting.
    end_date: last trading day to include. Default: yesterday UTC.
    Returns list of closes (oldest first).
    """
    if end_date is None:
        end_date = datetime.utcnow() - timedelta(days=1)
    start = end_date - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try: list[str] = [ticker]
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
    for t in tickers_to_try:
        try:
            hist = yf.download(t, start=start, end=end_date, interval="1d", progress=False)
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific date (no lookahead). For backtesting.
    Returns list of close prices (oldest first), with last close on or before end_date.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Historical daily closes up to end_date (no lookahead). For backtesting.
    end_date: last date to include (default: today). Use for point-in-time evals.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes up to end_date (point-in-time, no lookahead).
    For backtesting: use end_date = resolution date to get closes as-of that day.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
    if ticker in _COINGECKO_IDS.values():
        result = _coingecko_aggs(ticker, days_back)
    if not result:
        for t in [ticker] + _TICKER_FALLBACKS.get(ticker, []):
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (point-in-time).
    For backtesting: no lookahead bias. end_date defaults to yesterday (UTC).
    Returns list of close prices (oldest first).
    """
    end = end_date or (datetime.utcnow() - timedelta(days=1))
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes up to end_date (point-in-time, no lookahead).
    For backtesting: use closes that would have been available as of end_date.
    end_date: exclusive end; if None, use today.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | str | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes up to end_date (point-in-time, no lookahead).
    For backtesting: use end_date = snapshot date to avoid future data.
    end_date: datetime or YYYY-MM-DD string. Default: yesterday.
    """
    if end_date is None:
        end_date = datetime.utcnow() - timedelta(days=1)
    elif isinstance(end_date, str):
        end_date = datetime.strptime(end_date[:10], "%Y-%m-%d")
    start = end_date - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try: list[str] = [ticker]
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
    for t in tickers_to_try:
        try:
            hist = yf.download(t, start=start, end=end_date, interval="1d", progress=False)
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (no lookahead).
    For backtesting: use closes that would have been available at end_date.
    end_date: exclusive upper bound (closes before this date). Default: today.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (no lookahead).
    Used for point-in-time backtesting: e.g. "what would we have known on March 10?"
    end_date: last date to include. If None, uses today.
    Returns list of close prices (oldest first).
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try: list[str] = [ticker]
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
    for t in tickers_to_try:
        try:
            hist = yf.download(t, start=start, end=end, interval="1d", progress=False, auto_adjust=True)
            if hist is None or hist.empty:
                continue
            closes = None
            if "Close" in hist.columns:
                closes = hist["Close"].dropna()
            elif hasattr(hist.columns, "get_level_values") and "Close" in hist.columns.get_level_values(0):
                c = hist["Close"]
                if hasattr(c, "ndim") and c.ndim > 1:
                    c = c.iloc[:, 0]
                closes = c.dropna()
            if closes is not None and len(closes) > 0:
                result = [float(x) for x in closes.tolist()]
                if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                    result = [x * 10.0 for x in result]
                break
        except Exception:
            continue
    return result


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | str,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (no lookahead).
    Used for point-in-time backtesting and outcome inference.
    end_date: datetime or ISO date string (e.g. "2025-03-15").
    Returns list of close prices (oldest first).
    """
    if isinstance(end_date, str):
        end = datetime.strptime(end_date[:10], "%Y-%m-%d")
    else:
        end = end_date
    start = end - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try = [ticker] + _TICKER_FALLBACKS.get(ticker, [])
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes up to end_date (point-in-time, no lookahead).
    Used for backtesting: infer resolution outcome from asset close on settlement date.
    end_date: last trading day to include. Default: yesterday (utcnow - 1 day).
    """
    if end_date is None:
        end_date = datetime.utcnow() - timedelta(days=1)
    start = end_date - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try: list[str] = [ticker]
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
    for t in tickers_to_try:
        try:
            hist = yf.download(t, start=start, end=end_date, interval="1d", progress=False)
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
    config: dict | None = None,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (no lookahead).
    For backtesting: use closes that would have been available on end_date.
    end_date: inclusive; if None, uses today.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | str,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes up to (and including) end_date. No lookahead.
    Used for point-in-time backtesting. end_date: datetime or ISO date string.
    """
    if isinstance(end_date, str):
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            end_dt = datetime.strptime(end_date[:10], "%Y-%m-%d")
    else:
        end_dt = end_date
    if end_dt.tzinfo:
        end_dt = end_dt.replace(tzinfo=None)
    start_dt = end_dt - timedelta(days=days_back)
    result: list[float] = []
    tickers_to_try: list[str] = [ticker]
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
    for t in tickers_to_try:
        try:
            hist = yf.download(t, start=start_dt, end=end_dt + timedelta(days=1), interval="1d", progress=False)
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
    config: dict | None = None,
) -> list[float]:
    """
    Get historical daily closes up to end_date (no lookahead). For point-in-time backtesting.
    Returns list of close prices (oldest first). end_date defaults to today.
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime | None = None,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes up to end_date (no lookahead).
    For backtesting: point-in-time data as it would have been known on end_date.
    end_date: inclusive; if None, uses today.
    Returns list of close prices (oldest first).
    """
    end = end_date or datetime.utcnow()
    start = end - timedelta(days=days_back)
    result: list[float] = []
    if ticker in _COINGECKO_IDS.values():
        # CoinGecko: use timestamp range
        try:
            import time

            end_ts = int(end.timestamp())
            start_ts = int(start.timestamp())
            import requests

            id_map = {v: k for k, v in _COINGECKO_IDS.items()}
            cg_id = id_map.get(ticker)
            if not cg_id:
                return []
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
            by_day: dict[int, float] = {}
            for ts, p in prices:
                day = int(ts) // 86400
                by_day[day] = float(p)
            days_sorted = sorted(by_day.keys())
            result = [by_day[d] for d in days_sorted]
        except Exception as e:
            if _DEBUG:
                print(f"  [yfinance_client] CoinGecko get_aggs_as_of failed: {e}")
            return []
    else:
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


def get_aggs_as_of(
    ticker: str,
    end_date: datetime,
    days_back: int = 365,
) -> list[float]:
    """
    Get historical daily closes as of a specific end date (no lookahead).
    For backtesting: ensures we only use data that was available at end_date.
    Returns list of close prices (oldest first).
    """
    result: list[float] = []
    start = end_date - timedelta(days=days_back)
    tickers_to_try: list[str] = [ticker]
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))
    for t in tickers_to_try:
        try:
            hist = yf.download(t, start=start, end=end_date, interval="1d", progress=False)
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
