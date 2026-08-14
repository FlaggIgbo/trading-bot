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
   - Market Start Mark (:00, :15, :30, :45): Buy YES/NO if price is between 35¢ and 49¢.
   - 5-Min Past Mark (:05, :20, :35, :50):
     * Early Cut-Loss: If position bought at :00 is below 40¢ (< 40¢-49¢ range), sell immediately to cut losses.
     * Tighter Buy Entry: If no position held, buy YES/NO if price is between 40¢ and 49¢.
   - Spot Momentum Filter: Buy YES only if spot >= target; Buy NO only if spot < target.
   - 10-Min Past Mark (:10, :25, :40, :55): Evaluate Stop-Loss (Sell if loss >= 10¢ or price <= 30¢; let run if >= 70¢).
   - 82¢ / 76¢ Auto-Sell: Places a resting limit Sell order immediately upon buy execution.
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

from database import save_market_snapshots, save_portfolio_pnl, save_settlements
from signals.kalshi_client import KalshiClient, LogTag

# Set up logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPPORTED_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC")
http_session = requests.Session()


def calculate_seconds_until_next_5min_mark(offset_seconds: float = 30.0) -> float:
    """Calculates seconds to sleep until 30s past the next 5-minute mark (:00:30, :05:30, etc.)."""
    now = datetime.now(timezone.utc)
    seconds_past_5min = (now.minute % 5) * 60 + now.second + (now.microsecond / 1_000_000.0)
    wait_seconds = (300.0 - seconds_past_5min + offset_seconds) % 300.0
    return max(0.5, wait_seconds)


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """Extracts short crypto asset symbol (e.g., 'BTC', 'ETH')."""
    combined = f"{series_ticker} {ticker}".upper()
    return next((sym for sym in SUPPORTED_SYMBOLS if sym in combined), series_ticker or "CRYPTO")


def parse_cents(val_dollars: str | float | None, val_cents: int | str | None) -> int | None:
    """Safely converts string dollars (e.g. '0.3500') or cent integers to an integer cent value (1-99)."""
    for val, multiplier in ((val_dollars, 100), (val_cents, 1)):
        if val is not None:
            try:
                res = int(round(float(val) * multiplier))
                if 1 <= res <= 99:
                    return res
            except (ValueError, TypeError):
                continue
    return None


def is_spot_momentum_aligned(
    side: str,
    spot_price: float | None,
    target_price: float | None,
    otm_pct_threshold: float = 0.05,
) -> bool:
    """
    Relative Distance Gate / Out-Of-The-Money Spot Filter:
    - Near ATM (|pct_dist| <= 0.05%): Dead-heat zone; allow both YES and NO.
    - OTM (|pct_dist| > 0.05%): Enforce direction (YES if spot >= target, NO if spot < target).
    """
    if spot_price is None or target_price is None or target_price <= 0:
        return True

    pct_dist = ((spot_price - target_price) / target_price) * 100.0
    if abs(pct_dist) <= otm_pct_threshold:
        return True

    return (pct_dist >= 0) if side.lower() == "yes" else (pct_dist < 0)


def extract_price_cents(market: dict, side: str, kalshi_client: KalshiClient | None = None) -> int | None:
    """Extracts the Ask price in cents (1-99) for either 'yes' or 'no'."""
    prefix = side.lower()
    dollar_str, cent_val = market.get(f"{prefix}_ask_dollars"), market.get(f"{prefix}_ask")

    # Fallback API detail call if ask fields are missing in market payload
    if dollar_str is None and cent_val is None and kalshi_client and market.get("ticker"):
        try:
            detail = kalshi_client.get_market(market["ticker"]).get("market", {})
            dollar_str = detail.get(f"{prefix}_ask_dollars")
            cent_val = detail.get(f"{prefix}_ask")
        except Exception as err:
            log.debug(f"Fallback get_market failed for {market.get('ticker')}: {err}")

    return parse_cents(dollar_str, cent_val)


def get_crypto_spot_price(symbol: str) -> float | None:
    """Fetches real-time underlying spot price via Coinbase public API using a reusable session."""
    try:
        resp = http_session.get(f"https://api.coinbase.com/v2/prices/{symbol.upper()}-USD/spot", timeout=3.0)
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
    if market_result in ("yes", "no"):
        winning_side = market_result.lower()
    elif spot_price is not None and target_price is not None:
        winning_side = "yes" if spot_price >= target_price else "no"
    else:
        return "pending_settlement"

    won = (trade_side.lower() == winning_side)
    if action_type in ("stop_loss_sold", "early_loss_cut"):
        return "sold_early_would_have_won" if won else "sl_cap_saved"
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
    kalshi_client: KalshiClient,
    dry_run: bool,
) -> tuple[bool, dict | None]:
    """Helper to execute trade and construct trade snapshot payload."""
    side_lower = side.lower()
    side_upper = side.upper()
    momentum_label = "Bullish" if side_lower == "yes" else "Bearish"

    log_prefix = "🎯 [DRY-RUN MATCH]" if dry_run else "⚡ [STRATEGY MATCH]"
    log_action = f"WOULD BUY 1 SHARE OF [{side_upper}] + SET {take_profit_cents}¢ AUTO-SELL." if dry_run else f"Executing Maker Buy + {take_profit_cents}¢ Auto-Sell!"
    log.info(f"{log_prefix} {ticker} -> {side_upper} price is {price_cents}¢ (between {min_buy}¢ and {max_buy}¢ & {momentum_label} Spot Momentum!). {log_action}")

    effective_buy = price_cents
    if not dry_run:
        try:
            buy_res = kalshi_client.buy_and_set_take_profit(
                ticker=ticker,
                side=side_lower,
                buy_price_cents=price_cents,
                take_profit_cents=take_profit_cents,
                count=1,
                maker_bid_cents=maker_bid,
            )
            effective_buy = buy_res.get("buy_price_cents", price_cents)
        except Exception as error:
            log.error(f"❌ Failed to execute {side_upper} trade on {ticker}: {error}")
            return False, None

    trade_details = {
        **({"dry_run": True} if dry_run else {}),
        "action": "buy_opened",
        "side": side_lower,
        "buy": effective_buy,
        "tp": take_profit_cents,
        "sl": 10,
        "outcome": determine_counterfactual_outcome(side_lower, "buy_opened", spot_price, target_price, market_result),
    }
    return True, trade_details


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

    quotes = {
        k: parse_cents(market.get(f"{k}_dollars"), market.get(k))
        for k in ("yes_ask", "yes_bid", "no_ask", "no_bid")
    }
    sprd = (quotes["yes_ask"] - quotes["yes_bid"]) if (quotes["yes_ask"] is not None and quotes["yes_bid"] is not None) else None

    # 2. Extract spot & strike target metrics
    spot_price = get_crypto_spot_price(asset_symbol)
    target_price = market.get("floor_strike")

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

    # 4. Strategy Rules & Interval Evaluation
    minute_into_market = minute_past_hour % 15
    is_stop_loss_interval = (minute_into_market == 10)
    is_5min_mark = (minute_into_market == 5)
    min_buy_price = 40 if is_5min_mark else 35
    max_buy_price = 49
    take_profit_cents = 76 if is_5min_mark else 82

    trade_executed = False
    trade_details = None

    # Step 4A: Stop-Loss Evaluation (:10, :25, :40, :55)
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

    # Step 4B: Buy Strategy Decision & 5-Min Early Cut-Loss Check (:00, :05, :15, :20...)
    if is_trading_interval:
        if not dry_run:
            kalshi_client.cancel_unfilled_buy_orders(ticker)

        pos = kalshi_client.get_market_position(ticker)
        if pos is not None:
            if is_5min_mark:
                side = pos["side"]
                pos_price = yes_price_cents if side == "yes" else no_price_cents
                if pos_price is not None and pos_price < 40:
                    cut_res = kalshi_client.evaluate_and_execute_5min_cut_loss(
                        ticker=ticker,
                        current_yes_price=yes_price_cents,
                        current_no_price=no_price_cents,
                        min_price_threshold=40,
                        dry_run=dry_run,
                    )
                    if isinstance(cut_res, dict) and cut_res.get("triggered"):
                        trade_executed = True
                        trade_details = {
                            "action": "early_loss_cut",
                            "side": side,
                            "exit_price": cut_res.get("exit_price"),
                            "loss_cents": cut_res.get("loss_cents"),
                            "outcome": determine_counterfactual_outcome(side, "early_loss_cut", spot_price, target_price, market.get("result")),
                        }
                        pos = None

            if pos is not None:
                log.info(f"🛡️ [OPEN POSITION GATE] Already hold active {pos['side'].upper()} pos of {pos['count']} contract(s) for {ticker}. Skipping buy.")
                if not is_stop_loss_interval and not kalshi_client.has_resting_take_profit_order(ticker) and not dry_run:
                    kalshi_client.place_take_profit_order(
                        ticker=ticker,
                        side=pos["side"],
                        count=pos["count"],
                        take_profit_cents=take_profit_cents,
                    )
        elif kalshi_client.has_existing_position_or_order(ticker):
            log.info(f"🛡️ [OPEN POSITION GATE] Already hold an active position or resting order for {ticker}. Skipping buy.")
        else:
            # Evaluate eligible sides & select cheapest entry
            candidate_sides = []
            for side, p_cents, aligned in [
                ("yes", yes_price_cents, is_spot_momentum_aligned("yes", spot_price, target_price)),
                ("no", no_price_cents, is_spot_momentum_aligned("no", spot_price, target_price)),
            ]:
                if p_cents is not None and min_buy_price <= p_cents <= max_buy_price and aligned:
                    candidate_sides.append((side, p_cents))

            if candidate_sides:
                chosen_side, chosen_price = min(candidate_sides, key=lambda x: x[1])
                chosen_bid = quotes["yes_bid"] if chosen_side == "yes" else quotes["no_bid"]
                trade_executed, trade_details = _execute_buy_flow(
                    ticker, chosen_side, chosen_price, chosen_bid, min_buy_price, max_buy_price,
                    take_profit_cents, spot_price, target_price, market.get("result"), kalshi_client, dry_run
                )

    # 5. Build Compact Snapshot Document (~100 bytes)
    compact_document = {
        "ts": int(now_utc.timestamp()),
        "m": minute_past_hour,
        "sym": asset_symbol,
        "tick": ticker,
        "y": yes_price_cents,
        "n": no_price_cents,
        "y_bid": quotes["yes_bid"],
        "y_ask": quotes["yes_ask"],
        "n_bid": quotes["no_bid"],
        "n_ask": quotes["no_ask"],
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
    log.info(f"RUNNING BOT AT UTC TIME: {now_utc.isoformat()} (Minute :{current_minute:02d}:{now_utc.second:02d})")
    if dry_run:
        log.info("--> MODE: [DRY-RUN PREVIEW] (No real orders will be placed)")
    log.info(
        "--> INTERVAL: "
        + ("BUY TRADING & 5-MIN CUT-LOSS / DATA COLLECTION MARK (:00, :05, :15, :20...)" if is_trading_interval
           else "STOP-LOSS EVALUATION & DATA COLLECTION MARK (:10, :25...)" if is_stop_loss_interval
           else "DATA COLLECTION ONLY MARK")
    )
    log.info("=================================================================")

    try:
        client = KalshiClient()
    except Exception as error:
        log.error(f"Could not initialize KalshiClient: {error}")
        return

    # Fetch real-time Portfolio PnL & Balance
    try:
        pnl = client.get_portfolio_pnl_summary()
        pnl["ts"] = int(now_utc.timestamp())
        sign = "+" if pnl["total_realized_pnl_dollars"] >= 0 else ""

        log.info(
            f"{LogTag.PORTFOLIO_PNL.value} Cash: ${pnl['balance_dollars']:.2f} | "
            f"Port Value: ${pnl['portfolio_value_dollars']:.2f} | "
            f"Settled PnL: {sign}${pnl['total_realized_pnl_dollars']:.2f} ({sign}{pnl['total_realized_pnl_cents']}¢ across {pnl['settlements_count']} mkts) | "
            f"Active: {pnl['open_positions_count']} pos (${pnl['open_exposure_dollars']:.2f})"
        )

        if not dry_run:
            save_portfolio_pnl(pnl)
            if pnl.get("settlements"):
                save_settlements(pnl["settlements"])
    except Exception as err:
        log.warning(f"Could not retrieve portfolio PnL summary from Kalshi: {err}")

    log.info("Fetching active 15-minute crypto markets from Kalshi...")
    try:
        markets = client.get_15m_crypto_markets(status="open")
        log.info(f"Retrieved {len(markets)} active 15-minute crypto markets.")
    except Exception as error:
        log.error(f"Failed to fetch 15-minute crypto markets: {error}")
        return

    snapshots_to_save = []
    for market in markets:
        snapshot = process_single_market(market, is_trading_interval, client, now_utc, current_minute, dry_run=dry_run)
        snapshots_to_save.append(snapshot)

        diff_val = snapshot.get("diff")
        diff_str = f"+{diff_val}" if (diff_val is not None and diff_val > 0) else str(diff_val)
        log.info(
            f"  • Market [{snapshot['sym']} | {snapshot['tick']}] -> YES: {snapshot['y']}¢ | NO: {snapshot['n']}¢ | "
            f"Price: ${snapshot.get('p')} | Target: ${snapshot.get('tgt')} | Diff: {diff_str}"
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
        return

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
