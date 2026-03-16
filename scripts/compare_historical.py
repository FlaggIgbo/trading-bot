#!/usr/bin/env python3
"""
Evaluate Kalshi & Gemini prediction contracts using historical price data.

Goal: Use historical data to estimate P(price above/below threshold at resolution time),
      then compare to market price → recommend BUY YES / BUY NO / SKIP.

Example: "Bitcoin above $81,250 on Mar 16?" → historical says 5% of days closed above
         → if market YES = 40¢, that's overpriced (SKIP or BUY NO).

Designed to run every 15 minutes locally for continuous evaluation.
See README for cron setup.

Run: python3 scripts/compare_historical.py [--days 30] [--markets 0] [--edge 0.15]
"""
from __future__ import annotations

# TODO: Add cron/scheduler integration for 15-min runs (e.g. APScheduler, or document crontab)
# TODO: Parse resolution timestamp (e.g. "5pm EDT") for time-of-day–aware probability
# TODO: Support intraday data for "price today at 5pm" contracts (currently uses daily close)
# TODO: Integrate with Kalshi/Gemini order APIs for auto-execution when edge > threshold
# TODO: Add logging to file for historical run analysis
# TODO: GARCH or EWMA for time-varying volatility
# TODO: Implied volatility from options market (if available) for richer probability
# TODO: Parse exact resolution date per contract for accurate time-to-expiry

import argparse
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path so imports work when run from scripts/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from config import get_config
from gemini_client import (
    GeminiPredictionClient,
    GeminiTradingClient,
    _event_to_asset,
    _get_yes_contract,
    extract_strike_from_event,
    get_instrument_symbol,
    get_yes_price,
)
from kalshi_client import KalshiClient
from ollama_analysis import capture_and_analyze
from yfinance_client import get_aggs as yf_get_aggs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Gemini supports crypto only (no S&P 500). Used for event filtering.
GEMINI_CRYPTO_YF = {"BTC-USD", "ETH-USD", "SOL-USD"}


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


def _volatility_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    days_to_expiry: float = 30.0,
) -> float | None:
    """
    Log-normal / Black-Scholes style: P(price above/below K at expiry) using historical vol.
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


def _recommend(historical_p_yes: float, market_yes_price: float, min_edge: float) -> str:
    """
    Recommend BUY YES / BUY NO / SKIP based on historical vs market.
    Buy YES when historical says P(YES) > market_price + edge (market underpriced).
    Buy NO when historical says P(NO) > (1 - market_yes_price) + edge.
    """
    if market_yes_price <= 0:
        return "SKIP"  # No liquidity
    if historical_p_yes >= market_yes_price + min_edge:
        return "BUY YES"  # Market underpricing YES
    if (1 - historical_p_yes) >= (1 - market_yes_price) + min_edge:
        return "BUY NO"  # Market underpricing NO
    return "SKIP"


def _recommend_strict(
    historical_p_yes: float, market_yes_price: float, min_p_win: float, min_edge: float = 0.02
) -> str:
    """
    For other intraday (e.g. 8pm ET): only BUY when ALMOST ABSOLUTELY CERTAIN we win.
    Requires P(win) >= min_p_win (e.g. 0.90) AND small edge.
    """
    if market_yes_price <= 0:
        return "SKIP"
    # BUY YES only when hist_p >= 90% AND market underpriced
    if historical_p_yes >= min_p_win and historical_p_yes >= market_yes_price + min_edge:
        return "BUY YES"
    # BUY NO only when P(NO) >= 90% AND market underpricing NO
    p_no = 1 - historical_p_yes
    if p_no >= min_p_win and p_no >= (1 - market_yes_price) + min_edge:
        return "BUY NO"
    return "SKIP"


# ---------------------------------------------------------------------------
# Position sizing (Kelly criterion)
# ---------------------------------------------------------------------------
def _compute_edge_diff(
    market_yes: float,
    hist_p_yes: float,
    rec: str,
    violates_no_above: bool,
) -> float:
    """
    Edge = market_yes - fair_price. When we favor BUY YES (price >> strike),
    fair price is ~1.0. Otherwise use historical P(YES).
    """
    if rec == "BUY YES" and violates_no_above:
        fair_price = 1.0
    else:
        fair_price = hist_p_yes
    return market_yes - fair_price


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


def _find_gemini_event(gemini_events_by_asset: dict, ticker: str) -> dict:
    """Find Gemini event by ticker across all assets. Returns {} if not found."""
    for events in gemini_events_by_asset.values():
        for evt in events:
            if evt.get("ticker") == ticker:
                return evt
    return {}


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


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate Kalshi/Gemini contracts using historical data → BUY YES / BUY NO / SKIP"
    )
    parser.add_argument("--days", type=int, default=30, help="Days of historical data")
    parser.add_argument("--markets", type=int, default=0, help="Max markets per series (0 = all)")
    parser.add_argument("--edge", type=float, default=0.15, help="Min edge (0-1) to recommend buy")
    parser.add_argument("--no-llm", action="store_true", help="Skip Ollama analysis")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show closes range vs threshold (explains Historical P(YES))")
    parser.add_argument("--preview", action="store_true", help="Show API calls we would make (no orders placed)")
    parser.add_argument("--execute", action="store_true", help="Place real buy/sell orders (requires dry_run: false in config)")
    parser.add_argument("--use-volatility", action="store_true", help="Use volatility-based probability (option-style) in addition to empirical")
    parser.add_argument("--log", metavar="DIR", nargs="?", const="~/Desktop/trading-bot-logs", default=None, help="Log run output to ~/Desktop/trading-bot-logs/ or specified dir (creates eval_YYYYMMDD.log)")
    args = parser.parse_args()

    return capture_and_analyze(lambda: _run(args), "scripts/compare_historical.py", use_llm=not args.no_llm)


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

    # TODO: Fetch Gemini events in parallel with Kalshi for speed
    gemini = GeminiPredictionClient()
    gemini_events_by_asset: dict[str, list] = {}
    try:
        r = gemini.get_events(category="crypto", status="active", limit=200)
        for evt in r.get("data") or []:
            asset = _event_to_asset(evt)
            if asset:
                gemini_events_by_asset.setdefault(asset, []).append(evt)
    except Exception:
        pass

    pending_orders: list[dict] = []
    pending_sells: list[dict] = []
    days_to_expiry = float(strategy.get("days_to_expiry", 30))
    no_above_cushion = float(strategy.get("skip_buy_no_above_cushion", 0.01))
    sell_losing_pct = float(strategy.get("sell_losing_pct", 0.55))
    limit_dip_pct = float(strategy.get("limit_dip_pct", 0))
    intraday_other_min_p_win = float(strategy.get("intraday_other_min_p_win", 0.90))

    # Build ticker -> (series, yf_ticker, closes, market_data) for sell evaluation
    market_data_cache: dict[str, tuple[str, str, list[float], dict]] = {}

    use_vol = getattr(args, "use_volatility", False)
    for series in markets_list:
        series = series.strip()
        yf_ticker = yf_mapping.get(series.upper())
        if not yf_ticker:
            print(f"[{series}] No yfinance_tickers mapping - skip")
            continue

        closes = yf_get_aggs(yf_ticker, days_back=args.days)
        if not closes:
            print(f"[{series}] No close data for {yf_ticker}")
            continue

        current_price = closes[-1]
        print(f"\n[{series}] → {yf_ticker} | ${current_price:,.2f} | {len(closes)} days")

        try:
            r = kalshi.get_markets(series_ticker=series.upper(), status="open")
            mkt_list = r.get("markets", [])
            if args.markets > 0:
                mkt_list = mkt_list[: args.markets]
        except Exception as e:
            print(f"  Kalshi error: {e}")
            mkt_list = []

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
                # TODO: Handle "price at 5pm" contracts - use intraday data or volatility model
                print(f"  {ticker}: YES {yes_bid:.2f} | (no threshold) {title}")
                continue

            market_data_cache[ticker] = (series, yf_ticker, closes, m)

            hist_p = _empirical_probability(closes, threshold, direction)
            vol_p = _volatility_probability(closes, threshold, direction, days_to_expiry) if getattr(args, "use_volatility", False) else None
            if vol_p is not None and getattr(args, "use_volatility", False):
                hist_p = max(hist_p, vol_p)
            rec = (
                _recommend_strict(hist_p, yes_bid, intraday_other_min_p_win)
                if _is_other_intraday(m, yf_ticker)
                else _recommend(hist_p, yes_bid, min_edge)
            )
            # Skip BUY NO when price already above strike (risky to bet NO)
            violates_rule = _violates_no_above_rule(current_price, threshold, direction, no_above_cushion)
            if rec == "BUY NO" and violates_rule:
                rec = "SKIP"
            # Favor BUY YES when price is well above strike (e.g. ETH $2500, strike $1900 = safe YES)
            if rec == "SKIP" and violates_rule:
                if 0 < yes_bid < (1 - min_edge):  # Market hasn't priced it at 99%; room for edge
                    rec = "BUY YES"
            diff = _compute_edge_diff(yes_bid, hist_p, rec, violates_rule)

            print(f"  {ticker}")
            print(f"    {title}")
            print(f"    Threshold: ${threshold:,.0f} ({direction})")
            if vol_p is None:
                p_label = "P(YES)"
            else:
                emp_kalshi = _empirical_probability(closes, threshold, direction)
                p_label = f"P(YES) [emp={emp_kalshi:.1%} vol={vol_p:.1%}]"
            print(f"    {p_label}: {hist_p:.1%}  Market YES: {yes_bid:.1%}  →  {rec}")
            if getattr(args, "verbose", False):
                mn, mx = min(closes), max(closes)
                n = sum(1 for c in closes if (c > threshold if direction == "above" else c < threshold))
                print(f"    (closes ${mn:,.0f}-${mx:,.0f}; {n}/{len(closes)} days {'above' if direction == 'above' else 'below'} threshold)")
            if rec != "SKIP":
                print(f"    Edge: {abs(diff):.1%}")
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                bid = yes_bid if rec == "BUY YES" else no_bid
                # Limit price: bid or below (dip-catch). limit_dip_pct=0.05 → place 5% below bid
                price = max(0.01, min(0.99, bid * (1 - limit_dip_pct))) if limit_dip_pct else bid
                p_win = hist_p if rec == "BUY YES" else (1 - hist_p)
                count = _kelly_count(p_win, price, balance_cents, kelly_fraction, min_contracts) if use_kelly else 10
                pending_orders.append({
                    "platform": "kalshi",
                    "ticker": ticker,
                    "title": title,
                    "rec": rec,
                    "side": "yes" if rec == "BUY YES" else "no",
                    "price": price,
                    "edge": abs(diff),
                    "count": count,
                })
            print()

        # Gemini crypto
        if yf_ticker in GEMINI_CRYPTO_YF:
            gemini_evts = gemini_events_by_asset.get(yf_ticker, [])
            if args.markets > 0:
                gemini_evts = gemini_evts[: args.markets]
            use_vol = getattr(args, "use_volatility", False)
            for evt in gemini_evts:
                yes_c = _get_yes_contract(evt)
                impl = get_yes_price(yes_c) if yes_c else 0.0
                threshold_g, direction_g = extract_strike_from_event(evt)
                title = (evt.get("title") or evt.get("ticker", ""))[:55]
                if _should_skip_by_resolution(evt, yf_ticker):
                    print(f"  [Gemini] {evt.get('ticker','?')}: SKIP (unsupported resolution — use 5pm ET) | {title}")
                    continue
                if threshold_g is None or direction_g is None:
                    print(f"  [Gemini] {evt.get('ticker','?')}: YES {impl:.2f} | (no threshold) {title}")
                    continue
                emp = _empirical_probability(closes, threshold_g, direction_g)
                vol_p_gem = _volatility_probability(closes, threshold_g, direction_g, days_to_expiry) if use_vol else None
                hist_p_gem = max(emp, vol_p_gem) if vol_p_gem is not None and use_vol else emp
                rec = (
                    _recommend_strict(hist_p_gem, impl, intraday_other_min_p_win)
                    if _is_other_intraday(evt, yf_ticker)
                    else _recommend(hist_p_gem, impl, min_edge)
                )
                violates_rule_g = _violates_no_above_rule(current_price, threshold_g, direction_g, no_above_cushion)
                if rec == "BUY NO" and violates_rule_g:
                    rec = "SKIP"
                # Favor BUY YES when price is well above strike
                if rec == "SKIP" and violates_rule_g:
                    if 0 < impl < (1 - min_edge):
                        rec = "BUY YES"
                diff_gem = _compute_edge_diff(impl, hist_p_gem, rec, violates_rule_g)
                print(f"  [Gemini] {evt.get('ticker','?')}")
                print(f"    {title}")
                print(f"    Threshold: ${threshold_g:,.0f} ({direction_g})")
                if vol_p_gem is None:
                    p_label_gem = "P(YES)"
                else:
                    p_label_gem = f"P(YES) [emp={emp:.1%} vol={vol_p_gem:.1%}]"
                print(f"    {p_label_gem}: {hist_p_gem:.1%}  Market YES: {impl:.1%}  →  {rec}")
                if rec != "SKIP":
                    print(f"    Edge: {abs(diff_gem):.1%}")
                    no_price = 1.0 - impl
                    bid = impl if rec == "BUY YES" else no_price
                    price = max(0.01, min(0.99, bid * (1 - limit_dip_pct))) if limit_dip_pct else bid
                    p_win = hist_p_gem if rec == "BUY YES" else (1 - hist_p_gem)
                    count = _kelly_count(p_win, price, balance_cents, kelly_fraction, min_contracts) if use_kelly else 10
                    pending_orders.append({
                        "platform": "gemini",
                        "ticker": evt.get("ticker", "?"),
                        "title": title,
                        "rec": rec,
                        "side": "yes" if rec == "BUY YES" else "no",
                        "price": price,
                        "edge": abs(diff_gem),
                        "count": count,
                    })
                print()

    # Evaluate ALL positions (crypto / S&P 500) for sell when bet looks like a loser
    # Kalshi API: position_fp is signed (positive=YES, negative=NO); no "side"/"position" fields
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
                                for prefix, s in [("KXBTC", "KXBTCD"), ("KXETH", "KXETHD"), ("KXSOL", "KXSOLD"), ("KXINX", "KXINXU")]:
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
                                "platform": "kalshi",
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
                            closes = yf_get_aggs(pos_yf, days_back=args.days)
                            if closes:
                                threshold, direction = _extract_strike(m)
                                if threshold is not None and direction is not None:
                                    yes_bid = float(m.get("yes_bid_dollars") or 0) or float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)
                                    no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                                    hist_p = _empirical_probability(closes, threshold, direction)
                                    vol_p = _volatility_probability(closes, threshold, direction, days_to_expiry) if use_vol else None
                                    if vol_p is not None and use_vol:
                                        hist_p = max(hist_p, vol_p)
                                    rec = _recommend(hist_p, yes_bid, min_edge)
                                    current_price_pos = closes[-1]
                                    sell_price = yes_bid if side == "yes" else no_bid
                                    # SELL when definitely a loser
                                    p_lose = hist_p if side == "no" else (1.0 - hist_p)
                                    if p_lose >= sell_losing_pct:
                                        pending_sells.append({
                                            "platform": "kalshi",
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": sell_price,
                                            "rec": "LIKELY_LOSE",
                                        })
                                    elif (side == "yes" and rec == "BUY NO") or (side == "no" and rec == "BUY YES"):
                                        pending_sells.append({
                                            "platform": "kalshi",
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": sell_price,
                                            "rec": rec,
                                        })
                                    elif side == "no" and _violates_no_above_rule(current_price_pos, threshold, direction, no_above_cushion):
                                        pending_sells.append({
                                            "platform": "kalshi",
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": no_bid,
                                            "rec": "RULE_VIOLATION",
                                        })
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
                hist_p = _empirical_probability(closes, threshold, direction)
                vol_p = _volatility_probability(closes, threshold, direction, days_to_expiry) if use_vol else None
                if vol_p is not None and use_vol:
                    hist_p = max(hist_p, vol_p)
                rec = _recommend(hist_p, yes_bid, min_edge)
                current_price_pos = closes[-1]
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                sell_price = yes_bid if side == "yes" else no_bid

                # SELL when we think position will lose: P(lose) > sell_losing_pct
                p_lose = hist_p if side == "no" else (1.0 - hist_p)
                if p_lose >= sell_losing_pct:
                    pending_sells.append({
                        "platform": "kalshi",
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": sell_price,
                        "rec": "LIKELY_LOSE",
                    })
                # SELL YES when we'd now recommend BUY NO; SELL NO when we'd recommend BUY YES
                elif (side == "yes" and rec == "BUY NO") or (side == "no" and rec == "BUY YES"):
                    pending_sells.append({
                        "platform": "kalshi",
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": sell_price,
                        "rec": rec,
                    })
                # SELL NO when holding NO on "above X" but price already > X * (1 + cushion)
                elif side == "no" and _violates_no_above_rule(current_price_pos, threshold, direction, no_above_cushion):
                    pending_sells.append({
                        "platform": "kalshi",
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": no_bid,
                        "rec": "RULE_VIOLATION",
                    })
        except Exception:
            pass

    trading = cfg.get("trading", {})
    max_pct = float(trading.get("max_pct_of_balance", 0.90))
    dry_run = trading.get("dry_run", True)

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
        if any(o["platform"] == "gemini" for o in pending_orders) or any(s["platform"] == "gemini" for s in pending_sells):
            try:
                gemini_trading = GeminiTradingClient()
            except ValueError:
                print("  (Gemini: GEMINI_API_KEY/GEMINI_API_SECRET not set, skipping Gemini orders)")

        # Build sets of (ticker, side) / (symbol, outcome) that already have open buy orders — avoid duplicates
        kalshi_open_buys: set[tuple[str, str]] = set()
        try:
            for status in ("resting",):
                cursor = ""
                while True:
                    r = kalshi.get_orders(status=status, limit=200, **{"cursor": cursor} if cursor else {})
                    for ord_ in r.get("orders") or []:
                        if (ord_.get("action") or "").lower() == "buy":
                            t = ord_.get("ticker")
                            s = (ord_.get("side") or "").lower()
                            if t and s:
                                kalshi_open_buys.add((t, s))
                    cursor = r.get("cursor") or ""
                    if not cursor:
                        break
        except Exception:
            pass
        gemini_open_buys: set[tuple[str, str]] = set()
        if gemini_trading:
            try:
                act = gemini_trading.get_active_orders(limit=200)
                for ord_ in act.get("orders") or []:
                    if (ord_.get("side") or "").lower() == "buy":
                        sym = ord_.get("symbol") or ord_.get("instrumentSymbol")
                        out = (ord_.get("outcome") or ord_.get("label") or "").lower()
                        if sym and out in ("yes", "no"):
                            gemini_open_buys.add((str(sym), out))
            except Exception:
                pass

        # SELL first so balance reflects proceeds before we place buys
        for s in pending_sells:
            if s["platform"] == "kalshi":
                price_str = f"{s['price']:.4f}"  # Kalshi requires 4 decimal places
                try:
                    result = kalshi.create_order_safe(
                        ticker=s["ticker"],
                        side=s["side"],
                        action="sell",
                        count=s["count"],
                        yes_price_dollars=price_str if s["side"] == "yes" else None,
                        no_price_dollars=price_str if s["side"] == "no" else None,
                        max_pct_of_balance=max_pct,
                    )
                    if result:
                        print(f"  Placed SELL {s['side'].upper()} {s['ticker']} x {s['count']}")
                        sells_placed += 1
                    else:
                        print(f"  (skipped SELL {s['ticker']})")
                except Exception as ex:
                    print(f"  SELL {s['ticker']} failed: {ex}")
            elif s["platform"] == "gemini" and gemini_trading:
                try:
                    gemini_evt = _find_gemini_event(gemini_events_by_asset, s.get("ticker", ""))
                    sym = get_instrument_symbol(gemini_evt, s["side"]) if gemini_evt else None
                    if sym:
                        gemini_trading.place_order(symbol=sym, side="sell", outcome=s["side"], quantity=s["count"], price=s["price"])
                        print(f"  Placed SELL {s['side'].upper()} (Gemini) {s['ticker']} x {s['count']}")
                        sells_placed += 1
                except Exception as e:
                    print(f"  (Gemini sell failed for {s['ticker']}: {e})")

        # Then BUY — balance now reflects sell proceeds
        for o in pending_orders:
            if o["platform"] == "kalshi":
                if (o["ticker"], o["side"]) in kalshi_open_buys:
                    print(f"  (skip BUY {o['ticker']} — already have open limit order)")
                    continue
                price_str = f"{o['price']:.4f}"  # Kalshi requires 4 decimal places
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
                    )
                    if result:
                        print(f"  Placed BUY {o['side'].upper()} {o['ticker']} x {count}")
                        buys_placed += 1
                    else:
                        print(f"  (skipped BUY {o['ticker']} - balance/API constraint)")
                except Exception as ex:
                    print(f"  BUY {o['ticker']} failed: {ex}")
            else:
                count = o.get("count", 10)
                gemini_evt = _find_gemini_event(gemini_events_by_asset, o["ticker"])
                sym = get_instrument_symbol(gemini_evt, o["side"]) if gemini_evt else None
                if sym and (sym, o["side"]) in gemini_open_buys:
                    print(f"  (skip BUY {o['ticker']} — already have open limit order)")
                    continue
                if sym and gemini_trading:
                    try:
                        gemini_trading.place_order(symbol=sym, side="buy", outcome=o["side"], quantity=count, price=o["price"])
                        print(f"  Placed BUY {o['side'].upper()} (Gemini) {o['ticker']} x {count}")
                        buys_placed += 1
                    except Exception as e:
                        print(f"  (Gemini order failed for {o['ticker']}: {e})")
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
            if s["platform"] == "kalshi":
                payload = {
                    "ticker": s["ticker"],
                    "side": s["side"],
                    "action": "sell",
                    "count": s["count"],
                }
                payload["yes_price_dollars" if s["side"] == "yes" else "no_price_dollars"] = f"{s['price']:.2f}"
                print(f"   API: POST /portfolio/orders")
                print(f"   Payload: {payload}")
            elif s["platform"] == "gemini":
                print(f"   API: POST /v1/prediction-markets/order (place_order, side=sell)")

        for i, o in enumerate(pending_orders, 1):
            print(f"\n{i}. [{o['platform'].upper()}] {o['rec']} {o['ticker']}")
            print(f"   {o['title']}")
            print(f"   Edge: {o['edge']:.1%}")
            if o["platform"] == "kalshi":
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
                gemini_evt = _find_gemini_event(gemini_events_by_asset, o["ticker"])
                sym = get_instrument_symbol(gemini_evt, o["side"]) if gemini_evt else None
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
