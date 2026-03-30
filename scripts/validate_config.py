#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_config


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def main() -> int:
    cfg = get_config() or {}
    strategy = cfg.get("strategy") or {}
    backtest = cfg.get("backtest") or {}
    markets = cfg.get("markets") or []

    errors: list[str] = []
    warnings: list[str] = []

    min_edge = _as_float(strategy.get("min_edge"), 0.15)
    min_edge_easy_win = _as_float(strategy.get("min_edge_easy_win"), 0.01)
    min_fee_per_order = _as_float(strategy.get("min_fee_per_order"), 0.01)
    min_yes_price = _as_float(strategy.get("min_yes_price"), 0.05)
    min_no_price = _as_float(strategy.get("min_no_price"), 0.05)
    limit_order_mode = str(strategy.get("limit_order_mode", "fixed_pct")).strip().lower()
    tick_offset = _as_int(strategy.get("tick_offset"), 1)
    max_inventory_skew_pct = _as_float(strategy.get("max_inventory_skew_pct"), 0.10)
    min_stability_duration_seconds = _as_int(strategy.get("min_stability_duration_seconds"), 0)
    min_order_book_depth = _as_int(strategy.get("min_order_book_depth"), 0)
    max_pct_of_book_volume = _as_float(strategy.get("max_pct_of_book_volume"), 0)
    execution_cfg = cfg.get("execution") or {}
    near_expiry_hours = execution_cfg.get("near_expiry_aggressive_hours", strategy.get("near_expiry_aggressive_hours"))
    layered_order_levels = _as_int(strategy.get("layered_order_levels"), 3)
    limit_order_ttl_minutes = _as_int(strategy.get("limit_order_ttl_minutes"), 30)
    arb_inter_require_group_preflight = strategy.get("arb_inter_require_group_preflight", True)
    inter_arb_balance_aware_routing = strategy.get("inter_arb_balance_aware_routing", True)
    arb_inter_allow_single_leg_fallback = strategy.get("arb_inter_allow_single_leg_fallback", True)
    vol_model = str(strategy.get("vol_model", "auto")).strip().lower()
    garch_min_returns = _as_int(strategy.get("garch_min_returns"), 80)
    defensive_vol_ratio_threshold = _as_float(strategy.get("defensive_vol_ratio_threshold"), 3.0)
    stability_vol_ratio_ref = _as_float(strategy.get("stability_vol_ratio_ref"), 1.0)
    stability_max_multiplier = _as_float(strategy.get("stability_max_multiplier"), 10.0)
    vol_regime_edge_mult_per_x = _as_float(strategy.get("vol_regime_edge_mult_per_x"), 0.35)
    vol_regime_max_edge_mult = _as_float(strategy.get("vol_regime_max_edge_mult"), 2.5)
    vol_regime_dip_mult_per_x = _as_float(strategy.get("vol_regime_dip_mult_per_x"), 0.50)
    vol_regime_max_dip_mult = _as_float(strategy.get("vol_regime_max_dip_mult"), 2.0)
    vol_regime_size_mult_per_x = _as_float(strategy.get("vol_regime_size_mult_per_x"), 0.50)
    vol_regime_min_size_mult = _as_float(strategy.get("vol_regime_min_size_mult"), 0.35)
    cross_exchange_max_yes_gap = _as_float(strategy.get("cross_exchange_max_yes_gap"), 0.08)
    cross_exchange_spot_move_threshold_pct = _as_float(strategy.get("cross_exchange_spot_move_threshold_pct"), 0.01)

    kelly = _as_float(strategy.get("kelly_fraction"), 0.25)
    kelly_floor = _as_float(strategy.get("dynamic_kelly_floor"), 0.10)
    kelly_stress = _as_float(strategy.get("dynamic_kelly_stress_fraction"), 0.10)

    ewma_lambda = _as_float(strategy.get("ewma_lambda"), 0.94)
    intraday_ewma_lambda = _as_float(strategy.get("intraday_ewma_lambda"), ewma_lambda)
    intraday_other_min_p_win = _as_float(strategy.get("intraday_other_min_p_win"), 0.90)
    intraday_other_min_p_win_with_vol = _as_float(strategy.get("intraday_other_min_p_win_with_vol"), 0.80)

    max_cross_platform_exposure = _as_float(strategy.get("max_cross_platform_exposure"), 0.30)
    max_daily_loss_pct = _as_float(strategy.get("max_daily_loss_pct"), 0.10)
    two_sided_quote_min_edge = _as_float(strategy.get("two_sided_quote_min_edge"), 0.02)
    macro_signal_tickers = strategy.get("macro_signal_tickers") or []
    poll_interval_seconds = _as_int(cfg.get("poll_interval_seconds"), 60)
    synthetic_liquidity_friction_cents = _as_float(backtest.get("synthetic_liquidity_friction_cents"), 0.75)

    if not markets:
        errors.append("`markets` must contain at least one series ticker.")
    if poll_interval_seconds <= 0:
        errors.append("`poll_interval_seconds` must be > 0.")

    if not (0.0 <= min_yes_price <= 1.0):
        errors.append("`strategy.min_yes_price` must be in [0, 1].")
    if not (0.0 <= min_no_price <= 1.0):
        errors.append("`strategy.min_no_price` must be in [0, 1].")
    max_buy_limit_price = _as_float(strategy.get("max_buy_limit_price"), 0)
    if max_buy_limit_price < 0 or max_buy_limit_price > 1.0:
        errors.append("`strategy.max_buy_limit_price` must be in [0, 1] (0 disables cap).")
    if min_edge <= 0:
        errors.append("`strategy.min_edge` must be > 0.")
    if min_edge_easy_win < 0:
        errors.append("`strategy.min_edge_easy_win` must be >= 0.")
    if min_edge_easy_win > min_edge:
        errors.append("`strategy.min_edge_easy_win` should be <= `strategy.min_edge`.")
    consensus_sniper_enabled = bool(strategy.get("consensus_sniper_enabled", False))
    consensus_sniper_min_edge = _as_float(strategy.get("consensus_sniper_min_edge"), -0.01)
    barbell_enabled = bool(strategy.get("barbell_enabled", False))
    if consensus_sniper_enabled and not barbell_enabled:
        warnings.append(
            "`strategy.consensus_sniper_enabled` is true but `strategy.barbell_enabled` is false; "
            "consensus sniper is only applied when barbell is enabled."
        )
    if consensus_sniper_min_edge < -0.05:
        errors.append("`strategy.consensus_sniper_min_edge` must be >= -0.05 (cap on fee-drag tolerance).")
    if consensus_sniper_min_edge > min_edge:
        errors.append("`strategy.consensus_sniper_min_edge` should be <= `strategy.min_edge`.")
    barbell_soften_strict = bool(strategy.get("barbell_soften_strict_intraday", True))
    barbell_relaxed_p = _as_float(strategy.get("barbell_strict_relaxed_min_p_win"), 0.72)
    if not (0.01 <= barbell_relaxed_p <= 0.99):
        errors.append("`strategy.barbell_strict_relaxed_min_p_win` must be in [0.01, 0.99].")
    if barbell_soften_strict and not barbell_enabled:
        warnings.append(
            "`strategy.barbell_soften_strict_intraday` is true but `strategy.barbell_enabled` is false; "
            "strict-path softening is skipped until barbell is enabled."
        )
    if limit_order_mode not in {"fixed_pct", "dynamic_tick", "layered"}:
        errors.append("`strategy.limit_order_mode` must be one of: fixed_pct, dynamic_tick, layered.")
    if tick_offset < 1:
        errors.append("`strategy.tick_offset` must be >= 1.")
    if not (0.0 <= max_inventory_skew_pct <= 1.0):
        errors.append("`strategy.max_inventory_skew_pct` must be in [0, 1].")
    max_open_contracts_per_underlying = _as_int(strategy.get("max_open_contracts_per_underlying"), 0)
    if max_open_contracts_per_underlying < 0:
        errors.append("`strategy.max_open_contracts_per_underlying` must be >= 0 (0 disables per-underlying cap).")
    if min_stability_duration_seconds < 0:
        errors.append("`strategy.min_stability_duration_seconds` must be >= 0.")
    if min_order_book_depth < 0:
        errors.append("`strategy.min_order_book_depth` must be >= 0.")
    max_spread_pct = _as_float(strategy.get("max_spread_pct"), 0.20)
    if not (0.01 <= max_spread_pct <= 1.0):
        errors.append("`strategy.max_spread_pct` must be in [0.01, 1.0].")
    stop_loss_cooldown_hours = _as_float(strategy.get("stop_loss_cooldown_hours"), 0.0)
    if stop_loss_cooldown_hours < 0:
        errors.append("`strategy.stop_loss_cooldown_hours` must be >= 0.")
    if max_pct_of_book_volume < 0 or max_pct_of_book_volume > 1.0:
        errors.append("`strategy.max_pct_of_book_volume` must be in [0, 1] (0 disables book-% cap).")
    if near_expiry_hours is not None:
        neh = _as_float(near_expiry_hours, -1.0)
        if neh < 0 or neh > 168.0:
            errors.append("`execution.near_expiry_aggressive_hours` / `strategy.near_expiry_aggressive_hours` must be in [0, 168].")
    if layered_order_levels < 1:
        errors.append("`strategy.layered_order_levels` must be >= 1.")
    if limit_order_ttl_minutes < 0:
        errors.append("`strategy.limit_order_ttl_minutes` must be >= 0.")
    if not isinstance(arb_inter_require_group_preflight, bool):
        errors.append("`strategy.arb_inter_require_group_preflight` must be true or false.")
    if not isinstance(inter_arb_balance_aware_routing, bool):
        errors.append("`strategy.inter_arb_balance_aware_routing` must be true or false.")
    if not isinstance(arb_inter_allow_single_leg_fallback, bool):
        errors.append("`strategy.arb_inter_allow_single_leg_fallback` must be true or false.")
    log_decision_diagnostics = strategy.get("log_decision_diagnostics", False)
    if not isinstance(log_decision_diagnostics, bool):
        errors.append("`strategy.log_decision_diagnostics` must be true or false.")
    filter_disjoint = strategy.get("filter_disjoint_underlying_buys", False)
    if not isinstance(filter_disjoint, bool):
        errors.append("`strategy.filter_disjoint_underlying_buys` must be true or false.")
    if vol_model not in {"auto", "ewma", "garch", "sample"}:
        errors.append("`strategy.vol_model` must be one of: auto, ewma, garch, sample.")
    if garch_min_returns < 30:
        errors.append("`strategy.garch_min_returns` must be >= 30.")
    if defensive_vol_ratio_threshold < 1.0:
        errors.append("`strategy.defensive_vol_ratio_threshold` must be >= 1.0.")
    if stability_vol_ratio_ref <= 0:
        errors.append("`strategy.stability_vol_ratio_ref` must be > 0.")
    if stability_max_multiplier < 1.0:
        errors.append("`strategy.stability_max_multiplier` must be >= 1.0.")
    if vol_regime_edge_mult_per_x < 0 or vol_regime_dip_mult_per_x < 0 or vol_regime_size_mult_per_x < 0:
        errors.append("`strategy.vol_regime_*_per_x` slopes must be >= 0.")
    if vol_regime_max_edge_mult < 1.0:
        errors.append("`strategy.vol_regime_max_edge_mult` must be >= 1.0.")
    if vol_regime_max_dip_mult < 1.0:
        errors.append("`strategy.vol_regime_max_dip_mult` must be >= 1.0.")
    if not (0.0 < vol_regime_min_size_mult <= 1.0):
        errors.append("`strategy.vol_regime_min_size_mult` must be in (0, 1].")
    if not (0.0 <= cross_exchange_max_yes_gap <= 1.0):
        errors.append("`strategy.cross_exchange_max_yes_gap` must be in [0, 1].")
    if not (0.0 <= cross_exchange_spot_move_threshold_pct <= 1.0):
        errors.append("`strategy.cross_exchange_spot_move_threshold_pct` must be in [0, 1].")

    # Keep easy-win edge above a practical fee/slippage floor.
    fee_floor = max(min_fee_per_order, 0.01)
    if min_edge_easy_win < fee_floor:
        warnings.append(
            f"`strategy.min_edge_easy_win` ({min_edge_easy_win:.3f}) is below fee floor "
            f"({fee_floor:.3f}); fills may be unprofitable after rounding."
        )
    if consensus_sniper_min_edge > 0 and consensus_sniper_min_edge < fee_floor:
        warnings.append(
            f"`strategy.consensus_sniper_min_edge` ({consensus_sniper_min_edge:.3f}) is below fee floor "
            f"({fee_floor:.3f}); fills may be unprofitable after rounding."
        )
    if consensus_sniper_min_edge < 0:
        warnings.append(
            f"`strategy.consensus_sniper_min_edge` is negative ({consensus_sniper_min_edge:.3f}); "
            "consensus-shaped trades may clear despite small negative net edge after fees — monitor PnL."
        )

    # Guardrail: min edge should exceed a conservative fee + friction floor.
    friction_floor = (synthetic_liquidity_friction_cents / 100.0) + fee_floor
    if min_edge < friction_floor:
        warnings.append(
            f"`strategy.min_edge` ({min_edge:.3f}) is below fee+friction guardrail "
            f"({friction_floor:.3f}); consider raising edge or reducing friction assumptions."
        )

    if not (0.0 < kelly <= 1.0):
        errors.append("`strategy.kelly_fraction` must be in (0, 1].")
    if not (0.0 <= kelly_floor <= kelly):
        errors.append("`strategy.dynamic_kelly_floor` must be in [0, kelly_fraction].")
    if not (0.0 <= kelly_stress <= kelly):
        errors.append("`strategy.dynamic_kelly_stress_fraction` must be in [0, kelly_fraction].")

    if not (0.0 < ewma_lambda < 1.0):
        errors.append("`strategy.ewma_lambda` must be in (0, 1).")
    if not (0.0 < intraday_ewma_lambda < 1.0):
        errors.append("`strategy.intraday_ewma_lambda` must be in (0, 1).")
    if not (0.0 <= intraday_other_min_p_win <= 1.0):
        errors.append("`strategy.intraday_other_min_p_win` must be in [0, 1].")
    if not (0.0 <= intraday_other_min_p_win_with_vol <= 1.0):
        errors.append("`strategy.intraday_other_min_p_win_with_vol` must be in [0, 1].")
    if intraday_ewma_lambda > ewma_lambda:
        warnings.append(
            "`strategy.intraday_ewma_lambda` is higher than `strategy.ewma_lambda`; "
            "this makes intraday volatility slower than daily."
        )

    if not (0.0 <= max_cross_platform_exposure <= 1.0):
        errors.append("`strategy.max_cross_platform_exposure` must be in [0, 1].")

    max_bankroll_frac_ul = _as_float(strategy.get("max_bankroll_fraction_per_underlying"), 0.0)
    max_bankroll_frac_dir = _as_float(strategy.get("max_bankroll_fraction_per_direction"), 0.0)
    if max_bankroll_frac_ul < 0 or max_bankroll_frac_ul > 1.0:
        errors.append("`strategy.max_bankroll_fraction_per_underlying` must be in [0, 1] (0 disables).")
    if max_bankroll_frac_dir < 0 or max_bankroll_frac_dir > 1.0:
        errors.append("`strategy.max_bankroll_fraction_per_direction` must be in [0, 1] (0 disables).")

    _lb_raw = strategy.get("limit_buy_passive_only_below_edge")
    if _lb_raw is None:
        _lb_raw = execution_cfg.get("limit_buy_passive_only_below_edge")
    lb_passive = _as_float(_lb_raw, 0.0)
    if lb_passive < 0 or lb_passive > 0.95:
        errors.append("`strategy.limit_buy_passive_only_below_edge` / `execution.limit_buy_passive_only_below_edge` must be in [0, 0.95].")

    simple_ev = strategy.get("simple_ev_decision") or {}
    ev_boost_edge = _as_float(simple_ev.get("edge_size_boost_edge"), 0.0)
    ev_boost_mult = _as_float(simple_ev.get("edge_size_boost_mult"), 1.0)
    if ev_boost_edge < 0 or ev_boost_edge > 0.5:
        errors.append("`strategy.simple_ev_decision.edge_size_boost_edge` must be in [0, 0.5] (0 disables).")
    if ev_boost_mult < 1.0 or ev_boost_mult > 2.5:
        errors.append("`strategy.simple_ev_decision.edge_size_boost_mult` must be in [1.0, 2.5].")
    if not (0.0 <= max_daily_loss_pct <= 1.0):
        errors.append("`strategy.max_daily_loss_pct` must be in [0, 1].")

    cb_loss = _as_float(strategy.get("adaptive_loss_block_max_balance_loss_pct", 0.20), 0.20)
    if cb_loss < 0 or cb_loss > 1.0:
        errors.append("`strategy.adaptive_loss_block_max_balance_loss_pct` must be in [0, 1] (0 = disable).")
    cb_min_tr = _as_int(strategy.get("adaptive_loss_block_min_trades", 5), 5)
    if cb_min_tr < 1:
        errors.append("`strategy.adaptive_loss_block_min_trades` must be >= 1.")

    if strategy.get("enable_two_sided_quotes", True):
        if two_sided_quote_min_edge < 0:
            errors.append("`strategy.two_sided_quote_min_edge` must be >= 0.")
        if two_sided_quote_min_edge > min_edge:
            warnings.append(
                "`strategy.two_sided_quote_min_edge` is above `strategy.min_edge`; "
                "paired quote opportunities may be rare."
            )

    if strategy.get("macro_signal_enabled", True):
        if not isinstance(macro_signal_tickers, list) or not macro_signal_tickers:
            errors.append("`strategy.macro_signal_tickers` must be a non-empty list when macro signal is enabled.")

    min_payout_ratio = _as_float(strategy.get("min_payout_ratio"), 0)
    if min_payout_ratio < 0 or min_payout_ratio > 5.0:
        errors.append("`strategy.min_payout_ratio` must be in [0, 5] (0 disables).")
    dte_short = _as_float(strategy.get("dte_edge_short_threshold_days"), 1.0)
    dte_long = _as_float(strategy.get("dte_edge_long_threshold_days"), 3.0)
    if dte_short <= 0:
        errors.append("`strategy.dte_edge_short_threshold_days` must be > 0.")
    if dte_long < dte_short:
        errors.append("`strategy.dte_edge_long_threshold_days` must be >= dte_edge_short_threshold_days.")
    dte_mshort = _as_float(strategy.get("dte_edge_mult_short"), 0.88)
    dte_mlong = _as_float(strategy.get("dte_edge_mult_long"), 1.18)
    if dte_mshort <= 0 or dte_mlong <= 0:
        errors.append("`strategy.dte_edge_mult_short` and `dte_edge_mult_long` must be > 0.")
    max_new_buy_dte = _as_float(strategy.get("max_new_buy_dte_days"), 0)
    if max_new_buy_dte < 0:
        errors.append("`strategy.max_new_buy_dte_days` must be >= 0 (0 disables).")
    chase_min = _as_int(strategy.get("passive_bid_chase_refresh_minutes"), 0)
    if chase_min < 0:
        errors.append("`strategy.passive_bid_chase_refresh_minutes` must be >= 0.")

    flip_edge_buf = _as_float(strategy.get("position_flip_sell_min_negative_edge"), 0.0)
    if flip_edge_buf < 0 or flip_edge_buf > 0.99:
        errors.append("`strategy.position_flip_sell_min_negative_edge` must be in [0, 0.99] (0 = legacy flip sells).")
    p_lose_margin = _as_float(strategy.get("position_loser_sell_p_lose_margin"), 0.0)
    if p_lose_margin < 0 or p_lose_margin > 0.5:
        errors.append("`strategy.position_loser_sell_p_lose_margin` must be in [0, 0.5].")
    hold_itm_pct = _as_float(strategy.get("hold_winner_min_spot_vs_strike_pct"), 0.10)
    if hold_itm_pct < 0 or hold_itm_pct > 0.5:
        errors.append("`strategy.hold_winner_min_spot_vs_strike_pct` must be in [0, 0.5].")
    hold_max_dte = _as_float(strategy.get("hold_winner_max_dte_days"), 1.0)
    if hold_max_dte <= 0 or hold_max_dte > 30.0:
        errors.append("`strategy.hold_winner_max_dte_days` must be in (0, 30].")

    ultra_short_h = _as_float(strategy.get("ultra_short_dte_hours"), 0.0)
    if ultra_short_h < 0 or ultra_short_h > 168.0:
        errors.append("`strategy.ultra_short_dte_hours` must be in [0, 168] (0 = title/ticker ultra-short only).")
    for key, lo, hi in (
        ("ultra_short_min_p_win", 0.0, 1.0),
        ("ultra_short_min_p_win_with_vol", 0.0, 1.0),
    ):
        v = _as_float(strategy.get(key), 0.92)
        if v < lo or v > hi:
            errors.append(f"`strategy.{key}` must be in [{lo}, {hi}].")
    ultra_mult = _as_float(strategy.get("ultra_short_min_edge_mult"), 1.0)
    if ultra_mult < 1.0 or ultra_mult > 3.0:
        errors.append("`strategy.ultra_short_min_edge_mult` must be in [1.0, 3.0].")

    trading = cfg.get("trading") or {}
    dust_max = _as_int(trading.get("dust_trade_max_balance_cents"), 0)
    if dust_max < 0:
        errors.append("`trading.dust_trade_max_balance_cents` must be >= 0.")
    micro_max = _as_int(trading.get("micro_balance_max_balance_cents"), 0)
    if micro_max < 0:
        errors.append("`trading.micro_balance_max_balance_cents` must be >= 0 (0 = use dust cap).")
    min_bal_fb = _as_int(trading.get("min_balance_for_limit_fallback_cents"), 0)
    if min_bal_fb < 0:
        errors.append("`trading.min_balance_for_limit_fallback_cents` must be >= 0.")

    print("[Config Validation]")
    print(f"  errors={len(errors)} warnings={len(warnings)}")
    for msg in errors:
        print(f"  ERROR: {msg}")
    for msg in warnings:
        print(f"  WARN:  {msg}")

    if errors:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
