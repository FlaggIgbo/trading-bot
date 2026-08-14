"""
15-Minute Crypto Prediction Trading & Data Pipeline Bot
=========================================================

Overview & Architecture:
------------------------
This bot runs continuously 24/7 as a Render Background Worker.

1. Why We Execute 30 Seconds Past the 5-Minute Mark (:00:30, :05:30, :10:30...):
   - At exactly :00.000, :15.000, etc., Kalshi's exchange engine is actively expiring 
     the old 15-minute contracts and initializing the new ones.
   - Quicker API requests right at :00.000 can catch the orderbook in a brief reset state 
     (returning `None` for bid/ask prices).
   - By adding a 30-second buffer, we give Kalshi's API 30 seconds to populate live bids and asks.

2. Dynamic Trading Strategy & Risk Management:
   - Market Start Mark (:00, :15, :30, :45): Buy YES/NO if price is between 35¢ and 45¢.
   - 5-Min Past Mark (:05, :20, :35, :50): Tighter Buy YES/NO if price is between 40¢ and 45¢.
   - Spot Momentum Filter: Buy YES only if spot >= target; Buy NO only if spot < target.
   - 10-Min Past Mark (:10, :25, :40, :55): Evaluate Stop-Loss (Sell if loss >= 10¢).
   - 82¢ Auto-Sell: Places a resting limit Sell order at 82¢ immediately upon buy execution.
   - Open Position Gate: Skips buying if an active position/order already exists for that market.

3. Data Logging & MongoDB Atlas (Every 5 Minutes):
   - Saves ultra-compact snapshot records (~100 bytes each) for 12 crypto assets.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
import requests

from database import save_market_snapshots
from signals.kalshi_client import KalshiClient, LogTag, SERIES_15M_CRYPTO

# Set up logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPPORTED_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC"]


def calculate_seconds_until_next_5min_mark(offset_seconds: float = 30.0) -> float:
    """Calculates seconds to sleep until 30s past the next 5-minute mark (:00:30, :05:30, etc.)."""
    now = datetime.now(timezone.utc)
    # Calculate total seconds elapsed in the current 5-minute block
    seconds_past_5min = (now.minute % 5) * 60 + now.second + (now.microsecond / 1_000_000.0)
    
    # Wait time = 300s (5 mins) - elapsed seconds + offset
    wait_seconds = 300.0 - seconds_past_5min + offset_seconds
    if wait_seconds >= 300.0:
        wait_seconds -= 300.0

    return max(0.5, wait_seconds)


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """Extracts short crypto asset symbol (e.g., 'BTC', 'ETH')."""
    combined = f"{series_ticker} {ticker}".upper()
    for symbol in SUPPORTED_SYMBOLS:
        if symbol in combined:
            return symbol
    return series_ticker or "CRYPTO"


def parse_cents(val_dollars: str | float | None, val_cents: int | str | None) -> int | None:
    """Safely converts string dollars (e.g. '0.3500') or cent integers to an integer cent value (1-99)."""
    if val_dollars is not None:
        try:
            res = int(round(float(val_dollars) * 100))
            if 1 <= res <= 99:
                return res
        except (ValueError, TypeError):
            pass

    if val_cents is not None:
        try:
            res = int(val_cents)
            if 1 <= res <= 99:
                return res
        except (ValueError, TypeError):
            pass

    return None


def is_spot_momentum_aligned(
    side: str,
    spot_price: float | None,
    target_price: float | None,
    otm_pct_threshold: float = 0.05,
) -> bool:
    """
    Relative Distance Gate / Out-Of-The-Money Spot Filter:
    
    1. Dead-Heat Zone (|pct_dist| <= 0.05%):
       If spot price is within 0.05% of target strike (~$0.94 on ETH, ~$31 on BTC),
       the market is in a dead heat. Buying YES or NO in the 35¢-45¢ range is allowed!
       
    2. Out-Of-The-Money Zone (|pct_dist| > 0.05%):
       If spot price is significantly far from strike, enforce momentum direction:
       - YES is allowed ONLY IF spot_price >= target_price.
       - NO is allowed ONLY IF spot_price < target_price.
    """
    if spot_price is None or target_price is None or target_price <= 0:
        return True

    diff = spot_price - target_price
    pct_dist = (diff / target_price) * 100.0

    # Near At-The-Money Zone (<= 0.05% distance): Allow both YES and NO
    if abs(pct_dist) <= otm_pct_threshold:
        return True

    # Significantly Out-Of-The-Money (> 0.05% distance): Enforce momentum direction
    if side.lower() == "yes":
        return pct_dist >= 0
    else:
        return pct_dist < 0



def extract_price_cents(market: dict, side: str, kalshi_client: KalshiClient | None = None) -> int | None:
    """Extracts the price in cents (1-99) for either 'yes' or 'no' with client fallback on missing data."""
    is_yes = side.lower() == "yes"
    prefix = "yes" if is_yes else "no"

    dollar_str = market.get(f"{prefix}_ask_dollars") or market.get(f"{prefix}_bid_dollars")
    cent_val = market.get(f"{prefix}_ask") or market.get(f"{prefix}_bid") or (market.get("last_price") if is_yes else None)

    # Fallback API detail call if fields are missing in market payload
    if dollar_str is None and cent_val is None and kalshi_client and market.get("ticker"):
        try:
            detail = kalshi_client.get_market(market["ticker"]).get("market", {})
            dollar_str = detail.get(f"{prefix}_ask_dollars") or detail.get(f"{prefix}_bid_dollars")
            cent_val = detail.get(f"{prefix}_ask") or detail.get(f"{prefix}_bid") or (detail.get("last_price") if is_yes else None)
        except Exception as err:
            log.debug(f"Fallback get_market failed for {market.get('ticker')}: {err}")

    return parse_cents(dollar_str, cent_val)


def get_crypto_spot_price(symbol: str) -> float | None:
    """Fetches real-time underlying spot price via Coinbase public API."""
    try:
        url = f"https://api.coinbase.com/v2/prices/{symbol.upper()}-USD/spot"
        resp = requests.get(url, timeout=3.0)
        if resp.status_code == 200:
            return round(float(resp.json()["data"]["amount"]), 4)
    except Exception as error:
        log.debug(f"Could not fetch spot price for {symbol}: {error}")
    return None


def determine_counterfactual_outcome(
    trade_side: str,
    action_type: str,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None = None,
) -> str:
    """Evaluates counterfactual post-mortem trade outcomes for data logging."""
    winning_side = None
    if market_result in ("yes", "no"):
        winning_side = market_result.lower()
    elif spot_price is not None and target_price is not None:
        winning_side = "yes" if spot_price >= target_price else "no"

    if not winning_side:
        return "pending_settlement"

    trade_would_have_won = (trade_side.lower() == winning_side)

    if action_type == "stop_loss_sold":
        return "sold_early_would_have_won" if trade_would_have_won else "sl_cap_saved"
    elif action_type == "take_profit_sold":
        return "tp_full_win" if trade_would_have_won else "sold_early_saved_from_loss"

    return "open_position"


def _execute_buy_flow(
    ticker: str,
    side: str,
    price_cents: int,
    maker_bid: int | None,
    min_buy: int,
    max_buy: int,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None,
    kalshi_client: KalshiClient,
    dry_run: bool,
) -> tuple[bool, dict | None]:
    """Helper to eliminate code duplication across YES and NO buying logic."""
    side_upper = side.upper()
    momentum_label = "Bullish" if side.lower() == "yes" else "Bearish"

    if dry_run:
        log.info(
            f"🎯 [DRY-RUN MATCH] {ticker} -> {side_upper} price is {price_cents}¢ "
            f"(between {min_buy}¢ and {max_buy}¢ & {momentum_label} Spot Momentum!). "
            f"WOULD BUY 1 SHARE OF [{side_upper}] + SET 82¢ AUTO-SELL."
        )
        return True, {
            "dry_run": True,
            "action": "buy_opened",
            "side": side.lower(),
            "buy": price_cents,
            "tp": 82,
            "sl": 10,
            "outcome": determine_counterfactual_outcome(side, "buy_opened", spot_price, target_price, market_result),
        }

    log.info(
        f"⚡ [STRATEGY MATCH] {ticker} {side_upper} price is {price_cents}¢ "
        f"(between {min_buy}¢ and {max_buy}¢ & {momentum_label} Spot Momentum!). Executing Maker Buy + 82¢ Auto-Sell!"
    )
    try:
        kalshi_client.buy_and_set_take_profit(
            ticker=ticker,
            side=side.lower(),
            buy_price_cents=price_cents,
            take_profit_cents=82,
            count=1,
            maker_bid_cents=maker_bid,
        )
        return True, {
            "action": "buy_opened",
            "side": side.lower(),
            "buy": price_cents,
            "tp": 82,
            "sl": 10,
            "outcome": determine_counterfactual_outcome(side, "buy_opened", spot_price, target_price, market_result),
        }
    except Exception as error:
        log.error(f"❌ Failed to execute {side_upper} trade on {ticker}: {error}")
        return False, None


def process_single_market(
    market: dict,
    is_trading_interval: bool,
    kalshi_client: KalshiClient,
    now_utc: datetime,
    minute_past_hour: int,
    dry_run: bool = False,
) -> dict:
    """Processes a single 15-minute crypto market (pricing, signals, trade execution, snapshot generation)."""
    ticker = market.get("ticker", "UNKNOWN")
    series_ticker = market.get("series_ticker", "UNKNOWN")
    asset_symbol = extract_asset_symbol(series_ticker, ticker)

    # 1. Parse top-of-book prices and spreads
    yes_price_cents = extract_price_cents(market, "yes", kalshi_client=kalshi_client)
    no_price_cents = extract_price_cents(market, "no", kalshi_client=kalshi_client)

    y_ask = parse_cents(market.get("yes_ask_dollars"), market.get("yes_ask"))
    y_bid = parse_cents(market.get("yes_bid_dollars"), market.get("yes_bid"))
    n_ask = parse_cents(market.get("no_ask_dollars"), market.get("no_ask"))
    n_bid = parse_cents(market.get("no_bid_dollars"), market.get("no_bid"))

    sprd = (y_ask - y_bid) if (y_ask is not None and y_bid is not None) else None

    # 2. Extract spot & strike target metrics
    spot_price = get_crypto_spot_price(asset_symbol)
    target_price = market.get("floor_strike")

    price_diff, pct_dist = None, None
    if spot_price is not None and target_price is not None:
        try:
            p_float, tgt_float = float(spot_price), float(target_price)
            price_diff = round(p_float - tgt_float, 4)
            if tgt_float > 0:
                pct_dist = round(((p_float - tgt_float) / tgt_float) * 100.0, 4)
        except (ValueError, TypeError):
            pass

    # 3. Time decay and market volume
    rem_mins = None
    close_iso = market.get("close_time") or market.get("expected_expiration_time")
    if close_iso:
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            rem_mins = round(max(0.0, (close_dt - now_utc).total_seconds() / 60.0), 1)
        except Exception:
            pass

    vol_count = None
    vol_str = market.get("volume_fp") or market.get("volume_24h_fp")
    if vol_str is not None:
        try:
            vol_count = int(round(float(vol_str)))
        except (ValueError, TypeError):
            pass

    # 4. Strategy Rules & Interval Evaluation: Relative Distance Gate)
    spot_bullish = is_spot_momentum_aligned("yes", spot_price, target_price)
    spot_bearish = is_spot_momentum_aligned("no", spot_price, target_price)

    minute_into_market = minute_past_hour % 15
    is_stop_loss_interval = (minute_into_market == 10)
    min_buy_price = 40 if minute_into_market == 5 else 35
    max_buy_price = 45

    trade_executed = False
    trade_details = None

    # Step 4A: Stop-Loss Evaluation (10m past start: :10, :25, :40, :55)
    if is_stop_loss_interval:
        sl_res = kalshi_client.evaluate_and_execute_stop_loss(
            ticker=ticker,
            current_yes_price=yes_price_cents,
            current_no_price=no_price_cents,
            dry_run=dry_run,
            stop_loss_threshold_cents=10,
        )
        if isinstance(sl_res, dict) and sl_res.get("triggered"):
            trade_executed = True
            sl_side = sl_res.get("side", "yes")
            trade_details = {
                "action": "stop_loss_sold",
                "side": sl_side,
                "exit_price": sl_res.get("exit_price"),
                "loss_cents": sl_res.get("loss_cents"),
                "outcome": determine_counterfactual_outcome(sl_side, "stop_loss_sold", spot_price, target_price, market.get("result")),
            }

    # Step 4B: Buy Strategy Decision (0m and 5m past start: :00, :05, :15, :20...)
    if is_trading_interval:
        if kalshi_client.has_existing_position_or_order(ticker):
            log.info(f"🛡️ [OPEN POSITION GATE] Already hold an active position or order for {ticker}. Skipping buy.")
        else:
            # Evaluate YES Entry
            if yes_price_cents and min_buy_price <= yes_price_cents <= max_buy_price and spot_bullish:
                trade_executed, trade_details = _execute_buy_flow(
                    ticker, "yes", yes_price_cents, y_bid, min_buy_price, max_buy_price,
                    spot_price, target_price, market.get("result"), kalshi_client, dry_run
                )
            # Evaluate NO Entry
            elif no_price_cents and min_buy_price <= no_price_cents <= max_buy_price and spot_bearish:
                trade_executed, trade_details = _execute_buy_flow(
                    ticker, "no", no_price_cents, n_bid, min_buy_price, max_buy_price,
                    spot_price, target_price, market.get("result"), kalshi_client, dry_run
                )

    # 5. Build Compact Snapshot Document (~100 bytes)
    compact_document = {
        "ts": int(now_utc.timestamp()),
        "m": minute_past_hour,
        "sym": asset_symbol,
        "tick": ticker,
        "y": yes_price_cents,
        "n": no_price_cents,
        "y_bid": y_bid,
        "y_ask": y_ask,
        "n_bid": n_bid,
        "n_ask": n_ask,
        "sprd": sprd,
        "p": spot_price,
        "tgt": target_price,
        "diff": price_diff,
        "pct": pct_dist,
        "rem": rem_mins,
        "vol": vol_count,
    }

    if trade_executed and trade_details:
        compact_document["trd"] = trade_details

    return compact_document


def run_pipeline_pass(dry_run: bool = False, force_trade_mark: bool = False):
    """Executes a single market processing & trading pass."""
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute

    minute_into_market = current_minute % 15
    is_trading_interval = (minute_into_market in (0, 5)) or force_trade_mark
    is_stop_loss_interval = (minute_into_market == 10)

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {now_utc.isoformat()}")
    log.info(f"Current Minute Past Hour: :{current_minute:02d}:{now_utc.second:02d}")

    if dry_run:
        log.info("--> MODE: [DRY-RUN PREVIEW] (No real orders will be placed)")

    if is_trading_interval:
        log.info("--> INTERVAL: BUY TRADING & DATA COLLECTION MARK (:00, :05, :15, :20...)")
    elif is_stop_loss_interval:
        log.info("--> INTERVAL: STOP-LOSS EVALUATION & DATA COLLECTION MARK (:10, :25...)")
    else:
        log.info("--> INTERVAL: DATA COLLECTION ONLY MARK")
    log.info("=================================================================")

    try:
        client = KalshiClient()
    except Exception as error:
        log.error(f"Could not initialize KalshiClient: {error}")
        return

    log.info("Fetching active 15-minute crypto markets from Kalshi...")
    try:
        markets = client.get_15m_crypto_markets(status="open")
        log.info(f"Retrieved {len(markets)} active 15-minute crypto markets.")
    except Exception as error:
        log.error(f"Failed to fetch 15-minute crypto markets: {error}")
        return

    snapshots_to_save = []
    for market in markets:
        snapshot = process_single_market(
            market=market,
            is_trading_interval=is_trading_interval,
            kalshi_client=client,
            now_utc=now_utc,
            minute_past_hour=current_minute,
            dry_run=dry_run,
        )
        snapshots_to_save.append(snapshot)

        log.info(
            f"  • Market [{snapshot['sym']} | {snapshot['tick']}] -> YES: {snapshot['y']}¢ | NO: {snapshot['n']}¢ | "
            f"Price: ${snapshot.get('p')} | Target: ${snapshot.get('tgt')} | Diff: {snapshot.get('diff')}"
        )

    # Save non-empty price snapshots to MongoDB Atlas
    valid_snapshots = [s for s in snapshots_to_save if s.get("y") is not None or s.get("n") is not None]

    if valid_snapshots:
        log.info("\nSaving ultra-compact market snapshot data to MongoDB Atlas...")
        save_market_snapshots(valid_snapshots)
    else:
        log.warning("\nNo valid price snapshots retrieved in this pass. Skipping MongoDB Atlas save.")
    log.info("Pass complete.")


def main():
    """Main bot execution loop (continuous background worker or single execution)."""
    args = sys.argv[1:]
    dry_run = "--dry-run" in args or "-d" in args
    force_trade_mark = "--force-trade" in args or "-f" in args
    run_once = "--once" in args or "-o" in args

    if run_once or dry_run:
        run_pipeline_pass(dry_run=dry_run, force_trade_mark=force_trade_mark)
    else:
        log.info("Starting Trading Bot in Continuous Render Background Worker Mode...")
        while True:
            try:
                run_pipeline_pass(dry_run=dry_run, force_trade_mark=force_trade_mark)
            except Exception as error:
                log.error(f"Unexpected error in loop pass: {error}")

            sleep_seconds = calculate_seconds_until_next_5min_mark(offset_seconds=30.0)
            next_mark_min = (datetime.now(timezone.utc).minute // 5 * 5 + 5) % 60
            log.info(f"\n{LogTag.CLOCK_ALIGNMENT.value} Sleeping {sleep_seconds:.1f}s until 30s past next 5-minute mark (:{next_mark_min:02d}:30)...\n")
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
