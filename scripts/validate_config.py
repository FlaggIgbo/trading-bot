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
    if min_edge <= 0:
        errors.append("`strategy.min_edge` must be > 0.")
    if min_edge_easy_win < 0:
        errors.append("`strategy.min_edge_easy_win` must be >= 0.")
    if min_edge_easy_win > min_edge:
        errors.append("`strategy.min_edge_easy_win` should be <= `strategy.min_edge`.")
    if limit_order_mode not in {"fixed_pct", "dynamic_tick", "layered"}:
        errors.append("`strategy.limit_order_mode` must be one of: fixed_pct, dynamic_tick, layered.")
    if tick_offset < 1:
        errors.append("`strategy.tick_offset` must be >= 1.")
    if not (0.0 <= max_inventory_skew_pct <= 1.0):
        errors.append("`strategy.max_inventory_skew_pct` must be in [0, 1].")
    if min_stability_duration_seconds < 0:
        errors.append("`strategy.min_stability_duration_seconds` must be >= 0.")
    if min_order_book_depth < 0:
        errors.append("`strategy.min_order_book_depth` must be >= 0.")
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
    if intraday_ewma_lambda > ewma_lambda:
        warnings.append(
            "`strategy.intraday_ewma_lambda` is higher than `strategy.ewma_lambda`; "
            "this makes intraday volatility slower than daily."
        )

    if not (0.0 <= max_cross_platform_exposure <= 1.0):
        errors.append("`strategy.max_cross_platform_exposure` must be in [0, 1].")
    if not (0.0 <= max_daily_loss_pct <= 1.0):
        errors.append("`strategy.max_daily_loss_pct` must be in [0, 1].")

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
