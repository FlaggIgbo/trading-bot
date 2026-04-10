"""
Market Time and Resolution Utilities.

Handles parsing of settlement times from market titles (e.g. "at 5pm ET")
and calculation of time-to-expiry (DTE) for volatility models.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_RESOLUTION_HOUR_PATTERN = re.compile(
    r"(?:(?:today|tomorrow)\s+at|@|\bat)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)\b",
    re.I,
)

_RELATIVE_TIME = re.compile(
    r"(today|tomorrow)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?",
    re.I,
)

_AT_TIME_ONLY = re.compile(
    r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?",
    re.I,
)

_DATE_PATTERN = re.compile(
    r"(?:on\s+)?"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(\d{1,2})"
    r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?)?",
    re.I,
)

_DATE_WITH_YEAR_PATTERN = re.compile(
    r"(?:on\s+)?"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(\d{1,2})\s*,\s*(\d{4})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?",
    re.I,
)


def get_title(market_or_title: dict | str) -> str:
    """Extract full title string from market dict or raw string."""
    if isinstance(market_or_title, dict):
        title = (
            (market_or_title.get("title") or market_or_title.get("yes_sub_title") or "")
            + (market_or_title.get("no_sub_title") or "")
        )
    else:
        title = str(market_or_title)
    return title


def get_resolution_hour_from_title(title: str) -> int | None:
    """Parse intraday resolution hour from title. Returns 24h hour (16=4pm, 17=5pm) or None."""
    m = _RESOLUTION_HOUR_PATTERN.search(title)
    if not m:
        return None
    hour = int(m.group(1))
    am_pm = (m.group(2) or "").lower()
    if "pm" in am_pm and hour < 12:
        hour += 12
    elif "am" in am_pm and hour == 12:
        hour = 0
    return hour


def parse_iso8601_utc(value: str | None) -> datetime | None:
    """Parse API timestamps to UTC-aware datetime."""
    if not value or not str(value).strip():
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def resolution_utc_from_market_or_event(obj: dict) -> datetime | None:
    """Best-effort resolution instant from Kalshi/Gemini objects."""
    if not isinstance(obj, dict):
        return None
    inner = obj.get("market", obj)
    for key in ("close_time", "expected_expiration_time", "expiration_time", "expiryDate", "latest_expiration_time"):
        raw = inner.get(key)
        if raw:
            dt = parse_iso8601_utc(str(raw))
            if dt is not None:
                return dt
    for c in inner.get("contracts") or []:
        raw = c.get("expiryDate")
        if raw:
            dt = parse_iso8601_utc(str(raw))
            if dt is not None:
                return dt
    return None


def parse_resolution_datetime_from_title(title: str, is_us_equity_index: bool = False) -> datetime | None:
    """Parse resolution datetime from title. Default hour is 4pm for indices, 5pm otherwise."""
    if not title:
        return None
    now_et = datetime.now(ET).date()

    # 1. "today at 5pm" / "tomorrow at 5pm"
    m = _RELATIVE_TIME.search(title)
    if m:
        rel, h_str, min_str, am_pm = m.group(1), m.group(2), m.group(3), m.group(4)
        hour = int(h_str)
        minute = int(min_str) if min_str else 0
        if "pm" in am_pm.lower() and hour < 12:
            hour += 12
        elif "am" in am_pm.lower() and hour == 12:
            hour = 0
        base_date = now_et
        if "tomorrow" in rel.lower():
            base_date = base_date + timedelta(days=1)
        try:
            dt_et = datetime(base_date.year, base_date.month, base_date.day, hour, minute, 0, tzinfo=ET)
            return dt_et.astimezone(UTC)
        except (ValueError, TypeError):
            return None

    # 2. "on Mar 23, 2026 at 4pm"
    m2a = _DATE_WITH_YEAR_PATTERN.search(title)
    if m2a:
        mon_str, day_str, year_str, h_str, min_str, am_pm = (
            m2a.group(1), m2a.group(2), m2a.group(3),
            m2a.group(4), m2a.group(5), m2a.group(6),
        )
        month = _MONTHS.get(mon_str[:3].lower())
        if month is not None:
            day = int(day_str)
            year = int(year_str)
            hour = int(h_str)
            minute = int(min_str) if min_str else 0
            if "pm" in (am_pm or "").lower() and hour < 12:
                hour += 12
            elif "am" in (am_pm or "").lower() and hour == 12:
                hour = 0
            try:
                dt_et = datetime(year, month, day, hour, minute, 0, tzinfo=ET)
                return dt_et.astimezone(UTC)
            except (ValueError, TypeError):
                pass

    # 3. "at 5pm" - assume today
    m2 = _AT_TIME_ONLY.search(title)
    if m2:
        h_str, min_str, am_pm = m2.group(1), m2.group(2), m2.group(3)
        hour = int(h_str)
        minute = int(min_str) if min_str else 0
        if "pm" in am_pm.lower() and hour < 12:
            hour += 12
        elif "am" in am_pm.lower() and hour == 12:
            hour = 0
        try:
            dt_et = datetime(now_et.year, now_et.month, now_et.day, hour, minute, 0, tzinfo=ET)
            return dt_et.astimezone(UTC)
        except (ValueError, TypeError):
            return None

    # 4. Dates without year or with implicit hours
    m3 = _DATE_PATTERN.search(title)
    if m3:
        mon_str, day_str = m3.group(1), m3.group(2)
        month = _MONTHS.get(mon_str[:3].lower())
        if not month:
            return None
        day = int(day_str)
        year = now_et.year
        try:
            cand = datetime(year, month, day, tzinfo=ET)
        except ValueError:
            return None
        if cand.date() < now_et:
            year += 1
        default_hour = 16 if is_us_equity_index else 17
        if m3.group(3) is not None:
            h_str, min_str, am_pm = m3.group(3), m3.group(4), m3.group(5)
            hour = int(h_str)
            minute = int(min_str) if min_str else 0
            if "pm" in (am_pm or "").lower() and hour < 12:
                hour += 12
            elif "am" in (am_pm or "").lower() and hour == 12:
                hour = 0
        else:
            hour, minute = default_hour, 0
        try:
            dt_et = datetime(year, month, day, hour, minute, 0, tzinfo=ET)
            return dt_et.astimezone(UTC)
        except (ValueError, TypeError):
            return None

    return None


def hours_until_resolution(title: str, is_us_equity_index: bool = False) -> float | None:
    """Hours until resolution (positive = future, negative = past). None if unparseable."""
    res_dt = parse_resolution_datetime_from_title(title, is_us_equity_index)
    if res_dt is None:
        return None
    return (res_dt - datetime.now(UTC)).total_seconds() / 3600.0


def hours_until_resolution_unified(market_or_title: dict | str, is_us_equity_index: bool = False) -> float | None:
    """Unified helper for hours until resolution from API objects or titles."""
    if isinstance(market_or_title, dict):
        res_dt = resolution_utc_from_market_or_event(market_or_title)
        if res_dt is not None:
            return (res_dt - datetime.now(UTC)).total_seconds() / 3600.0
    title = get_title(market_or_title)
    return hours_until_resolution(title, is_us_equity_index)


def minutes_until_resolution_unified(market_or_title: dict | str, is_us_equity_index: bool = False) -> float | None:
    """Minutes until resolution; None if unknown."""
    h = hours_until_resolution_unified(market_or_title, is_us_equity_index)
    return None if h is None else max(0.0, h * 60.0)


def get_days_to_expiry_from_market(
    market_or_title: dict | str, is_us_equity_index: bool = False, default_days: float = 30.0
) -> float:
    """Compute days until resolution for volatility models."""
    h = hours_until_resolution_unified(market_or_title, is_us_equity_index)
    if h is None:
        return default_days
    return max(0.0007, h / 24.0)


def is_weekend_et() -> bool:
    """True if current time in New York is Sat or Sun."""
    now_et = datetime.now(ET)
    return now_et.weekday() >= 5

def extract_resolution_key_from_ticker(ticker: str) -> str | None:
    """Extract YYYYMMDDHH or similar from standard ticker formats."""
    m = re.search(r"-(\d{6,10})(?:-|$)", ticker)
    return m.group(1) if m else None


def extract_resolution_key_from_gemini_ticker(ticker: str) -> str | None:
    """Extract YYYYMMDDHH from Gemini tickers (e.g. BTC2503161700)."""
    m = re.search(r"(\d{8,10})$", ticker)
    return m.group(1) if m else None


def get_resolution_key_for_order(o: dict) -> str | None:
    """Stable key for cross-platform exposure grouping (asset + date)."""
    yf = o.get("yf_ticker")
    if not yf:
        return None
    plat = o.get("platform")
    ticker = str(o.get("ticker") or "")
    if plat == "gemini":
        rk = extract_resolution_key_from_gemini_ticker(ticker)
    else:
        rk = extract_resolution_key_from_ticker(ticker)
    if not rk:
        return str(yf)
    return f"{yf}:{rk}"
