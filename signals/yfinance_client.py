"""
yfinance client for S&P 500 (primary) and backup for crypto/stocks.
No API key required. Tickers: ^GSPC (S&P 500), BTC-USD, ETH-USD, SOL-USD, etc.

Crypto spot prices:
- **Gemini Exchange** public REST (`v2/ticker`, `v2/candles`) is used first for BTC/ETH/SOL/XRP
  USD pairs — fresher than delayed Yahoo aggregates for live `get_snapshot` and exchange-native OHLC.
  Docs: https://docs.gemini.com/rest-api/market-data
- Then **yfinance**, then **CoinGecko** (no key) if earlier sources fail or return too little data.

**Why not Yahoo `interval=1m` for everything?** Intraday history is capped (on the order of ~7 days for
1-minute bars). Daily empirical windows (e.g. 90–365 days) and hourly-resolution intraday series still
need `1d` / `1h` (or Gemini `1day` / `1hr`), not 1m.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

_DEBUG = os.getenv("YFINANCE_DEBUG", "").lower() in ("1", "true", "yes")

# Set YFINANCE_CRYPTO_GEMINI=0 to disable Gemini spot (yfinance/CoinGecko only).
_USE_GEMINI_SPOT = os.getenv("YFINANCE_CRYPTO_GEMINI", "1").lower() not in ("0", "false", "no")
_USE_SPY_FOR_SP500 = os.getenv("YFINANCE_SPY_AS_SP500", "1").lower() not in ("0", "false", "no")

# Public Gemini spot API (same host as prediction markets docs; no auth for ticker/candles).
_GEMINI_SPOT_API = os.getenv("GEMINI_SPOT_API_BASE", "https://api.gemini.com").rstrip("/")

# yfinance ticker -> Gemini pair slug (lowercase)
_YF_TO_GEMINI_PAIR: dict[str, str] = {
    "BTC-USD": "btcusd",
    "ETH-USD": "ethusd",
    "SOL-USD": "solusd",
    "XRP-USD": "xrpusd",
}

# ---------------------------------------------------------------------------
# CoinGecko fallback (when Yahoo is blocked)
# ---------------------------------------------------------------------------
# CoinGecko id -> yfinance ticker (fallback when Yahoo fails)
_COINGECKO_IDS: dict[str, str] = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "solana": "SOL-USD",
}


def _gemini_pair_for_ticker(ticker: str) -> str | None:
    return _YF_TO_GEMINI_PAIR.get(ticker)


def _gemini_v2_ticker_close(pair: str) -> float | None:
    """Latest trade close from Gemini v2 ticker (string decimals → float)."""
    try:
        import requests

        r = requests.get(f"{_GEMINI_SPOT_API}/v2/ticker/{pair}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return None
        close = data.get("close")
        if close is None:
            return None
        return float(str(close).replace(",", ""))
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] Gemini v2/ticker failed for {pair}: {e}")
        return None


def _utc_end_of_calendar_day_ms(end: datetime) -> int:
    """Last instant of the calendar day containing `end`, expressed in UTC (for candle cutoff)."""
    if end.tzinfo is not None:
        end = end.astimezone(UTC)
        y, m, d = end.year, end.month, end.day
    else:
        y, m, d = end.year, end.month, end.day
    eod = datetime(y, m, d, 23, 59, 59, 999999, tzinfo=UTC)
    return int(eod.timestamp() * 1000)


def _gemini_daily_closes(pair: str, days_back: int) -> list[float]:
    """
    Daily candles from Gemini (1day). Returns closes oldest→newest, trimmed to days_back.
    Empty list on failure or if fewer than min(14, days_back) bars (fall back to other providers).
    """
    try:
        import requests

        r = requests.get(f"{_GEMINI_SPOT_API}/v2/candles/{pair}/1day", timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return []
        # [time_ms, open, high, low, close, volume]
        valid = [row for row in rows if isinstance(row, (list, tuple)) and len(row) > 4]
        if not valid:
            return []
        valid.sort(key=lambda x: int(x[0]))
        closes = [float(row[4]) for row in valid]
        need = min(days_back, len(closes))
        min_required = min(14, days_back)
        if len(closes) < min_required:
            return []
        return closes[-need:]
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] Gemini 1day candles failed for {pair}: {e}")
        return []


def _gemini_daily_closes_as_of(pair: str, days_back: int, end: datetime) -> list[float]:
    """
    Same as _gemini_daily_closes but only candles with bar time <= end of `end` calendar day (UTC).
    Single HTTP request; for backtest no-lookahead paths.
    """
    try:
        import requests

        r = requests.get(f"{_GEMINI_SPOT_API}/v2/candles/{pair}/1day", timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return []
        valid = [row for row in rows if isinstance(row, (list, tuple)) and len(row) > 4]
        if not valid:
            return []
        valid.sort(key=lambda x: int(x[0]))
        end_ms = _utc_end_of_calendar_day_ms(end)
        closes = [float(row[4]) for row in valid if int(row[0]) <= end_ms]
        need = min(days_back, len(closes))
        min_required = min(14, days_back)
        if len(closes) < min_required:
            return []
        return closes[-need:]
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] Gemini 1day candles as-of failed for {pair}: {e}")
        return []


def _gemini_hourly_for_resolution(pair: str, resolution_hour_et: int, period_days: int) -> list[float]:
    """
    Build one price per calendar day at resolution_hour_et using Gemini 1hr candles.
    Returns oldest→newest (by date key). Empty on failure.
    """
    try:
        import requests

        r = requests.get(f"{_GEMINI_SPOT_API}/v2/candles/{pair}/1hr", timeout=25)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return []
        by_date: dict[str, float] = {}
        cutoff = datetime.now(UTC) - timedelta(days=period_days)
        cutoff_ts = cutoff.timestamp()
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            ts_ms = int(row[0])
            if ts_ms / 1000.0 < cutoff_ts:
                continue
            close = float(row[4])
            ts_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
            ts_et = ts_utc.astimezone(ET)
            if ts_et.hour == resolution_hour_et:
                date_key = ts_et.strftime("%Y-%m-%d")
                by_date[date_key] = close
        if not by_date:
            return []
        dates_sorted = sorted(by_date.keys())
        return [by_date[d] for d in dates_sorted]
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] Gemini 1hr candles failed for {pair}: {e}")
        return []


def _extract_closes_from_hist(hist) -> list[float]:
    if hist is None or hist.empty:
        return []
    closes_raw = None
    if hasattr(hist.columns, "get_level_values") and "Close" in hist.columns.get_level_values(0):
        closes = hist["Close"]
        if hasattr(closes, "ndim") and closes.ndim > 1:
            closes = closes.iloc[:, 0]
        closes_raw = closes.dropna()
    elif "Close" in hist.columns:
        closes_raw = hist["Close"].dropna()
    if closes_raw is None or len(closes_raw) == 0:
        return []
    return [float(x) for x in closes_raw.tolist()]


def _yf_daily_closes(ticker: str, start: datetime, end: datetime) -> list[float]:
    hist = yf.download(ticker, start=start, end=end, interval="1d", progress=False)
    return _extract_closes_from_hist(hist)


def _merge_sp500_closes(spy: list[float], gspc: list[float], days_back: int) -> list[float]:
    spy = [x * 10.0 for x in spy] if spy else []
    if not spy and not gspc:
        return []
    if not spy:
        merged = gspc
    elif not gspc:
        merged = spy
    else:
        min_len = min(len(spy), len(gspc))
        prefix: list[float] = []
        if len(spy) > min_len:
            prefix = spy[: len(spy) - min_len]
        elif len(gspc) > min_len:
            prefix = gspc[: len(gspc) - min_len]
        tail = [(spy[-i] + gspc[-i]) / 2.0 for i in range(1, min_len + 1)]
        merged = prefix + list(reversed(tail))
    need = min(days_back, len(merged))
    min_required = min(14, days_back)
    if len(merged) < min_required:
        return []
    return merged[-need:]


def _combined_sp500_closes(end: datetime, days_back: int) -> list[float]:
    start = end - timedelta(days=days_back)
    spy = _yf_daily_closes("SPY", start, end)
    gspc = _yf_daily_closes("^GSPC", start, end)
    return _merge_sp500_closes(spy, gspc, days_back)


def _yfinance_snapshot(ticker: str) -> float | None:
    try:
        yf_t = yf.Ticker(ticker)
        hist = yf_t.history(period="5d", interval="1d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            price = float(hist["Close"].iloc[-1])
            return price
        info = yf_t.info
        if isinstance(info, dict):
            for key in ("regularMarketPrice", "previousClose", "open"):
                if info.get(key) is not None:
                    return float(info[key])
    except Exception as e:
        if _DEBUG:
            print(f"  [yfinance_client] yfinance failed for {ticker}: {e}")
    return None


def _combine_sp500_snapshot(spy_price: float | None, gspc_price: float | None) -> float | None:
    values: list[float] = []
    if spy_price is not None:
        values.append(spy_price * 10.0)
    if gspc_price is not None:
        values.append(gspc_price)
    if not values:
        return None
    return sum(values) / len(values)


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
    Crypto: Gemini v2/ticker (exchange spot) when enabled, else yfinance, else CoinGecko.
    S&P 500 (^GSPC): yfinance, fallback to SPY × 10.
    """
    pair = _gemini_pair_for_ticker(ticker)
    if pair and _USE_GEMINI_SPOT:
        g = _gemini_v2_ticker_close(pair)
        if g is not None and g > 0:
            return g

    if ticker in ("^GSPC", "GSPC") and _USE_SPY_FOR_SP500:
        combined_snapshot = _combine_sp500_snapshot(
            _yfinance_snapshot("SPY"), _yfinance_snapshot(ticker)
        )
        if combined_snapshot is not None:
            return combined_snapshot

    # ^GSPC: try yfinance, then SPY. Crypto without Gemini: yfinance then CoinGecko.
    to_try: list[tuple[str, bool]] = []
    if ticker in _COINGECKO_IDS.values() or ticker in _YF_TO_GEMINI_PAIR:
        to_try = [(ticker, False), (ticker, True)]
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
    pair = _gemini_pair_for_ticker(ticker)
    if pair and _USE_GEMINI_SPOT:
        result = _gemini_daily_closes(pair, days_back)
    # For crypto: CoinGecko / Yahoo when Gemini insufficient or disabled
    if not result and ticker in _COINGECKO_IDS.values():
        result = _coingecko_aggs(ticker, days_back)
    if not result:
        end = datetime.utcnow()
        start = end - timedelta(days=days_back)
        if ticker in ("^GSPC", "GSPC") and _USE_SPY_FOR_SP500:
            combined = _combined_sp500_closes(end, days_back)
            if combined:
                result = combined
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

    if result:
        try:
            from storage import set_yf_closes
            set_yf_closes(ticker, days_back, result, cfg)
        except ImportError:
            pass
    return result


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
    pair = _gemini_pair_for_ticker(ticker)
    if pair and _USE_GEMINI_SPOT:
        result = _gemini_hourly_for_resolution(pair, resolution_hour_et, period_days)

    tickers_to_try: list[str] = []
    if ticker in ("^GSPC", "GSPC"):
        if _USE_SPY_FOR_SP500:
            tickers_to_try.append("SPY")
        tickers_to_try.append(ticker)
    else:
        tickers_to_try.append(ticker)
    tickers_to_try.extend(_TICKER_FALLBACKS.get(ticker, []))

    for t in tickers_to_try:
        if result:
            break
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
    pair = _gemini_pair_for_ticker(ticker)
    if pair and _USE_GEMINI_SPOT:
        result = _gemini_daily_closes_as_of(pair, days_back, end)
    if not result and ticker in _COINGECKO_IDS.values():
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
