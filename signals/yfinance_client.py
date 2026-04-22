"""yfinance client for S&P 500 (primary) and backup for crypto/stocks.
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
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# macOS LibreSSL: avoid importing urllib3 before filter (that triggers the warning).
warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
    category=Warning,
)

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
    "DOGE-USD": "dogeusd",
    "ZEC-USD": "zecusd",
    "BNB-USD": "bnbusd",
}

# ---------------------------------------------------------------------------
# CoinGecko fallback (when Yahoo is blocked)
# ---------------------------------------------------------------------------
# CoinGecko id -> yfinance ticker (fallback when Yahoo fails)
_COINGECKO_IDS: dict[str, str] = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "solana": "SOL-USD",
    "dogecoin": "DOGE-USD",
    "zcash": "ZEC-USD",
    "binancecoin": "BNB-USD",
    "shiba-inu": "SHIB-USD",
    "hyperliquid": "HYPE32196-USD",
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
        
        tail: list[float] = []
        for i in range(1, min_len + 1):
            s_val = spy[-i]
            g_val = gspc[-i]
            # If they disagree by more than 2%, use the one that is more consistent with previous points
            # or just skip if both are suspicious.
            if abs(s_val - g_val) / max(s_val, g_val) > 0.02:
                # Disagreement! This often precedes a phantom crash in one feed.
                # Use the one closer to the other feed's average if available, or just average and hope _plausible catches it.
                # For now, let's just pick the max to be conservative against "phantom crashes" (min would be the crash).
                tail.append(max(s_val, g_val))
            else:
                tail.append((s_val + g_val) / 2.0)
                
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
    spy_adj = spy_price * 10.0 if spy_price is not None else None
    if spy_adj is None and gspc_price is None:
        return None
    if spy_adj is None:
        return gspc_price
    if gspc_price is None:
        return spy_adj
    
    # If they disagree by > 2%, pick the one that is likely NOT a phantom crash (max)
    if abs(spy_adj - gspc_price) / max(spy_adj, gspc_price) > 0.02:
        return max(spy_adj, gspc_price)
        
    return (spy_adj + gspc_price) / 2.0


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
    Get current/latest price. Returns None on failure or if price is suspicious.
    Crypto: Gemini v2/ticker (exchange spot) when enabled, else yfinance, else CoinGecko.
    S&P 500 (^GSPC): yfinance, fallback to SPY × 10.
    """
    pair = _gemini_pair_for_ticker(ticker)
    price = None
    if pair and _USE_GEMINI_SPOT:
        price = _gemini_v2_ticker_close(pair)

    if price is None and ticker in ("^GSPC", "GSPC") and _USE_SPY_FOR_SP500:
        price = _combine_sp500_snapshot(
            _yfinance_snapshot("SPY"), _yfinance_snapshot(ticker)
        )

    if price is None:
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
                    price = p
                    break
                continue
            try:
                yf_t = yf.Ticker(t)
                hist = yf_t.history(period="5d", interval="1d")
                if hist is not None and not hist.empty and "Close" in hist.columns:
                    p = float(hist["Close"].iloc[-1])
                    if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                        p *= 10.0
                    price = p
                    break
                info = yf_t.info
                if isinstance(info, dict):
                    for key in ("regularMarketPrice", "previousClose", "open"):
                        if info.get(key) is not None:
                            p = float(info[key])
                            if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                                p *= 10.0
                            price = p
                            break
                    if price is not None:
                        break
            except Exception as e:
                if _DEBUG:
                    print(f"  [yfinance_client] yfinance failed for {t}: {e}")
                continue

    # --- DATA HYGIENE: Staleness Check ---
    # Reject snapshots that are too old (e.g. > 1h) to avoid trading on stale information.
    if price is not None:
        try:
            yf_t = yf.Ticker(ticker)
            # Check market time from info or history index
            mkt_time_raw = yf_t.info.get("regularMarketTime") or yf_t.info.get("lastTradeDate")
            if mkt_time_raw:
                mkt_ts = float(mkt_time_raw)
                now_ts = datetime.utcnow().timestamp()
                age_sec = now_ts - mkt_ts
                if age_sec > 3600: # 1 hour stale
                     print(f"  [yfinance_client] REJECTING stale snapshot for {ticker}: age {age_sec/60:.1f} min > 60 min")
                     return None
        except Exception:
             pass

    # VALIDATION: Check against cached last close if available
    if price is not None:
        try:
            from storage import get_yf_closes
            cached = get_yf_closes(ticker, days_back=5)
            if cached:
                last_close = cached[-1]
                # Reject if price moved > 25% from last close (unless known volatile crypto)
                if last_close > 0:
                    ratio = price / last_close
                    if (ratio > 1.25 or ratio < 0.75) and ticker not in ("DOGE-USD", "PEPE-USD", "SHIB-USD"):
                        print(f"  [yfinance_client] REJECTING suspicious snapshot for {ticker}: {price:.2f} (vs cached close {last_close:.2f}, ratio {ratio:.2f})")
                        return None
        except Exception:
            pass

    return price


# Fallback tickers when primary fails (e.g. ^GSPC blocked by proxy, SPY often works)
_TICKER_FALLBACKS: dict[str, list[str]] = {
    "^GSPC": ["SPY"],   # S&P 500 index -> SPY ETF (same index, different scale ~1/10)
    "GSPC": ["^GSPC", "SPY"],
}


def _filter_cross_pollinated_prices(ticker: str, prices: list[float]) -> list[float]:
    """Remove values that are clearly from a different asset scale."""
    t = (ticker or "").strip().upper()
    cleaned: list[float] = []
    for x in prices or []:
        if x <= 0:
            continue
        if t in ("^GSPC", "GSPC") and x < 1000.0:
            continue
        if t in ("^NDX", "NDX") and x < 2000.0:
            continue
        if t == "SI=F" and x > 200.0:
            continue
        if t == "HG=F" and x > 10.0:
            continue
        if t == "GC=F" and x > 10000.0:
            continue
        cleaned.append(float(x))
    return cleaned


def _yf_daily_closes_plausible(ticker: str, closes: list[float]) -> bool:
    """
    Drop corrupted SQLite rows (e.g. index prices stored under a futures ticker).
    Commonly happens when S&P 500 (^GSPC ~5000) data leaks into Copper (HG=F ~4) or Silver (SI=F ~30).
    """
    if not closes:
        return False
    t = (ticker or "").strip().upper()
    
    # Check for stale 0.0 values or negative prices
    if any(x <= 0 for x in closes):
        return False

    tail = closes[-min(120, len(closes)) :]
    mx = max(tail)
    mn = min(tail)
    
    # 1. SCALE VALIDATION:
    # Commodities should not be at S&P 500 scale (4000-6000) unless there's hyperinflation.
    # S&P 500 should not be at commodity scale.
    
    if t in ("RB=F", "CL=F", "BZ=F", "KC=F", "LE=F", "ZC=F"):
        if mx > 1000.0: # Most energy/soft/grain futures < $1000
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $1000.0) — index scale leakage?")
            return False
    
    if t == "ZS=F":
        if mx > 3000.0: # Soybeans usually < $20 or 2000c
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $3000.0)")
            return False

    if t == "CC=F":
        if mx > 20000.0: # Cocoa was ~$10k recently; cap at 20k
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $20000.0)")
            return False

    if t == "SB=F":
        if mx > 100.0: # Sugar usually < $0.50 or 50c
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $100.0)")
            return False
    
    if t in ("^NDX", "NDX"):
        if mn < 2000.0: # Nasdaq-100 hasn't been below 2000 in a long time
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (min value {mn:.1f} < $2000.0) — likely commodity scale leakage")
            return False
            
    if t in ("^GSPC", "GSPC"):
        if mn < 1000.0: # S&P 500 hasn't been below 1000 in decades
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (min value {mn:.1f} < $1000.0) — likely commodity scale leakage")
            return False
            
    if t == "SI=F":
        if mx > 200.0: # Silver usually < $50
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $200.0)")
            return False
            
    if t == "HG=F":
        # Copper usually ~4.50 USD. Reject if it hits 10.0+ (likely index scale)
        if mx > 10.0:
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $10.0) — likely index cross-pollination")
            return False
            
    if t == "GC=F":
        if mx > 10000.0: # Gold is ~$2500, shouldn't be 10k+
            print(f"  [yfinance_client] WARNING: Rejecting corrupted {t} data (max value {mx:.1f} > $10000.0)")
            return False

    # 2. PHANTOM CRASH VALIDATION:
    # Large single-day jumps (>50% or < -50%) are almost always data errors for these assets.
    for i in range(1, len(closes)):
        prev = closes[i-1]
        curr = closes[i]
        if prev > 0:
            ratio = curr / prev
            if ratio > 2.0 or ratio < 0.5:
                # Exception for ultra-volatile crypto if needed, but for indices and commodities it's definitely an error.
                if t not in ("DOGE-USD", "SHIB-USD", "PEPE-USD"):
                    print(f"  [yfinance_client] WARNING: Rejecting {t} data due to phantom move: {prev:.2f} -> {curr:.2f} ({ratio-1:.1%})")
                    return False

    return True


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
        from storage import get_yf_closes, clear_yf_cache
        cached = get_yf_closes(ticker, days_back, cfg)
        if cached:
            if _yf_daily_closes_plausible(ticker, cached):
                return cached
            else:
                # Corrupted data in cache (likely index cross-pollination); purge it.
                clear_yf_cache(ticker, days_back, cfg)
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
                    # Single-ticker downloads often return MultiIndex when threads=True in some yf versions; 
                    # use threads=False to minimize cross-pollination and keep the structure simple.
                    hist = yf.download(t, start=start, end=end, interval="1d", progress=False, threads=False)
                    if hist is None or hist.empty:
                        continue
                    
                    closes_raw = None
                    # Support both new yfinance (MultiIndex with 'Price') and older formats
                    if hasattr(hist.columns, "get_level_values") and "Close" in hist.columns.get_level_values(0):
                        closes_raw = hist["Close"]
                    elif "Close" in hist.columns:
                        closes_raw = hist["Close"]
                        
                    if closes_raw is not None and hasattr(closes_raw, "ndim") and closes_raw.ndim > 1:
                        closes_raw = closes_raw.iloc[:, 0]
                        
                    if closes_raw is not None:
                        closes_raw = closes_raw.dropna()
                        if len(closes_raw) > 0:
                            result = [float(x) for x in closes_raw.tolist()]
                            if t == "SPY" and ticker in ("^GSPC", "GSPC"):
                                result = [x * 10.0 for x in result]
                            break
                except Exception:
                    continue

    if result:
        # Clean obvious cross-pollination pollution before plausibility checks.
        result = _filter_cross_pollinated_prices(ticker, result)

        # Final validation before caching
        if not _yf_daily_closes_plausible(ticker, result):
            print(f"  [yfinance_client] CRITICAL: Downloaded data for {ticker} failed plausibility check — not caching.")
            return []
            
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
        from storage import get_yf_intraday_at_hour, clear_yf_cache
        cached = get_yf_intraday_at_hour(ticker, days_back, resolution_hour_et, cfg)
        if cached:
            if _yf_daily_closes_plausible(ticker, cached):
                return cached
            else:
                clear_yf_cache(ticker, days_back, cfg)
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
            hist = yf.download(t, start=start, end=end, interval="1h", progress=False, auto_adjust=True, threads=False)
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

    # Clean and validate before caching intraday snapshots.
    result = _filter_cross_pollinated_prices(ticker, result)
    if not _yf_daily_closes_plausible(ticker, result):
        print(f"  [yfinance_client] WARNING: Intraday data for {ticker} failed plausibility; falling back to daily closes.")
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
                hist = yf.download(t, start=start, end=end, interval="1d", progress=False, threads=False)
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
