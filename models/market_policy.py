"""
Market Policy and Asset Classification.

Defines supported underlyings (Crypto, Indices, Commodities, Weather)
and provides classification logic for intraday resolution rules.
"""
from __future__ import annotations

import re
from typing import Any

from constants import PLATFORM_KALSHI, PLATFORM_GEMINI, REC_ARB_INTER
from models.strategy_config import StrategyParams
from utils.market_time import (
    get_title, get_resolution_hour_from_title,
    hours_until_resolution_unified, minutes_until_resolution_unified,
)

# Yahoo underlyings evaluate cross-platform (crypto + WTI + Brent on Gemini commodities).
GEMINI_CRYPTO_YF = {
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",
    "DOGE-USD",
    "BNB-USD",
    "ZEC-USD",
    "HYPE-USD",
    "HYPE32196-USD",  # Yahoo symbol for Hyperliquid; Gemini buckets still use HYPE-USD from _event_to_asset
    "CL=F",  # WTI — Gemini KK_RFR_WTIUSDC / WTIOIL-USDC; Kalshi KXWTI uses same Yahoo continuous future
    "BZ=F",  # Brent — Gemini KK_RFR_BRENTOILUSDC; Yahoo ICE continuous — https://finance.yahoo.com/quote/BZ=F/
    "WEATHER:NYC", "WEATHER:CHI", "WEATHER:BOS", "WEATHER:MIA", "WEATHER:LAX",
    "WEATHER:AUS", "WEATHER:DAL", "WEATHER:PHL", "WEATHER:PHX", "WEATHER:DEN",
    "WEATHER:LAS", "WEATHER:SEA", "WEATHER:SFO", "WEATHER:HOU", "WEATHER:DC",
    "WEATHER:ATL", "WEATHER:MSP", "WEATHER:MSY", "WEATHER:OKC", "WEATHER:SAT",
}

# When Yahoo ticker ≠ Gemini _event_to_asset key, map here for inter-arb / sanity / Gemini loop lookups.
GEMINI_ASSET_KEY_FOR_YFIN_TICKER: dict[str, str] = {
    "HYPE32196-USD": "HYPE-USD",
}

# US equity indices: same 4pm ET “regular close” intraday rules as ^GSPC (e.g. Kalshi Nasdaq-100).
_US_EQUITY_INDEX_YF_TICKERS = frozenset({"^GSPC", "GSPC", "^NDX", "NDX"})
# Yahoo continuous futures for Kalshi WTI / Brent / metals / RBOB (gas proxy) — allow titled intraday times.
_COMMODITY_FUTURES_YF_TICKERS = frozenset({"CL=F", "BZ=F", "GC=F", "SI=F", "HG=F", "RB=F"})
# Kalshi maker fee override (index-like); match _maker_fee_rate_for_market.
_KALSHI_INDEX_MAKER_FEE_YF = frozenset({"^GSPC", "GSPC", "^NDX", "NDX", "SPY", "QQQ"})


def yf_is_us_equity_index(yf_ticker: str) -> bool:
    return (yf_ticker or "").strip().upper() in _US_EQUITY_INDEX_YF_TICKERS


def yf_is_commodity_future(yf_ticker: str) -> bool:
    return (yf_ticker or "").strip().upper() in _COMMODITY_FUTURES_YF_TICKERS


def gemini_events_list_for_yf_ticker(events_by_asset: dict[str, list], yf_ticker: str) -> list:
    key = GEMINI_ASSET_KEY_FOR_YFIN_TICKER.get(yf_ticker, yf_ticker)
    return events_by_asset.get(key, [])


def is_intraday_market(market_or_title: dict | str, yf_ticker: str) -> bool:
    """Whether market resolves at a specific intraday hour (4pm for index, custom for commodities)."""
    title = get_title(market_or_title)
    if yf_is_us_equity_index(yf_ticker):
        return "@ 4pm" in title or "at 4pm" in title or "at 3:59pm" in title
    if yf_is_commodity_future(yf_ticker):
        h = get_resolution_hour_from_title(title)
        return h is not None
    return False


def is_other_intraday(market_or_title: dict | str, yf_ticker: str) -> bool:
    """True if intraday but NOT standard 4pm Index / 5pm Crypto."""
    title = get_title(market_or_title)
    h = get_resolution_hour_from_title(title)
    if h is None:
        return False
    is_idx = yf_is_us_equity_index(yf_ticker)
    is_crypto = yf_ticker in GEMINI_CRYPTO_YF
    is_cfu = yf_is_commodity_future(yf_ticker)
    if is_idx and h == 16:
        return False
    if is_crypto and h == 17:
        return False
    if is_cfu and h in (16, 17):
        return False
    return is_idx or is_crypto or is_cfu


def is_ultra_short_horizon(market_or_title: dict | str, yf_ticker: str, ultra_short_dte_hours: float) -> bool:
    """True if market resolves within ultra_short_dte_hours (e.g. 4.1h)."""
    h = hours_until_resolution_unified(market_or_title, yf_is_us_equity_index(yf_ticker))
    if h is None:
        return False
    return 0 < h < ultra_short_dte_hours


def strict_intraday_evaluation(market_or_title: dict | str, yf_ticker: str, ultra_short_dte_hours: float) -> bool:
    """Use strict thresholds for non-standard or ultra-short horizons."""
    if is_other_intraday(market_or_title, yf_ticker):
        return True
    return is_ultra_short_horizon(market_or_title, yf_ticker, ultra_short_dte_hours)


def is_intraday_or_ultra_short(market_or_title: dict | str, yf_ticker: str, ultra_short_dte_hours: float) -> bool:
    """Clock intraday OR ultra-short."""
    return is_intraday_market(market_or_title, yf_ticker) or is_ultra_short_horizon(market_or_title, yf_ticker, ultra_short_dte_hours)


def should_skip_by_resolution(
    market_or_title: dict | str,
    yf_ticker: str,
    min_hours: float = 0.05,
    max_hours: float = 500.0,
) -> tuple[bool, str]:
    """Check if market should be skipped due to near/far resolution."""
    is_idx = yf_is_us_equity_index(yf_ticker)
    h = hours_until_resolution_unified(market_or_title, is_idx)
    if h is None:
        return False, ""
    if h < min_hours:
        return True, f"SKIP (too close to resolution: {h:.2f}h < {min_hours}h)"
    if h > max_hours:
        return True, f"SKIP (too far from resolution: {h:.1f}h > {max_hours}h)"
    return False, ""


def extract_strike(market: dict) -> tuple[float | None, str | None, float | None]:
    """
    Extract threshold, direction, and optional high-threshold for ranges from Kalshi market.
    Returns (threshold, direction, threshold_hi) - direction: 'above'|'below'|'between'|None.
    """
    m = market.get("market", market)
    strike_type = (m.get("strike_type") or "").lower()
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    threshold, direction, threshold_hi = None, None, None

    if strike_type == "between" and floor is not None and cap is not None:
        threshold, direction, threshold_hi = float(floor), "between", float(cap)
    elif floor is not None and strike_type in ("greater", "greater_or_equal"):
        threshold, direction = float(floor), "above"
    elif cap is not None and strike_type in ("less", "less_or_equal"):
        threshold, direction = float(cap), "below"
    elif floor is not None:
        threshold, direction = float(floor), "above"
    elif cap is not None:
        threshold, direction = float(cap), "below"

    if threshold is None:
        title = (m.get("title") or m.get("yes_sub_title") or "") + " " + (m.get("no_sub_title") or "")
        # Range/Bracket: "61-62°" or "between 50 and 60"
        range_match = re.search(r"([\d.,]+)\s*-\s*([\d.,]+)°", title)
        if not range_match:
            range_match = re.search(r"between\s*([\d.,]+)\s*and\s*([\d.,]+)", title, re.I)
        if range_match:
            threshold = float(range_match.group(1).replace(",", ""))
            threshold_hi = float(range_match.group(2).replace(",", ""))
            direction = "between"
        else:
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

    # Fallback: parse strike from Kalshi ticker
    ticker = m.get("ticker") or ""
    if threshold is None:
        # Bracket: -B61.5 -> [61, 62] (center +/- 0.5)
        b_match = re.search(r"-B([\d.]+)$", ticker)
        if b_match:
            center = float(b_match.group(1))
            threshold = center - 0.5
            threshold_hi = center + 0.5
            direction = "between"
        else:
            t_match = re.search(r"-T([\d.]+)$", ticker)
            if t_match:
                threshold = float(t_match.group(1))
                direction = "above"

    return threshold, direction, threshold_hi


def maker_fee_rate_for_market(sp: StrategyParams, platform: str, yf_ticker: str | None) -> float:
    """Platform-specific maker fee with index override support."""
    if platform == PLATFORM_KALSHI and (yf_ticker or "").strip().upper() in _KALSHI_INDEX_MAKER_FEE_YF:
        return max(0.0, float(sp.kalshi_sp500_maker_fee_rate))
    if platform == PLATFORM_GEMINI:
        return max(0.0, float(sp.gemini_maker_fee_rate))
    return max(0.0, float(sp.kalshi_maker_fee_rate))


def apply_inter_arb_group_preflight(
    pending_orders: list[dict],
    *,
    sp: StrategyParams,
    kalshi_balance_cents: int,
    gemini_balance_cents: int,
    max_pct_of_balance: float,
    gemini_ready: bool,
    logger: Any = print,
) -> None:
    """
    Ensure REC_ARB_INTER orders execute as balanced pairs.
    """
    if not pending_orders or not sp.enable_inter_arb or not sp.arb_inter_require_group_preflight:
        return

    groups: dict[str, list[dict]] = {}
    for o in pending_orders:
        if o.get("rec") != REC_ARB_INTER:
            continue
        gid = str(o.get("arb_group") or "")
        if gid:
            groups.setdefault(gid, []).append(o)
    if not groups:
        return

    dropped_groups: set[str] = set()
    max_pct = max(0.0, float(max_pct_of_balance))

    def _leg_max_count(leg: dict) -> int:
        price_c = int(max(1, round(float(leg.get("price", 0.0)) * 100)))
        if price_c <= 0:
            return 0
        platform = str(leg.get("platform") or "")
        bal = gemini_balance_cents if platform == PLATFORM_GEMINI else kalshi_balance_cents
        spend = int(max(0, int(bal)) * max_pct)
        return max(0, spend // price_c)

    for gid, legs in groups.items():
        if not gemini_ready and any(l.get("platform") == PLATFORM_GEMINI for l in legs):
             dropped_groups.add(gid)
             continue
        if len(legs) < 2:
            dropped_groups.add(gid)
            continue
        # Ensure parity: all legs must have same count
        counts = [int(l.get("count", 0)) for l in legs]
        min_c = min(counts)
        # Also cap by what each venue can afford
        for leg in legs:
            min_c = min(min_c, _leg_max_count(leg))

        if min_c <= 0:
            dropped_groups.add(gid)
            continue
        for leg in legs:
            leg["count"] = min_c

    if dropped_groups:
        pending_orders[:] = [o for o in pending_orders if str(o.get("arb_group")) not in dropped_groups]


def find_gemini_event(gemini_events_by_asset: dict, ticker: str) -> dict:
    """Find Gemini event by ticker across all assets. Returns {} if not found."""
    for events in gemini_events_by_asset.values():
        for evt in events:
            if evt.get("ticker") == ticker:
                return evt
    return {}
