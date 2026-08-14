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
   - By adding a 30-second buffer (:00:30, :05:30, :10:30, :15:30, :20:30, :25:30, :30:30, 
     :35:30, :40:30, :45:30, :50:30, :55:30), we give Kalshi's API 30 seconds to populate 
     active, live bids and asks!

2. Dynamic Trading Strategy & Risk Management:
   - Market Start Mark (:00, :15, :30, :45): Buy YES/NO if price is between 35¢ and 45¢.
   - 5-Min Past Mark (:05, :20, :35, :50): Tighter Buy YES/NO if price is between 40¢ and 45¢.
   - 10-Min Past Mark (:10, :25, :40, :55): Evaluate Stop-Loss (Sell open position if loss >= 15¢).
   - 75¢ Auto-Sell: Places a resting limit Sell order at 75¢ immediately upon buy execution.
   - Open Position Gate: Skips buying if an active position/order already exists for that market.

3. Data Logging & MongoDB Atlas (Every 5 Minutes):
   - At every 5-minute mark (:00:30, :05:30, :10:30...), ultra-compact snapshot records 
     (~100 bytes each) are saved to MongoDB Atlas for 12 crypto assets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ADA, TON, BCH, NEAR, ZEC).
   - Captures orderbook depth (bids/asks), spread, spot price % distance, time decay, volume, and counterfactual trade outcomes.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone

from database import save_market_snapshots
from signals.kalshi_client import KalshiClient, SERIES_15M_CRYPTO

# Set up logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def calculate_seconds_until_next_5min_mark(offset_seconds: float = 30.0) -> float:
    """
    Calculates the exact number of seconds to sleep until 30 seconds past the next 5-minute clock mark
    (:00:30, :05:30, :10:30, :15:30, :20:30, :25:30, :30:30, :35:30, :40:30, :45:30, :50:30, :55:30).

    Args:
        offset_seconds: Number of seconds to delay past the 5-minute mark (default: 30.0s).

    Returns:
        float: Seconds to sleep.
    """
    now = datetime.now(timezone.utc)
    # Calculate seconds past the previous 5-minute mark (:00, :05, :10...)
    seconds_past_5min = (now.minute % 5) * 60 + now.second + now.microsecond / 1_000_000.0

    # Wait time = 300 seconds (5 mins) - elapsed seconds + offset_seconds
    wait_seconds = 300.0 - seconds_past_5min + offset_seconds

    # Adjust if wait_seconds exceeds 300.0 (e.g. if we just passed the :30 mark by a few milliseconds)
    if wait_seconds >= 300.0:
        wait_seconds -= 300.0

    return max(0.5, wait_seconds)


def extract_asset_symbol(series_ticker: str, ticker: str) -> str:
    """Extracts short crypto asset symbol (e.g. 'BTC', 'ETH', 'SOL', 'ADA', 'TON', etc.)."""
    combined = f"{series_ticker} {ticker}".upper()
    for symbol in ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "TON", "BCH", "NEAR", "ZEC"]:
        if symbol in combined:
            return symbol
    return series_ticker or "CRYPTO"


def extract_price_cents(market: dict, side: str, kalshi_client: KalshiClient | None = None) -> int | None:
    """
    Extracts the price in cents (1-99) for either 'yes' or 'no' from a Kalshi market dict.
    Supports dollar string fields (e.g. '0.3500') and integer cent fields.
    If market summary returns None, attempts a fallback API call for fresh market detail.
    """
    side_clean = side.lower()

    if side_clean == "yes":
        dollar_str = market.get("yes_ask_dollars") or market.get("yes_bid_dollars")
        cent_val = market.get("yes_ask") or market.get("yes_bid") or market.get("last_price")
    else:
        dollar_str = market.get("no_ask_dollars") or market.get("no_bid_dollars")
        cent_val = market.get("no_ask") or market.get("no_bid")

    # Fallback: If prices are None in summary payload, fetch detailed single-market object
    if dollar_str is None and cent_val is None and kalshi_client is not None:
        ticker = market.get("ticker")
        if ticker:
            try:
                detail = kalshi_client.get_market(ticker).get("market", {})
                if side_clean == "yes":
                    dollar_str = detail.get("yes_ask_dollars") or detail.get("yes_bid_dollars")
                    cent_val = detail.get("yes_ask") or detail.get("yes_bid") or detail.get("last_price")
                else:
                    dollar_str = detail.get("no_ask_dollars") or detail.get("no_bid_dollars")
                    cent_val = detail.get("no_ask") or detail.get("no_bid")
            except Exception as err:
                log.debug(f"Fallback get_market detail failed for {ticker}: {err}")

    if dollar_str is not None:
        try:
            val = int(round(float(dollar_str) * 100))
            if 1 <= val <= 99:
                return val
        except (ValueError, TypeError):
            pass

    if cent_val is not None:
        try:
            val = int(cent_val)
            if 1 <= val <= 99:
                return val
        except (ValueError, TypeError):
            pass

    return None


def get_crypto_spot_price(symbol: str) -> float | None:
    """
    Fetches the current real-time underlying spot price for a crypto asset (e.g., BTC, ETH, SOL).
    Uses public API endpoints (Coinbase / Binance) with fast timeout handling.
    """
    import requests
    symbol_upper = symbol.upper()

    try:
        url = f"https://api.coinbase.com/v2/prices/{symbol_upper}-USD/spot"
        resp = requests.get(url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            return round(float(data["data"]["amount"]), 4)
    except Exception as error:
        log.debug(f"Could not fetch spot price for {symbol_upper}: {error}")
    return None


def parse_cents(val_dollars, val_cents) -> int | None:
    """Parses cent values (1-99) from either string dollars (e.g. '0.3500') or integer cents."""
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


def determine_counterfactual_outcome(
    trade_side: str,
    action_type: str,
    spot_price: float | None,
    target_price: float | None,
    market_result: str | None = None,
) -> str:
    """
    Evaluates counterfactual post-mortem trade outcomes:
    1. 'sold_early_would_have_won': Sold early on Stop-Loss, but market ultimately won (False Alarm).
    2. 'sold_early_saved_from_loss': Exited early at 75¢ Take-Profit, but market ultimately lost (Saved from reversal).
    3. 'sl_cap_saved': Sold early on Stop-Loss, and market rightfully lost (Stop-loss capped loss).
    4. 'tp_full_win': Exited early at 75¢ Take-Profit, and market expired a winner.
    """
    winning_side = None
    if market_result in ("yes", "no"):
        winning_side = market_result.lower()
    elif spot_price is not None and target_price is not None:
        winning_side = "yes" if spot_price >= target_price else "no"

    if winning_side is None:
        return "pending_settlement"

    trade_would_have_won = (trade_side.lower() == winning_side)

    if action_type == "stop_loss_sold":
        if trade_would_have_won:
            return "sold_early_would_have_won"      # Scenario 1: Stop-loss sold, but would have won
        else:
            return "sl_cap_saved"                    # Rightful stop-loss saved full loss

    elif action_type == "take_profit_sold":
        if not trade_would_have_won:
            return "sold_early_saved_from_loss"     # Scenario 2: Take-profit sold, saved from loss
        else:
            return "tp_full_win"                     # Take-profit sold, and market won

    return "open_position"


def process_single_market(
    market: dict,
    is_trading_interval: bool,
    kalshi_client: KalshiClient,
    now_utc: datetime,
    minute_past_hour: int,
    dry_run: bool = False,
) -> dict:
    """
    Processes a single 15-minute crypto market:
    1. Reads YES/NO prices, orderbook depth (bids/asks), and bid-ask spread.
    2. Reads underlying spot price, target strike price to beat, price diff, and % distance.
    3. Calculates minutes remaining until contract expiration and market volume.
    4. Evaluates Stop-Loss (15¢ threshold) at 10m past start (:10, :25, :40, :55).
    5. Evaluates Buy strategy at 0m (25¢-45¢) and 5m (40¢-45¢).
    6. Logs counterfactual trade outcomes to MongoDB Atlas.
    """
    ticker = market.get("ticker", "UNKNOWN")
    series_ticker = market.get("series_ticker", "UNKNOWN")
    asset_symbol = extract_asset_symbol(series_ticker, ticker)

    # Extract prices in cents (with Kalshi client fallback for live prices)
    yes_price_cents = extract_price_cents(market, "yes", kalshi_client=kalshi_client)
    no_price_cents = extract_price_cents(market, "no", kalshi_client=kalshi_client)

    # Extract top-of-book quotes for YES and NO sides
    y_ask = parse_cents(market.get("yes_ask_dollars"), market.get("yes_ask"))
    y_bid = parse_cents(market.get("yes_bid_dollars"), market.get("yes_bid"))
    n_ask = parse_cents(market.get("no_ask_dollars"), market.get("no_ask"))
    n_bid = parse_cents(market.get("no_bid_dollars"), market.get("no_bid"))

    # Bid-Ask Spread in cents
    sprd = (y_ask - y_bid) if (y_ask is not None and y_bid is not None) else None

    # Underlying asset spot price, target strike price, difference ($), and % distance
    spot_price = get_crypto_spot_price(asset_symbol)
    target_price = market.get("floor_strike")

    price_diff = None
    pct_dist = None
    if spot_price is not None and target_price is not None:
        try:
            p_float = float(spot_price)
            tgt_float = float(target_price)
            price_diff = round(p_float - tgt_float, 4)
            if tgt_float > 0:
                pct_dist = round(((p_float - tgt_float) / tgt_float) * 100.0, 4)
        except (ValueError, TypeError):
            pass

    # Minutes remaining until contract expiration/close
    close_iso = market.get("close_time") or market.get("expected_expiration_time")
    rem_mins = None
    if close_iso:
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            rem_mins = round(max(0.0, (close_dt - now_utc).total_seconds() / 60.0), 1)
        except Exception:
            pass

    # Market traded volume
    vol_str = market.get("volume_fp") or market.get("volume_24h_fp")
    vol_count = None
    if vol_str is not None:
        try:
            vol_count = int(round(float(vol_str)))
        except (ValueError, TypeError):
            pass

    # EV Optimization Rule 2: Spot Momentum Gate Filter
    # - Buy YES only if spot price is ABOVE or EQUAL to strike target (spot_price >= target_price)
    # - Buy NO only if spot price is BELOW strike target (spot_price < target_price)
    spot_bullish = (spot_price is not None and target_price is not None and spot_price >= target_price)
    spot_bearish = (spot_price is not None and target_price is not None and spot_price < target_price)

    # Dynamic Buy Range Selection based on Market Age & Volatility:
    # - At 0m past market start (:00, :15, :30, :45): Buy Range: 35¢ - 45¢
    # - At 5m past market start (:05, :20, :35, :50): Tighter Buy Range: 40¢ - 45¢
    # - At 10m past market start (:10, :25, :40, :55): Evaluate Stop-Loss (Sell if loss >= 10¢)
    minute_into_market = minute_past_hour % 15
    is_stop_loss_interval = (minute_into_market == 10)

    if minute_into_market == 5:
        min_buy_price = 40
        max_buy_price = 45
    else:
        min_buy_price = 35
        max_buy_price = 45

    trade_executed = False
    trade_details = None

    # Step 2A: Stop-Loss Evaluation (Executed at 10m past market start: :10, :25, :40, :55)
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
            cf_outcome = determine_counterfactual_outcome(
                trade_side=sl_side,
                action_type="stop_loss_sold",
                spot_price=spot_price,
                target_price=target_price,
                market_result=market.get("result"),
            )
            trade_details = {
                "action": "stop_loss_sold",
                "side": sl_side,
                "exit_price": sl_res.get("exit_price"),
                "loss_cents": sl_res.get("loss_cents"),
                "outcome": cf_outcome,
            }

    # Step 2B: Buy Trading Decision (Allowed at 0m and 5m past market start: :00, :05, :15, :20, :30, :35, :45, :50)
    if is_trading_interval:
        # Open Position Gate: Check if we ALREADY hold an active position or resting order for this market
        has_existing_position = kalshi_client.has_existing_position_or_order(ticker)

        if has_existing_position:
            log.info(
                f"🛡️ [OPEN POSITION GATE] Already hold an active position or order for {ticker}. "
                f"Skipping new buy order."
            )
        else:
            # Check YES price condition AND Spot Momentum Filter (spot >= target)
            if yes_price_cents is not None and min_buy_price <= yes_price_cents <= max_buy_price and spot_bullish:
                if dry_run:
                    log.info(
                        f"🎯 [DRY-RUN MATCH] {ticker} -> YES price is {yes_price_cents}¢ "
                        f"(between {min_buy_price}¢ and {max_buy_price}¢ & Bullish Spot Momentum!). WOULD BUY 1 SHARE OF [YES] + SET 82¢ AUTO-SELL."
                    )
                    trade_executed = True
                    trade_details = {
                        "dry_run": True,
                        "action": "buy_opened",
                        "side": "yes",
                        "buy": yes_price_cents,
                        "tp": 82,
                        "sl": 10,
                        "outcome": determine_counterfactual_outcome(
                            trade_side="yes",
                            action_type="buy_opened",
                            spot_price=spot_price,
                            target_price=target_price,
                            market_result=market.get("result"),
                        ),
                    }
                else:
                    log.info(
                        f"⚡ [STRATEGY MATCH] {ticker} YES price is {yes_price_cents}¢ "
                        f"(between {min_buy_price}¢ and {max_buy_price}¢ & Bullish Spot Momentum!). Executing Maker Buy + 82¢ Auto-Sell!"
                    )
                    try:
                        result = kalshi_client.buy_and_set_take_profit(
                            ticker=ticker,
                            side="yes",
                            buy_price_cents=yes_price_cents,
                            take_profit_cents=82,
                            count=1,
                            maker_bid_cents=y_bid,
                        )
                        trade_executed = True
                        trade_details = {
                            "action": "buy_opened",
                            "side": "yes",
                            "buy": yes_price_cents,
                            "tp": 82,
                            "sl": 10,
                            "outcome": determine_counterfactual_outcome(
                                trade_side="yes",
                                action_type="buy_opened",
                                spot_price=spot_price,
                                target_price=target_price,
                                market_result=market.get("result"),
                            ),
                        }
                    except Exception as error:
                        log.error(f"❌ Failed to execute YES trade on {ticker}: {error}")

            # Check NO price condition AND Spot Momentum Filter (spot < target)
            elif no_price_cents is not None and min_buy_price <= no_price_cents <= max_buy_price and spot_bearish:
                if dry_run:
                    log.info(
                        f"🎯 [DRY-RUN MATCH] {ticker} -> NO price is {no_price_cents}¢ "
                        f"(between {min_buy_price}¢ and {max_buy_price}¢ & Bearish Spot Momentum!). WOULD BUY 1 SHARE OF [NO] + SET 82¢ AUTO-SELL."
                    )
                    trade_executed = True
                    trade_details = {
                        "dry_run": True,
                        "action": "buy_opened",
                        "side": "no",
                        "buy": no_price_cents,
                        "tp": 82,
                        "sl": 10,
                        "outcome": determine_counterfactual_outcome(
                            trade_side="no",
                            action_type="buy_opened",
                            spot_price=spot_price,
                            target_price=target_price,
                            market_result=market.get("result"),
                        ),
                    }
                else:
                    log.info(
                        f"⚡ [STRATEGY MATCH] {ticker} NO price is {no_price_cents}¢ "
                        f"(between {min_buy_price}¢ and {max_buy_price}¢ & Bearish Spot Momentum!). Executing Maker Buy + 82¢ Auto-Sell!"
                    )
                    try:
                        result = kalshi_client.buy_and_set_take_profit(
                            ticker=ticker,
                            side="no",
                            buy_price_cents=no_price_cents,
                            take_profit_cents=82,
                            count=1,
                            maker_bid_cents=n_bid,
                        )
                        trade_executed = True
                        trade_details = {
                            "action": "buy_opened",
                            "side": "no",
                            "buy": no_price_cents,
                            "tp": 82,
                            "sl": 10,
                            "outcome": determine_counterfactual_outcome(
                                trade_side="no",
                                action_type="buy_opened",
                                spot_price=spot_price,
                                target_price=target_price,
                                market_result=market.get("result"),
                            ),
                        }
                    except Exception as error:
                        log.error(f"❌ Failed to execute NO trade on {ticker}: {error}")

    # Step 3: Build expanded ultra-compact MongoDB snapshot document (~100 bytes)
    compact_document = {
        "ts": int(now_utc.timestamp()),   # Unix epoch timestamp (int)
        "m": minute_past_hour,            # Minute int (e.g. 0, 5, 15)
        "sym": asset_symbol,              # Asset symbol ("BTC", "ETH", "SOL", etc.)
        "tick": ticker,                   # Full market ticker
        "y": yes_price_cents,             # YES price in cents
        "n": no_price_cents,              # NO price in cents
        "y_bid": y_bid,                   # YES top bid in cents
        "y_ask": y_ask,                   # YES top ask in cents
        "n_bid": n_bid,                   # NO top bid in cents
        "n_ask": n_ask,                   # NO top ask in cents
        "sprd": sprd,                     # YES Bid-Ask spread in cents
        "p": spot_price,                  # Live underlying asset spot price (e.g. 63270.94)
        "tgt": target_price,              # Target strike price to beat (e.g. 63318.74)
        "diff": price_diff,               # Spot - Strike ($ diff)
        "pct": pct_dist,                  # Spot - Strike (% diff)
        "rem": rem_mins,                  # Minutes remaining to contract expiration
        "vol": vol_count,                 # Market contract volume
    }

    if trade_executed and trade_details:
        compact_document["trd"] = trade_details

    return compact_document


def run_pipeline_pass(dry_run: bool = False, force_trade_mark: bool = False):
    """
    Executes a single market processing & trading pass.
    """
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute
    timestamp_iso = now_utc.isoformat()

    # Schedule breakdown:
    # 1. Trading marks (0m & 5m past market start): :00, :05, :15, :20, :30, :35, :45, :50
    # 2. Stop-loss evaluation mark (10m past market start): :10, :25, :40, :55
    minute_into_market = current_minute % 15
    is_trading_interval = (minute_into_market in (0, 5)) or force_trade_mark
    is_stop_loss_interval = (minute_into_market == 10)

    log.info("=================================================================")
    log.info(f"RUNNING BOT AT UTC TIME: {timestamp_iso}")
    log.info(f"Current Minute Past Hour: :{current_minute:02d}:{now_utc.second:02d}")

    if dry_run:
        log.info("--> MODE: [DRY-RUN PREVIEW] (No real orders will be placed)")

    if is_trading_interval:
        log.info("--> INTERVAL: BUY TRADING & DATA COLLECTION MARK (:00, :05, :15, :20, :30, :35, :45, :50)")
    elif is_stop_loss_interval:
        log.info("--> INTERVAL: STOP-LOSS EVALUATION & DATA COLLECTION MARK (:10, :25, :40, :55)")
    else:
        log.info("--> INTERVAL: DATA COLLECTION ONLY MARK")
    log.info("=================================================================")

    # Initialize Kalshi API Client
    try:
        client = KalshiClient()
    except Exception as error:
        log.error(f"Could not initialize KalshiClient: {error}")
        return

    # Query active 15-minute crypto markets
    log.info("Fetching active 15-minute crypto markets from Kalshi...")
    try:
        markets = client.get_15m_crypto_markets(status="open")
        log.info(f"Retrieved {len(markets)} active 15-minute crypto markets.")
    except Exception as error:
        log.error(f"Failed to fetch 15-minute crypto markets: {error}")
        return

    # Process each market: check buy conditions and collect snapshots
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

        ticker = snapshot["tick"]
        sym = snapshot["sym"]
        y_cents = snapshot["y"]
        n_cents = snapshot["n"]
        p_val = snapshot.get("p")
        tgt_val = snapshot.get("tgt")
        diff_val = snapshot.get("diff")
        log.info(
            f"  • Market [{sym} | {ticker}] -> YES: {y_cents}¢ | NO: {n_cents}¢ | "
            f"Price: ${p_val} | Target: ${tgt_val} | Diff: {diff_val}"
        )

    # Filter out incomplete snapshots (where both YES and NO prices are None)
    valid_snapshots = [s for s in snapshots_to_save if s.get("y") is not None or s.get("n") is not None]

    if valid_snapshots:
        log.info("\nSaving ultra-compact market snapshot data to MongoDB Atlas...")
        save_market_snapshots(valid_snapshots)
    else:
        log.warning("\nNo valid price snapshots retrieved in this pass. Skipping MongoDB Atlas save.")
    log.info("Pass complete.")


def main():
    """
    Main bot execution loop.
    Runs continuously on Render Background Worker by default, aligning to 30s past 5-minute marks.
    Use --once for a single pass or --dry-run for preview mode.
    """
    args = sys.argv[1:]
    dry_run = "--dry-run" in args or "-d" in args
    force_trade_mark = "--force-trade" in args or "-f" in args
    run_once = "--once" in args or "-o" in args

    if run_once or dry_run:
        # Run a single pass and exit (useful for testing or single-run execution)
        run_pipeline_pass(dry_run=dry_run, force_trade_mark=force_trade_mark)
    else:
        # Continuous Render Background Worker loop
        log.info("Starting Trading Bot in Continuous Render Background Worker Mode...")
        while True:
            try:
                run_pipeline_pass(dry_run=dry_run, force_trade_mark=force_trade_mark)
            except Exception as error:
                log.error(f"Unexpected error in loop pass: {error}")

            # Calculate exact seconds to sleep until 30 seconds past the next 5-minute mark
            sleep_seconds = calculate_seconds_until_next_5min_mark(offset_seconds=30.0)
            next_mark_min = (datetime.now(timezone.utc).minute // 5 * 5 + 5) % 60
            log.info(
                f"\n[CLOCK ALIGNMENT] Sleeping {sleep_seconds:.1f}s until 30s past next 5-minute mark (:{next_mark_min:02d}:30)...\n"
            )
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
