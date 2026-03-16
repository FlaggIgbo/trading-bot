#!/usr/bin/env python3
"""
Evaluate Kalshi & Gemini prediction contracts using historical price data.

Goal: Use historical data to estimate P(price above/below threshold at resolution time),
      then compare to market price → recommend BUY YES / BUY NO / SKIP.

Example: "Bitcoin above $81,250 on Mar 16?" → historical says 5% of days closed above
         → if market YES = 40¢, that's overpriced (SKIP or BUY NO).

Designed to run every 15 minutes locally for continuous evaluation.
See README for cron setup.

Run: python3 scripts/compare_historical.py [--days 365] [--markets 0] [--edge 0.15]
"""
from __future__ import annotations

# TODO: Add cron/scheduler integration for 15-min runs (e.g. APScheduler, or document crontab)
# TODO: Add logging to file for historical run analysis
# TODO: GARCH or EWMA for time-varying volatility
# TODO: Implied volatility from options market (if available) for richer probability
import argparse
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

# Add project root to path so imports work when run from scripts/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config
from constants import (
    PLATFORM_GEMINI,
    PLATFORM_KALSHI,
    REC_ARB_INTER,
    REC_ARB_INTRA,
    REC_BUY_NO,
    REC_BUY_YES,
    REC_LIKELY_LOSE,
    REC_RULE_VIOLATION,
    REC_SKIP,
)
from gemini_client import (
    GeminiPredictionClient,
    GeminiTradingClient,
    _event_to_asset,
    _get_yes_contract,
    expand_gemini_events_to_markets,
    extract_strike_from_event,
    get_instrument_symbol,
    get_no_price,
    get_yes_price,
)
from kalshi_client import KalshiClient
from ollama_analysis import analyze_single_trade, capture_and_analyze

try:
    from storage import (
        get_calibration_stats,
        get_last_snapshot_for_ticker,
        get_unresolved_predictions,
        record_prediction,
        record_prediction_outcome,
        record_resolved,
    )
except ImportError:
    get_calibration_stats = get_last_snapshot_for_ticker = get_unresolved_predictions = None
    record_prediction = record_prediction_outcome = record_resolved = None
from yfinance_client import get_aggs as yf_get_aggs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Gemini supports crypto only (BTC, ETH, SOL, XRP; no S&P 500). Used for event filtering.
GEMINI_CRYPTO_YF = {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"}


@dataclass
class _StrategyParams:
    """Consolidated strategy config from config.yaml. Load once, use throughout _run_impl."""

    days_to_expiry: float
    no_above_cushion: float
    sell_losing_pct: float
    limit_dip_pct: float
    limit_dip_pct_high_confidence: float
    limit_dip_pct_price_decline: float
    vol_blend_weight: float | None
    use_ewma_vol: bool
    ewma_lambda: float
    intraday_other_min_p_win: float
    intraday_other_min_p_win_with_vol: float
    min_arb_fee: float
    min_yes_price: float
    min_no_price: float
    max_buy_yes_strike_above_pct: float
    max_buy_no_strike_above_pct: float
    enable_intra_arb: bool
    enable_inter_arb: bool
    kalshi_maker_fee_rate: float
    gemini_maker_fee_rate: float
    absolute_winner_min_p_win: float
    absolute_winner_max_price: float
    absolute_winner_max_pct_of_balance: float
    absolute_winner_max_contracts: int


def _load_strategy_params(strategy: dict) -> _StrategyParams:
    return _StrategyParams(
        days_to_expiry=float(strategy.get("days_to_expiry", 30)),
        no_above_cushion=float(strategy.get("skip_buy_no_above_cushion", 0.01)),
        sell_losing_pct=float(strategy.get("sell_losing_pct", 0.55)),
        limit_dip_pct=float(strategy.get("limit_dip_pct", 0)),
        limit_dip_pct_high_confidence=float(strategy.get("limit_dip_pct_high_confidence", 0)),
        limit_dip_pct_price_decline=float(strategy.get("limit_dip_pct_price_decline", 0)),
        vol_blend_weight=strategy.get("vol_blend_weight"),
        use_ewma_vol=bool(strategy.get("use_ewma_vol", False)),
        ewma_lambda=float(strategy.get("ewma_lambda", 0.94)),
        intraday_other_min_p_win=float(strategy.get("intraday_other_min_p_win", 0.90)),
        intraday_other_min_p_win_with_vol=float(strategy.get("intraday_other_min_p_win_with_vol", 0.80)),
        min_arb_fee=float(strategy.get("min_arb_fee", 0.02)),
        min_yes_price=float(strategy.get("min_yes_price", 0.05)),
        min_no_price=float(strategy.get("min_no_price", 0.05)),
        max_buy_yes_strike_above_pct=float(strategy.get("max_buy_yes_strike_above_pct", 0.05)),
        max_buy_no_strike_above_pct=float(strategy.get("max_buy_no_strike_above_pct", 0.05)),
        enable_intra_arb=bool(strategy.get("enable_intra_arb", True)),
        enable_inter_arb=bool(strategy.get("enable_inter_arb", True)),
        kalshi_maker_fee_rate=float(strategy.get("kalshi_maker_fee_rate", 0.0175)),
        gemini_maker_fee_rate=float(strategy.get("gemini_maker_fee_rate", 0.0175)),
        absolute_winner_min_p_win=float(strategy.get("absolute_winner_min_p_win", 0.95)),
        absolute_winner_max_price=float(strategy.get("absolute_winner_max_price", 0.05)),
        absolute_winner_max_pct_of_balance=float(strategy.get("absolute_winner_max_pct_of_balance", 0.50)),
        absolute_winner_max_contracts=int(strategy.get("absolute_winner_max_contracts", 500)),
    )


# ---------------------------------------------------------------------------
# Intraday vs daily-close: supported resolution times
# ---------------------------------------------------------------------------
# We only evaluate intraday markets that match our supported times:
# - Crypto (BTC, ETH, SOL): 5pm ET (typical crypto settlement)
# - S&P 500 (^GSPC): 4pm ET (US market close)
# All other intraday times (e.g. 7pm EDT) are skipped.
_RESOLUTION_HOUR_PATTERN = re.compile(
    r"(?:today|tomorrow)\s+at\s+(\d{1,2})(?::\d{2})?\s*(am|pm)"
    r"|"
    r"at\s+(\d{1,2})(?::\d{2})?\s*(am|pm)",
    re.I,
)


def _get_resolution_hour_from_title(title: str) -> int | None:
    """
    Parse intraday resolution hour from title. Returns 24h hour (16=4pm, 17=5pm) or None if no intraday.
    """
    m = _RESOLUTION_HOUR_PATTERN.search(title)
    if not m:
        return None
    # Group 1,2 = "today at N am/pm"  or  Group 3,4 = "at N am/pm"
    hour_str = m.group(1) or m.group(3)
    am_pm = (m.group(2) or m.group(4) or "").lower()
    if not hour_str or not am_pm:
        return None
    hour = int(hour_str)
    if "pm" in am_pm and hour < 12:
        hour += 12
    elif "am" in am_pm and hour == 12:
        hour = 0
    return hour


def _get_title(market_or_title: dict | str) -> str:
    """Extract full title string from market dict or raw string."""
    if isinstance(market_or_title, dict):
        title = (
            (market_or_title.get("title") or market_or_title.get("yes_sub_title") or "")
            + (market_or_title.get("no_sub_title") or "")
        )
    else:
        title = str(market_or_title)
    return title


def _should_skip_by_resolution(market_or_title: dict | str, yf_ticker: str) -> bool:
    """
    True if we should SKIP this market (never evaluate). We evaluate:
    - Daily-close markets (no intraday time in title)
    - Crypto and S&P 500 at ANY intraday time (5pm, 4pm, 8pm, etc.)
      - 5pm crypto / 4pm S&P: use standard min_edge
      - Other times (e.g. 8pm ET): use strict intraday_other_min_p_win (only buy when almost certain)
    - Non–crypto/non–S&P with intraday → skip (no supported time)
    """
    title = _get_title(market_or_title)
    hour = _get_resolution_hour_from_title(title)
    if hour is None:
        return False  # No intraday time → treat as daily close, don't skip

    is_sp500 = yf_ticker.upper() in ("^GSPC", "GSPC")
    is_crypto = yf_ticker in GEMINI_CRYPTO_YF
    if is_sp500 or is_crypto:
        return False  # Evaluate all crypto/S&P intraday (strict threshold for non-5pm/4pm)
    # Other assets (non-crypto, non-S&P) with intraday → skip (no supported time)
    return True


def _is_intraday_market(market_or_title: dict | str, yf_ticker: str) -> bool:
    """True if market settles at a specific time (5pm, 4pm, etc.) — not daily close.
    For intraday, empirical P(daily close above X) is wrong; prefer volatility model."""
    title = _get_title(market_or_title)
    hour = _get_resolution_hour_from_title(title)
    if hour is None:
        return False
    is_sp500 = yf_ticker.upper() in ("^GSPC", "GSPC")
    is_crypto = yf_ticker in GEMINI_CRYPTO_YF
    return is_sp500 or is_crypto


def _is_other_intraday(market_or_title: dict | str, yf_ticker: str) -> bool:
    """
    True if this is an intraday market at a time OTHER than our standard 5pm crypto / 4pm S&P.
    E.g. "Bitcoin today at 8pm ET" → use strict "almost certain" threshold (intraday_other_min_p_win).
    """
    title = _get_title(market_or_title)
    hour = _get_resolution_hour_from_title(title)
    if hour is None:
        return False

    is_sp500 = yf_ticker.upper() in ("^GSPC", "GSPC")
    is_crypto = yf_ticker in GEMINI_CRYPTO_YF
    if is_sp500 and hour == 16:
        return False  # Standard 4pm S&P
    if is_crypto and hour == 17:
        return False  # Standard 5pm crypto
    # Has intraday time but not standard → other intraday
    return is_sp500 or is_crypto


# ---------------------------------------------------------------------------
# Resolution datetime parsing (for per-contract time-to-expiry)
# ---------------------------------------------------------------------------
# Parse titles like "Bitcoin above $81,250 tomorrow at 5pm ET" or "on March 16 at 5pm"
# into UTC datetime for accurate volatility model T (time to expiry).

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# today/tomorrow at 5pm / 5:00pm (optional ET/EDT/EST)
_RELATIVE_TIME = re.compile(
    r"(today|tomorrow)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?",
    re.I,
)
# at 5pm (assume today)
_AT_TIME_ONLY = re.compile(
    r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?",
    re.I,
)
# on March 16 / on Mar 16 / March 16 at 5pm
_DATE_PATTERN = re.compile(
    r"(?:on\s+)?"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(\d{1,2})"
    r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?)?",
    re.I,
)


def _parse_resolution_datetime_from_title(title: str, yf_ticker: str) -> datetime | None:
    """
    Parse resolution datetime from title (e.g. "tomorrow at 5pm ET", "on March 16 at 5pm").
    Returns timezone-aware datetime in UTC, or None if unparseable.
    Uses ET (America/New_York) for all times; crypto defaults to 5pm, S&P to 4pm for date-only.
    """
    if not title:
        return None
    title_lower = title.lower()
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

    # 2. "at 5pm" (no today/tomorrow) - assume today
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

    # 3. "on March 16" / "March 16 at 5pm" - explicit date
    m3 = _DATE_PATTERN.search(title)
    if m3:
        mon_str, day_str = m3.group(1), m3.group(2)
        month = _MONTHS.get(mon_str[:3].lower())
        if not month:
            return None
        day = int(day_str)
        year = now_et.year
        # If date has passed this year, assume next year
        try:
            cand = datetime(year, month, day, tzinfo=ET)
        except ValueError:
            return None
        if cand.date() < now_et:
            year += 1
        # Hour: from "at 5pm" in match, or default 17 (5pm) for crypto, 16 (4pm) for S&P
        is_sp = yf_ticker.upper() in ("^GSPC", "GSPC")
        default_hour = 16 if is_sp else 17
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


def _get_days_to_expiry_from_market(
    market_or_title: dict | str, yf_ticker: str, default_days: float = 30.0
) -> float:
    """
    Compute days (or fraction) until resolution. Uses _parse_resolution_datetime_from_title.
    Falls back to default_days if title cannot be parsed (e.g. daily-close "on March 16" without time).
    """
    title = _get_title(market_or_title)
    res_dt = _parse_resolution_datetime_from_title(title, yf_ticker)
    if res_dt is None:
        return default_days
    # Ensure we use aware datetimes and compare in UTC
    if res_dt.tzinfo is None:
        res_dt = res_dt.replace(tzinfo=ET)
    now_utc = datetime.now(UTC)
    res_utc = res_dt.astimezone(UTC)
    delta = res_utc - now_utc
    days = delta.total_seconds() / 86400.0
    return max(0.01, days)  # Minimum 0.01 days (~15 min) for numerical stability


# ---------------------------------------------------------------------------
# Strike/threshold extraction (Kalshi vs Gemini)
# ---------------------------------------------------------------------------


def _extract_strike(market: dict) -> tuple[float | None, str | None]:
    """
    Extract threshold and direction from Kalshi market.
    Returns (threshold, direction) - direction: 'above'|'below'|None.
    """
    m = market.get("market", market)
    strike_type = (m.get("strike_type") or "").lower()
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    threshold, direction = None, None
    if floor is not None and strike_type in ("greater", "greater_or_equal"):
        threshold, direction = float(floor), "above"
    elif cap is not None and strike_type in ("less", "less_or_equal"):
        threshold, direction = float(cap), "below"
    elif floor is not None:
        threshold, direction = float(floor), "above"
    elif cap is not None:
        threshold, direction = float(cap), "below"

    if threshold is None:
        title = (m.get("title") or m.get("yes_sub_title") or "") + (m.get("no_sub_title") or "")
        for pattern, dir_ in [
            (r"above\s*\$?([\d,]+(?:\.\d+)?)", "above"),
            (r">\s*\$?([\d,]+(?:\.\d+)?)", "above"),
            (r"over\s*\$?([\d,]+(?:\.\d+)?)", "above"),
            (r"below\s*\$?([\d,]+(?:\.\d+)?)", "below"),
            (r"<\s*\$?([\d,]+(?:\.\d+)?)", "below"),
        ]:
            match = re.search(pattern, title, re.I)
            if match:
                threshold = float(match.group(1).replace(",", ""))
                direction = dir_
                break
    # Fallback: parse strike from Kalshi ticker (e.g. KXXRPD-26MAR1600-T1.4199 -> 1.4199)
    if threshold is None:
        ticker = m.get("ticker") or ""
        match = re.search(r"-T([\d.]+)$", ticker)
        if match:
            threshold = float(match.group(1))
            direction = "above"  # Kalshi price tickers typically encode "above" strikes
    return threshold, direction


# ---------------------------------------------------------------------------
# Probability models
# ---------------------------------------------------------------------------
def _empirical_probability(closes: list[float], threshold: float, direction: str) -> float:
    """P(close above/below threshold) from historical daily closes.
    Simple fraction: count of days above/below threshold divided by total days."""
    if not closes:
        return 0.0
    if direction == "above":
        return sum(1 for c in closes if c > threshold) / len(closes)
    return sum(1 for c in closes if c < threshold) / len(closes)


def _ewma_variance(returns: list[float], lam: float = 0.94) -> float:
    """
    EWMA variance: sigma²_t = lam * sigma²_{t-1} + (1 - lam) * r²_t.
    Initialize with sample variance of first returns. lam ~ 0.94 is common (RiskMetrics).
    """
    if not returns or lam <= 0 or lam >= 1:
        return 0.0
    n = len(returns)
    if n < 3:
        return 0.0
    # Initialize with sample variance of first half (or first 20)
    init_n = min(20, max(3, n // 2))
    init_var = sum(r**2 for r in returns[:init_n]) / init_n
    var = init_var
    for i in range(init_n, n):
        var = lam * var + (1 - lam) * (returns[i] ** 2)
    return var


def _volatility_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    days_to_expiry: float = 30.0,
    use_ewma: bool = False,
    ewma_lambda: float = 0.94,
) -> float | None:
    """
    Log-normal / Black-Scholes style: P(price above/below K at expiry) using historical vol.
    use_ewma: if True, use EWMA variance instead of sample variance (time-varying vol).
    Returns None if insufficient data.
    """
    if not closes or len(closes) < 5 or threshold <= 0:
        return None
    S0 = float(closes[-1])
    if S0 <= 0:
        return None
    # Log returns
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            r = math.log(closes[i] / closes[i - 1])
            returns.append(r)
    if len(returns) < 3:
        return None
    if use_ewma:
        variance = _ewma_variance(returns, ewma_lambda)
    else:
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    sigma_daily = variance ** 0.5
    sigma_annual = sigma_daily * (252 ** 0.5)
    T = max(0.01, days_to_expiry / 365.0)
    if sigma_annual < 1e-8:
        return None
    # d = (ln(S0/K) - sigma^2*T/2) / (sigma*sqrt(T)); P(S>K) = Φ(d)
    d = (math.log(S0 / threshold) - 0.5 * sigma_annual**2 * T) / (sigma_annual * (T ** 0.5))
    # Φ(x) = (1 + erf(x/sqrt(2))) / 2
    phi_d = 0.5 * (1 + math.erf(d / (2 ** 0.5)))
    phi_d = max(0.0, min(1.0, phi_d))
    if direction == "above":
        return phi_d
    return 1.0 - phi_d


def _violates_no_above_rule(
    current_price: float, threshold: float, direction: str, cushion: float
) -> bool:
    """
    For "above X" contracts: buying NO when price is already well above X is risky.
    Returns True if we should NOT buy NO (or should sell existing NO) because
    current_price > X * (1 + cushion).
    """
    if direction != "above" or threshold <= 0:
        return False
    return current_price > threshold * (1 + cushion)


def _fee_per_contract(price: float, maker_rate: float) -> float:
    """
    Maker fee per contract: rate × P × (1−P). Kalshi & Gemini use this formula.
    Rounded up to next cent per exchange docs. Returns 0 if rate=0 or invalid price.
    """
    if maker_rate <= 0 or price <= 0 or price >= 1:
        return 0.0
    fee = maker_rate * price * (1.0 - price)
    if fee <= 0:
        return 0.0
    return math.ceil(fee * 100) / 100


def _recommend(
    historical_p_yes: float,
    market_yes_price: float,
    min_edge: float,
    maker_fee_rate: float = 0.0,
) -> str:
    """
    Recommend BUY YES / BUY NO / SKIP based on historical vs market.
    Buy YES when historical says P(YES) > market_price + edge (market underpriced).
    Buy NO when historical says P(NO) > (1 - market_yes_price) + edge.
    When both have edge, prefer whichever yields the larger net return.
    min_edge is the minimum *net* edge after fees (e.g. 0.01 = 1¢ profit per $1 contract).
    """
    if market_yes_price <= 0:
        return REC_SKIP  # No liquidity
    no_price = 1.0 - market_yes_price
    fee_yes = _fee_per_contract(market_yes_price, maker_fee_rate)
    fee_no = _fee_per_contract(no_price, maker_fee_rate)
    net_edge_yes = historical_p_yes - market_yes_price - fee_yes
    net_edge_no = (1.0 - historical_p_yes) - no_price - fee_no
    has_yes = net_edge_yes >= min_edge
    has_no = net_edge_no >= min_edge
    if has_yes and has_no:
        return REC_BUY_YES if net_edge_yes >= net_edge_no else REC_BUY_NO
    if has_yes:
        return REC_BUY_YES
    if has_no:
        return REC_BUY_NO
    return REC_SKIP


def _recommend_strict(
    historical_p_yes: float,
    market_yes_price: float,
    min_p_win: float,
    min_edge: float = 0.02,
    maker_fee_rate: float = 0.0,
) -> str:
    """
    For other intraday (e.g. 8pm ET): only BUY when ALMOST ABSOLUTELY CERTAIN we win.
    Requires P(win) >= min_p_win (e.g. 0.90) AND net edge after fees >= min_edge.
    When both YES and NO qualify, prefer whichever yields the larger net return.
    """
    if market_yes_price <= 0:
        return REC_SKIP
    no_price = 1.0 - market_yes_price
    fee_yes = _fee_per_contract(market_yes_price, maker_fee_rate)
    fee_no = _fee_per_contract(no_price, maker_fee_rate)
    net_edge_yes = historical_p_yes - market_yes_price - fee_yes
    net_edge_no = (1.0 - historical_p_yes) - no_price - fee_no
    p_no = 1.0 - historical_p_yes
    has_yes = historical_p_yes >= min_p_win and net_edge_yes >= min_edge
    has_no = p_no >= min_p_win and net_edge_no >= min_edge
    if has_yes and has_no:
        return REC_BUY_YES if net_edge_yes >= net_edge_no else REC_BUY_NO
    if has_yes:
        return REC_BUY_YES
    if has_no:
        return REC_BUY_NO
    return REC_SKIP


# ---------------------------------------------------------------------------
# Position sizing (Kelly criterion)
# ---------------------------------------------------------------------------
def _format_recommendation_reason(
    rec: str,
    hist_p: float,
    market_yes: float,
    threshold: float,
    current_price: float,
    direction: str,
    violates_no_above: bool,
    min_yes_price: float,
    min_no_price: float,
    max_buy_yes_strike_above_pct: float,
    vol_p: float | None,
    edge_pct: float,
) -> str:
    """Human-readable explanation for BUY YES / BUY NO / SKIP."""
    if rec == REC_BUY_YES:
        if violates_no_above:
            return f"Price ${current_price:,.0f} above strike → favor YES (edge {edge_pct:.1%} vs market {market_yes:.1%})"
        return f"Model P(YES)={hist_p:.1%} > market {market_yes:.1%} + min_edge → underpriced by {edge_pct:.1%}"
    if rec == REC_BUY_NO:
        p_no = 1 - hist_p
        no_price = 1 - market_yes
        return f"Model P(NO)={p_no:.1%} > market NO {no_price:.1%} → underpriced by {edge_pct:.1%}"
    # SKIP — explain which filter(s) applied
    parts: list[str] = []
    if hist_p >= market_yes + 0.10 and not violates_no_above:
        parts.append(f"model {hist_p:.1%} vs market {market_yes:.1%} but edge < min")
    if violates_no_above and hist_p < market_yes + 0.10:
        parts.append(f"price above strike but no edge (model {hist_p:.1%} vs market {market_yes:.1%})")
    if market_yes < min_yes_price and rec == REC_SKIP:
        parts.append(f"market YES {market_yes:.1%} < {min_yes_price:.0%} min (illiquid/far OTM)")
    if direction == "above" and max_buy_yes_strike_above_pct > 0:
        if threshold > current_price * (1 + max_buy_yes_strike_above_pct):
            vol_str = f"{vol_p:.1%}" if vol_p is not None else "n/a"
            parts.append(f"strike ${threshold:,.0f} >{max_buy_yes_strike_above_pct:.0%} above ${current_price:,.0f} (vol {vol_str} < 95%)")
    no_price = 1 - market_yes
    if no_price < min_no_price:
        parts.append(f"market NO {no_price:.1%} < {min_no_price:.0%} min")
    if (1 - hist_p) < (1 - market_yes) + 0.10 and hist_p < market_yes + 0.10:
        parts.append(f"no edge either way (model {hist_p:.1%}, market {market_yes:.1%})")
    return " · ".join(parts) if parts else f"model {hist_p:.1%} vs market {market_yes:.1%} — no edge"


def _compute_edge_diff(
    market_yes: float,
    hist_p_yes: float,
    rec: str,
    violates_no_above: bool,
    maker_fee_rate: float = 0.0,
) -> float:
    """
    Net edge after fees (per $1 contract). For BUY YES: hist_p - market_yes - fee.
    For BUY NO: (1-hist_p) - (1-market_yes) - fee. Returns 0 for SKIP.
    Caller uses abs(diff) for display.
    """
    if rec == REC_SKIP:
        return 0.0
    no_price = 1.0 - market_yes
    fee_yes = _fee_per_contract(market_yes, maker_fee_rate)
    fee_no = _fee_per_contract(no_price, maker_fee_rate)
    if rec == REC_BUY_YES:
        if violates_no_above:
            raw = 1.0 - market_yes  # favor YES when price above strike
        else:
            raw = hist_p_yes - market_yes
        return raw - fee_yes
    # REC_BUY_NO
    raw = (1.0 - hist_p_yes) - no_price
    return raw - fee_no


def _format_reason(
    rec: str,
    hist_p: float,
    market_yes: float,
    threshold: float,
    current_price: float,
    direction: str,
    violates_rule: bool,
    vol_p: float | None,
    min_yes_price: float,
    min_no_price: float,
    max_buy_yes_strike_above_pct: float,
    no_bid: float,
    min_edge: float,
    use_vol: bool,
) -> str:
    """Build human-readable reasoning for the recommendation."""
    if rec == REC_BUY_YES:
        if violates_rule:
            return f"Price ${current_price:,.0f} above strike ${threshold:,.0f} → favor YES (edge {hist_p - market_yes:.1%})"
        return f"Model P(YES) {hist_p:.1%} > market {market_yes:.1%} by {hist_p - market_yes:.1%} (min {min_edge:.0%})"
    if rec == REC_BUY_NO:
        p_no = 1 - hist_p
        mkt_no = 1 - market_yes
        return f"Model P(NO) {p_no:.1%} > market NO {mkt_no:.1%} by {p_no - mkt_no:.1%} (min {min_edge:.0%})"
    # SKIP — explain why
    parts: list[str] = []
    if market_yes < min_yes_price and rec == REC_SKIP:
        vol_override = use_vol and vol_p is not None and hist_p >= 0.95
        if not vol_override:
            parts.append(f"market YES {market_yes:.1%} < min {min_yes_price:.0%} (low liquidity / far OTM)")
    if no_bid < min_no_price and direction == "above":
        vol_override = use_vol and vol_p is not None and (1 - hist_p) >= 0.95
        if not vol_override:
            parts.append(f"market NO {no_bid:.1%} < min {min_no_price:.0%}")
    if (
        direction == "above"
        and max_buy_yes_strike_above_pct > 0
        and threshold > current_price * (1 + max_buy_yes_strike_above_pct)
    ):
        vol_conf = f"{vol_p:.1%}" if vol_p is not None else "N/A"
        parts.append(
            f"strike ${threshold:,.0f} >{max_buy_yes_strike_above_pct:.0%} above current ${current_price:,.0f} "
            f"(vol model {vol_conf} < 95% required)"
        )
    if violates_rule and hist_p < market_yes + min_edge:
        parts.append(f"would favor YES (price above strike) but no edge: model {hist_p:.1%} vs market {market_yes:.1%}")
    if hist_p >= market_yes + min_edge and (1 - hist_p) < (1 - market_yes) + min_edge and not parts:
        parts.append(f"no edge either way: model {hist_p:.1%} vs market {market_yes:.1%} (gap < {min_edge:.0%})")
    if not parts:
        edge_yes = hist_p - market_yes
        edge_no = (1 - hist_p) - (1 - market_yes)
        if abs(edge_yes) < min_edge and abs(edge_no) < min_edge:
            parts.append(f"insufficient edge: YES gap {edge_yes:.1%}, NO gap {edge_no:.1%} (min {min_edge:.0%})")
        else:
            parts.append(f"model {hist_p:.1%} vs market {market_yes:.1%} — no clear mispricing")
    return "  ".join(parts) if parts else "no edge"


def _kelly_count(
    p_win: float,
    price: float,
    balance_cents: float = 100_00,  # default $100
    kelly_fraction: float = 0.25,
    min_contracts: int = 1,
    max_contracts: int = 100,
) -> int:
    """
    Kelly criterion for binary contracts: f* = (p*b - q) / b.
    For a contract at price c: b = (1-c)/c (payout per dollar risked), q = 1-p.
    Returns suggested contract count (capped).
    """
    if price <= 0 or price >= 1 or p_win <= 0 or p_win >= 1:
        return min_contracts
    q = 1 - p_win
    b = (1 - price) / price  # odds: win (1-price) per price risked
    f = (p_win * b - q) / b
    if f <= 0:
        return min_contracts
    f = min(f, kelly_fraction)
    cost_per_contract_cents = price * 100
    if cost_per_contract_cents <= 0:
        return min_contracts
    count = int(balance_cents * f / cost_per_contract_cents)
    return max(min_contracts, min(max_contracts, count))


def _compute_buy_count(
    p_win: float,
    price: float,
    balance_cents: float,
    sp: _StrategyParams,
    use_kelly: bool,
    kelly_fraction: float,
    min_contracts: int,
) -> int:
    """
    Compute contract count for a buy. When it's an "absolute winner" (very high P(win), very low
    price — e.g. 1¢ on a near-certainty), allow buying more contracts while respecting balance.
    Otherwise use Kelly or default.
    """
    is_absolute_winner = (
        price <= sp.absolute_winner_max_price
        and p_win >= sp.absolute_winner_min_p_win
        and sp.absolute_winner_max_pct_of_balance > 0
    )
    if is_absolute_winner:
        cost_cents = max(1, int(price * 100))
        available_cents = int(balance_cents * sp.absolute_winner_max_pct_of_balance)
        count = min(
            available_cents // cost_cents,
            sp.absolute_winner_max_contracts,
        )
        return max(min_contracts, count)
    if use_kelly:
        return _kelly_count(
            p_win, price, balance_cents, kelly_fraction, min_contracts
        )
    return 10


def _find_gemini_event(gemini_events_by_asset: dict, ticker: str) -> dict:
    """Find Gemini event by ticker across all assets. Returns {} if not found."""
    for events in gemini_events_by_asset.values():
        for evt in events:
            if evt.get("ticker") == ticker:
                return evt
    return {}


def _check_intra_arb_gemini(evt: dict, min_arb_fee: float) -> tuple[float, float, float] | None:
    """
    Intra-exchange arb on Gemini: if yes_price + no_price < 1 - fee, buy both for risk-free profit.
    Uses bestBid/lastTrade as proxy for executable price. Returns (yes_p, no_p, profit_pct) or None.
    """
    yes_p = get_yes_price(_get_yes_contract(evt) or {})
    no_p = get_no_price(evt) if yes_p > 0 else (1.0 - yes_p)
    if no_p <= 0:
        no_p = 1.0 - yes_p
    total = yes_p + no_p
    if total > 0 and total < (1.0 - min_arb_fee):
        return (yes_p, no_p, (1.0 - total) * 100)
    return None


def _check_intra_arb_gemini(evt: dict, min_arb_fee: float) -> tuple[float, float, float] | None:
    """
    Intra-exchange arb on Gemini: if yes + no < 1 - fee, buy both for risk-free profit.
    Uses bestBid/implied prices as proxy for ask (Gemini may not expose bestAsk).
    Returns (yes_price, no_price, profit_pct) or None.
    """
    yes_c = _get_yes_contract(evt)
    yes_p = get_yes_price(yes_c) if yes_c else 0.0
    no_p = get_no_price(evt) if yes_p > 0 else (1.0 - yes_p)
    if no_p <= 0:
        no_p = 1.0 - yes_p
    total = yes_p + no_p
    if total > 0 and total < (1.0 - min_arb_fee):
        return (yes_p, no_p, (1.0 - total) * 100)
    return None


def _check_intra_arb_kalshi(m: dict, min_arb_fee: float) -> tuple[float, float, float] | None:
    """
    Intra-exchange arb: if yes_ask + no_ask < 1 - fee, buy both for risk-free profit.
    Returns (yes_ask, no_ask, profit_pct) or None.
    """
    yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_bid_dollars") or 0)
    no_ask = float(m.get("no_ask_dollars") or m.get("no_bid_dollars") or 0)
    if yes_ask <= 0:
        yes_ask = float(m.get("last_price_dollars") or 0) or (1.0 - no_ask)
    if no_ask <= 0:
        no_ask = 1.0 - yes_ask
    total = yes_ask + no_ask
    if total > 0 and total < (1.0 - min_arb_fee):
        return (yes_ask, no_ask, (1.0 - total) * 100)
    return None


def _find_matching_gemini_event(
    gemini_evts: list[dict], threshold: float, direction: str
) -> dict | None:
    """Find Gemini event with same threshold (±1%) and direction for inter-arb matching."""
    for evt in gemini_evts or []:
        th_g, dir_g = extract_strike_from_event(evt)
        if th_g is None or dir_g != direction:
            continue
        if threshold <= 0:
            continue
        if abs(th_g - threshold) / threshold < 0.01:
            return evt
    return None


def _check_inter_arb(
    kalshi_m: dict,
    gemini_evt: dict,
    min_arb_fee: float,
) -> tuple[str, str, float, float, float, float] | None:
    """
    Inter-exchange arb: buy YES on one platform + NO on other when sum < 1 - fee.
    Returns (best_yes_platform, best_no_platform, yes_price, no_price, other_yes, other_no) or None.
    Best combination is the one with lowest total cost.
    """
    yes_k = float(kalshi_m.get("yes_ask_dollars") or kalshi_m.get("yes_bid_dollars") or 0)
    no_k = float(kalshi_m.get("no_ask_dollars") or kalshi_m.get("no_bid_dollars") or 0)
    if yes_k <= 0:
        yes_k = float(kalshi_m.get("last_price_dollars") or 0)
    if no_k <= 0:
        no_k = 1.0 - yes_k
    yes_g = get_yes_price(_get_yes_contract(gemini_evt) or {})
    no_g = get_no_price(gemini_evt) if yes_g > 0 else (1.0 - yes_g)
    if no_g <= 0:
        no_g = 1.0 - yes_g
    cost_k_yes_g_no = yes_k + no_g
    cost_g_yes_k_no = yes_g + no_k
    if cost_k_yes_g_no < (1.0 - min_arb_fee):
        return (PLATFORM_KALSHI, PLATFORM_GEMINI, yes_k, no_g, yes_g, no_k)
    if cost_g_yes_k_no < (1.0 - min_arb_fee):
        return (PLATFORM_GEMINI, PLATFORM_KALSHI, yes_g, no_k, yes_k, no_g)
    return None


# ---------------------------------------------------------------------------
# Contradictory-position avoidance (same underlying + resolution, mutually exclusive outcomes)
# ---------------------------------------------------------------------------
_INF = 1e12


def _extract_resolution_key_from_ticker(ticker: str) -> str | None:
    """Extract resolution key from Kalshi ticker e.g. KXSOLD-26MAR1617-T82.9999 -> 26MAR1617."""
    if not ticker:
        return None
    parts = ticker.split("-")
    return parts[1] if len(parts) >= 2 else None


def _extract_resolution_key_from_gemini_ticker(ticker: str) -> str | None:
    """Extract resolution key from Gemini ticker e.g. SOL2603162100 -> 2603162100 (YYMMDDHHMM)."""
    if not ticker or len(ticker) < 10:
        return None
    # Asset prefix is 3-4 chars (BTC, ETH, SOL, XRP); rest is date+time
    for n in (4, 3):
        if len(ticker) > n and ticker[n:].isdigit():
            return ticker[n:]
    return None


def _get_resolution_key_for_order(o: dict) -> str | None:
    """Get resolution key for any order (Kalshi or Gemini)."""
    platform = o.get("platform", "")
    ticker = o.get("ticker", "")
    if platform == PLATFORM_KALSHI:
        return _extract_resolution_key_from_ticker(ticker)
    if platform == PLATFORM_GEMINI:
        return _extract_resolution_key_from_gemini_ticker(ticker)
    return None


def _get_win_range(threshold: float, direction: str, side: str) -> tuple[float, float]:
    """
    Return (lo, hi) price range where this position wins. Uses -_INF/_INF for unbounded.
    YES above X: [X, inf). NO above X: (-inf, X). YES below X: (-inf, X]. NO below X: (X, inf).
    """
    if direction == "above":
        if side == "yes":
            return (threshold, _INF)
        return (-_INF, threshold)
    # direction == "below"
    if side == "yes":
        return (-_INF, threshold)
    return (threshold, _INF)


def _ranges_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    """True if ranges [lo1,hi1) and [lo2,hi2) overlap. (Use open endpoint for strict < vs <=.)"""
    # Ranges overlap iff lo1 < hi2 and lo2 < hi1
    return lo1 < hi2 and lo2 < hi1


def _filter_contradictory_buys(
    pending_orders: list[dict],
    existing_by_underlying: dict[tuple[str, str, str], list[tuple[float, str, str]]],
) -> None:
    """
    Remove BUYs that contradict positions we're keeping OR other pending BUYs (same underlying + resolution + platform).
    Contradiction check is per-platform only: NO on SOL>87 (Kalshi) + YES on SOL>93 (Gemini) is allowed.
    existing_by_underlying: (yf_ticker, res_key, platform) -> [(threshold, direction, side), ...]
    Process in descending edge order so we keep the best bet when there's a conflict.
    """
    # Deep-copy existing; we'll add accepted orders as we go
    accepted: dict[tuple[str, str, str], list[tuple[float, str, str]]] = {
        k: list(v) for k, v in existing_by_underlying.items()
    }
    # Indexed by original position; sort by edge desc so we keep higher-edge when conflicts exist
    indexed = [(i, o) for i, o in enumerate(pending_orders)]
    indexed.sort(key=lambda x: x[1].get("edge", 0.0), reverse=True)

    to_remove: set[int] = set()
    for i, o in indexed:
        if i in to_remove:
            continue
        platform = o.get("platform", "")
        yf = o.get("yf_ticker")
        th = o.get("threshold")
        direction = o.get("direction")
        rec = o.get("rec", "")
        side = o.get("side", "yes" if rec == REC_BUY_YES else "no")
        if not yf or th is None or direction is None:
            continue
        res_key = _get_resolution_key_for_order(o)
        if not res_key:
            continue
        key = (yf, res_key, platform)
        our_lo, our_hi = _get_win_range(th, direction, side)
        conflicts = False
        for (ex_th, ex_dir, ex_side) in accepted.get(key, []):
            # Arb: same threshold + direction, one YES one NO — intentional, allow both
            if ex_th == th and ex_dir == direction and ex_side != side:
                continue
            ex_lo, ex_hi = _get_win_range(ex_th, ex_dir, ex_side)
            if not _ranges_overlap(our_lo, our_hi, ex_lo, ex_hi):
                conflicts = True
                break
        if conflicts:
            to_remove.add(i)
            ticker = o.get("ticker", "?")
            print(f"  (skip BUY {ticker} — contradicts existing or other BUY on same underlying)")
        else:
            accepted.setdefault(key, []).append((th, direction, side))

    for i in sorted(to_remove, reverse=True):
        pending_orders.pop(i)


def _resolve_series_for_position(m: dict, full: dict, ticker: str, yf_mapping: dict) -> str | None:
    """Resolve series_ticker for a position. Returns yf_ticker or None if not mappable."""
    series = (
        m.get("series_ticker")
        or m.get("event_ticker")
        or (full.get("event") or {}).get("series_ticker")
        or (full.get("event") or {}).get("event_ticker")
        or ""
    )
    pos_yf = yf_mapping.get((series or "").upper()) if series else None
    if pos_yf:
        return pos_yf
    # Fallback: infer from ticker (e.g. KXBTC-25MAR16 -> KXBTCD, KXINX -> KXINXU)
    ticker_upper = (ticker or "").upper()
    for series_key, yf_tick in yf_mapping.items():
        sk = (series_key or "").upper()
        if sk and (sk[:5] in ticker_upper or sk.replace("D", "")[:5] in ticker_upper):
            return yf_tick
    return None


def _format_trades_for_ollama(
    pending_orders: list[dict],
    pending_sells: list[dict],
) -> str:
    """Format proposed trades for Ollama — concise, focused on tickers, edges, prices."""
    lines: list[str] = []
    if pending_sells:
        lines.append(f"PROPOSED SELLs ({len(pending_sells)}):")
        for i, s in enumerate(pending_sells, 1):
            lines.append(
                f"  {i}. [{s['platform'].upper()}] SELL {s['side'].upper()} {s['ticker']} | "
                f"{s.get('title', '')[:60]} | Count: {s['count']} @ ${s['price']:.2f}"
            )
        lines.append("")
    if pending_orders:
        lines.append(f"PROPOSED BUYs ({len(pending_orders)}):")
        for i, o in enumerate(pending_orders, 1):
            title = (o.get("title") or "")[:60]
            lines.append(
                f"  {i}. [{o['platform'].upper()}] {o['rec']} {o['ticker']} | "
                f"{title} | Edge: {o['edge']:.1%} | Price: ${o['price']:.2f} | Count: {o.get('count', 10)}"
            )
    if not lines:
        return "No proposed trades."
    return "\n".join(lines).strip()


def _allocate_budget_to_orders(
    pending_orders: list[dict],
    balance_cents: int,
    max_pct: float,
) -> None:
    """
    Adjust order counts so total cost fits budget. Prefer buying 1 of many
    contracts over blowing the budget on a few. Modifies pending_orders in place.
    """
    if not pending_orders:
        return
    available_cents = int(balance_cents * max_pct)
    total_cost_cents = sum(
        o.get("count", 10) * int(float(o.get("price", 0)) * 100)
        for o in pending_orders
    )
    if total_cost_cents <= available_cents:
        return
    # Scale down: prefer 1 of each for diversification
    cost_if_1_each = sum(int(float(o.get("price", 0)) * 100) for o in pending_orders)
    if cost_if_1_each <= available_cents:
        for o in pending_orders:
            o["count"] = 1
        return
    # Can't afford 1 of each: keep highest-edge trades first
    by_edge = sorted(pending_orders, key=lambda x: -float(x.get("edge", 0)))
    kept: list[dict] = []
    running_cents = 0
    for o in by_edge:
        cost_1 = int(float(o.get("price", 0)) * 100)
        if running_cents + cost_1 <= available_cents:
            o["count"] = 1
            kept.append(o)
            running_cents += cost_1
        else:
            o["count"] = 0  # Will be filtered
    pending_orders[:] = [o for o in by_edge if o.get("count", 0) > 0]


def _sync_kalshi_settlements(kalshi: KalshiClient, cfg: dict) -> None:
    """Fetch Kalshi settlements and update our unresolved predictions with outcomes."""
    if not get_unresolved_predictions or not record_prediction_outcome:
        return
    unresolved = get_unresolved_predictions(cfg)
    kalshi_preds = [p for p in unresolved if p.get("platform") == PLATFORM_KALSHI]
    if not kalshi_preds:
        return
    tickers_needed = {p["ticker"] for p in kalshi_preds}
    settlements_by_ticker: dict[str, dict] = {}
    try:
        cursor = ""
        while True:
            r = kalshi.get_settlements(limit=200, **({"cursor": cursor} if cursor else {}))
            for s in r.get("settlements") or []:
                t = s.get("ticker")
                if t and t in tickers_needed:
                    settlements_by_ticker[t] = s
            cursor = r.get("cursor") or ""
            if not cursor:
                break
    except Exception:
        return
    for p in kalshi_preds:
        t = p.get("ticker")
        s = settlements_by_ticker.get(t) if t else None
        if not s:
            continue
        mr = (s.get("market_result") or "").lower()
        yes_settled = mr == "yes"
        if mr not in ("yes", "no"):
            continue
        entry = float(p.get("entry_price") or 0)
        cnt = int(p.get("count") or 0)
        if cnt <= 0:
            continue
        side = (p.get("side") or "").lower()
        won = (side == "yes" and yes_settled) or (side == "no" and not yes_settled)
        pnl_cents = int((100 - entry * 100) * cnt) if won else int(-entry * 100 * cnt)
        record_prediction_outcome(p["id"], yes_settled, pnl_cents, cfg)
        if record_resolved and get_last_snapshot_for_ticker:
            snap = get_last_snapshot_for_ticker(t, platform=PLATFORM_KALSHI, config=cfg)
            record_resolved(
                ticker=t,
                yes_settled=yes_settled,
                platform=PLATFORM_KALSHI,
                title=snap.get("title") if snap else None,
                our_last_rec=snap.get("rec") if snap else None,
                our_last_hist_p=snap.get("hist_p") if snap else None,
                market_yes_at_last=snap.get("yes_bid") if snap else None,
                config=cfg,
            )


def main():
    cfg = get_config()
    strategy = cfg.get("strategy", {})
    default_days = int(strategy.get("days_back", 365))

    parser = argparse.ArgumentParser(
        description="Evaluate Kalshi/Gemini contracts using historical data → BUY YES / BUY NO / SKIP"
    )
    parser.add_argument("--days", type=int, default=default_days, help="Days of historical data (default: config days_back or 365)")
    parser.add_argument("--markets", type=int, default=0, help="Max markets per series (0 = all)")
    parser.add_argument("--edge", type=float, default=0.15, help="Min edge (0-1) to recommend buy")
    parser.add_argument("--no-llm", action="store_true", help="Skip Ollama analysis")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show closes range vs threshold (explains Historical P(YES))")
    parser.add_argument("--preview", action="store_true", help="Show API calls we would make (no orders placed)")
    parser.add_argument("--execute", action="store_true", help="Place real buy/sell orders (requires dry_run: false in config)")
    parser.add_argument("--use-volatility", action="store_true", help="Use volatility-based probability (option-style) in addition to empirical")
    parser.add_argument("--log", metavar="DIR", nargs="?", const="~/Desktop/trading-bot-logs", default=None, help="Log run output to ~/Desktop/trading-bot-logs/ or specified dir (creates eval_YYYYMMDD.log)")
    args = parser.parse_args()
    args.use_llm = not args.no_llm

    return capture_and_analyze(
        lambda: _run(args),
        "scripts/compare_historical.py",
        use_llm=args.use_llm,
        per_trade_analysis=True,
    )


def _run(args) -> int:
    cfg = get_config()
    markets_list = cfg.get("markets", [])
    yf_mapping = cfg.get("yfinance_tickers", {})
    strategy = cfg.get("strategy", {})
    trading = cfg.get("trading", {})
    min_edge = float(strategy.get("min_edge", args.edge))
    use_kelly = strategy.get("use_kelly", False)
    kelly_fraction = float(strategy.get("kelly_fraction", 0.25))
    whitelist = strategy.get("whitelist")  # if set, only these series
    blacklist = set((strategy.get("blacklist") or []))  # exclude these
    min_contracts = int(trading.get("min_contracts", 1))

    # Filter markets by whitelist/blacklist (Freqtrade-style)
    if whitelist:
        markets_list = [m for m in markets_list if m.strip().upper() in {s.upper() for s in whitelist}]
    if blacklist:
        markets_list = [m for m in markets_list if m.strip().upper() not in {s.upper() for s in blacklist}]

    if not markets_list or not yf_mapping:
        print("Configure markets and yfinance_tickers in config.yaml")
        return 1

    # Optional file logging (Freqtrade-style run history)
    log_dir = getattr(args, "log", None)
    log_file = None
    orig_stdout = None
    if log_dir:
        log_dir_resolved = Path(log_dir).expanduser().resolve()
        log_dir_resolved.mkdir(parents=True, exist_ok=True)
        log_path = log_dir_resolved / f"eval_{datetime.utcnow().strftime('%Y%m%d')}.log"
        log_file = open(log_path, "a", encoding="utf-8")
        orig_stdout = sys.stdout
        print(f"Logging to {log_path}", flush=True)

        class Tee:
            """Writes to multiple file handles (stdout and log file)."""

            def __init__(self, *files):
                self.files = files

            def write(self, obj):
                for f in self.files:
                    f.write(obj)
                    f.flush()

            def flush(self):
                for f in self.files:
                    f.flush()

        sys.stdout = Tee(orig_stdout, log_file)

    try:
        return _run_impl(args, cfg, markets_list, yf_mapping, strategy, use_kelly, kelly_fraction, min_contracts)
    finally:
        if log_file and orig_stdout:
            sys.stdout = orig_stdout
            log_file.close()


def _run_impl(args, cfg, markets_list, yf_mapping, strategy, use_kelly, kelly_fraction, min_contracts) -> int:
    min_edge = float(strategy.get("min_edge", args.edge))
    kalshi = KalshiClient()
    balance_cents = 100_00  # default $100 for Kelly when balance unavailable
    try:
        bal = kalshi.get_balance()
        balance_cents = int(bal.get("balance", 100_00))
    except Exception:
        pass
    to_date = datetime.utcnow()
    from_date = to_date - timedelta(days=args.days)
    from_ts, to_ts = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")

    print("Contract Evaluation: Historical Data → Buy/Skip Recommendation")
    print("=" * 75)
    print(f"Period: {from_ts} to {to_ts} ({args.days} days) | min_edge={min_edge:.0%}")
    print("Historical P(YES) = fraction of those days the close was above/below threshold.")
    print("  Use --verbose to see closes range (explains why 0%% when threshold is far from price).")
    print()

    # Sync Kalshi settlements → update unresolved predictions with outcomes
    try:
        _sync_kalshi_settlements(kalshi, cfg)
    except Exception:
        pass

    # Fetch Gemini events and Kalshi markets in parallel
    gemini = GeminiPredictionClient()
    gemini_events_by_asset: dict[str, list] = {}
    kalshi_markets_by_series: dict[str, list] = {}

    def _fetch_gemini_events() -> dict[str, list]:
        out: dict[str, list] = {}
        try:
            events = gemini.get_crypto_events(limit=500)
            for evt in events:
                asset = _event_to_asset(evt)
                if asset:
                    out.setdefault(asset, []).append(evt)
        except Exception:
            pass
        return out

    def _fetch_kalshi_markets() -> dict[str, list]:
        out: dict[str, list] = {}
        for s in markets_list:
            series = s.strip()
            try:
                r = kalshi.get_markets(series_ticker=series.upper(), status="open")
                mkt_list = r.get("markets", []) or []
                if args.markets > 0:
                    mkt_list = mkt_list[: args.markets]
                out[series] = mkt_list
            except Exception:
                out[series] = []
        return out

    with ThreadPoolExecutor(max_workers=2) as executor:
        fut_gemini = executor.submit(_fetch_gemini_events)
        fut_kalshi = executor.submit(_fetch_kalshi_markets)
        gemini_events_by_asset = fut_gemini.result()
        kalshi_markets_by_series = fut_kalshi.result()

    # Diagnostic: how many Gemini events per asset
    total_gem = sum(len(v) for v in gemini_events_by_asset.values())
    if total_gem > 0:
        per_asset = ", ".join(f"{a}:{len(v)}" for a, v in sorted(gemini_events_by_asset.items()))
        print(f"[Gemini] Fetched {total_gem} events ({per_asset})")

    pending_orders: list[dict] = []
    pending_sells: list[dict] = []
    sp = _load_strategy_params(strategy)

    # Build ticker -> (series, yf_ticker, closes, market_data) for sell evaluation
    market_data_cache: dict[str, tuple[str, str, list[float], dict]] = {}

    use_vol = getattr(args, "use_volatility", False)
    for series in markets_list:
        series = series.strip()
        yf_ticker = yf_mapping.get(series.upper())
        if not yf_ticker:
            print(f"[{series}] No yfinance_tickers mapping - skip")
            continue

        closes = yf_get_aggs(yf_ticker, days_back=args.days, config=cfg)
        if not closes:
            print(f"[{series}] No close data for {yf_ticker}")
            continue

        current_price = closes[-1]
        print(f"\n[{series}] → {yf_ticker} | ${current_price:,.2f} | {len(closes)} days")

        mkt_list = kalshi_markets_by_series.get(series, [])

        gemini_used_in_arb: set[str] = set()
        for mkt in mkt_list or []:
            ticker = mkt.get("ticker", "?")
            title = (mkt.get("title") or "")[:55]
            try:
                full = kalshi.get_market(ticker)
                m = full.get("market", full)
            except Exception:
                m = mkt

            if _should_skip_by_resolution(m, yf_ticker):
                print(f"  {ticker}: SKIP (unsupported resolution — use 5pm ET crypto / 4pm ET S&P) | {title}")
                continue

            yes_bid = float(m.get("yes_bid_dollars") or 0)
            if yes_bid <= 0:
                yes_bid = float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)

            threshold, direction = _extract_strike(m)
            if threshold is None or direction is None:
                # Skip: empirical and vol models both require a threshold (from title, API, or ticker)
                print(f"  {ticker}: YES {yes_bid:.2f} | (no threshold) {title}")
                continue

            market_data_cache[ticker] = (series, yf_ticker, closes, m)

            # Systematic edges: intra-exchange arb (buy YES+NO when sum < 1 - fee)
            arb_used = False
            if sp.enable_intra_arb:
                intra = _check_intra_arb_kalshi(m, sp.min_arb_fee)
                if intra:
                    yes_ask, no_ask, profit_pct = intra
                    count_arb = min(10, max(1, int(balance_cents * 0.1 / (yes_ask + no_ask) / 100)))
                    print(f"  {ticker}")
                    print(f"    {title}")
                    print(f"    ARB_INTRA: yes_ask+no_ask={yes_ask+no_ask:.2f} < 1-{sp.min_arb_fee:.0%} → BUY BOTH (profit ~{profit_pct:.1f}%)")
                    pending_orders.append({"platform": PLATFORM_KALSHI, "ticker": ticker, "title": title, "rec": REC_ARB_INTRA, "side": "yes", "price": yes_ask, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{ticker}-intra"})
                    pending_orders.append({"platform": PLATFORM_KALSHI, "ticker": ticker, "title": title, "rec": REC_ARB_INTRA, "side": "no", "price": no_ask, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{ticker}-intra"})
                    arb_used = True

            # Inter-exchange arb (Kalshi + Gemini): buy YES on one, NO on other when sum < 1 - fee
            if not arb_used and sp.enable_inter_arb and yf_ticker in GEMINI_CRYPTO_YF:
                gemini_match = _find_matching_gemini_event(gemini_events_by_asset.get(yf_ticker, []), threshold, direction)
                if gemini_match:
                    inter = _check_inter_arb(m, gemini_match, sp.min_arb_fee)
                    if inter:
                        yes_plat, no_plat, yes_p, no_p, _, _ = inter
                        cost = yes_p + no_p
                        profit_pct = (1.0 - cost) * 100
                        count_arb = min(10, max(1, int(balance_cents * 0.1 / cost / 100)))
                        print(f"  {ticker}")
                        print(f"    {title}")
                        print(f"    ARB_INTER: {yes_plat} YES@{yes_p:.2f} + {no_plat} NO@{no_p:.2f} = {cost:.2f} < 1-{sp.min_arb_fee:.0%} → hedge (profit ~{profit_pct:.1f}%)")
                        k_ticker = ticker
                        g_ticker = gemini_match.get("ticker", "?")
                        if yes_plat == PLATFORM_KALSHI:
                            pending_orders.append({"platform": PLATFORM_KALSHI, "ticker": k_ticker, "title": title, "rec": REC_ARB_INTER, "side": "yes", "price": yes_p, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{k_ticker}-{g_ticker}"})
                            pending_orders.append({"platform": PLATFORM_GEMINI, "ticker": g_ticker, "title": (gemini_match.get("title") or "")[:55], "rec": REC_ARB_INTER, "side": "no", "price": no_p, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{k_ticker}-{g_ticker}", "gemini_evt": gemini_match})
                        else:
                            pending_orders.append({"platform": PLATFORM_GEMINI, "ticker": g_ticker, "title": (gemini_match.get("title") or "")[:55], "rec": REC_ARB_INTER, "side": "yes", "price": yes_p, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{k_ticker}-{g_ticker}", "gemini_evt": gemini_match})
                            pending_orders.append({"platform": PLATFORM_KALSHI, "ticker": k_ticker, "title": title, "rec": REC_ARB_INTER, "side": "no", "price": no_p, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{k_ticker}-{g_ticker}"})
                        gemini_used_in_arb.add(g_ticker)
                        arb_used = True

            if arb_used:
                print()
                continue

            emp = _empirical_probability(closes, threshold, direction)
            hist_p = emp
            dte = _get_days_to_expiry_from_market(m, yf_ticker, sp.days_to_expiry)
            vol_p = (
                _volatility_probability(closes, threshold, direction, dte, use_ewma=sp.use_ewma_vol, ewma_lambda=sp.ewma_lambda)
                if use_vol else None
            )
            if vol_p is not None and use_vol:
                # Intraday: empirical is wrong for short-horizon; use vol model.
                if _is_intraday_market(m, yf_ticker):
                    hist_p = vol_p
                elif sp.vol_blend_weight is not None:
                    hist_p = sp.vol_blend_weight * emp + (1 - sp.vol_blend_weight) * vol_p
                else:
                    hist_p = max(emp, vol_p)
            # For non-standard intraday (e.g. 8pm ET): use relaxed threshold when volatility model available
            min_p_win = sp.intraday_other_min_p_win_with_vol if (use_vol and vol_p is not None) else sp.intraday_other_min_p_win
            rec = (
                _recommend_strict(hist_p, yes_bid, min_p_win, min_edge, sp.kalshi_maker_fee_rate)
                if _is_other_intraday(m, yf_ticker)
                else _recommend(hist_p, yes_bid, min_edge, sp.kalshi_maker_fee_rate)
            )
            # Skip BUY NO when price already above strike (risky to bet NO)
            violates_rule = _violates_no_above_rule(current_price, threshold, direction, sp.no_above_cushion)
            if rec == REC_BUY_NO and violates_rule:
                rec = REC_SKIP
            # Favor BUY YES when price is well above strike — but only if we have edge (hist_p >= market)
            if rec == REC_SKIP and violates_rule:
                if 0 < yes_bid < (1 - min_edge) and hist_p >= yes_bid + min_edge:
                    rec = REC_BUY_YES
            # Skip BUY YES when market prices it very low (e.g. 1¢): market thinks <5% chance; often illiquid or model overconfidence for far OTM
            if rec == REC_BUY_YES and yes_bid < sp.min_yes_price:
                if not (use_vol and vol_p is not None and hist_p >= 0.95):  # Override when vol model shows high confidence
                    rec = REC_SKIP
            # Skip BUY YES when strike is far above current (far OTM) unless vol model shows ≥95% confidence
            if rec == REC_BUY_YES and direction == "above" and sp.max_buy_yes_strike_above_pct > 0:
                if threshold > current_price * (1 + sp.max_buy_yes_strike_above_pct):
                    if not (use_vol and vol_p is not None and vol_p >= 0.95):
                        rec = REC_SKIP
            # Skip BUY NO when market prices NO very low (market thinks our side unlikely); same override when vol shows high P(NO)
            no_bid_early = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
            if rec == REC_BUY_NO and no_bid_early < sp.min_no_price:
                if not (use_vol and vol_p is not None and (1 - hist_p) >= 0.95):
                    rec = REC_SKIP
            # Skip BUY NO on "below X" when strike is far above current (BUY NO = "price > X" needs big rally) unless vol shows P(NO) ≥95%
            if rec == REC_BUY_NO and direction == "below" and sp.max_buy_no_strike_above_pct > 0:
                if threshold > current_price * (1 + sp.max_buy_no_strike_above_pct):
                    if not (use_vol and vol_p is not None and (1 - hist_p) >= 0.95):
                        rec = REC_SKIP
            diff = _compute_edge_diff(yes_bid, hist_p, rec, violates_rule, sp.kalshi_maker_fee_rate)

            print(f"  {ticker}")
            print(f"    {title}")
            print(f"    Threshold: ${threshold:,.0f} ({direction})")
            if vol_p is None:
                p_label = "P(YES)"
            else:
                emp_kalshi = _empirical_probability(closes, threshold, direction)
                p_label = f"P(YES) [emp={emp_kalshi:.1%} vol={vol_p:.1%}]"
            print(f"    {p_label}: {hist_p:.1%}  Market YES: {yes_bid:.1%}  →  {rec}")
            reason = _format_reason(
                rec, hist_p, yes_bid, threshold, current_price, direction,
                violates_rule, vol_p, sp.min_yes_price, sp.min_no_price,
                sp.max_buy_yes_strike_above_pct, no_bid_early, min_edge, use_vol,
            )
            print(f"    Why: {reason}")
            if getattr(args, "verbose", False):
                mn, mx = min(closes), max(closes)
                n = sum(1 for c in closes if (c > threshold if direction == "above" else c < threshold))
                print(f"    (closes ${mn:,.0f}-${mx:,.0f}; {n}/{len(closes)} days {'above' if direction == 'above' else 'below'} threshold)")
            try:
                from storage import record_market_snapshot
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                record_market_snapshot(PLATFORM_KALSHI, ticker, yes_bid, no_bid, hist_p, rec, threshold, direction, vol_p, abs(diff), title, yf_ticker, cfg)
            except ImportError:
                pass
            if rec != REC_SKIP:
                print(f"    Edge: {abs(diff):.1%}")
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                bid = yes_bid if rec == REC_BUY_YES else no_bid
                p_win = hist_p if rec == REC_BUY_YES else (1 - hist_p)
                dip = sp.limit_dip_pct
                # When model suggests contract price might decrease (vol regime or 2+ days to expiry), prioritize limit orders with larger dip
                price_might_decline = (use_vol and vol_p is not None) or (dte is not None and dte >= 2)
                if price_might_decline and sp.limit_dip_pct_price_decline > 0:
                    dip = max(dip, sp.limit_dip_pct_price_decline)
                if p_win >= 0.90 and sp.limit_dip_pct_high_confidence > 0:
                    dip = max(dip, sp.limit_dip_pct_high_confidence)
                # Limit price: bid or below (dip-catch). High confidence → larger dip (e.g. 15%) to catch bigger price drops
                price = max(0.01, min(0.99, bid * (1 - dip))) if dip else bid
                count = _compute_buy_count(
                    p_win, price, balance_cents, sp, use_kelly, kelly_fraction, min_contracts
                )
                pending_orders.append({
                    "platform": PLATFORM_KALSHI,
                    "ticker": ticker,
                    "title": title,
                    "rec": rec,
                    "side": "yes" if rec == REC_BUY_YES else "no",
                    "price": price,
                    "edge": abs(diff),
                    "count": count,
                    "hist_p": hist_p,
                    "vol_p": vol_p,
                    "yf_ticker": yf_ticker,
                    "threshold": threshold,
                    "direction": direction,
                })
            print()

        # Gemini crypto — expand to individual markets (binary: 1 per event; categorical: 1 per strike contract)
        if yf_ticker in GEMINI_CRYPTO_YF:
            gemini_evts = gemini_events_by_asset.get(yf_ticker, [])
            gemini_markets = expand_gemini_events_to_markets(gemini_evts)
            if args.markets > 0:
                gemini_markets = gemini_markets[: args.markets]
            use_vol = getattr(args, "use_volatility", False)
            for mkt in gemini_markets:
                evt = mkt["event"]
                contract = mkt.get("contract")
                g_ticker = mkt["ticker"]
                threshold_g = mkt["threshold"]
                direction_g = mkt["direction"]
                title = mkt["title"]
                instrument_symbol = mkt.get("instrument_symbol")
                if g_ticker in gemini_used_in_arb:
                    continue  # Already used in ARB_INTER with Kalshi
                impl = get_yes_price(contract) if contract else get_yes_price(_get_yes_contract(evt) or {})
                if _should_skip_by_resolution(evt, yf_ticker):
                    print(f"  [Gemini] {g_ticker}: SKIP (unsupported resolution — use 5pm ET) | {title}")
                    continue
                # Intra-exchange arb: binary only (categorical contracts have per-strike yes/no)
                arb_used_g = False
                if sp.enable_intra_arb and contract is None:
                    intra_g = _check_intra_arb_gemini(evt, sp.min_arb_fee)
                    if intra_g:
                        yes_p, no_p, profit_pct = intra_g
                        count_arb = min(10, max(1, int(balance_cents * 0.1 / (yes_p + no_p) / 100)))
                        print(f"  [Gemini] {g_ticker}")
                        print(f"    {title}")
                        print(f"    ARB_INTRA: yes+no={yes_p+no_p:.2f} < 1-{sp.min_arb_fee:.0%} → BUY BOTH (profit ~{profit_pct:.1f}%)")
                        pending_orders.append({"platform": PLATFORM_GEMINI, "ticker": g_ticker, "title": title, "rec": REC_ARB_INTRA, "side": "yes", "price": yes_p, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{g_ticker}-intra", "gemini_evt": evt})
                        pending_orders.append({"platform": PLATFORM_GEMINI, "ticker": g_ticker, "title": title, "rec": REC_ARB_INTRA, "side": "no", "price": no_p, "edge": profit_pct/100, "count": count_arb, "arb_group": f"{g_ticker}-intra", "gemini_evt": evt})
                        arb_used_g = True
                if arb_used_g:
                    print()
                    continue
                emp = _empirical_probability(closes, threshold_g, direction_g)
                dte_g = _get_days_to_expiry_from_market(evt, yf_ticker, sp.days_to_expiry)
                vol_p_gem = (
                    _volatility_probability(closes, threshold_g, direction_g, dte_g, use_ewma=sp.use_ewma_vol, ewma_lambda=sp.ewma_lambda)
                    if use_vol else None
                )
                if vol_p_gem is not None and use_vol:
                    hist_p_gem = vol_p_gem if _is_intraday_market(evt, yf_ticker) else (
                        (sp.vol_blend_weight * emp + (1 - sp.vol_blend_weight) * vol_p_gem) if sp.vol_blend_weight is not None
                        else max(emp, vol_p_gem)
                    )
                else:
                    hist_p_gem = emp
                # For non-standard intraday: use relaxed threshold when volatility model available
                min_p_win_gem = sp.intraday_other_min_p_win_with_vol if (use_vol and vol_p_gem is not None) else sp.intraday_other_min_p_win
                rec = (
                    _recommend_strict(hist_p_gem, impl, min_p_win_gem, min_edge, sp.gemini_maker_fee_rate)
                    if _is_other_intraday(evt, yf_ticker)
                    else _recommend(hist_p_gem, impl, min_edge, sp.gemini_maker_fee_rate)
                )
                violates_rule_g = _violates_no_above_rule(current_price, threshold_g, direction_g, sp.no_above_cushion)
                if rec == REC_BUY_NO and violates_rule_g:
                    rec = REC_SKIP
                # Favor BUY YES when price is well above strike — only if we have edge (hist_p >= market)
                if rec == REC_SKIP and violates_rule_g:
                    if 0 < impl < (1 - min_edge) and hist_p_gem >= impl + min_edge:
                        rec = REC_BUY_YES
                # Skip BUY YES when market prices it very low (e.g. 1¢); override when vol model shows ≥95% confidence
                if rec == REC_BUY_YES and impl < sp.min_yes_price:
                    if not (use_vol and vol_p_gem is not None and hist_p_gem >= 0.95):
                        rec = REC_SKIP
                # Skip BUY YES when strike is far above current (far OTM) unless vol model shows ≥95% confidence
                if rec == REC_BUY_YES and direction_g == "above" and sp.max_buy_yes_strike_above_pct > 0:
                    if threshold_g > current_price * (1 + sp.max_buy_yes_strike_above_pct):
                        if not (use_vol and vol_p_gem is not None and vol_p_gem >= 0.95):
                            rec = REC_SKIP
                # Skip BUY NO when market prices NO very low; override when vol model shows P(NO) ≥ 95%
                no_price_gem = 1.0 - impl
                if rec == REC_BUY_NO and no_price_gem < sp.min_no_price:
                    if not (use_vol and vol_p_gem is not None and (1 - hist_p_gem) >= 0.95):
                        rec = REC_SKIP
                # Skip BUY NO on "below X" when strike is far above current (BUY NO = "price > X" needs big rally)
                if rec == REC_BUY_NO and direction_g == "below" and sp.max_buy_no_strike_above_pct > 0:
                    if threshold_g > current_price * (1 + sp.max_buy_no_strike_above_pct):
                        if not (use_vol and vol_p_gem is not None and (1 - hist_p_gem) >= 0.95):
                            rec = REC_SKIP
                diff_gem = _compute_edge_diff(impl, hist_p_gem, rec, violates_rule_g, sp.gemini_maker_fee_rate)
                print(f"  [Gemini] {evt.get('ticker','?')}")
                print(f"    {title}")
                print(f"    Threshold: ${threshold_g:,.0f} ({direction_g})")
                if vol_p_gem is None:
                    p_label_gem = "P(YES)"
                else:
                    p_label_gem = f"P(YES) [emp={emp:.1%} vol={vol_p_gem:.1%}]"
                print(f"    {p_label_gem}: {hist_p_gem:.1%}  Market YES: {impl:.1%}  →  {rec}")
                try:
                    from storage import record_market_snapshot
                    record_market_snapshot(PLATFORM_GEMINI, evt.get("ticker", "?"), impl, 1.0 - impl, hist_p_gem, rec, threshold_g, direction_g, vol_p_gem, abs(diff_gem), title, yf_ticker, cfg)
                except ImportError:
                    pass
                if rec != REC_SKIP:
                    print(f"    Edge: {abs(diff_gem):.1%}")
                    no_price = 1.0 - impl
                    bid = impl if rec == REC_BUY_YES else no_price
                    p_win = hist_p_gem if rec == REC_BUY_YES else (1 - hist_p_gem)
                    dip = sp.limit_dip_pct
                    price_might_decline = (use_vol and vol_p_gem is not None) or (dte_g is not None and dte_g >= 2)
                    if price_might_decline and sp.limit_dip_pct_price_decline > 0:
                        dip = max(dip, sp.limit_dip_pct_price_decline)
                    if p_win >= 0.90 and sp.limit_dip_pct_high_confidence > 0:
                        dip = max(dip, sp.limit_dip_pct_high_confidence)
                    price = max(0.01, min(0.99, bid * (1 - dip))) if dip else bid
                    count = _compute_buy_count(
                        p_win, price, balance_cents, sp, use_kelly, kelly_fraction, min_contracts
                    )
                    order_entry = {
                        "platform": PLATFORM_GEMINI,
                        "ticker": evt.get("ticker", "?"),
                        "title": title,
                        "rec": rec,
                        "side": "yes" if rec == REC_BUY_YES else "no",
                        "price": price,
                        "edge": abs(diff_gem),
                        "count": count,
                        "hist_p": hist_p_gem,
                        "vol_p": vol_p_gem,
                        "yf_ticker": yf_ticker,
                        "threshold": threshold_g,
                        "direction": direction_g,
                        "gemini_evt": evt,
                    }
                    if instrument_symbol:
                        order_entry["instrument_symbol"] = instrument_symbol
                    pending_orders.append(order_entry)
                print()

    # Evaluate ALL positions (crypto / S&P 500) for sell when bet looks like a loser
    # Kalshi API: position_fp is signed (positive=YES, negative=NO); no "side"/"position" fields
    existing_by_underlying: dict[tuple[str, str, str], list[tuple[float, str, str]]] = {}
    if True:  # Always evaluate positions; --preview/--execute control display and order placement
        try:
            kalshi_positions: list[dict] = []
            cursor = ""
            while True:
                params = {"limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                pos_r = kalshi.get_positions(**params)
                page = pos_r.get("market_positions") or []
                kalshi_positions.extend(page)
                cursor = pos_r.get("cursor") or ""
                if not cursor:
                    break
            eval_count = len([p for p in kalshi_positions if abs(float(p.get("position_fp") or p.get("position") or 0)) > 0])
            if eval_count:
                print(f"\nEvaluating {eval_count} position(s) for keep vs sell...")
            for pos in kalshi_positions:
                ticker = pos.get("ticker")
                # position_fp: string, positive=YES, negative=NO (e.g. "3.00", "-4.00")
                fp_raw = pos.get("position_fp") or pos.get("position") or "0"
                fp_val = float(fp_raw) if fp_raw else 0.0
                side = "yes" if fp_val > 0 else "no"
                position_count = int(abs(fp_val))
                if not ticker or position_count <= 0:
                    continue
                cached = market_data_cache.get(ticker)
                if not cached:
                    # Position not in cache — fetch market and evaluate for sell (crypto / S&P 500)
                    try:
                        full = kalshi.get_market(ticker)
                        m = full.get("market", full)
                        event = full.get("event", {})
                        series = (
                            m.get("series_ticker")
                            or m.get("event_ticker")
                            or event.get("series_ticker")
                            or event.get("event_ticker")
                            or ""
                        )
                        # Fallback: infer series from ticker if API doesn't return it
                        if not series and ticker:
                            t = ticker.upper()
                            for s, yf in yf_mapping.items():
                                if s.upper() in t or (s.replace("D", "")[:5] in t if len(s) > 4 else False):
                                    series = s
                                    break
                            if not series:
                                for prefix, s in [("KXBTC", "KXBTCD"), ("KXETH", "KXETHD"), ("KXSOL", "KXSOLD"), ("KXXRP", "KXXRPD"), ("KXINX", "KXINXU")]:
                                    if prefix in t:
                                        series = s
                                        break
                        pos_yf = yf_mapping.get(series.upper()) if series else None
                        # Unsupported intraday (e.g. 7pm) → sell to exit
                        if pos_yf and _should_skip_by_resolution(m, pos_yf):
                            yes_bid = float(m.get("yes_bid_dollars") or 0) or float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)
                            no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                            sell_price = yes_bid if side == "yes" else no_bid
                            pending_sells.append({
                                "platform": PLATFORM_KALSHI,
                                "ticker": ticker,
                                "title": (m.get("title") or "")[:55],
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": sell_price,
                                "rec": "INTRADAY_MARKET",
                            })
                            continue
                        # Supported market (5pm crypto, 4pm S&P, daily close) — fetch closes and evaluate
                        if pos_yf:
                            closes = yf_get_aggs(pos_yf, days_back=args.days, config=cfg)
                            if closes:
                                threshold, direction = _extract_strike(m)
                                if threshold is not None and direction is not None:
                                    yes_bid = float(m.get("yes_bid_dollars") or 0) or float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)
                                    no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                                    emp_pos = _empirical_probability(closes, threshold, direction)
                                    hist_p = emp_pos
                                    dte_pos = _get_days_to_expiry_from_market(m, pos_yf, sp.days_to_expiry)
                                    vol_p = (
                                        _volatility_probability(closes, threshold, direction, dte_pos, use_ewma=sp.use_ewma_vol, ewma_lambda=sp.ewma_lambda)
                                        if use_vol else None
                                    )
                                    if vol_p is not None and use_vol:
                                        hist_p = vol_p if _is_intraday_market(m, pos_yf) else (
                                            (sp.vol_blend_weight * emp_pos + (1 - sp.vol_blend_weight) * vol_p) if sp.vol_blend_weight is not None
                                            else max(emp_pos, vol_p)
                                        )
                                    rec = _recommend(hist_p, yes_bid, min_edge, sp.kalshi_maker_fee_rate)
                                    current_price_pos = closes[-1]
                                    sell_price = yes_bid if side == "yes" else no_bid
                                    # SELL when definitely a loser
                                    p_lose = hist_p if side == "no" else (1.0 - hist_p)
                                    if p_lose >= sp.sell_losing_pct:
                                        pending_sells.append({
                                            "platform": PLATFORM_KALSHI,
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": sell_price,
                                            "rec": REC_LIKELY_LOSE,
                                        })
                                    elif (side == "yes" and rec == REC_BUY_NO) or (side == "no" and rec == REC_BUY_YES):
                                        pending_sells.append({
                                            "platform": PLATFORM_KALSHI,
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": sell_price,
                                            "rec": rec,
                                        })
                                    elif side == "no" and _violates_no_above_rule(current_price_pos, threshold, direction, sp.no_above_cushion):
                                        pending_sells.append({
                                            "platform": PLATFORM_KALSHI,
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": no_bid,
                                            "rec": REC_RULE_VIOLATION,
                                        })
                                    else:
                                        res_key = _extract_resolution_key_from_ticker(ticker)
                                        if res_key and pos_yf:
                                            key = (pos_yf, res_key, PLATFORM_KALSHI)
                                            existing_by_underlying.setdefault(key, []).append((threshold, direction, side))
                            elif getattr(args, "verbose", False):
                                print(f"    (no hist data for {ticker}, skip sell eval)")
                    except Exception as ex:
                        if getattr(args, "verbose", False):
                            print(f"    (could not evaluate {ticker}: {ex})")
                    continue
                _series, _yf, closes, m = cached
                threshold, direction = _extract_strike(m)
                if threshold is None or direction is None:
                    continue
                yes_bid = float(m.get("yes_bid_dollars") or 0) or float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)
                emp_cached = _empirical_probability(closes, threshold, direction)
                hist_p = emp_cached
                dte_cached = _get_days_to_expiry_from_market(m, _yf, sp.days_to_expiry)
                vol_p = (
                    _volatility_probability(closes, threshold, direction, dte_cached, use_ewma=sp.use_ewma_vol, ewma_lambda=sp.ewma_lambda)
                    if use_vol else None
                )
                if vol_p is not None and use_vol:
                    hist_p = vol_p if _is_intraday_market(m, _yf) else (
                        (sp.vol_blend_weight * emp_cached + (1 - sp.vol_blend_weight) * vol_p) if sp.vol_blend_weight is not None
                        else max(emp_cached, vol_p)
                    )
                rec = _recommend(hist_p, yes_bid, min_edge, sp.kalshi_maker_fee_rate)
                current_price_pos = closes[-1]
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                sell_price = yes_bid if side == "yes" else no_bid

                # SELL when we think position will lose: P(lose) > sell_losing_pct
                p_lose = hist_p if side == "no" else (1.0 - hist_p)
                if p_lose >= sp.sell_losing_pct:
                    pending_sells.append({
                        "platform": PLATFORM_KALSHI,
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": sell_price,
                        "rec": REC_LIKELY_LOSE,
                    })
                # SELL YES when we'd now recommend BUY NO; SELL NO when we'd recommend BUY YES
                elif (side == "yes" and rec == REC_BUY_NO) or (side == "no" and rec == REC_BUY_YES):
                    pending_sells.append({
                        "platform": PLATFORM_KALSHI,
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": sell_price,
                        "rec": rec,
                    })
                # SELL NO when holding NO on "above X" but price already > X * (1 + cushion)
                elif side == "no" and _violates_no_above_rule(current_price_pos, threshold, direction, sp.no_above_cushion):
                    pending_sells.append({
                        "platform": PLATFORM_KALSHI,
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": no_bid,
                        "rec": REC_RULE_VIOLATION,
                    })
                else:
                    res_key = _extract_resolution_key_from_ticker(ticker)
                    if res_key and _yf:
                        key = (_yf, res_key, PLATFORM_KALSHI)
                        existing_by_underlying.setdefault(key, []).append((threshold, direction, side))
        except Exception:
            pass

    trading = cfg.get("trading", {})
    max_pct = float(trading.get("max_pct_of_balance", 0.90))
    max_total_buy_pct = float(trading.get("max_total_buy_pct", 0.50))
    min_balance_fallback = int(trading.get("min_balance_for_limit_fallback_cents", 0))
    dry_run = trading.get("dry_run", True)

    # Skip BUYs that contradict kept positions or other pending BUYs (e.g. NO on SOL>$87 + YES on SOL>$93).
    # Per-platform only: cross-platform bets are allowed (e.g. NO Kalshi + YES Gemini on same underlying).
    _filter_contradictory_buys(pending_orders, existing_by_underlying)

    # Budget-aware allocation per platform (Kalshi and Gemini have separate balances)
    kalshi_orders = [o for o in pending_orders if o.get("platform") == PLATFORM_KALSHI]
    gemini_orders = [o for o in pending_orders if o.get("platform") == PLATFORM_GEMINI]
    _allocate_budget_to_orders(kalshi_orders, balance_cents, max_total_buy_pct)
    gemini_balance_cents = 0
    if gemini_orders:
        try:
            _gt = GeminiTradingClient()
            gemini_balance_cents = int(_gt.get_balance().get("balance", 0) or 0)
        except Exception:
            pass
    _allocate_budget_to_orders(gemini_orders, gemini_balance_cents, max_total_buy_pct)
    pending_orders[:] = [o for o in kalshi_orders + gemini_orders if o.get("count", 0) > 0]

    # Proposed trades + per-trade Ollama: print each trade and Ollama's interpretation
    if pending_orders or pending_sells:
        print("\n" + "=" * 75)
        print("PROPOSED TRADES")
        print("=" * 75)
        use_llm = getattr(args, "use_llm", True)
        for i, s in enumerate(pending_sells, 1):
            print(f"\n  SELL {i}. [{s['platform'].upper()}] SELL {s['side'].upper()} {s['ticker']} | {s.get('title', '')[:55]}")
            print(f"   Count: {s['count']} @ ${s['price']:.2f} ({s.get('rec', '')})")
            if use_llm:
                analysis = analyze_single_trade(s, "sell")
                if analysis:
                    print(f"   [Ollama] {analysis}")
        for i, o in enumerate(pending_orders, 1):
            print(f"\n  BUY {i}. [{o['platform'].upper()}] {o['rec']} {o['ticker']} | {(o.get('title') or '')[:55]}")
            print(f"   Edge: {o['edge']:.1%} | Price: ${o['price']:.2f} | Count: {o.get('count', 10)}")
            if use_llm:
                analysis = analyze_single_trade(o, "buy")
                if analysis:
                    print(f"   [Ollama] {analysis}")
        print()

    if getattr(args, "execute", False):
        if dry_run:
            print("\n" + "=" * 75)
            print("ERROR: --execute requires dry_run: false in config.yaml (trading section)")
            print("Set trading.dry_run: false for live order placement.")
            print("=" * 75)
            return 1
        print("\n" + "=" * 75)
        print("EXECUTE: Placing real orders (live run)")
        print("=" * 75)
        buys_placed, sells_placed = 0, 0
        gemini_trading = None
        if any(o["platform"] == PLATFORM_GEMINI for o in pending_orders) or any(s["platform"] == PLATFORM_GEMINI for s in pending_sells):
            try:
                gemini_trading = GeminiTradingClient()
            except ValueError:
                print("  (Gemini: GEMINI_API_KEY/GEMINI_API_SECRET not set, skipping Gemini orders)")

        # Build pending lookups for order review: (ticker, side) or (symbol, outcome) → {price, count, ...}
        kalshi_pending_buys = {(o["ticker"], o["side"]): o for o in pending_orders if o.get("platform") == PLATFORM_KALSHI}
        kalshi_pending_sells = {(s["ticker"], s["side"]): s for s in pending_sells if s.get("platform") == PLATFORM_KALSHI}
        gemini_pending_buys: dict[tuple[str, str], dict] = {}
        gemini_pending_sells: dict[tuple[str, str], dict] = {}
        for o in pending_orders:
            if o.get("platform") == PLATFORM_GEMINI:
                sym = o.get("instrument_symbol")
                if not sym:
                    evt = o.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, o.get("ticker", ""))
                    sym = get_instrument_symbol(evt, o["side"]) if evt else None
                if sym:
                    gemini_pending_buys[(str(sym), o["side"])] = o
        for s in pending_sells:
            if s.get("platform") == PLATFORM_GEMINI:
                evt = s.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, s.get("ticker", ""))
                sym = get_instrument_symbol(evt, s["side"]) if evt else None
                if sym:
                    gemini_pending_sells[(str(sym), s["side"])] = s

        # Fetch open orders (buy + sell) and decide: cancel (no longer want / need price update) or keep
        kalshi_keep_buys: set[tuple[str, str]] = set()
        kalshi_keep_sells: set[tuple[str, str]] = set()
        gemini_keep_buys: set[tuple[str, str]] = set()
        gemini_keep_sells: set[tuple[str, str]] = set()
        to_cancel: list[tuple[str, str, str]] = []  # (platform, order_id, label)

        try:
            cursor = ""
            while True:
                r = kalshi.get_orders(status="resting", limit=200, **{"cursor": cursor} if cursor else {})
                for ord_ in r.get("orders") or []:
                    oid = ord_.get("order_id")
                    t = ord_.get("ticker")
                    side = (ord_.get("side") or "").lower()
                    action = (ord_.get("action") or "").lower()
                    if not oid or not t or not side:
                        continue
                    yes_d = ord_.get("yes_price_dollars") or "0"
                    no_d = ord_.get("no_price_dollars") or "0"
                    old_price = float(yes_d) if side == "yes" else float(no_d)
                    label = f"{t} {action} {side}"
                    if action == "buy":
                        pend = kalshi_pending_buys.get((t, side))
                        if not pend:
                            to_cancel.append((PLATFORM_KALSHI, oid, label))
                        elif pend["price"] > old_price:
                            to_cancel.append((PLATFORM_KALSHI, oid, label))
                        else:
                            kalshi_keep_buys.add((t, side))
                    else:
                        pend = kalshi_pending_sells.get((t, side))
                        if not pend:
                            to_cancel.append((PLATFORM_KALSHI, oid, label))
                        elif pend["price"] > old_price:
                            to_cancel.append((PLATFORM_KALSHI, oid, label))
                        else:
                            kalshi_keep_sells.add((t, side))
                cursor = r.get("cursor") or ""
                if not cursor:
                    break
        except Exception:
            pass

        if gemini_trading:
            try:
                act = gemini_trading.get_active_orders(limit=200)
                for ord_ in act.get("orders") or []:
                    oid = ord_.get("orderId") or ord_.get("order_id")
                    sym = ord_.get("symbol") or ord_.get("instrumentSymbol")
                    out = (ord_.get("outcome") or ord_.get("label") or "").lower()
                    side = (ord_.get("side") or "").lower()
                    if not oid or not sym or out not in ("yes", "no"):
                        continue
                    old_price = float(ord_.get("price") or ord_.get("originalAmount") or 0)
                    key = (str(sym), out)
                    label = f"{sym} {side} {out}"
                    if side == "buy":
                        pend = gemini_pending_buys.get(key)
                        if not pend:
                            to_cancel.append((PLATFORM_GEMINI, str(oid), label))
                        elif pend["price"] > old_price:
                            to_cancel.append((PLATFORM_GEMINI, str(oid), label))
                        else:
                            gemini_keep_buys.add(key)
                    else:
                        pend = gemini_pending_sells.get(key)
                        if not pend:
                            to_cancel.append((PLATFORM_GEMINI, str(oid), label))
                        elif pend["price"] > old_price:
                            to_cancel.append((PLATFORM_GEMINI, str(oid), label))
                        else:
                            gemini_keep_sells.add(key)
            except Exception:
                pass

        # Execute cancels
        cancels_done = 0
        for platform, order_id, label in to_cancel:
            if platform == PLATFORM_KALSHI:
                if kalshi.cancel_order_safe(order_id, ticker=label):
                    print(f"  Cancelled (Kalshi) {label} — updating or no longer recommended")
                    cancels_done += 1
            elif platform == PLATFORM_GEMINI and gemini_trading:
                if gemini_trading.cancel_order_safe(order_id, ticker_label=label):
                    print(f"  Cancelled (Gemini) {label} — updating or no longer recommended")
                    cancels_done += 1
        if cancels_done:
            print()

        # SELL first so balance reflects proceeds before we place buys
        for s in pending_sells:
            if s["platform"] == PLATFORM_KALSHI:
                if (s["ticker"], s["side"]) in kalshi_keep_sells:
                    print(f"  (skip SELL {s['ticker']} — already have open limit order at same/better price)")
                    continue
                cached = market_data_cache.get(s["ticker"])
                price_ranges = cached[3].get("price_ranges") if cached and len(cached) > 3 else None
                price_str = f"{s['price']:.4f}"  # round_price_to_tick in create_order_safe
                try:
                    result = kalshi.create_order_safe(
                        ticker=s["ticker"],
                        side=s["side"],
                        action="sell",
                        count=s["count"],
                        yes_price_dollars=price_str if s["side"] == "yes" else None,
                        no_price_dollars=price_str if s["side"] == "no" else None,
                        max_pct_of_balance=max_pct,
                        price_ranges=price_ranges,
                    )
                    if result:
                        print(f"  Placed SELL {s['side'].upper()} {s['ticker']} x {s['count']}")
                        sells_placed += 1
                    else:
                        print(f"  (skipped SELL {s['ticker']})")
                except Exception as ex:
                    print(f"  SELL {s['ticker']} failed: {ex}")
            elif s["platform"] == PLATFORM_GEMINI and gemini_trading:
                sym = s.get("instrument_symbol")
                if not sym:
                    gemini_evt = s.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, s.get("ticker", ""))
                    sym = get_instrument_symbol(gemini_evt, s["side"]) if gemini_evt else None
                if sym and (sym, s["side"]) in gemini_keep_sells:
                    print(f"  (skip SELL (Gemini) {s['ticker']} — already have open limit order at same/better price)")
                elif sym:
                    result = gemini_trading.place_order_safe(
                        symbol=sym, side="sell", outcome=s["side"], quantity=s["count"],
                        price=s["price"], ticker_label=s["ticker"],
                    )
                    if result:
                        print(f"  Placed SELL {s['side'].upper()} (Gemini) {s['ticker']} x {s['count']}")
                        sells_placed += 1
                    else:
                        print(f"  (skipped SELL (Gemini) {s['ticker']} - balance/API constraint)")

        # Then BUY — balance now reflects sell proceeds
        for o in pending_orders:
            if o["platform"] == PLATFORM_KALSHI:
                if (o["ticker"], o["side"]) in kalshi_keep_buys:
                    print(f"  (skip BUY {o['ticker']} — already have open limit order)")
                    continue
                cached = market_data_cache.get(o["ticker"])
                price_ranges = cached[3].get("price_ranges") if cached and len(cached) > 3 else None
                price_str = f"{o['price']:.4f}"  # round_price_to_tick in create_order_safe
                count = o.get("count", 10)
                try:
                    result = kalshi.create_order_safe(
                        ticker=o["ticker"],
                        side=o["side"],
                        action="buy",
                        count=count,
                        yes_price_dollars=price_str if o["side"] == "yes" else None,
                        no_price_dollars=price_str if o["side"] == "no" else None,
                        max_pct_of_balance=max_pct,
                        min_balance_for_limit_fallback_cents=min_balance_fallback,
                        price_ranges=price_ranges,
                    )
                    if result:
                        print(f"  Placed BUY {o['side'].upper()} {o['ticker']} x {count}")
                        buys_placed += 1
                        if record_prediction and o.get("rec") in (REC_BUY_YES, REC_BUY_NO) and o.get("hist_p") is not None:
                            ord_obj = result.get("order", result)
                            oid = ord_obj.get("order_id") if isinstance(ord_obj, dict) else None
                            record_prediction(
                                platform=PLATFORM_KALSHI,
                                ticker=o["ticker"],
                                side=o["side"],
                                hist_p=o["hist_p"],
                                entry_price=o["price"],
                                count=count,
                                order_id=oid,
                                vol_p=o.get("vol_p"),
                                yf_ticker=o.get("yf_ticker"),
                                threshold=o.get("threshold"),
                                direction=o.get("direction"),
                                rec=o.get("rec"),
                                config=cfg,
                            )
                    else:
                        print(f"  (skipped BUY {o['ticker']} - balance/API constraint)")
                except Exception as ex:
                    print(f"  BUY {o['ticker']} failed: {ex}")
            else:
                count = o.get("count", 10)
                sym = o.get("instrument_symbol")
                if not sym:
                    gemini_evt = o.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, o["ticker"])
                    sym = get_instrument_symbol(gemini_evt, o["side"]) if gemini_evt else None
                if not sym:
                    print(f"  (skip BUY Gemini {o['ticker']} — no instrumentSymbol for {o['side']}; check contract structure)")
                    continue
                if (sym, o["side"]) in gemini_keep_buys:
                    print(f"  (skip BUY {o['ticker']} — already have open limit order)")
                    continue
                if gemini_trading:
                    result = gemini_trading.place_order_safe(
                        symbol=sym, side="buy", outcome=o["side"], quantity=count, price=o["price"],
                        ticker_label=o["ticker"],
                        max_pct_of_balance=max_pct,
                    )
                    if result:
                        print(f"  Placed BUY {o['side'].upper()} (Gemini) {o['ticker']} x {count}")
                        buys_placed += 1
                        if record_prediction and o.get("rec") in (REC_BUY_YES, REC_BUY_NO) and o.get("hist_p") is not None:
                            oid = str(result.get("orderId") or result.get("order_id", "")) if isinstance(result, dict) else None
                            record_prediction(
                                platform=PLATFORM_GEMINI,
                                ticker=o["ticker"],
                                side=o["side"],
                                hist_p=o["hist_p"],
                                entry_price=o["price"],
                                count=count,
                                order_id=oid,
                                vol_p=o.get("vol_p"),
                                yf_ticker=o.get("yf_ticker"),
                                threshold=o.get("threshold"),
                                direction=o.get("direction"),
                                rec=o.get("rec"),
                                config=cfg,
                            )
                    else:
                        print(f"  (skipped BUY (Gemini) {o['ticker']} - balance/API constraint)")
                elif sym and not gemini_trading:
                    print(f"  (Gemini {o['ticker']}: GEMINI_API_KEY/SECRET not set - skip)")

        print("\n" + "=" * 75)
        print(f"Live run complete. {buys_placed} buys, {sells_placed} sells placed.")
        print("=" * 75)
    elif getattr(args, "preview", False) and (pending_orders or pending_sells):
        print("\n" + "=" * 75)
        print("PREVIEW: Orders we would place (--preview mode, no actual orders)")
        print("  SELLs first, then BUYs (so balance reflects proceeds before buys)")
        print("=" * 75)

        for i, s in enumerate(pending_sells, 1):
            print(f"\n  SELL {i}. [{s['platform'].upper()}] SELL {s['side'].upper()} {s['ticker']}")
            print(f"   {s['title']}")
            print(f"   Count: {s['count']} @ {s['price']:.2f} (edge flipped → now {s['rec']})")
            if s["platform"] == PLATFORM_KALSHI:
                payload = {
                    "ticker": s["ticker"],
                    "side": s["side"],
                    "action": "sell",
                    "count": s["count"],
                }
                payload["yes_price_dollars" if s["side"] == "yes" else "no_price_dollars"] = f"{s['price']:.2f}"
                print(f"   API: POST /portfolio/orders")
                print(f"   Payload: {payload}")
            elif s["platform"] == PLATFORM_GEMINI:
                print(f"   API: POST /v1/prediction-markets/order (place_order, side=sell)")

        for i, o in enumerate(pending_orders, 1):
            print(f"\n{i}. [{o['platform'].upper()}] {o['rec']} {o['ticker']}")
            print(f"   {o['title']}")
            print(f"   Edge: {o['edge']:.1%}")
            if o["platform"] == PLATFORM_KALSHI:
                side, action = o["side"], "buy"
                price_str = f"{o['price']:.2f}"
                count = o.get("count", 10)
                payload = {"ticker": o["ticker"], "side": side, "action": action, "count": count}
                if side == "yes":
                    payload["yes_price_dollars"] = price_str
                else:
                    payload["no_price_dollars"] = price_str
                print(f"   API: POST /portfolio/orders")
                print(f"   Payload: {payload}")
                print(f"   (count capped by balance * {max_pct:.0%} via create_order_safe)")
            else:
                count = o.get("count", 10)
                sym = o.get("instrument_symbol") or (
                    get_instrument_symbol(
                        o.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, o["ticker"]),
                        o["side"],
                    )
                    if o.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, o["ticker"])
                    else None
                )
                if sym:
                    print(f"   API: POST /v1/prediction-markets/order (place_order)")
                    print(f"   Payload: symbol={sym}, side=buy, outcome={o['side']}, quantity={count}, price={o['price']:.2f}")
                else:
                    print(f"   Would: {o['rec']} at {o['price']:.2f} (side={o['side']}, count={count})")

        print("\n" + "=" * 75)

    print("\n" + "=" * 75)
    if pending_sells and not (getattr(args, "preview", False) or getattr(args, "execute", False)):
        print(f"  Note: {len(pending_sells)} position(s) look like losers. Run with --preview to see details, --execute to sell.")
    if getattr(args, "execute", False):
        print("Done. Run with --execute for future live runs (or set up cron).")
    else:
        print("Done. Run every 15 min (cron) for continuous evaluation. See README.")
        if not pending_sells:
            print("  Use --preview to see positions we would SELL (losers / rule violations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
