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

import argparse
import math
import random
import re
import sys
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

# Add project root to path so imports work when run from scripts/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import warnings

# Before any `requests`/Gemini import loads urllib3: don't `import urllib3` here — that emits the warning.
warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
    category=Warning,
)

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
    REC_EDGE_EXIT,
    REC_LIKELY_LOSE,
    REC_RULE_VIOLATION,
    REC_SKIP,
)
from execution_engine import sync_and_cancel_resting_orders
from fill_reconciliation import reconcile_confirmed_fills
from gemini_client import (
    GeminiPredictionClient,
    GeminiTradingClient,
    _event_to_asset,
    _extract_threshold_from_title,
    _get_no_contract,
    _get_yes_contract,
    expand_gemini_events_to_markets,
    extract_strike_from_event,
    get_instrument_symbol,
    get_no_price,
    get_yes_price,
)
from kalshi_client import KalshiClient
from ollama_analysis import analyze_single_trade, capture_and_analyze
from strategies import BaseStrategy, DefaultThresholdStrategy, VolRegimeStrategy
from models.anti_hallucination import (
    AntiHallucinationConfig,
    adaptive_min_edge,
    apply_anti_hallucination_pipeline,
    load_anti_hallucination_config,
    regime_filter,
)
from models.alpha_engine import (
    AlphaEngineConfig,
    compute_obi,
    lead_lag_edge_adjustment,
    lead_lag_signal,
    load_alpha_engine_config,
    obi_edge_adjustment,
    toxic_flow_edge_penalty,
    vol_adjusted_kelly,
)
from models.decision_diagnostics import build_required_edge_breakdown, print_decision_diagnostics_line
from models.trading_edges import (
    maker_fee_per_contract as _fee_per_contract,
    net_edges_buy_legs as _net_edges_buy_legs,
    simple_ev_spend_fraction,
)

try:
    from storage import (
        expire_unfilled_orders,
        get_calibration_stats,
        get_ewma_win_rate,
        get_fill_attribution_stats,
        get_last_snapshot_for_ticker,
        get_limit_order_analysis,
        get_loss_streak,
        get_market_snapshots_for_backtest,
        get_position_evaluation_stats,
        get_predictions_for_backtest,
        get_realized_pnl_stats,
        get_recent_performance,
        get_recent_performance_by_series,
        get_seconds_since_last_resolved_trade,
        get_price_after_buy_from_snapshots,
        get_unresolved_predictions,
        get_ticker_entry_counts,
        get_unresolved_contract_sums_by_platform_yf,
        mark_prediction_cancelled,
        record_position_evaluation,
        record_prediction_from_fill,
        record_prediction_early_exit,
        record_limit_order,
        record_market_snapshot,
        record_order_cancel,
        record_prediction_outcome,
        record_resolved,
        resolve_position_evaluations,
        get_markout_history,
    )
except ImportError:
    expire_unfilled_orders = get_calibration_stats = get_ewma_win_rate = get_fill_attribution_stats = get_last_snapshot_for_ticker = get_limit_order_analysis = get_loss_streak = get_market_snapshots_for_backtest = get_predictions_for_backtest = get_realized_pnl_stats = get_recent_performance = get_recent_performance_by_series = get_seconds_since_last_resolved_trade = get_unresolved_predictions = get_ticker_entry_counts = None
    get_position_evaluation_stats = get_price_after_buy_from_snapshots = mark_prediction_cancelled = record_position_evaluation = record_prediction_from_fill = record_prediction_early_exit = record_limit_order = record_market_snapshot = record_order_cancel = record_prediction_outcome = record_resolved = resolve_position_evaluations = get_markout_history = get_unresolved_contract_sums_by_platform_yf = None
from yfinance_client import (
    get_aggs as yf_get_aggs,
    get_intraday_aggs_at_hour as yf_get_intraday_aggs,
    get_snapshot as yf_get_snapshot,
)
from underlying_adjust import (
    apply_underlying_closes,
    apply_underlying_price,
    proxy_basis_extra_min_edge,
    proxy_strike_validation_tolerance_pct,
    underlying_adjust_params,
)

try:
    import numpy as np
except ImportError:
    np = None

try:
    from scipy.stats import norm as scipy_norm
    from scipy.stats import t as scipy_t
except ImportError:
    scipy_norm = None
    scipy_t = None

try:
    from arch import arch_model
except ImportError:
    arch_model = None

try:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
except (ImportError, OSError):
    # OSError: broken/missing extension libs in some minimal or mixed-arch installs
    IsotonicRegression = LogisticRegression = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Yahoo underlyings we fetch Gemini events for and evaluate (crypto + WTI crude = same CL=F as Kalshi KXWTI).
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
}
# When Yahoo ticker ≠ Gemini _event_to_asset key, map here for inter-arb / sanity / Gemini loop lookups.
GEMINI_ASSET_KEY_FOR_YFIN_TICKER: dict[str, str] = {
    "HYPE32196-USD": "HYPE-USD",
}

# US equity indices: same 4pm ET “regular close” intraday rules as ^GSPC (e.g. Kalshi Nasdaq-100).
_US_EQUITY_INDEX_YF_TICKERS = frozenset({"^GSPC", "GSPC", "^NDX", "NDX"})
# Yahoo continuous futures for Kalshi WTI / metals / RBOB (gas proxy) — allow titled intraday times.
_COMMODITY_FUTURES_YF_TICKERS = frozenset({"CL=F", "GC=F", "SI=F", "RB=F"})
# Kalshi maker fee override (index-like); match _maker_fee_rate_for_market.
_KALSHI_INDEX_MAKER_FEE_YF = frozenset({"^GSPC", "GSPC", "^NDX", "NDX", "SPY", "QQQ"})


def _yf_is_us_equity_index(yf_ticker: str) -> bool:
    return (yf_ticker or "").strip().upper() in _US_EQUITY_INDEX_YF_TICKERS


def _yf_is_commodity_future(yf_ticker: str) -> bool:
    return (yf_ticker or "").strip().upper() in _COMMODITY_FUTURES_YF_TICKERS


def _gemini_events_list_for_yf_ticker(events_by_asset: dict[str, list], yf_ticker: str) -> list:
    key = GEMINI_ASSET_KEY_FOR_YFIN_TICKER.get(yf_ticker, yf_ticker)
    return events_by_asset.get(key, [])


_GARCH_VAR_CACHE: dict[tuple[int, int, int], float] = {}


@dataclass
class _StrategyParams:
    """Consolidated strategy config from config.yaml. Load once, use throughout _run_impl."""

    days_to_expiry: float
    no_above_cushion: float
    sell_losing_pct: float
    limit_dip_pct: float
    limit_dip_pct_high_confidence: float
    limit_dip_pct_price_decline: float
    max_dip_cap: float
    limit_order_mode: str
    tick_offset: int
    max_inventory_skew_pct: float
    min_stability_duration_seconds: int
    min_order_book_depth: int
    # PnL: cap proposed BUY size to this fraction of same-side visible bid depth (0 = off)
    max_pct_of_book_volume: float
    # PnL: when time-to-resolution < this many hours, use fill-friendly dip quoting (overrides passive mid/bid_plus_tick)
    near_expiry_aggressive_hours: float
    cancel_on_vol_spike: bool
    cancel_all_on_regime_shift: bool
    limit_order_ttl_minutes: int
    layered_order_levels: int
    vol_blend_weight: float | None
    use_ewma_vol: bool
    ewma_lambda: float
    intraday_ewma_lambda: float
    intraday_other_min_p_win: float
    intraday_other_min_p_win_with_vol: float
    min_arb_fee: float
    min_fee_per_order: float
    min_yes_price: float
    min_no_price: float
    max_yes_price_for_buy: float
    max_buy_limit_price: float
    force_min_buy_contracts_one: bool
    # Anti–steamroller: min (profit_if_win / stake) = (1-price)/price on the bought side; 0 = off
    min_payout_ratio: float
    dte_edge_scaling_enabled: bool
    dte_edge_short_threshold_days: float
    dte_edge_long_threshold_days: float
    dte_edge_mult_short: float
    dte_edge_mult_long: float
    max_new_buy_dte_days: float  # 0 = off; skip directional BUY when DTE exceeds
    passive_bid_chase_enabled: bool
    passive_bid_chase_refresh_minutes: int
    max_buy_yes_strike_above_pct: float
    max_buy_no_strike_above_pct: float
    enable_intra_arb: bool
    enable_inter_arb: bool
    arb_inter_require_group_preflight: bool
    inter_arb_balance_aware_routing: bool
    arb_inter_allow_single_leg_fallback: bool
    kalshi_maker_fee_rate: float
    kalshi_sp500_maker_fee_rate: float
    gemini_maker_fee_rate: float
    absolute_winner_min_p_win: float
    absolute_winner_max_price: float
    absolute_winner_max_pct_of_balance: float
    absolute_winner_max_contracts: int
    use_calibration: bool
    favor_yes_when_above_strike: bool
    enforce_no_above_rule: bool
    min_calibration_samples: int
    calibration_min_win_rate_above_yes: float
    calibration_extra_edge_above_yes: float
    calibration_hard_block_above_yes_win_rate: float
    calibration_hard_block_above_yes_min_samples: int
    calibration_hard_block_above_yes_min_avg_entry: float
    calibration_extra_edge_overall: float
    min_calibration_samples_overall: int
    use_bucket_penalty: bool
    min_bucket_samples: int
    min_vol_confidence_far_otm: float
    enable_sell_and_rebuy: bool
    dynamic_sell_threshold: bool
    p_lose_blend_market_max_weight: float
    p_lose_blend_market_half_life_days: float
    # Position exits: loosen mean-reversion / EV sells without touching stop-loss or rule violations.
    position_flip_sell_min_negative_edge: float
    position_loser_sell_p_lose_margin: float
    hold_winner_to_expiry_enabled: bool
    hold_winner_min_spot_vs_strike_pct: float
    hold_winner_max_dte_days: float
    # Sub-hour / 15m contracts (e.g. Kalshi KXBTC15M): stricter buys when enabled.
    ultra_short_dte_hours: float
    ultra_short_min_p_win: float
    ultra_short_min_p_win_with_vol: float
    ultra_short_min_edge_mult: float
    ultra_short_late_minutes_threshold: float
    ultra_short_late_min_edge_mult: float
    use_intraday_empirical: bool
    dynamic_kelly_enabled: bool
    dynamic_kelly_floor: float
    dynamic_kelly_stress_fraction: float
    dynamic_kelly_stress_win_rate: float
    dynamic_kelly_recovery_win_rate: float
    enable_two_sided_quotes: bool
    two_sided_quote_min_edge: float
    macro_signal_enabled: bool
    macro_signal_tickers: list[str]
    enable_easy_win: bool
    min_edge_easy_win: float
    easy_win_min_p_win: float
    easy_win_min_market_p: float
    use_live_price: bool
    live_price_weight: float
    empirical_recent_days: int
    empirical_stress_recent_days: int
    empirical_stress_calibration_max_wr: float
    empirical_stress_min_samples: int
    empirical_recent_weight: float
    empirical_use_rolling_window: bool
    use_volatility_by_default: bool
    vol_dynamic_n0: float
    uncertainty_edge_mult: float
    stop_loss_pct: float
    weekend_edge_multiplier: float
    enable_momentum_protection: bool
    momentum_crash_threshold_pct: float
    max_cross_platform_exposure: float
    max_daily_loss_pct: float
    max_daily_loss_cents: int
    enable_vol_spike_safety: bool
    vol_spike_threshold_mult: float
    vol_spike_edge_multiplier: float
    parity_penalty_enabled: bool
    parity_lookback_days: int
    parity_base_penalty: float
    parity_max_penalty: float
    enable_strike_validation: bool
    strike_validation_tolerance_pct: float
    enable_otm_smile_adjustment: bool
    otm_smile_strength: float
    otm_smile_max_mult: float
    vol_distribution: str
    vol_t_df: float
    vol_model: str
    garch_min_returns: int
    liquidity_spread_bps_soft: float
    liquidity_spread_bps_hard: float
    liquidity_min_size_multiplier: float
    liquidity_missing_ask_multiplier: float
    enable_defensive_mode: bool
    defensive_vol_ratio_threshold: float
    enable_dynamic_stability_window: bool
    stability_vol_ratio_ref: float
    stability_max_multiplier: float
    enable_vol_regime_scaling: bool
    vol_regime_edge_mult_per_x: float
    vol_regime_max_edge_mult: float
    vol_regime_dip_mult_per_x: float
    vol_regime_max_dip_mult: float
    vol_regime_size_mult_per_x: float
    vol_regime_min_size_mult: float
    enable_cross_exchange_sanity_gate: bool
    cross_exchange_max_yes_gap: float
    cross_exchange_spot_move_threshold_pct: float
    enable_signal_model: bool
    signal_model_weights: dict[str, float]
    use_sklearn_calibration: bool
    sklearn_calibration_method: str
    sklearn_calibration_min_samples: int
    enable_auto_signal_weight_fit: bool
    signal_weight_fit_min_samples: int
    signal_weight_fit_l2_c: float
    far_dte_dip_scaling_enabled: bool
    far_dte_threshold_days: float
    far_dte_dip_scale_per_day: float
    far_dte_max_extra_dip: float
    far_dte_min_confidence: float
    far_dte_confidence_boost: float
    enable_disagreement_dip: bool
    disagreement_dip_min_gap: float
    disagreement_dip_min_p_win: float
    disagreement_dip_pct_of_bid: float
    disagreement_dip_min_price: float
    disagreement_dip_max_price: float
    disagreement_dip_contracts: int
    enable_lottery_limits: bool
    lottery_limit_min_p_win: float
    lottery_limit_price: float
    lottery_limit_contracts: int
    lottery_limit_price_mode: str
    lottery_limit_max_price: float
    lottery_penny_jump_apply_cap_bid_below: float
    lottery_market_yes_band_enabled: bool
    lottery_market_yes_band_lo: float
    lottery_market_yes_band_hi: float
    mc_enabled: bool
    mc_paths: int
    mc_blend_weight: float
    vol_disagree_threshold: float
    vol_disagree_extra_edge: float
    vol_disagree_dampen_blend: float
    joint_high_confidence_enabled: bool
    joint_high_emp_vol_threshold: float
    joint_high_pull_to_half: float
    intraday_vol_cap: float
    intraday_min_empirical_weight: float
    adaptive_learning_enabled: bool
    adaptive_edge_lookback_hours: float
    adaptive_edge_min_trades: int
    adaptive_edge_scale_per_pct: float
    adaptive_edge_max_extra: float
    loss_streak_threshold: int
    loss_streak_extra_edge: float
    loss_streak_max_extra: float
    series_cooldown_threshold: int
    series_cooldown_extra_edge: float
    ewma_halflife_trades: int
    ewma_edge_scale_per_pct: float
    adaptive_loss_block_max_balance_loss_pct: float
    adaptive_loss_block_lookback_hours: float
    adaptive_loss_block_min_trades: int
    adaptive_edge_idle_decay_start_hours: float
    adaptive_edge_idle_decay_halflife_hours: float
    adaptive_stress_size_edge_threshold: float
    adaptive_stress_size_min_mult: float
    # Barbell: base = high-P consensus; alpha = cheap + large net edge; middle zone skipped when enabled.
    barbell_enabled: bool
    barbell_base_min_market_yes: float
    barbell_base_min_model_p: float
    barbell_base_max_model_vs_market_gap: float
    barbell_base_max_yes_price: float
    barbell_base_max_no_price: float
    barbell_alpha_max_cheap_price: float
    barbell_alpha_min_model_p: float
    barbell_alpha_min_net_edge: float
    barbell_base_kelly_mult: float
    barbell_alpha_kelly_mult: float
    consensus_sniper_enabled: bool
    consensus_sniper_min_edge: float
    barbell_soften_strict_intraday: bool
    barbell_strict_relaxed_min_p_win: float
    yes_overconfidence_extra_edge: float
    model_probability_shrinkage: float
    passive_limit_quote: str
    empirical_weighting: str
    empirical_exponential_halflife_bars: int
    enable_model_prob_hard_cap: bool
    model_prob_hard_lo: float
    model_prob_hard_hi: float
    # Execution / exits (optional; many merged from execution: in config)
    position_exit_min_net_edge: float
    skip_new_buy_when_unresolved_same_side: bool
    limit_buy_edge_aggression_enabled: bool
    limit_buy_cross_spread_min_edge: float
    limit_buy_near_market_min_edge: float
    resting_cancel_if_market_bid_above_limit_by: float
    # Simple EV mode: single net-edge gate, Kelly sizing, uncertainty/calibration → size only.
    simple_ev_decision_enabled: bool
    simple_ev_min_edge: float
    simple_ev_kelly_dampen: float
    simple_ev_max_bankroll_fraction: float
    simple_ev_spread_wide_fraction: float
    simple_ev_spread_wide_size_mult: float
    simple_ev_emp_vol_disagree_threshold: float
    simple_ev_disagree_size_mult: float
    simple_ev_calibration_wr_threshold: float
    simple_ev_calibration_poor_size_mult: float
    simple_ev_min_bid_size: float
    simple_ev_ultra_short_size_mult: float
    # Simple-EV only: skip ambiguous model P(win) band; stricter net edge on 5–10¢ legs.
    simple_ev_skip_p_win_mid_enabled: bool
    simple_ev_skip_p_win_mid_lo: float
    simple_ev_skip_p_win_mid_hi: float
    simple_ev_cheap_band_lo: float
    simple_ev_cheap_band_hi: float
    simple_ev_cheap_band_min_net_edge: float
    log_effective_min_edge: bool
    log_decision_diagnostics: bool
    ah_cfg: AntiHallucinationConfig = None  # type: ignore[assignment]
    ae_cfg: AlphaEngineConfig = None  # type: ignore[assignment]


def _load_strategy_params(strategy: dict) -> _StrategyParams:
    return _StrategyParams(
        days_to_expiry=float(strategy.get("days_to_expiry", 30)),
        no_above_cushion=float(strategy.get("skip_buy_no_above_cushion", 0.01)),
        sell_losing_pct=float(strategy.get("sell_losing_pct", 0.55)),
        limit_dip_pct=float(strategy.get("limit_dip_pct", 0)),
        limit_dip_pct_high_confidence=float(strategy.get("limit_dip_pct_high_confidence", 0)),
        limit_dip_pct_price_decline=float(strategy.get("limit_dip_pct_price_decline", 0)),
        max_dip_cap=float(strategy.get("max_dip_cap", 0.25)),
        limit_order_mode=str(strategy.get("limit_order_mode", "fixed_pct")).strip().lower(),
        tick_offset=max(1, int(strategy.get("tick_offset", 1))),
        max_inventory_skew_pct=max(0.0, float(strategy.get("max_inventory_skew_pct", 0.10))),
        min_stability_duration_seconds=max(0, int(strategy.get("min_stability_duration_seconds", 0))),
        min_order_book_depth=max(0, int(strategy.get("min_order_book_depth", 0))),
        max_pct_of_book_volume=max(0.0, min(1.0, float(strategy.get("max_pct_of_book_volume", 0) or 0))),
        near_expiry_aggressive_hours=max(0.0, float(strategy.get("near_expiry_aggressive_hours", 0) or 0)),
        cancel_on_vol_spike=bool(strategy.get("cancel_on_vol_spike", True)),
        cancel_all_on_regime_shift=bool(strategy.get("cancel_all_on_regime_shift", True)),
        limit_order_ttl_minutes=max(0, int(strategy.get("limit_order_ttl_minutes", 30))),
        layered_order_levels=max(1, int(strategy.get("layered_order_levels", 3))),
        vol_blend_weight=strategy.get("vol_blend_weight"),
        use_ewma_vol=bool(strategy.get("use_ewma_vol", False)),
        ewma_lambda=float(strategy.get("ewma_lambda", 0.94)),
        intraday_ewma_lambda=float(strategy.get("intraday_ewma_lambda", strategy.get("ewma_lambda", 0.94))),
        intraday_other_min_p_win=float(strategy.get("intraday_other_min_p_win", 0.90)),
        intraday_other_min_p_win_with_vol=float(strategy.get("intraday_other_min_p_win_with_vol", 0.80)),
        min_arb_fee=float(strategy.get("min_arb_fee", 0.02)),
        min_fee_per_order=float(strategy.get("min_fee_per_order", 0.01)),
        min_yes_price=float(strategy.get("min_yes_price", 0.05)),
        min_no_price=float(strategy.get("min_no_price", 0.05)),
        max_yes_price_for_buy=float(strategy.get("max_yes_price_for_buy", 0.90)),
        max_buy_limit_price=max(0.0, min(1.0, float(strategy.get("max_buy_limit_price", 0) or 0))),
        force_min_buy_contracts_one=bool(strategy.get("force_min_buy_contracts_one", False)),
        min_payout_ratio=max(0.0, float(strategy.get("min_payout_ratio", 0) or 0)),
        dte_edge_scaling_enabled=bool(strategy.get("dte_edge_scaling_enabled", False)),
        dte_edge_short_threshold_days=max(1e-6, float(strategy.get("dte_edge_short_threshold_days", 1.0))),
        dte_edge_long_threshold_days=max(1e-3, float(strategy.get("dte_edge_long_threshold_days", 3.0))),
        dte_edge_mult_short=max(0.1, float(strategy.get("dte_edge_mult_short", 0.88))),
        dte_edge_mult_long=max(0.1, float(strategy.get("dte_edge_mult_long", 1.18))),
        max_new_buy_dte_days=max(0.0, float(strategy.get("max_new_buy_dte_days", 0) or 0)),
        passive_bid_chase_enabled=bool(strategy.get("passive_bid_chase_enabled", False)),
        passive_bid_chase_refresh_minutes=max(0, int(strategy.get("passive_bid_chase_refresh_minutes", 0) or 0)),
        max_buy_yes_strike_above_pct=float(strategy.get("max_buy_yes_strike_above_pct", 0.05)),
        max_buy_no_strike_above_pct=float(strategy.get("max_buy_no_strike_above_pct", 0.05)),
        enable_intra_arb=bool(strategy.get("enable_intra_arb", True)),
        enable_inter_arb=bool(strategy.get("enable_inter_arb", True)),
        arb_inter_require_group_preflight=bool(strategy.get("arb_inter_require_group_preflight", True)),
        inter_arb_balance_aware_routing=bool(strategy.get("inter_arb_balance_aware_routing", True)),
        arb_inter_allow_single_leg_fallback=bool(strategy.get("arb_inter_allow_single_leg_fallback", True)),
        kalshi_maker_fee_rate=float(strategy.get("kalshi_maker_fee_rate", 0.0175)),
        kalshi_sp500_maker_fee_rate=float(strategy.get("kalshi_sp500_maker_fee_rate", 0.00875)),
        gemini_maker_fee_rate=float(strategy.get("gemini_maker_fee_rate", 0.0175)),
        absolute_winner_min_p_win=float(strategy.get("absolute_winner_min_p_win", 0.95)),
        absolute_winner_max_price=float(strategy.get("absolute_winner_max_price", 0.05)),
        absolute_winner_max_pct_of_balance=float(strategy.get("absolute_winner_max_pct_of_balance", 0.50)),
        absolute_winner_max_contracts=int(strategy.get("absolute_winner_max_contracts", 500)),
        use_calibration=bool(strategy.get("use_calibration", True)),
        favor_yes_when_above_strike=bool(strategy.get("favor_yes_when_above_strike", False)),
        enforce_no_above_rule=bool(strategy.get("enforce_no_above_rule", False)),
        min_calibration_samples=int(strategy.get("min_calibration_samples", 5)),
        calibration_min_win_rate_above_yes=float(strategy.get("calibration_min_win_rate_above_yes", 0.45)),
        calibration_extra_edge_above_yes=float(strategy.get("calibration_extra_edge_above_yes", 0.05)),
        calibration_hard_block_above_yes_win_rate=float(strategy.get("calibration_hard_block_above_yes_win_rate", 0.0)),
        calibration_hard_block_above_yes_min_samples=max(
            1, int(strategy.get("calibration_hard_block_above_yes_min_samples", 30))
        ),
        calibration_hard_block_above_yes_min_avg_entry=max(
            0.0, min(1.0, float(strategy.get("calibration_hard_block_above_yes_min_avg_entry", 0.0)))
        ),
        calibration_extra_edge_overall=float(strategy.get("calibration_extra_edge_overall", 0.02)),
        min_calibration_samples_overall=int(strategy.get("min_calibration_samples_overall", 20)),
        use_bucket_penalty=bool(strategy.get("use_bucket_penalty", True)),
        min_bucket_samples=int(strategy.get("min_bucket_samples", 5)),
        min_vol_confidence_far_otm=float(strategy.get("min_vol_confidence_far_otm", 0.97)),
        enable_sell_and_rebuy=bool(strategy.get("enable_sell_and_rebuy", False)),
        dynamic_sell_threshold=bool(strategy.get("dynamic_sell_threshold", True)),
        p_lose_blend_market_max_weight=float(strategy.get("p_lose_blend_market_max_weight", 0.5)),
        p_lose_blend_market_half_life_days=float(strategy.get("p_lose_blend_market_half_life_days", 0.5)),
        position_flip_sell_min_negative_edge=float(strategy.get("position_flip_sell_min_negative_edge", 0.0)),
        position_loser_sell_p_lose_margin=float(strategy.get("position_loser_sell_p_lose_margin", 0.0)),
        hold_winner_to_expiry_enabled=bool(strategy.get("hold_winner_to_expiry_enabled", False)),
        hold_winner_min_spot_vs_strike_pct=float(strategy.get("hold_winner_min_spot_vs_strike_pct", 0.10)),
        hold_winner_max_dte_days=float(strategy.get("hold_winner_max_dte_days", 1.0)),
        ultra_short_dte_hours=float(strategy.get("ultra_short_dte_hours", 0.0)),
        ultra_short_min_p_win=float(strategy.get("ultra_short_min_p_win", 0.92)),
        ultra_short_min_p_win_with_vol=float(strategy.get("ultra_short_min_p_win_with_vol", 0.86)),
        ultra_short_min_edge_mult=float(strategy.get("ultra_short_min_edge_mult", 1.0)),
        ultra_short_late_minutes_threshold=max(0.0, float(strategy.get("ultra_short_late_minutes_threshold", 0.0))),
        ultra_short_late_min_edge_mult=max(1.0, float(strategy.get("ultra_short_late_min_edge_mult", 1.0))),
        use_intraday_empirical=bool(strategy.get("use_intraday_empirical", True)),
        dynamic_kelly_enabled=bool(strategy.get("dynamic_kelly_enabled", True)),
        dynamic_kelly_floor=float(strategy.get("dynamic_kelly_floor", 0.10)),
        dynamic_kelly_stress_fraction=float(strategy.get("dynamic_kelly_stress_fraction", 0.10)),
        dynamic_kelly_stress_win_rate=float(strategy.get("dynamic_kelly_stress_win_rate", 0.45)),
        dynamic_kelly_recovery_win_rate=float(strategy.get("dynamic_kelly_recovery_win_rate", 0.55)),
        enable_two_sided_quotes=bool(strategy.get("enable_two_sided_quotes", True)),
        two_sided_quote_min_edge=float(strategy.get("two_sided_quote_min_edge", 0.02)),
        macro_signal_enabled=bool(strategy.get("macro_signal_enabled", True)),
        macro_signal_tickers=[str(x) for x in (strategy.get("macro_signal_tickers") or ["^GSPC", "DX-Y.NYB"])],
        enable_easy_win=bool(strategy.get("enable_easy_win", True)),
        min_edge_easy_win=float(strategy.get("min_edge_easy_win", 0.01)),
        easy_win_min_p_win=float(strategy.get("easy_win_min_p_win", 0.90)),
        easy_win_min_market_p=float(strategy.get("easy_win_min_market_p", 0.85)),
        use_live_price=bool(strategy.get("use_live_price", True)),
        live_price_weight=float(strategy.get("live_price_weight", 0.6)),
        empirical_recent_days=int(strategy.get("empirical_recent_days", 90)),
        empirical_stress_recent_days=max(0, int(strategy.get("empirical_stress_recent_days", 0))),
        empirical_stress_calibration_max_wr=float(strategy.get("empirical_stress_calibration_max_wr", 0.0)),
        empirical_stress_min_samples=max(1, int(strategy.get("empirical_stress_min_samples", 25))),
        empirical_recent_weight=float(strategy.get("empirical_recent_weight", 3.0)),
        empirical_use_rolling_window=bool(strategy.get("empirical_use_rolling_window", True)),
        use_volatility_by_default=bool(strategy.get("use_volatility_by_default", True)),
        vol_dynamic_n0=float(strategy.get("vol_dynamic_n0", 90.0)),
        uncertainty_edge_mult=float(strategy.get("uncertainty_edge_mult", 0.5)),
        stop_loss_pct=float(strategy.get("stop_loss_pct", 0.5)),
        weekend_edge_multiplier=float(strategy.get("weekend_edge_multiplier", 1.0)),
        enable_momentum_protection=bool(strategy.get("enable_momentum_protection", False)),
        momentum_crash_threshold_pct=float(strategy.get("momentum_crash_threshold_pct", -0.02)),
        max_cross_platform_exposure=float(strategy.get("max_cross_platform_exposure", 0.30)),
        max_daily_loss_pct=float(strategy.get("max_daily_loss_pct", 0.10)),
        max_daily_loss_cents=int(strategy.get("max_daily_loss_cents", 0)),
        enable_vol_spike_safety=bool(strategy.get("enable_vol_spike_safety", True)),
        vol_spike_threshold_mult=float(strategy.get("vol_spike_threshold_mult", 2.0)),
        vol_spike_edge_multiplier=float(strategy.get("vol_spike_edge_multiplier", 2.0)),
        parity_penalty_enabled=bool(strategy.get("parity_penalty_enabled", True)),
        parity_lookback_days=int(strategy.get("parity_lookback_days", 14)),
        parity_base_penalty=float(strategy.get("parity_base_penalty", 0.02)),
        parity_max_penalty=float(strategy.get("parity_max_penalty", 0.10)),
        enable_strike_validation=bool(strategy.get("enable_strike_validation", True)),
        strike_validation_tolerance_pct=float(strategy.get("strike_validation_tolerance_pct", 0.05)),
        enable_otm_smile_adjustment=bool(strategy.get("enable_otm_smile_adjustment", True)),
        otm_smile_strength=float(strategy.get("otm_smile_strength", 0.35)),
        otm_smile_max_mult=float(strategy.get("otm_smile_max_mult", 2.0)),
        vol_distribution=str(strategy.get("vol_distribution", "student_t")).strip().lower(),
        vol_t_df=float(strategy.get("vol_t_df", 6.0)),
        vol_model=str(strategy.get("vol_model", "auto")).strip().lower(),
        garch_min_returns=max(30, int(strategy.get("garch_min_returns", 80))),
        liquidity_spread_bps_soft=float(strategy.get("liquidity_spread_bps_soft", 1500)),
        liquidity_spread_bps_hard=float(strategy.get("liquidity_spread_bps_hard", 4000)),
        liquidity_min_size_multiplier=float(strategy.get("liquidity_min_size_multiplier", 0.2)),
        liquidity_missing_ask_multiplier=float(strategy.get("liquidity_missing_ask_multiplier", 0.8)),
        enable_defensive_mode=bool(strategy.get("enable_defensive_mode", True)),
        defensive_vol_ratio_threshold=float(strategy.get("defensive_vol_ratio_threshold", 3.0)),
        enable_dynamic_stability_window=bool(strategy.get("enable_dynamic_stability_window", True)),
        stability_vol_ratio_ref=float(strategy.get("stability_vol_ratio_ref", 1.0)),
        stability_max_multiplier=float(strategy.get("stability_max_multiplier", 10.0)),
        enable_vol_regime_scaling=bool(strategy.get("enable_vol_regime_scaling", True)),
        vol_regime_edge_mult_per_x=float(strategy.get("vol_regime_edge_mult_per_x", 0.35)),
        vol_regime_max_edge_mult=float(strategy.get("vol_regime_max_edge_mult", 2.5)),
        vol_regime_dip_mult_per_x=float(strategy.get("vol_regime_dip_mult_per_x", 0.50)),
        vol_regime_max_dip_mult=float(strategy.get("vol_regime_max_dip_mult", 2.0)),
        vol_regime_size_mult_per_x=float(strategy.get("vol_regime_size_mult_per_x", 0.50)),
        vol_regime_min_size_mult=float(strategy.get("vol_regime_min_size_mult", 0.35)),
        enable_cross_exchange_sanity_gate=bool(strategy.get("enable_cross_exchange_sanity_gate", True)),
        cross_exchange_max_yes_gap=float(strategy.get("cross_exchange_max_yes_gap", 0.08)),
        cross_exchange_spot_move_threshold_pct=float(strategy.get("cross_exchange_spot_move_threshold_pct", 0.01)),
        enable_signal_model=bool(strategy.get("enable_signal_model", True)),
        signal_model_weights=dict(strategy.get("signal_model_weights", {})),
        use_sklearn_calibration=bool(strategy.get("use_sklearn_calibration", True)),
        sklearn_calibration_method=str(strategy.get("sklearn_calibration_method", "isotonic")),
        sklearn_calibration_min_samples=int(strategy.get("sklearn_calibration_min_samples", 50)),
        enable_auto_signal_weight_fit=bool(strategy.get("enable_auto_signal_weight_fit", True)),
        signal_weight_fit_min_samples=int(strategy.get("signal_weight_fit_min_samples", 150)),
        signal_weight_fit_l2_c=float(strategy.get("signal_weight_fit_l2_c", 1.0)),
        far_dte_dip_scaling_enabled=bool(strategy.get("far_dte_dip_scaling_enabled", True)),
        far_dte_threshold_days=float(strategy.get("far_dte_threshold_days", 0.042)),
        far_dte_dip_scale_per_day=float(strategy.get("far_dte_dip_scale_per_day", 0.015)),
        far_dte_max_extra_dip=float(strategy.get("far_dte_max_extra_dip", 0.12)),
        far_dte_min_confidence=float(strategy.get("far_dte_min_confidence", 0.80)),
        far_dte_confidence_boost=float(strategy.get("far_dte_confidence_boost", 1.5)),
        enable_disagreement_dip=bool(strategy.get("enable_disagreement_dip", False)),
        disagreement_dip_min_gap=float(strategy.get("disagreement_dip_min_gap", 0.35)),
        disagreement_dip_min_p_win=float(strategy.get("disagreement_dip_min_p_win", 0.25)),
        disagreement_dip_pct_of_bid=float(strategy.get("disagreement_dip_pct_of_bid", 0.50)),
        disagreement_dip_min_price=float(strategy.get("disagreement_dip_min_price", 0.02)),
        disagreement_dip_max_price=float(strategy.get("disagreement_dip_max_price", 0.15)),
        disagreement_dip_contracts=int(strategy.get("disagreement_dip_contracts", 2)),
        enable_lottery_limits=bool(strategy.get("enable_lottery_limits", False)),
        lottery_limit_min_p_win=float(strategy.get("lottery_limit_min_p_win", 0.95)),
        lottery_limit_price=max(0.01, min(0.99, float(strategy.get("lottery_limit_price", 0.01)))),
        lottery_limit_contracts=int(strategy.get("lottery_limit_contracts", 1)),
        lottery_limit_price_mode=str(strategy.get("lottery_limit_price_mode", "fixed")).strip().lower(),
        lottery_limit_max_price=max(0.01, min(0.99, float(strategy.get("lottery_limit_max_price", 0.99)))),
        lottery_penny_jump_apply_cap_bid_below=max(0.01, min(0.99, float(strategy.get("lottery_penny_jump_apply_cap_bid_below", 0.22)))),
        lottery_market_yes_band_enabled=bool(strategy.get("lottery_market_yes_band_enabled", False)),
        lottery_market_yes_band_lo=max(0.0, min(0.99, float(strategy.get("lottery_market_yes_band_lo", 0.40)))),
        lottery_market_yes_band_hi=max(0.0, min(1.0, float(strategy.get("lottery_market_yes_band_hi", 0.60)))),
        mc_enabled=bool(strategy.get("mc_enabled", True)),
        mc_paths=max(100, int(strategy.get("mc_paths", 10000))),
        mc_blend_weight=max(0.0, min(1.0, float(strategy.get("mc_blend_weight", 0.3)))),
        vol_disagree_threshold=float(strategy.get("vol_disagree_threshold", 0.30)),
        vol_disagree_extra_edge=float(strategy.get("vol_disagree_extra_edge", 0.08)),
        vol_disagree_dampen_blend=float(strategy.get("vol_disagree_dampen_blend", 0.50)),
        joint_high_confidence_enabled=bool(strategy.get("joint_high_confidence_enabled", True)),
        joint_high_emp_vol_threshold=max(0.0, min(1.0, float(strategy.get("joint_high_emp_vol_threshold", 0.88)))),
        joint_high_pull_to_half=max(0.0, min(1.0, float(strategy.get("joint_high_pull_to_half", 0.20)))),
        intraday_vol_cap=float(strategy.get("intraday_vol_cap", 0.92)),
        intraday_min_empirical_weight=float(strategy.get("intraday_min_empirical_weight", 0.20)),
        adaptive_learning_enabled=bool(strategy.get("adaptive_learning_enabled", True)),
        adaptive_edge_lookback_hours=float(strategy.get("adaptive_edge_lookback_hours", 6.0)),
        adaptive_edge_min_trades=int(strategy.get("adaptive_edge_min_trades", 8)),
        adaptive_edge_scale_per_pct=float(strategy.get("adaptive_edge_scale_per_pct", 0.003)),
        adaptive_edge_max_extra=float(strategy.get("adaptive_edge_max_extra", 0.15)),
        loss_streak_threshold=int(strategy.get("loss_streak_threshold", 5)),
        loss_streak_extra_edge=float(strategy.get("loss_streak_extra_edge", 0.04)),
        loss_streak_max_extra=float(strategy.get("loss_streak_max_extra", 0.20)),
        series_cooldown_threshold=int(strategy.get("series_cooldown_threshold", 4)),
        series_cooldown_extra_edge=float(strategy.get("series_cooldown_extra_edge", 0.06)),
        ewma_halflife_trades=int(strategy.get("ewma_halflife_trades", 30)),
        ewma_edge_scale_per_pct=float(strategy.get("ewma_edge_scale_per_pct", 0.004)),
        adaptive_loss_block_max_balance_loss_pct=float(
            strategy.get("adaptive_loss_block_max_balance_loss_pct", 0.20)
        ),
        adaptive_loss_block_lookback_hours=float(strategy.get("adaptive_loss_block_lookback_hours", 8.0)),
        adaptive_loss_block_min_trades=int(strategy.get("adaptive_loss_block_min_trades", 5)),
        adaptive_edge_idle_decay_start_hours=max(0.0, float(strategy.get("adaptive_edge_idle_decay_start_hours", 2.5))),
        adaptive_edge_idle_decay_halflife_hours=max(0.01, float(strategy.get("adaptive_edge_idle_decay_halflife_hours", 10.0))),
        adaptive_stress_size_edge_threshold=max(0.0, float(strategy.get("adaptive_stress_size_edge_threshold", 0.06))),
        adaptive_stress_size_min_mult=max(0.05, min(1.0, float(strategy.get("adaptive_stress_size_min_mult", 0.60)))),
        barbell_enabled=bool(strategy.get("barbell_enabled", False)),
        barbell_base_min_market_yes=max(0.0, min(1.0, float(strategy.get("barbell_base_min_market_yes", 0.70)))),
        barbell_base_min_model_p=max(0.0, min(1.0, float(strategy.get("barbell_base_min_model_p", 0.78)))),
        barbell_base_max_model_vs_market_gap=max(0.0, min(1.0, float(strategy.get("barbell_base_max_model_vs_market_gap", 0.10)))),
        barbell_base_max_yes_price=max(0.01, min(0.99, float(strategy.get("barbell_base_max_yes_price", 0.92)))),
        barbell_base_max_no_price=max(0.01, min(0.99, float(strategy.get("barbell_base_max_no_price", 0.92)))),
        barbell_alpha_max_cheap_price=max(0.01, min(0.99, float(strategy.get("barbell_alpha_max_cheap_price", 0.20)))),
        barbell_alpha_min_model_p=max(0.0, min(1.0, float(strategy.get("barbell_alpha_min_model_p", 0.28)))),
        barbell_alpha_min_net_edge=max(0.0, min(1.0, float(strategy.get("barbell_alpha_min_net_edge", 0.18)))),
        barbell_base_kelly_mult=max(0.05, min(3.0, float(strategy.get("barbell_base_kelly_mult", 1.0)))),
        barbell_alpha_kelly_mult=max(0.05, min(3.0, float(strategy.get("barbell_alpha_kelly_mult", 0.12)))),
        consensus_sniper_enabled=bool(strategy.get("consensus_sniper_enabled", False)),
        consensus_sniper_min_edge=max(-0.05, min(1.0, float(strategy.get("consensus_sniper_min_edge", -0.01)))),
        barbell_soften_strict_intraday=bool(strategy.get("barbell_soften_strict_intraday", True)),
        barbell_strict_relaxed_min_p_win=max(
            0.01, min(0.99, float(strategy.get("barbell_strict_relaxed_min_p_win", 0.72)))
        ),
        yes_overconfidence_extra_edge=float(strategy.get("yes_overconfidence_extra_edge", 0.0)),
        model_probability_shrinkage=float(strategy.get("model_probability_shrinkage", 1.0)),
        passive_limit_quote=str(strategy.get("passive_limit_quote", "dip")).strip().lower(),
        empirical_weighting=str(strategy.get("empirical_weighting", "step_recent")).strip().lower(),
        empirical_exponential_halflife_bars=max(1, int(strategy.get("empirical_exponential_halflife_bars", 14))),
        enable_model_prob_hard_cap=bool(strategy.get("enable_model_prob_hard_cap", True)),
        model_prob_hard_lo=max(1e-6, float(strategy.get("model_prob_hard_lo", 0.02))),
        model_prob_hard_hi=min(0.999, float(strategy.get("model_prob_hard_hi", 0.98))),
        position_exit_min_net_edge=max(0.0, min(0.5, float(strategy.get("position_exit_min_net_edge", 0.0)))),
        skip_new_buy_when_unresolved_same_side=bool(strategy.get("skip_new_buy_when_unresolved_same_side", False)),
        limit_buy_edge_aggression_enabled=bool(strategy.get("limit_buy_edge_aggression_enabled", False)),
        limit_buy_cross_spread_min_edge=max(0.0, min(0.95, float(strategy.get("limit_buy_cross_spread_min_edge", 0.10)))),
        limit_buy_near_market_min_edge=max(0.0, min(0.95, float(strategy.get("limit_buy_near_market_min_edge", 0.05)))),
        resting_cancel_if_market_bid_above_limit_by=max(0.0, min(0.5, float(strategy.get("resting_cancel_if_market_bid_above_limit_by", 0.0)))),
        simple_ev_decision_enabled=bool((strategy.get("simple_ev_decision") or {}).get("enabled", False)),
        simple_ev_min_edge=max(0.0, min(0.5, float((strategy.get("simple_ev_decision") or {}).get("min_edge", 0.03)))),
        simple_ev_kelly_dampen=max(0.001, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("kelly_dampen", 0.25)))),
        simple_ev_max_bankroll_fraction=max(0.001, min(0.25, float((strategy.get("simple_ev_decision") or {}).get("max_bankroll_fraction", 0.02)))),
        simple_ev_spread_wide_fraction=max(0.0, min(0.5, float((strategy.get("simple_ev_decision") or {}).get("spread_wide_fraction", 0.05)))),
        simple_ev_spread_wide_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("spread_wide_size_mult", 0.5)))),
        simple_ev_emp_vol_disagree_threshold=max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("emp_vol_disagree_threshold", 0.15)))),
        simple_ev_disagree_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("disagree_size_mult", 0.5)))),
        simple_ev_calibration_wr_threshold=max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("calibration_win_rate_threshold", 0.40)))),
        simple_ev_calibration_poor_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("calibration_poor_size_mult", 0.7)))),
        simple_ev_min_bid_size=max(0.0, float((strategy.get("simple_ev_decision") or {}).get("min_bid_size", 0.0))),
        simple_ev_ultra_short_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("ultra_short_size_mult", 1.0)))),
        simple_ev_skip_p_win_mid_enabled=bool(
            (strategy.get("simple_ev_decision") or {}).get("skip_p_win_mid_band_enabled", False)
        ),
        simple_ev_skip_p_win_mid_lo=min(
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("skip_p_win_mid_lo", 0.60)))),
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("skip_p_win_mid_hi", 0.80)))),
        ),
        simple_ev_skip_p_win_mid_hi=max(
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("skip_p_win_mid_lo", 0.60)))),
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("skip_p_win_mid_hi", 0.80)))),
        ),
        simple_ev_cheap_band_lo=min(
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("cheap_band_price_lo", 0.05)))),
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("cheap_band_price_hi", 0.10)))),
        ),
        simple_ev_cheap_band_hi=max(
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("cheap_band_price_lo", 0.05)))),
            max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("cheap_band_price_hi", 0.10)))),
        ),
        simple_ev_cheap_band_min_net_edge=max(
            0.0,
            min(0.5, float((strategy.get("simple_ev_decision") or {}).get("cheap_band_min_net_edge", 0.0))),
        ),
        log_effective_min_edge=bool(strategy.get("log_effective_min_edge", True)),
        log_decision_diagnostics=bool(strategy.get("log_decision_diagnostics", False)),
        ah_cfg=load_anti_hallucination_config(strategy),
        ae_cfg=load_alpha_engine_config(strategy),
    )


def _strategy_recommend_extras(sp: _StrategyParams) -> dict[str, Any]:
    """Forwarded to strategy_engine.recommend; consensus sniper only active when barbell is on."""
    if sp.simple_ev_decision_enabled:
        return {
            "consensus_sniper_enabled": False,
            "consensus_sniper_min_edge": None,
            "barbell_base_min_market_yes": sp.barbell_base_min_market_yes,
            "barbell_base_min_model_p": sp.barbell_base_min_model_p,
            "barbell_base_max_model_vs_market_gap": sp.barbell_base_max_model_vs_market_gap,
            "barbell_base_max_yes_price": sp.barbell_base_max_yes_price,
            "barbell_base_max_no_price": sp.barbell_base_max_no_price,
        }
    _on = bool(sp.barbell_enabled and sp.consensus_sniper_enabled)
    return {
        "consensus_sniper_enabled": _on,
        "consensus_sniper_min_edge": float(sp.consensus_sniper_min_edge) if _on else None,
        "barbell_base_min_market_yes": sp.barbell_base_min_market_yes,
        "barbell_base_min_model_p": sp.barbell_base_min_model_p,
        "barbell_base_max_model_vs_market_gap": sp.barbell_base_max_model_vs_market_gap,
        "barbell_base_max_yes_price": sp.barbell_base_max_yes_price,
        "barbell_base_max_no_price": sp.barbell_base_max_no_price,
    }


def _strategy_recommend_strict_extras(sp: _StrategyParams) -> dict[str, Any]:
    """
    When barbell_soften_strict_intraday: apply easy-win + consensus sniper edge floors and
    relaxed min P(win) for barbell-base-shaped lines on strict intraday / ultra-short path.
    """
    if sp.simple_ev_decision_enabled:
        return {}
    if not sp.barbell_soften_strict_intraday or not sp.barbell_enabled:
        return {}
    out: dict[str, Any] = {
        "min_edge_easy_win": sp.min_edge_easy_win if sp.enable_easy_win else None,
        "easy_win_min_p_win": sp.easy_win_min_p_win,
        "easy_win_min_market_p": sp.easy_win_min_market_p,
        "barbell_strict_relaxed_min_p_win": float(sp.barbell_strict_relaxed_min_p_win),
    }
    out.update(_strategy_recommend_extras(sp))
    return out


def _build_strategy_engine(strategy_cfg: dict) -> BaseStrategy:
    """
    Build pluggable recommendation strategy.

    Default remains the production path (``vol_regime`` or alias ``vol_regime_threshold``).
    Both use VolRegimeStrategy: same threshold logic as DefaultThresholdStrategy; vol
    regime scaling is applied in compare_historical, not inside recommend().
    """
    from models.strategy_registry import build_strategy_engine_by_name

    name = str(strategy_cfg.get("strategy_engine", "vol_regime")).strip().lower()
    try:
        return build_strategy_engine_by_name(name)
    except ValueError:
        return VolRegimeStrategy()


def _print_multi_strategy_compare_line(
    *,
    compare_strategies_arg: str,
    hist_p: float,
    market_yes: float,
    min_p_win: float,
    effective_min: float,
    maker_fee_rate: float,
    min_fee_per_order: float,
    effective_min_yes: float,
    effective_min_no: float,
    is_strict_intraday: bool,
    sp: _StrategyParams,
) -> None:
    """Same model P(hist_p); alternate strategy engines only change recommend() thresholds."""
    raw = (compare_strategies_arg or "").strip()
    if not raw:
        return
    from models.strategy_registry import build_strategy_engine_by_name, parse_strategy_list

    parts: list[str] = []
    for sn in parse_strategy_list(raw):
        try:
            eng = build_strategy_engine_by_name(sn)
        except ValueError:
            parts.append(f"{sn}:?")
            continue
        if is_strict_intraday:
            mr = eng.recommend_strict(
                p_yes=hist_p,
                market_yes=market_yes,
                min_p_win=min_p_win,
                min_edge=effective_min,
                maker_fee_rate=maker_fee_rate,
                min_fee_per_order=min_fee_per_order,
                min_edge_yes=effective_min_yes,
                min_edge_no=effective_min_no,
                **_strategy_recommend_strict_extras(sp),
            )
        else:
            mr = eng.recommend(
                p_yes=hist_p,
                market_yes=market_yes,
                min_edge=effective_min,
                maker_fee_rate=maker_fee_rate,
                min_fee_per_order=min_fee_per_order,
                min_edge_yes=effective_min_yes,
                min_edge_no=effective_min_no,
                min_edge_easy_win=sp.min_edge_easy_win if sp.enable_easy_win else None,
                easy_win_min_p_win=sp.easy_win_min_p_win,
                easy_win_min_market_p=sp.easy_win_min_market_p,
                **_strategy_recommend_extras(sp),
            )
        parts.append(f"{sn}:{mr}")
    if parts:
        print(f"    ├─ Multi-strategy:  {' | '.join(parts)}")


def _adaptive_limit_dip(base_dip: float, closes: list[float], *, cap: float = 0.25) -> float:
    """
    ARR-inspired adaptive limit dip.
    Uses average absolute relative move as a practical volatility proxy and
    widens dip in higher-vol regimes: dip ~= max(base, 0.5 * ARR).
    """
    if len(closes) < 12:
        return max(0.0, base_dip)
    rel_moves: list[float] = []
    for i in range(1, min(len(closes), 72)):
        prev = float(closes[-(i + 1)] or 0.0)
        curr = float(closes[-i] or 0.0)
        if prev > 0:
            rel_moves.append(abs((curr - prev) / prev))
    if not rel_moves:
        return max(0.0, base_dip)
    arr = sum(rel_moves) / len(rel_moves)
    return min(max(0.0, cap), max(float(base_dip), 0.5 * arr))


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI on the underlying asset's close prices."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss < 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _rsi_dip_adjustment(base_dip: float, rsi: float | None, max_dip_cap: float = 0.25) -> float:
    """
    Modulate limit dip depth based on RSI.
    Oversold (RSI<30): tighten dip to fill faster on the bounce.
    Overbought (RSI>70): widen dip to wait for the correction.
    """
    if rsi is None:
        return base_dip
    if rsi < 30:
        return base_dip * 0.6
    if rsi > 70:
        return min(max_dip_cap, base_dip * 1.4)
    return base_dip


def _dte_confidence_dip(
    base_dip: float,
    p_win: float,
    dte_days: float | None,
    sp: _StrategyParams,
) -> float:
    """
    Scale limit order dip depth based on DTE × confidence interaction.

    When resolution is far out, contract prices are subject to uncertainty-driven dips
    even when the eventual outcome is near-certain. High-confidence, far-DTE trades
    benefit from deeper limits that catch these transient price drops.

    Returns the combined dip (base + DTE extra), capped at max_dip_cap.
    """
    if not sp.far_dte_dip_scaling_enabled:
        return base_dip
    if dte_days is None or dte_days <= sp.far_dte_threshold_days:
        return base_dip
    if p_win < sp.far_dte_min_confidence:
        return base_dip

    excess_days = dte_days - sp.far_dte_threshold_days
    extra_dip = min(sp.far_dte_max_extra_dip, excess_days * sp.far_dte_dip_scale_per_day)

    if p_win >= 0.90:
        extra_dip *= sp.far_dte_confidence_boost

    return min(sp.max_dip_cap, base_dip + extra_dip)


def _extract_book_fields(market: dict, yes_bid: float, no_bid: float) -> dict[str, float | None]:
    """
    Extract top-of-book prices/sizes for snapshot persistence.
    Uses tolerant key matching because Kalshi/Gemini payloads differ.
    """
    def _f(keys: list[str]) -> float | None:
        for k in keys:
            if k in market and market.get(k) is not None:
                try:
                    return float(market.get(k))
                except (TypeError, ValueError):
                    continue
        return None

    yes_ask = _f(["yes_ask_dollars", "yes_ask", "ask_yes_price"])
    no_ask = _f(["no_ask_dollars", "no_ask", "ask_no_price"])
    # Binary fallback: ask can be inferred from opposite side bid if not explicit.
    if yes_ask is None and 0 < no_bid < 1:
        yes_ask = 1.0 - no_bid
    if no_ask is None and 0 < yes_bid < 1:
        no_ask = 1.0 - yes_bid
    return {
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_bid_size": _f(["yes_bid_size", "yes_bid_qty", "best_yes_bid_size"]),
        "no_bid_size": _f(["no_bid_size", "no_bid_qty", "best_no_bid_size"]),
        "yes_ask_size": _f(["yes_ask_size", "yes_ask_qty", "best_yes_ask_size"]),
        "no_ask_size": _f(["no_ask_size", "no_ask_qty", "best_no_ask_size"]),
    }


def _clip_price(v: float) -> float:
    return max(0.01, min(0.99, float(v)))


def _compute_lottery_limit_price(
    *,
    sp: _StrategyParams,
    side: str,
    yes_bid: float,
    no_bid: float,
    yes_ask: float | None,
    no_ask: float | None,
) -> float:
    """
    Lottery resting bids: fixed micro price, or penny_jump (best bid on that side + 1¢).
    When the working bid is below lottery_penny_jump_apply_cap_bid_below, clip to
    lottery_limit_max_price so cheap-leg lotteries stay small; high-priced legs skip the cap.
    """
    mode = (sp.lottery_limit_price_mode or "fixed").strip().lower()
    if mode in ("", "fixed", "absolute"):
        return _clip_price(float(sp.lottery_limit_price))
    if mode not in ("penny_jump", "bid_plus_tick", "bid_plus_one_tick", "jump"):
        return _clip_price(float(sp.lottery_limit_price))
    tick = 0.01
    if side.lower() == "yes":
        bid = float(yes_bid)
        ask = yes_ask
    else:
        bid = float(no_bid)
        ask = no_ask
    # Match passive bid_plus_tick: front-of-queue without crossing the ask
    px = bid + tick
    if ask is not None and float(ask) > 0:
        px = min(px, max(bid, float(ask) - tick))
    th = float(sp.lottery_penny_jump_apply_cap_bid_below)
    if bid < th:
        px = min(px, float(sp.lottery_limit_max_price))
    return _clip_price(px)


def _resolve_passive_quote_price(
    *,
    passive_quote: str,
    bid: float,
    ask: float | None,
    dip_based_price: float,
    fair_cap: float,
) -> float:
    """
    Micro-balance / maker-friendly quotes: default keeps dip-based passive limits;
    'mid' uses (bid+ask)/2 capped by fair edge; 'bid_plus_tick' is bid+1¢ (tighter than deep dip).
    """
    pq = (passive_quote or "dip").strip().lower()
    if pq in ("", "dip", "default", "below_bid", "layered"):
        return _clip_price(min(float(dip_based_price), float(fair_cap)))
    fc = max(0.01, min(0.99, float(fair_cap)))
    if pq == "mid":
        if ask is not None and float(ask) > 0 and float(bid) > 0:
            mid = (float(bid) + float(ask)) / 2.0
            return _clip_price(min(mid, fc))
        return _clip_price(min(float(bid), fc))
    if pq in ("bid_plus_tick", "bid_plus_one_tick"):
        tick = 0.01
        j = float(bid) + tick
        if ask is not None and float(ask) > 0:
            j = min(j, max(float(bid), float(ask) - tick))
        return _clip_price(min(j, fc))
    return _clip_price(min(float(dip_based_price), fc))


def _sanitize_model_probability(p: float, *, lo: float, hi: float, enabled: bool) -> float:
    """Hard cap model P so Kelly / edge never assume certainty (e.g. vol model at 100%)."""
    if not enabled:
        return p
    x = float(p)
    lo_f = max(1e-6, min(0.99, float(lo)))
    hi_f = max(0.01, min(0.999, float(hi)))
    if lo_f >= hi_f:
        return x
    return max(lo_f, min(hi_f, x))


def _display_model_probability(p: float | None, sp: _StrategyParams) -> float | None:
    """Log-friendly P: same hard cap as sizing so logs don't show misleading '100%'."""
    if p is None:
        return None
    return _sanitize_model_probability(float(p), lo=sp.model_prob_hard_lo, hi=sp.model_prob_hard_hi, enabled=sp.enable_model_prob_hard_cap)


def _strategy_with_recovery_overrides(cfg: dict) -> dict:
    """
    Merge optional top-level `recovery:` into a copy of strategy (recovery injector / micro-balance).
    Keys: min_edge, adaptive_edge_max_extra, loss_streak_max_extra, force_min_buy_contracts_one,
    calibration_extra_edge_overall, calibration_extra_edge_above_yes (BUY YES above-X downgrade gate),
    joint_high_pull_to_half / joint_high_confidence_enabled,
    calibration_hard_block_above_yes_win_rate / calibration_hard_block_above_yes_min_avg_entry,
    parity_penalty_enabled, uncertainty_edge_mult,
    vol_disagree_extra_edge, max_buy_limit_price (0 = disable cap on bought-leg limit),
    enable_vol_regime_scaling, dte_edge_scaling_enabled, ultra_short_min_edge_mult (1.0 = no extra on 15m).

    Note: Lowering min_edge alone often still yields all SKIP because calibration (win rate < 50%)
    adds calibration_extra_edge_overall and parity adds large YES/NO penalties — override those here
    for short preview/live trials, then remove when performance recovers.
    uncertainty_edge_mult / vol_disagree_extra_edge stack on top of min_edge (empirical CI width,
    emp-vs-vol disagreement) and often explain high effective hurdles in SKIP reasons even when base min_edge is ~4–6%.
    yes_overconfidence_extra_edge: extra required edge on BUY YES only (merged so recovery can set 0).
    """
    s = dict(cfg.get("strategy") or {})
    r = cfg.get("recovery") or {}
    if not r:
        return s
    if r.get("min_edge") is not None:
        v = float(r["min_edge"])
        s["min_edge"] = v
        sed = s.get("simple_ev_decision")
        if isinstance(sed, dict):
            s["simple_ev_decision"] = {**sed, "min_edge": v}
    if r.get("adaptive_edge_max_extra") is not None:
        s["adaptive_edge_max_extra"] = float(r["adaptive_edge_max_extra"])
    if r.get("loss_streak_max_extra") is not None:
        s["loss_streak_max_extra"] = float(r["loss_streak_max_extra"])
    if r.get("force_min_buy_contracts_one") is not None:
        s["force_min_buy_contracts_one"] = bool(r["force_min_buy_contracts_one"])
    if r.get("calibration_extra_edge_overall") is not None:
        s["calibration_extra_edge_overall"] = float(r["calibration_extra_edge_overall"])
    if r.get("calibration_extra_edge_above_yes") is not None:
        s["calibration_extra_edge_above_yes"] = max(0.0, float(r["calibration_extra_edge_above_yes"]))
    if r.get("parity_penalty_enabled") is not None:
        s["parity_penalty_enabled"] = bool(r["parity_penalty_enabled"])
    if r.get("uncertainty_edge_mult") is not None:
        s["uncertainty_edge_mult"] = float(r["uncertainty_edge_mult"])
    if r.get("vol_disagree_extra_edge") is not None:
        s["vol_disagree_extra_edge"] = float(r["vol_disagree_extra_edge"])
    if r.get("joint_high_pull_to_half") is not None:
        s["joint_high_pull_to_half"] = max(0.0, min(1.0, float(r["joint_high_pull_to_half"])))
    if r.get("joint_high_confidence_enabled") is not None:
        s["joint_high_confidence_enabled"] = bool(r["joint_high_confidence_enabled"])
    if r.get("calibration_hard_block_above_yes_min_avg_entry") is not None:
        s["calibration_hard_block_above_yes_min_avg_entry"] = max(
            0.0, min(1.0, float(r["calibration_hard_block_above_yes_min_avg_entry"]))
        )
    if r.get("calibration_hard_block_above_yes_win_rate") is not None:
        s["calibration_hard_block_above_yes_win_rate"] = max(0.0, float(r["calibration_hard_block_above_yes_win_rate"]))
    if r.get("yes_overconfidence_extra_edge") is not None:
        s["yes_overconfidence_extra_edge"] = float(r["yes_overconfidence_extra_edge"])
    # Non–5pm/4pm intraday uses recommend_strict: needs P(win) ≥ these AND edge — often the real “all SKIP” cause.
    if r.get("intraday_other_min_p_win") is not None:
        s["intraday_other_min_p_win"] = float(r["intraday_other_min_p_win"])
    if r.get("intraday_other_min_p_win_with_vol") is not None:
        s["intraday_other_min_p_win_with_vol"] = float(r["intraday_other_min_p_win_with_vol"])
    if r.get("max_buy_limit_price") is not None:
        s["max_buy_limit_price"] = max(0.0, min(1.0, float(r["max_buy_limit_price"])))
    if r.get("enable_vol_regime_scaling") is not None:
        s["enable_vol_regime_scaling"] = bool(r["enable_vol_regime_scaling"])
    if r.get("dte_edge_scaling_enabled") is not None:
        s["dte_edge_scaling_enabled"] = bool(r["dte_edge_scaling_enabled"])
    if r.get("ultra_short_min_edge_mult") is not None:
        s["ultra_short_min_edge_mult"] = max(1.0, float(r["ultra_short_min_edge_mult"]))
    return s


def _effective_strategy_dict(cfg: dict) -> dict:
    """strategy.yaml + optional `recovery:` + optional `execution:` merges."""
    base = _strategy_with_recovery_overrides(cfg)
    return _merge_execution_into_strategy({**cfg, "strategy": base})


def _merge_execution_into_strategy(cfg: dict) -> dict:
    """
    Apply optional top-level `execution:` overrides into a copy of `strategy:`.
    - price_strategy: passive → mid or bid+1¢ from limit_offset_cents (spread-aware maker quotes)
    - aggressive → restore deep dip style (passive_limit_quote=dip)
    """
    strategy = dict(cfg.get("strategy") or {})
    ex = cfg.get("execution") or {}
    if not ex:
        return strategy
    # Optional numeric/bool execution knobs merged into strategy for _load_strategy_params
    _ex_float = (
        "resting_cancel_if_market_bid_above_limit_by",
        "position_exit_min_net_edge",
        "limit_buy_cross_spread_min_edge",
        "limit_buy_near_market_min_edge",
    )
    for k in _ex_float:
        if ex.get(k) is not None:
            try:
                strategy[k] = float(ex[k])
            except (TypeError, ValueError):
                pass
    for k in ("limit_buy_edge_aggression_enabled", "skip_new_buy_when_unresolved_same_side"):
        if ex.get(k) is not None:
            strategy[k] = bool(ex[k])
    ps = str(ex.get("price_strategy") or "").strip().lower()
    if ps == "passive":
        try:
            off = int(ex.get("limit_offset_cents", 0))
        except (TypeError, ValueError):
            off = 0
        if off >= 1:
            strategy["passive_limit_quote"] = "bid_plus_tick"
        else:
            strategy.setdefault("passive_limit_quote", "mid")
    elif ps in ("aggressive", "market", "cross"):
        strategy["passive_limit_quote"] = "dip"
    ne = ex.get("near_expiry_aggressive_hours")
    if ne is not None:
        try:
            strategy["near_expiry_aggressive_hours"] = max(0.0, float(ne))
        except (TypeError, ValueError):
            pass
    return strategy


def _effective_passive_limit_quote(sp: _StrategyParams, dte_days: float | None) -> str:
    """
    When resolution is very soon, prefer dip-style limits so penny books actually fill (PnL > maker queue risk).
    """
    base = sp.passive_limit_quote
    h = float(sp.near_expiry_aggressive_hours or 0.0)
    if h <= 0 or dte_days is None:
        return base
    try:
        d = float(dte_days)
    except (TypeError, ValueError):
        return base
    # Inclusive at h: DTE of exactly h wall-clock hours still gets dip (fill / adverse-selection vs missing by <).
    if d * 24.0 <= h:
        return "dip"
    return base


def _cap_buy_count_by_book_pct(count: int, book_bid_size: float | None, max_pct: float) -> int:
    """Cap order size vs visible same-side depth to reduce adverse selection and book impact."""
    if count <= 0 or max_pct <= 0:
        return count
    if book_bid_size is None:
        return count
    try:
        depth = float(book_bid_size)
    except (TypeError, ValueError):
        return count
    if depth <= 0:
        return count
    cap = max(1, int(depth * max_pct))
    return min(count, cap)


def _load_blacklist_extra_file(path: str | None) -> set[str]:
    """Series tickers (one per line) merged into strategy blacklist; # starts comment."""
    out: set[str] = set()
    if not path or not str(path).strip():
        return out
    p = Path(str(path).strip()).expanduser()
    if not p.is_file():
        return out
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if s:
            out.add(s)
    return out


def _inventory_skew_factor(held_count: int, max_skew_pct: float) -> float:
    """
    Linear risk-aversion skew: every 100 held contracts adds skew up to max_skew_pct.
    """
    if held_count <= 0 or max_skew_pct <= 0:
        return 0.0
    return min(float(max_skew_pct), (held_count / 100.0) * float(max_skew_pct))


def _is_midprice_stable(
    *,
    ticker: str,
    platform: str,
    cfg: dict,
    min_stability_seconds: int,
    max_mid_move_pct: float = 0.03,
    skip_in_preview: bool = False,
) -> bool:
    """
    Anti-sniping guard: require recent mid-price to remain within a tight range
    for at least min_stability_seconds.
    In --preview mode, skip this check so proposed orders are shown.
    """
    if skip_in_preview or min_stability_seconds <= 0 or not get_market_snapshots_for_backtest:
        return True
    end_ts = time.time()
    start_ts = end_ts - max(float(min_stability_seconds + 60), 300.0)
    snaps = get_market_snapshots_for_backtest(
        start_ts=start_ts,
        end_ts=end_ts,
        platform=platform,
        config=cfg,
    ) or []
    mids: list[tuple[float, float]] = []
    for s in snaps:
        if str(s.get("ticker") or "") != str(ticker):
            continue
        y = s.get("yes_bid")
        n = s.get("no_bid")
        try:
            yf = float(y) if y is not None else None
            nf = float(n) if n is not None else None
        except (TypeError, ValueError):
            continue
        if yf is None or nf is None or yf <= 0 or nf <= 0:
            continue
        mids.append((float(s.get("ts") or 0.0), (yf + nf) / 2.0))
    if len(mids) < 2:
        return False
    mids.sort(key=lambda x: x[0])
    if mids[-1][0] - mids[0][0] < float(min_stability_seconds):
        return False
    vals = [m for _, m in mids]
    lo, hi = min(vals), max(vals)
    move_pct = (hi - lo) / max(0.01, vals[-1])
    return move_pct <= max(0.0, float(max_mid_move_pct))


def _compute_limit_price(
    *,
    bid: float,
    ask: float | None,
    p_win: float,
    maker_fee_rate: float,
    min_fee_per_order: float,
    mode: str,
    base_dip: float,
    tick_offset: int,
    held_same_side: int,
    held_opp_side: int,
    max_inventory_skew_pct: float,
    layered_order_levels: int,
    order_count: int,
    max_dip_cap: float = 0.25,
    passive_quote: str = "dip",
    net_edge: float | None = None,
    edge_aggression_enabled: bool = False,
    cross_spread_min_edge: float = 0.10,
    near_market_min_edge: float = 0.05,
) -> tuple[float, float]:
    """
    Returns (limit_price, dip_used_proxy).
    dip_used_proxy is for logging/analysis; dynamic modes may return 0.
    When edge_aggression_enabled and net_edge is set, quote closer to the market for large edge
    (near bid+tick, or just inside ask) instead of only deep dip bids.
    """
    mode_norm = (mode or "fixed_pct").strip().lower()
    if mode_norm not in {"fixed_pct", "dynamic_tick", "layered"}:
        mode_norm = "fixed_pct"

    fair_cap = _clip_price(float(p_win) - _fee_per_contract(float(p_win), maker_fee_rate, min_fee_per_order))

    if (
        edge_aggression_enabled
        and net_edge is not None
        and float(net_edge) > 0
        and fair_cap > 0
    ):
        ne = float(net_edge)
        tick = max(1, int(tick_offset)) * 0.01
        if ask is not None and ask > 0 and ne >= float(cross_spread_min_edge):
            aggressive = min(max(0.01, float(ask) - 0.01), fair_cap, float(ask))
            return _clip_price(aggressive), 0.0
        if ne >= float(near_market_min_edge):
            jump = float(bid) + tick
            if ask is not None and ask > 0:
                jump = min(jump, max(0.01, float(ask) - 0.01))
            near_px = min(jump, fair_cap, float(bid) + max(tick, 0.02))
            return _clip_price(near_px), 0.0

    if mode_norm == "fixed_pct":
        dip = max(0.0, min(max_dip_cap, float(base_dip)))
        raw_price = bid * (1.0 - dip) if dip > 0 else bid
    elif mode_norm == "dynamic_tick":
        tick = max(1, int(tick_offset)) * 0.01
        jump_price = bid + tick
        if ask is not None and ask > 0:
            jump_price = min(jump_price, max(0.01, float(ask) - 0.01))
        raw_price = min(jump_price, fair_cap)
        dip = 0.0
    else:
        # Layered: place passive limits BELOW bid using the dip to catch transient
        # price drops instead of penny-jumping above bid.
        dip = max(0.0, min(max_dip_cap, float(base_dip)))
        base_price = bid * (1.0 - dip) if dip > 0 else bid

        # Stagger deeper when we already hold same-side inventory (additional
        # re-buys target even cheaper levels for better avg cost).
        levels = max(1, int(layered_order_levels))
        if levels > 1 and order_count > 1:
            level_idx = min(order_count - 1, levels - 1)
            depth_per_level = max(0.01, bid * dip * 0.5) if dip > 0 else 0.01
            base_price -= level_idx * depth_per_level

        raw_price = min(base_price, fair_cap)

    # Inventory-based quote skew: less aggressive on same-side inventory, slightly
    # more aggressive when holding opposite side (helps rebalance exposure).
    same_skew = _inventory_skew_factor(int(held_same_side), max_inventory_skew_pct)
    opp_skew = _inventory_skew_factor(int(held_opp_side), max_inventory_skew_pct) * 0.5
    skewed = raw_price * (1.0 - same_skew + opp_skew)
    merged = _resolve_passive_quote_price(
        passive_quote=passive_quote,
        bid=bid,
        ask=ask,
        dip_based_price=skewed,
        fair_cap=fair_cap,
    )
    return merged, float(dip)


def _adaptive_kelly_fraction(base_kelly: float, calibration: dict[str, Any], sp: _StrategyParams) -> float:
    """Scale Kelly down when recent calibration quality degrades."""
    base = max(0.01, float(base_kelly))
    if not sp.dynamic_kelly_enabled:
        return base
    wr = calibration.get("win_rate")
    n = int(calibration.get("resolved_count", 0) or 0)
    if wr is None or n < max(10, sp.min_calibration_samples_overall):
        return base
    stress_wr = float(sp.dynamic_kelly_stress_win_rate)
    recover_wr = max(stress_wr + 1e-6, float(sp.dynamic_kelly_recovery_win_rate))
    floor_k = max(0.01, min(base, float(sp.dynamic_kelly_floor), float(sp.dynamic_kelly_stress_fraction)))
    if wr <= stress_wr:
        return floor_k
    if wr >= recover_wr:
        return base
    alpha = (float(wr) - stress_wr) / (recover_wr - stress_wr)
    return floor_k + alpha * (base - floor_k)


def _compute_macro_signal_24h(cfg: dict, sp: _StrategyParams) -> float:
    """
    Macro risk-on/risk-off feature from major indices.
    Returns average 24h return across configured tickers.
    """
    if not sp.macro_signal_enabled:
        return 0.0
    rets: list[float] = []
    for tk in sp.macro_signal_tickers:
        try:
            closes = yf_get_aggs(str(tk), days_back=5, config=cfg)
            if closes and len(closes) >= 2 and closes[-2] > 0:
                rets.append((float(closes[-1]) / float(closes[-2])) - 1.0)
        except Exception:
            continue
    if not rets:
        return 0.0
    return max(-0.10, min(0.10, sum(rets) / len(rets)))


# ---------------------------------------------------------------------------
# Intraday vs daily-close: supported resolution times
# ---------------------------------------------------------------------------
# We only evaluate intraday markets that match our supported times:
# - Crypto (BTC, ETH, SOL, …): 5pm ET (typical crypto settlement)
# - US equity indices (^GSPC, ^NDX): 4pm ET (regular session close)
# - Listed commodity futures (CL=F, GC=F, SI=F, RB=F): any titled hour (16–17 ET = “standard” lenient path)
# All other intraday times on unsupported underlyings (e.g. 7pm EDT) are skipped.
_RESOLUTION_HOUR_PATTERN = re.compile(
    r"(?:(?:today|tomorrow)\s+at|@|\bat)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)\b",
    re.I,
)


def _get_resolution_hour_from_title(title: str) -> int | None:
    """
    Parse intraday resolution hour from title. Returns 24h hour (16=4pm, 17=5pm) or None if no intraday.
    """
    m = _RESOLUTION_HOUR_PATTERN.search(title)
    if not m:
        return None
    hour_str = m.group(1)
    am_pm = (m.group(2) or "").lower()
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
    - Crypto, US equity indices (^GSPC, ^NDX), and listed commodity futures at ANY intraday time
      - 5pm crypto / 4pm index: use standard min_edge
      - Commodity futures: 4pm or 5pm ET = standard; other titled hours → strict
      - Other times (e.g. 8pm ET): use strict intraday_other_min_p_win (only buy when almost certain)
    - Non–supported underlyings with intraday → skip (no supported time)
    """
    title = _get_title(market_or_title)
    hour = _get_resolution_hour_from_title(title)
    if hour is None:
        return False  # No intraday time → treat as daily close, don't skip

    is_idx = _yf_is_us_equity_index(yf_ticker)
    is_crypto = yf_ticker in GEMINI_CRYPTO_YF
    is_cfu = _yf_is_commodity_future(yf_ticker)
    if is_idx or is_crypto or is_cfu:
        return False  # Evaluate (strict threshold for non-standard hours where applicable)
    return True


def _is_intraday_market(market_or_title: dict | str, yf_ticker: str) -> bool:
    """True if market settles at a specific time (5pm, 4pm, etc.) — not daily close.
    For intraday, empirical P(daily close above X) is wrong; prefer volatility model."""
    title = _get_title(market_or_title)
    hour = _get_resolution_hour_from_title(title)
    if hour is None:
        return False
    return (
        _yf_is_us_equity_index(yf_ticker)
        or yf_ticker in GEMINI_CRYPTO_YF
        or _yf_is_commodity_future(yf_ticker)
    )


def _is_other_intraday(market_or_title: dict | str, yf_ticker: str) -> bool:
    """
    True if this is an intraday market at a time OTHER than our standard 5pm crypto / 4pm US index
    / 4–5pm commodity futures window.
    E.g. "Bitcoin today at 8pm ET" → use strict "almost certain" threshold (intraday_other_min_p_win).
    """
    title = _get_title(market_or_title)
    hour = _get_resolution_hour_from_title(title)
    if hour is None:
        return False

    is_idx = _yf_is_us_equity_index(yf_ticker)
    is_crypto = yf_ticker in GEMINI_CRYPTO_YF
    is_cfu = _yf_is_commodity_future(yf_ticker)
    if is_idx and hour == 16:
        return False  # Standard 4pm US equity index
    if is_crypto and hour == 17:
        return False  # Standard 5pm crypto
    if is_cfu and hour in (16, 17):
        return False  # US cash-session close window for futures-backed titles
    # Has intraday time but not standard → other intraday
    return is_idx or is_crypto or is_cfu


_ULTRA_SHORT_TITLE_RE = re.compile(
    r"(?i)\b15\s*min|15m\b|next\s+fifteen|up\s+in\s+next\s+15",
)


def _hours_until_resolution_unified(market_or_title: dict | str, yf_ticker: str) -> float | None:
    """Hours until resolution from API fields (Kalshi/Gemini) or parsed title. None if unknown."""
    if isinstance(market_or_title, dict):
        res_dt = _resolution_utc_from_market_or_event(market_or_title)
        if res_dt is not None:
            return (res_dt - datetime.now(UTC)).total_seconds() / 3600.0
    title = _get_title(market_or_title)
    res_dt = _parse_resolution_datetime_from_title(title, yf_ticker)
    if res_dt is None:
        return None
    if res_dt.tzinfo is None:
        res_dt = res_dt.replace(tzinfo=ET)
    return (res_dt.astimezone(UTC) - datetime.now(UTC)).total_seconds() / 3600.0


def _minutes_until_resolution_unified(market_or_title: dict | str, yf_ticker: str) -> float | None:
    """Minutes until resolution; None if unknown."""
    h = _hours_until_resolution_unified(market_or_title, yf_ticker)
    if h is None:
        return None
    return max(0.0, h * 60.0)


def _is_ultra_short_horizon(
    market_or_title: dict | str, yf_ticker: str, sp: _StrategyParams
) -> bool:
    """
    True for sub-hour style contracts: title hints (15m) or time-to-expiry <= ultra_short_dte_hours.
    When ultra_short_dte_hours <= 0, only title / ticker patterns trigger (e.g. Kalshi KX*15M, Gemini *15M).
    """
    if isinstance(market_or_title, dict):
        ser = str(market_or_title.get("series") or "")
        tkr = str(market_or_title.get("ticker") or "")
        # Kalshi: KXBTC15M, KXDOGE15M, …; Gemini event tickers often *15M*
        if re.search(r"(?i)KX[A-Z0-9]*15M|[A-Z]{2,10}15M", f"{ser} {tkr}"):
            return True
    title = _get_title(market_or_title)
    if _ULTRA_SHORT_TITLE_RE.search(title):
        return True
    if sp.ultra_short_dte_hours <= 0:
        return False
    h = _hours_until_resolution_unified(market_or_title, yf_ticker)
    if h is None:
        return False
    return 0 < h <= sp.ultra_short_dte_hours


def _strict_intraday_evaluation(
    market_or_title: dict | str, yf_ticker: str, sp: _StrategyParams
) -> bool:
    """
    Use recommend_strict for non-5pm/4pm intraday or ultra-short horizons.

    When barbell_soften_strict_intraday and barbell_enabled, strict calls also apply
    easy-win / consensus-sniper edge floors and barbell_strict_relaxed_min_p_win for base-shaped lines.
    """
    if _is_other_intraday(market_or_title, yf_ticker):
        return True
    return _is_ultra_short_horizon(market_or_title, yf_ticker, sp)


def _is_intraday_or_ultra_short(
    market_or_title: dict | str, yf_ticker: str, sp: _StrategyParams
) -> bool:
    """Intraday-style vol cap / EWMA / blend: clock intraday OR ultra-short (e.g. Kalshi KXBTC15M)."""
    if _is_intraday_market(market_or_title, yf_ticker):
        return True
    return _is_ultra_short_horizon(market_or_title, yf_ticker, sp)


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
# "Mar 23, 2026 at 4pm" — explicit date + year + time (try before _AT_TIME_ONLY to avoid "today")
_DATE_WITH_YEAR_PATTERN = re.compile(
    r"(?:on\s+)?"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(\d{1,2})\s*,\s*(\d{4})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*(?:ET|EDT|EST|Eastern))?",
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

    # 2. "on Mar 23, 2026 at 4pm" — explicit date + year (must run before _AT_TIME_ONLY)
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

    # 3. "at 5pm" (no today/tomorrow/explicit date) - assume today
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

    # 4. "on March 16" / "March 16 at 5pm" - explicit date (no year)
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
        # Hour: from "at 5pm" in match, or default 16 (4pm) US index / 17 (5pm) crypto & others
        default_hour = 16 if _yf_is_us_equity_index(yf_ticker) else 17
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


def _parse_iso8601_utc(value: str | None) -> datetime | None:
    """Parse API timestamps (Kalshi close_time, Gemini expiryDate) to UTC-aware datetime."""
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


def _resolution_utc_from_market_or_event(obj: dict) -> datetime | None:
    """
    Best-effort resolution instant from Kalshi market dict, Gemini event, or wrapped {market: ...}.
    Prefers close_time / expected_expiration_time (Kalshi) and expiryDate (Gemini).
    """
    if not isinstance(obj, dict):
        return None
    inner = obj.get("market", obj)
    for key in ("close_time", "expected_expiration_time", "expiration_time", "expiryDate", "latest_expiration_time"):
        raw = inner.get(key)
        if raw:
            dt = _parse_iso8601_utc(str(raw))
            if dt is not None:
                return dt
    for c in inner.get("contracts") or []:
        raw = c.get("expiryDate")
        if raw:
            dt = _parse_iso8601_utc(str(raw))
            if dt is not None:
                return dt
    return None


def _get_days_to_expiry_from_market(
    market_or_title: dict | str, yf_ticker: str, default_days: float = 30.0
) -> float:
    """
    Compute days (or fraction) until resolution.
    Uses API timestamps when present (critical for KXBTC15M / sub-hour titles without clock text),
    else _parse_resolution_datetime_from_title.
    """
    if isinstance(market_or_title, dict):
        res_dt = _resolution_utc_from_market_or_event(market_or_title)
        if res_dt is not None:
            now_utc = datetime.now(UTC)
            days = (res_dt - now_utc).total_seconds() / 86400.0
            return max(0.0007, days)  # ~1 min floor for very short-dated contracts

    title = _get_title(market_or_title)
    res_dt = _parse_resolution_datetime_from_title(title, yf_ticker)
    if res_dt is None:
        return default_days
    if res_dt.tzinfo is None:
        res_dt = res_dt.replace(tzinfo=ET)
    now_utc = datetime.now(UTC)
    res_utc = res_dt.astimezone(UTC)
    delta = res_utc - now_utc
    days = delta.total_seconds() / 86400.0
    return max(0.01, days)  # Minimum 0.01 days (~15 min) for numerical stability


def _hours_until_resolution(title: str, yf_ticker: str) -> float | None:
    """Hours until resolution (positive = future, negative = past). None if unparseable."""
    res_dt = _parse_resolution_datetime_from_title(title, yf_ticker)
    if res_dt is None:
        return None
    now_utc = datetime.now(UTC)
    res_utc = res_dt.astimezone(UTC)
    return (res_utc - now_utc).total_seconds() / 3600.0


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
def _empirical_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    *,
    recent_days: int = 90,
    recent_weight: float = 3.0,
    use_rolling_window: bool = True,
    weighting: str = "step_recent",
    exponential_halflife_bars: int = 14,
) -> float:
    """P(close above/below threshold) from historical closes with recency weighting.

    - If use_rolling_window, keep only the most recent `recent_days`.
    - weighting ``step_recent``: extra weight on the most recent `recent_days` slice (legacy).
    - weighting ``exponential``: exponentially decay weights toward older bars (halflife in bars).
    """
    if not closes:
        return 0.0
    data = closes[-recent_days:] if (use_rolling_window and recent_days > 0) else closes
    n = len(data)
    if n == 0:
        return 0.0
    wmode = (weighting or "step_recent").strip().lower()
    if wmode == "exponential":
        hl = max(1.0, float(exponential_halflife_bars))
        lam = math.log(2) / hl
        if np is not None:
            arr = np.asarray(data, dtype=float)
            # Newest observation gets highest weight: index n-1 has age 0
            ages = np.arange(n - 1, -1, -1, dtype=float)
            w = np.exp(-lam * ages)
            w = w / max(1e-12, float(w.sum()))
            if direction == "above":
                hits = (arr > threshold).astype(float)
            else:
                hits = (arr < threshold).astype(float)
            return float(np.dot(hits, w))
        tw = 0.0
        wins = 0.0
        for i, c in enumerate(data):
            age = n - 1 - i
            wi = math.exp(-lam * age)
            tw += wi
            if direction == "above":
                wins += wi if c > threshold else 0.0
            else:
                wins += wi if c < threshold else 0.0
        return float(wins / tw) if tw > 0 else 0.0
    if np is not None:
        arr = np.asarray(data, dtype=float)
        if recent_days <= 0 or recent_weight <= 1.0:
            if direction == "above":
                return float(np.mean(arr > threshold))
            return float(np.mean(arr < threshold))
        weights = np.ones(n, dtype=float)
        cutoff = max(0, n - recent_days)
        weights[cutoff:] = float(recent_weight)
        if direction == "above":
            wins = np.where(arr > threshold, weights, 0.0).sum()
        else:
            wins = np.where(arr < threshold, weights, 0.0).sum()
        total_w = float(weights.sum())
        return float(wins / total_w) if total_w > 0 else 0.0

    if recent_days <= 0 or recent_weight <= 1.0:
        if direction == "above":
            return sum(1 for c in data if c > threshold) / n
        return sum(1 for c in data if c < threshold) / n

    cutoff = max(0, n - recent_days)
    wins = 0.0
    total_w = 0.0
    for i, c in enumerate(data):
        w = recent_weight if i >= cutoff else 1.0
        total_w += w
        if direction == "above":
            wins += w if c > threshold else 0.0
        else:
            wins += w if c < threshold else 0.0
    return wins / total_w if total_w > 0 else 0.0


def _empirical_ci_halfwidth(p_hat: float, n: int, z: float = 1.64) -> float:
    """Approximate confidence half-width for Bernoulli p (Wilson-ish normal approx)."""
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, p_hat))
    return z * math.sqrt(max(1e-9, p * (1 - p) / n))


def _dynamic_empirical_weight(base_w: float | None, sample_n: int, n0: float) -> float:
    """Dynamic empirical weight: more empirical with more samples, more vol with fewer."""
    base = 0.5 if base_w is None else float(base_w)
    base = max(0.0, min(1.0, base))
    n = max(0.0, float(sample_n))
    n0 = max(1.0, float(n0))
    data_conf = n / (n + n0)
    # Blend configured preference with data confidence.
    w = 0.5 * base + 0.5 * data_conf
    return max(0.1, min(0.9, w))


@dataclass
class _SignalFeatures:
    """Normalized feature set passed to the signal model."""

    emp_p: float
    vol_p: float
    market_yes: float
    edge_yes: float
    edge_no: float
    moneyness: float
    dte_norm: float
    spread_norm: float
    liquidity_consistency: float


def _clip01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _fit_probability_calibrator(
    sp: _StrategyParams,
    cfg: dict,
) -> callable | None:
    """
    Fit a lightweight sklearn calibrator on resolved predictions (hist_p -> outcome).
    Returns callable p->calibrated_p or None if unavailable/insufficient data.
    """
    if not sp.use_sklearn_calibration or not get_predictions_for_backtest:
        return None
    if (IsotonicRegression is None) or (LogisticRegression is None):
        return None
    try:
        rows = get_predictions_for_backtest(resolved_only=True, config=cfg) or []
        train = [
            r for r in rows
            if r.get("hist_p") is not None and r.get("outcome") in ("win", "lose")
        ]
        if len(train) < max(10, sp.sklearn_calibration_min_samples):
            return None
        x = [float(r["hist_p"]) for r in train]
        y = [1 if r["outcome"] == "win" else 0 for r in train]
        method = (sp.sklearn_calibration_method or "isotonic").strip().lower()
        if method == "platt":
            lr = LogisticRegression(solver="lbfgs")
            x2 = [[v] for v in x]
            lr.fit(x2, y)

            def _calibrate(p: float) -> float:
                return _clip01(float(lr.predict_proba([[float(p)]])[0][1]))

            return _calibrate
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(x, y)

        def _calibrate(p: float) -> float:
            return _clip01(float(iso.predict([float(p)])[0]))

        return _calibrate
    except Exception:
        return None


def _fit_signal_model_weights(
    sp: _StrategyParams,
    cfg: dict,
) -> dict[str, float] | None:
    """
    Learn signal-model feature weights from resolved predictions.

    Uses logistic regression on available prediction columns. Features that are not
    historically stored (e.g. moneyness, dte_norm) remain at default runtime weights.
    """
    if (
        not sp.enable_auto_signal_weight_fit
        or not get_predictions_for_backtest
        or LogisticRegression is None
    ):
        return None
    try:
        rows = get_predictions_for_backtest(resolved_only=True, config=cfg) or []
        train = [
            r for r in rows
            if r.get("hist_p") is not None and r.get("outcome") in ("win", "lose")
        ]
        if len(train) < max(30, sp.signal_weight_fit_min_samples):
            return None

        # Feature schema mirrors the runtime scorer where possible.
        feat_names = [
            "emp_p",
            "vol_p",
            "market_yes",
            "edge_yes",
            "edge_no",
            "spread_norm",
            "liquidity_consistency",
            "macro_24h_ret",
        ]
        x_rows: list[list[float]] = []
        y_rows: list[int] = []

        for r in train:
            emp_p = _clip01(float(r.get("hist_p") or 0.5))
            vol_p = _clip01(float(r.get("vol_p") if r.get("vol_p") is not None else emp_p))
            market_yes = r.get("bid_at_place")
            if market_yes is None:
                market_yes = r.get("theoretical_price")
            if market_yes is None:
                market_yes = r.get("entry_price")
            m = _clip01(float(market_yes or 0.5))
            spread_bps = float(r.get("spread_bps_at_place") or 0.0)
            spread_norm = _clip01(spread_bps / 10_000.0)

            edge_yes = max(-1.0, min(1.0, emp_p - m))
            edge_no = max(-1.0, min(1.0, (1.0 - emp_p) - (1.0 - m)))
            liq_consistency = 1.0  # Not persisted historically; neutral default.

            # Historical rows currently do not persist macro features, so keep neutral 0.0.
            x_rows.append([emp_p, vol_p, m, edge_yes, edge_no, spread_norm, liq_consistency, 0.0])
            y_rows.append(1 if r.get("outcome") == "win" else 0)

        if len(x_rows) < max(30, sp.signal_weight_fit_min_samples):
            return None

        lr = LogisticRegression(
            solver="lbfgs",
            max_iter=1000,
            C=max(1e-4, float(sp.signal_weight_fit_l2_c)),
            class_weight="balanced",
        )
        lr.fit(x_rows, y_rows)
        coefs = lr.coef_[0]
        learned = {"bias": float(lr.intercept_[0])}
        for i, name in enumerate(feat_names):
            learned[name] = float(coefs[i])
        return learned
    except Exception:
        return None


def _build_signal_features(
    *,
    emp_p: float,
    vol_p: float | None,
    market_yes: float,
    threshold: float,
    current_price: float,
    dte_days: float | None,
    spread_bps: float | None,
    no_bid: float | None,
) -> _SignalFeatures:
    vol = _clip01(vol_p if vol_p is not None else emp_p)
    mkt = _clip01(market_yes)
    edge_yes = emp_p - mkt
    edge_no = (1.0 - emp_p) - (1.0 - mkt)
    if threshold > 0 and current_price > 0:
        moneyness = math.log(max(1e-9, current_price / threshold))
    else:
        moneyness = 0.0
    dte_norm = _clip01((float(dte_days or 0.0)) / 30.0)
    spread_norm = _clip01((float(spread_bps or 0.0)) / 10_000.0)
    consistency = 1.0
    if no_bid is not None:
        consistency = 1.0 - abs((mkt + _clip01(float(no_bid))) - 1.0)
    return _SignalFeatures(
        emp_p=_clip01(emp_p),
        vol_p=vol,
        market_yes=mkt,
        edge_yes=max(-1.0, min(1.0, edge_yes)),
        edge_no=max(-1.0, min(1.0, edge_no)),
        moneyness=max(-2.0, min(2.0, moneyness)),
        dte_norm=dte_norm,
        spread_norm=spread_norm,
        liquidity_consistency=_clip01(consistency),
    )


def _score_signal_model(features: _SignalFeatures, sp: _StrategyParams) -> float:
    """
    Simple interpretable signal model: weighted logistic over normalized features.
    Weights are fully configurable in strategy.signal_model_weights.
    """
    w = {
        "bias": 0.0,
        "emp_p": 1.8,
        "vol_p": 1.3,
        "market_yes": 0.3,
        "edge_yes": 1.2,
        "edge_no": -0.8,
        "moneyness": 0.2,
        "dte_norm": 0.1,
        "spread_norm": -0.5,
        "liquidity_consistency": 0.3,
    }
    w.update(sp.signal_model_weights or {})
    z = (
        float(w["bias"])
        + float(w["emp_p"]) * features.emp_p
        + float(w["vol_p"]) * features.vol_p
        + float(w["market_yes"]) * features.market_yes
        + float(w["edge_yes"]) * features.edge_yes
        + float(w["edge_no"]) * features.edge_no
        + float(w["moneyness"]) * features.moneyness
        + float(w["dte_norm"]) * features.dte_norm
        + float(w["spread_norm"]) * features.spread_norm
        + float(w["liquidity_consistency"]) * features.liquidity_consistency
    )
    if np is not None:
        return _clip01(float(1.0 / (1.0 + np.exp(-z))))
    return _clip01(1.0 / (1.0 + math.exp(-z)))


def _regime_adjusted_recent_weight(
    closes: list[float],
    base_recent_weight: float,
    lookback: int = 30,
    vol_ratio_trigger: float = 1.5,
    boost: float = 1.5,
) -> float:
    """Increase recency weight in fast-changing regimes (recent vol >> long-run vol)."""
    if base_recent_weight <= 1.0 or len(closes) < max(lookback + 2, 20):
        return base_recent_weight
    rets: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < max(lookback, 10):
        return base_recent_weight
    recent = rets[-lookback:]
    full = rets
    recent_vol = math.sqrt(sum(r * r for r in recent) / max(1, len(recent)))
    full_vol = math.sqrt(sum(r * r for r in full) / max(1, len(full)))
    if full_vol <= 1e-12:
        return base_recent_weight
    ratio = recent_vol / full_vol
    return base_recent_weight * boost if ratio >= vol_ratio_trigger else base_recent_weight


def _validate_threshold_with_market(
    *,
    threshold: float,
    direction: str,
    current_price: float,
    yes_bid: float,
    tolerance_pct: float,
) -> bool:
    """Cross-check threshold extraction against current price and market probability."""
    if threshold <= 0 or current_price <= 0 or not (0 <= yes_bid <= 1):
        return False
    tol = max(0.0, tolerance_pct)
    # If price is clearly above threshold, YES should not be tiny.
    if direction == "above" and current_price > threshold * (1 + tol) and yes_bid < 0.2:
        return False
    # If price is clearly below threshold, YES should not be near-certain.
    if direction == "above" and current_price < threshold * (1 - tol) and yes_bid > 0.8:
        return False
    # Mirror check for "below" contracts.
    if direction == "below" and current_price < threshold * (1 - tol) and yes_bid < 0.2:
        return False
    if direction == "below" and current_price > threshold * (1 + tol) and yes_bid > 0.8:
        return False
    return True


def _get_closes_for_empirical(
    yf_ticker: str,
    days_back: int,
    closes_daily: list[float],
    market_or_title: dict | str,
    sp: "_StrategyParams",
    cfg: dict,
    kalshi_series: str | None = None,
) -> list[float]:
    """For intraday + use_intraday_empirical: return prices at resolution hour; else daily closes."""
    if _is_intraday_or_ultra_short(market_or_title, yf_ticker, sp) and sp.use_intraday_empirical:
        hour = _get_resolution_hour_from_title(_get_title(market_or_title))
        if hour is not None:
            intraday = yf_get_intraday_aggs(yf_ticker, days_back, hour, cfg)
            if intraday:
                return apply_underlying_closes(cfg, kalshi_series or "", intraday)
    return closes_daily


def _ewma_lambda_for_market(sp: "_StrategyParams", market_or_title: dict | str, yf_ticker: str) -> float:
    """Use a faster EWMA decay for intraday contracts when configured."""
    if _is_intraday_or_ultra_short(market_or_title, yf_ticker, sp):
        return float(sp.intraday_ewma_lambda)
    return float(sp.ewma_lambda)


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
    if np is not None:
        arr = np.asarray(returns, dtype=float)
        init_n = min(20, max(3, n // 2))
        var = float(np.mean(arr[:init_n] ** 2))
        for i in range(init_n, n):
            var = lam * var + (1 - lam) * float(arr[i] ** 2)
        return var
    # Initialize with sample variance of first half (or first 20)
    init_n = min(20, max(3, n // 2))
    init_var = sum(r**2 for r in returns[:init_n]) / init_n
    var = init_var
    for i in range(init_n, n):
        var = lam * var + (1 - lam) * (returns[i] ** 2)
    return var


def _sample_variance(returns: list[float]) -> float:
    if len(returns) < 3:
        return 0.0
    mean_return = sum(returns) / len(returns)
    return sum((r - mean_return) ** 2 for r in returns) / len(returns)


def _garch_cache_key(returns: list[float]) -> tuple[int, int, int]:
    if not returns:
        return (0, 0, 0)
    tail = returns[-min(24, len(returns)) :]
    first = int(round(tail[0] * 1_000_000))
    last = int(round(tail[-1] * 1_000_000))
    return (len(returns), first, last)


def _garch_variance(returns: list[float], min_returns: int = 80) -> float | None:
    """
    One-step-ahead GARCH(1,1) conditional variance estimate.
    Returns None when dependency/model fitting is unavailable.
    """
    if arch_model is None or len(returns) < max(30, int(min_returns)):
        return None
    key = _garch_cache_key(returns)
    cached = _GARCH_VAR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        series = [float(r) * 100.0 for r in returns]  # stabilize optimizer scale
        model = arch_model(series, p=1, q=1, mean="Zero", vol="GARCH", dist="normal", rescale=False)
        fit = model.fit(disp="off")
        forecast = fit.forecast(horizon=1)
        var_pct2 = float(forecast.variance.values[-1, 0])
        variance = max(0.0, var_pct2 / 10_000.0)
        _GARCH_VAR_CACHE[key] = variance
        return variance
    except Exception:
        return None


def _volatility_probability(
    closes: list[float],
    threshold: float,
    direction: str,
    days_to_expiry: float = 30.0,
    use_ewma: bool = False,
    ewma_lambda: float = 0.94,
    current_price: float | None = None,
    *,
    vol_model: str = "auto",
    garch_min_returns: int = 80,
    enable_otm_smile_adjustment: bool = True,
    otm_smile_strength: float = 0.35,
    otm_smile_max_mult: float = 2.0,
    distribution: str = "normal",
    t_df: float = 6.0,
) -> float | None:
    """
    Log-normal / Black-Scholes style: P(price above/below K at expiry) using historical vol.
    use_ewma: if True, use EWMA variance instead of sample variance (time-varying vol).
    current_price: if provided and >0, use as S0 (live price) instead of closes[-1]; reflects
        very recent moves for better safety. Fallback to closes[-1] when None.
    Returns None if insufficient data.
    """
    if not closes or len(closes) < 5 or threshold <= 0:
        return None
    S0 = float(current_price) if (current_price is not None and current_price > 0) else float(closes[-1])
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
    model = (vol_model or "auto").strip().lower()
    variance = 0.0
    if model in {"garch", "auto"}:
        garch_var = _garch_variance(returns, min_returns=garch_min_returns)
        if garch_var is not None and garch_var > 0:
            variance = garch_var
    if variance <= 0 and (use_ewma or model in {"ewma", "auto"}):
        variance = _ewma_variance(returns, ewma_lambda)
    if variance <= 0:
        variance = _sample_variance(returns)
    sigma_daily = variance ** 0.5
    sigma_annual = sigma_daily * (252 ** 0.5)
    # OTM smile adjustment: increase vol for OTM strikes so tail probabilities are less understated.
    if enable_otm_smile_adjustment and sigma_annual > 0:
        m = math.log(max(1e-12, threshold / S0))
        is_otm_for_yes = (direction == "above" and threshold > S0) or (
            direction == "below" and threshold < S0
        )
        if is_otm_for_yes:
            mult = 1.0 + max(0.0, otm_smile_strength) * abs(m)
            mult = min(max(1.0, mult), max(1.0, otm_smile_max_mult))
            sigma_annual *= mult
    T = max(1e-6, days_to_expiry / 365.0)
    if sigma_annual < 1e-8:
        return None
    # d = (ln(S0/K) - sigma^2*T/2) / (sigma*sqrt(T))
    d = (math.log(S0 / threshold) - 0.5 * sigma_annual**2 * T) / (sigma_annual * (T ** 0.5))
    dist = (distribution or "normal").strip().lower()
    if dist in ("student_t", "student-t", "t"):
        # Heavy-tail model for crypto: preserves more probability mass in extremes.
        df = max(2.1, float(t_df))
        if scipy_t is not None:
            phi_d = float(scipy_t.cdf(d, df=df))
        elif scipy_norm is not None:
            # Approximate fallback if scipy t-distribution isn't available.
            scale = math.sqrt(df / max(1.0, (df - 2.0)))
            phi_d = float(scipy_norm.cdf(d / scale))
        else:
            phi_d = 0.5 * (1 + math.erf((d / math.sqrt(df / max(1.0, (df - 2.0)))) / (2 ** 0.5)))
    elif scipy_norm is not None:
        phi_d = float(scipy_norm.cdf(d))
    else:
        # Φ(x) = (1 + erf(x/sqrt(2))) / 2
        phi_d = 0.5 * (1 + math.erf(d / (2 ** 0.5)))
    phi_d = max(0.0, min(1.0, phi_d))
    if direction == "above":
        return phi_d
    return 1.0 - phi_d


def _monte_carlo_probability(
    S0: float,
    sigma_annual: float,
    T: float,
    threshold: float,
    direction: str,
    n_paths: int = 10000,
    distribution: str = "normal",
    t_df: float = 6.0,
) -> float:
    """
    Monte Carlo GBM simulation: run n_paths price paths and return fraction
    where S_T crosses threshold. Multi-step (daily) to capture path-dependent
    effects; Student-t innovations for heavy-tail realism.
    """
    if S0 <= 0 or sigma_annual < 1e-8 or T <= 0 or threshold <= 0:
        return 0.5
    n_steps = max(1, round(T * 365))
    dt = T / n_steps
    drift = -0.5 * sigma_annual ** 2 * dt
    diffusion = sigma_annual * math.sqrt(dt)
    dist = (distribution or "normal").strip().lower()
    use_t = dist in ("student_t", "student-t", "t")
    df = max(2.1, float(t_df)) if use_t else 0.0
    t_scale = math.sqrt((df - 2.0) / df) if use_t and df > 2.0 else 1.0

    if np is not None:
        rng = np.random.default_rng()
        if use_t and scipy_t is not None:
            innovations = scipy_t.rvs(df, size=(n_paths, n_steps), random_state=rng) * t_scale
        elif use_t:
            innovations = rng.standard_normal((n_paths, n_steps)) * t_scale
        else:
            innovations = rng.standard_normal((n_paths, n_steps))
        log_returns = drift + diffusion * innovations
        log_S_T = math.log(S0) + np.sum(log_returns, axis=1)
        S_T = np.exp(log_S_T)
        if direction == "above":
            return float(np.mean(S_T >= threshold))
        return float(np.mean(S_T <= threshold))

    hits = 0
    for _ in range(n_paths):
        log_s = math.log(S0)
        for _ in range(n_steps):
            z = random.gauss(0, t_scale)
            log_s += drift + diffusion * z
        S_T = math.exp(log_s)
        if direction == "above":
            hits += (S_T >= threshold)
        else:
            hits += (S_T <= threshold)
    return hits / n_paths


def _volatility_probability_with_mc(
    closes: list[float],
    threshold: float,
    direction: str,
    days_to_expiry: float = 30.0,
    use_ewma: bool = False,
    ewma_lambda: float = 0.94,
    current_price: float | None = None,
    *,
    vol_model: str = "auto",
    garch_min_returns: int = 80,
    enable_otm_smile_adjustment: bool = True,
    otm_smile_strength: float = 0.35,
    otm_smile_max_mult: float = 2.0,
    distribution: str = "normal",
    t_df: float = 6.0,
    mc_paths: int = 10000,
    mc_blend_weight: float = 0.3,
) -> float | None:
    """
    Blended analytical + Monte Carlo volatility probability.
    Runs _volatility_probability for the analytical CDF result, then
    _monte_carlo_probability for a simulation-based estimate, and returns
    the weighted blend. mc_blend_weight controls MC influence (0 = pure
    analytical, 1 = pure MC).
    """
    analytical = _volatility_probability(
        closes, threshold, direction, days_to_expiry,
        use_ewma=use_ewma, ewma_lambda=ewma_lambda,
        current_price=current_price,
        vol_model=vol_model, garch_min_returns=garch_min_returns,
        enable_otm_smile_adjustment=enable_otm_smile_adjustment,
        otm_smile_strength=otm_smile_strength,
        otm_smile_max_mult=otm_smile_max_mult,
        distribution=distribution, t_df=t_df,
    )
    if analytical is None:
        return None
    if mc_paths <= 0 or mc_blend_weight <= 0:
        return analytical
    S0 = float(current_price) if (current_price is not None and current_price > 0) else float(closes[-1])
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))
    if len(returns) < 3:
        return analytical
    model = (vol_model or "auto").strip().lower()
    variance = 0.0
    if model in {"garch", "auto"}:
        gv = _garch_variance(returns, min_returns=garch_min_returns)
        if gv is not None and gv > 0:
            variance = gv
    if variance <= 0 and (use_ewma or model in {"ewma", "auto"}):
        variance = _ewma_variance(returns, ewma_lambda)
    if variance <= 0:
        variance = _sample_variance(returns)
    sigma_daily = variance ** 0.5
    sigma_annual = sigma_daily * (252 ** 0.5)
    if enable_otm_smile_adjustment and sigma_annual > 0:
        m = math.log(max(1e-12, threshold / S0))
        is_otm_for_yes = (direction == "above" and threshold > S0) or (
            direction == "below" and threshold < S0
        )
        if is_otm_for_yes:
            mult = 1.0 + max(0.0, otm_smile_strength) * abs(m)
            mult = min(max(1.0, mult), max(1.0, otm_smile_max_mult))
            sigma_annual *= mult
    T = max(1e-6, days_to_expiry / 365.0)
    mc = _monte_carlo_probability(
        S0, sigma_annual, T, threshold, direction,
        n_paths=mc_paths, distribution=distribution, t_df=t_df,
    )
    w = max(0.0, min(1.0, mc_blend_weight))
    return (1.0 - w) * analytical + w * mc


def _dte_adjusted_strike_pct(base_pct: float, dte_days: float, reference_days: float = 1.0) -> float:
    """Scale a strike-distance percentage by sqrt(time) so that short-dated
    contracts get tighter limits.  For dte >= reference_days the base_pct
    is returned unchanged."""
    if dte_days >= reference_days or reference_days <= 0:
        return base_pct
    ratio = max(0.0, dte_days) / reference_days
    return base_pct * math.sqrt(max(1e-9, ratio))


def _dte_adjusted_empirical_weight(
    base_w: float, dte_days: float, reference_days: float = 1.0,
) -> float:
    """Reduce the empirical model's weight when DTE is very short.
    Empirical probability doesn't condition on current price or remaining
    time, so it becomes unreliable for sub-day horizons.  We linearly
    decay the empirical weight toward 0 as dte shrinks below
    reference_days.  For dte >= reference_days the weight is unchanged."""
    if dte_days >= reference_days or reference_days <= 0:
        return base_w
    ratio = max(0.0, dte_days) / reference_days
    return base_w * ratio


def _cap_intraday_vol_p(vol_p: float | None, cap: float) -> float | None:
    """Clamp vol-model output for intraday contracts to prevent extreme
    overconfidence (e.g. 99.6% on an hourly ETH contract)."""
    if vol_p is None:
        return None
    return min(float(vol_p), max(0.50, float(cap)))


def _vol_disagree_dampened_blend(
    emp: float,
    vol_p: float,
    w_emp: float,
    disagree_threshold: float,
    dampen_factor: float,
) -> float:
    """Blend empirical and vol-model probabilities, pulling the vol-model
    weight down when the two models strongly disagree.

    When |emp - vol_p| > disagree_threshold, reduce the vol weight by
    dampen_factor (e.g. 0.50 → halve the vol contribution), shifting the
    blend toward the more conservative empirical estimate.
    """
    gap = abs(emp - vol_p)
    w_vol = 1.0 - w_emp
    if gap > disagree_threshold and disagree_threshold > 0:
        excess = (gap - disagree_threshold) / max(0.01, 1.0 - disagree_threshold)
        penalty = dampen_factor + (1.0 - dampen_factor) * (1.0 - min(1.0, excess))
        w_vol *= penalty
    total = w_emp + w_vol
    if total <= 0:
        return emp
    return (w_emp * emp + w_vol * vol_p) / total


def _joint_dual_high_pessimism(
    blended: float,
    emp: float,
    vol_p: float,
    sp: _StrategyParams,
) -> float:
    """
    When empirical and vol paths *both* sit at extreme high P, disagreement dampening
    never fires; pull the blend toward 50% to reduce joint overconfidence (PnL).
    """
    if not sp.joint_high_confidence_enabled or sp.joint_high_pull_to_half <= 0:
        return blended
    t = float(sp.joint_high_emp_vol_threshold)
    if min(float(emp), float(vol_p)) < t:
        return blended
    lam = float(sp.joint_high_pull_to_half)
    b = float(blended)
    return b * (1.0 - lam) + 0.5 * lam


def _vol_emp_blend_hist_p_with_joint_trace(
    emp: float,
    vol_p: float,
    w_emp: float,
    sp: _StrategyParams,
) -> tuple[float, float]:
    """Empirical–vol dampened blend: (P before joint-high pull, P after)."""
    blended = _vol_disagree_dampened_blend(
        emp, vol_p, w_emp, sp.vol_disagree_threshold, sp.vol_disagree_dampen_blend
    )
    after = _joint_dual_high_pessimism(blended, emp, vol_p, sp)
    return blended, after


def _vol_emp_blend_hist_p(
    emp: float,
    vol_p: float | None,
    w_emp: float,
    sp: _StrategyParams,
) -> float:
    """Empirical–vol blend + vol-disagreement dampening + joint high-P pessimism."""
    if vol_p is None:
        return float(emp)
    _, after = _vol_emp_blend_hist_p_with_joint_trace(emp, vol_p, w_emp, sp)
    return after


def _print_barbell_joint_blend_diagnostic(
    sp: _StrategyParams,
    *,
    emp: float,
    vol_p: float,
    yes_bid: float,
    no_bid: float,
    maker_fee_rate: float,
    joint_pre: float | None,
    joint_post: float | None,
    final_p_yes: float,
) -> None:
    """
    Log whether joint-high pessimism moves vol/emp blend across barbell *base* gates
    (hypothetical BUY YES / BUY NO at current quotes). Final trade logic uses final_p_yes
    after shrinkage / signal / anti-hallucination.
    """
    if not sp.barbell_enabled or joint_pre is None or joint_post is None:
        return
    yb = _barbell_yes_bucket(joint_pre, yes_bid, maker_fee_rate, sp)
    ya = _barbell_yes_bucket(joint_post, yes_bid, maker_fee_rate, sp)
    nb = _barbell_no_bucket(joint_pre, no_bid, maker_fee_rate, sp)
    na = _barbell_no_bucket(joint_post, no_bid, maker_fee_rate, sp)
    dual_zone = min(float(emp), float(vol_p)) >= float(sp.joint_high_emp_vol_threshold)
    zone_lbl = (
        f"dual-high zone min(emp,vol)≥{sp.joint_high_emp_vol_threshold:.0%}"
        if dual_zone
        else f"outside dual-high zone (<{sp.joint_high_emp_vol_threshold:.0%})"
    )
    def _buck(x: str | None) -> str:
        return x if x else "—"

    warn = ""
    if yb == "base" and ya != "base":
        warn = "  [WARN] joint-high pulled YES leg out of base bucket @ blend"
    elif nb == "base" and na != "base":
        warn = "  [WARN] joint-high pulled NO leg out of base bucket @ blend"
    print(
        f"    ├─ Barbell@blend: P(YES) {joint_pre:.1%}→{joint_post:.1%} "
        f"(joint_high λ={sp.joint_high_pull_to_half:.2f}, {zone_lbl}); "
        f"YES-base: {_buck(yb)}→{_buck(ya)}; NO-base: {_buck(nb)}→{_buck(na)}; "
        f"final P(YES) {final_p_yes:.1%} (used for rec)"
        f"{warn}"
    )


def _adaptive_stress_size_multiplier(bump: float, sp: _StrategyParams) -> float:
    """Shrink Kelly sizing when adaptive edge bump is large (edge strictness + smaller risk)."""
    if not sp.adaptive_learning_enabled or bump <= 0:
        return 1.0
    thresh = float(sp.adaptive_stress_size_edge_threshold)
    if bump < thresh:
        return 1.0
    cap = max(1e-6, float(sp.adaptive_edge_max_extra) + float(sp.loss_streak_max_extra))
    floor_m = float(sp.adaptive_stress_size_min_mult)
    span = max(1e-6, cap - thresh)
    t = min(1.0, max(0.0, (float(bump) - thresh) / span))
    return 1.0 - t * (1.0 - floor_m)


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


def _apply_bucket_penalty(
    hist_p: float,
    calibration: dict[str, Any],
    sp: _StrategyParams,
) -> float:
    """
    Dampen hist_p when its bucket has lower win rate than expected (overconfidence).
    effective_hist_p = hist_p * (bucket_win_rate / bucket_midpoint) when bucket underperforms.
    """
    if not sp.use_bucket_penalty or not sp.use_calibration or not calibration:
        return hist_p
    buckets = calibration.get("buckets", [])
    if not buckets:
        return hist_p
    lo = round(hist_p * 10) / 10
    hi = lo + 0.1
    midpoint = (lo + hi) / 2
    for b in buckets:
        if b.get("lo") == lo and b.get("hi") == hi:
            cnt = b.get("count", 0)
            wr = b.get("win_rate", midpoint)
            if cnt >= sp.min_bucket_samples and wr < midpoint:
                scale = wr / midpoint if midpoint > 0 else 1.0
                return max(0.01, min(0.99, hist_p * scale))
            break
    return hist_p


def _get_effective_min_edge(
    min_edge: float,
    direction: str,
    calibration: dict[str, Any],
    sp: _StrategyParams,
) -> float:
    """Apply calibration-based min_edge adjustments (overall + above+yes)."""
    if not sp.use_calibration or not calibration:
        return min_edge
    effective = min_edge
    resolved = calibration.get("resolved_count", 0)
    overall_wr = calibration.get("win_rate")
    if (
        resolved >= sp.min_calibration_samples_overall
        and overall_wr is not None
        and overall_wr < 0.5
    ):
        effective += sp.calibration_extra_edge_overall
    # Weekend/illiquid session risk premium.
    if sp.weekend_edge_multiplier > 1.0:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:  # Sat/Sun ET
            effective *= sp.weekend_edge_multiplier
    return effective


def _simple_ev_recommend(
    p_yes: float,
    yes_bid: float,
    no_bid: float | None,
    min_edge_net: float,
    maker_fee_rate: float,
    min_fee_per_order: float,
    sp: _StrategyParams,
) -> str:
    """Single-threshold BUY YES / NO / SKIP from net edge after maker fee (symmetric legs)."""
    if yes_bid <= 0:
        return REC_SKIP
    nb = (
        float(no_bid)
        if no_bid is not None and float(no_bid) > 0
        else max(1e-6, min(0.99, 1.0 - float(yes_bid)))
    )
    edge_yes, edge_no = _net_edges_buy_legs(
        float(p_yes), float(yes_bid), nb, maker_fee_rate, min_fee_per_order
    )
    req_base = float(min_edge_net)
    req_yes = req_base
    req_no = req_base
    c_lo = min(sp.simple_ev_cheap_band_lo, sp.simple_ev_cheap_band_hi)
    c_hi = max(sp.simple_ev_cheap_band_lo, sp.simple_ev_cheap_band_hi)
    cheap_floor = float(sp.simple_ev_cheap_band_min_net_edge)
    if cheap_floor > 0.0:
        if c_lo <= float(yes_bid) <= c_hi:
            req_yes = max(req_yes, cheap_floor)
        if c_lo <= float(nb) <= c_hi:
            req_no = max(req_no, cheap_floor)
    hy = edge_yes >= req_yes
    hn = edge_no >= req_no
    if sp.simple_ev_skip_p_win_mid_enabled:
        m_lo = min(sp.simple_ev_skip_p_win_mid_lo, sp.simple_ev_skip_p_win_mid_hi)
        m_hi = max(sp.simple_ev_skip_p_win_mid_lo, sp.simple_ev_skip_p_win_mid_hi)
        py = float(p_yes)
        p_no = 1.0 - py
        if hy and m_lo <= py <= m_hi:
            hy = False
        if hn and m_lo <= p_no <= m_hi:
            hn = False
    if hy and hn:
        return REC_BUY_YES if edge_yes >= edge_no else REC_BUY_NO
    if hy:
        return REC_BUY_YES
    if hn:
        return REC_BUY_NO
    return REC_SKIP


def _simple_ev_net_min_edge_bundle(
    sp: _StrategyParams, market_or_event: dict | str, yf_ticker: str
) -> tuple[float, dict[str, Any]]:
    """
    Base simple_ev min_edge; extra × on ultra-short horizons; optional × when very close to resolution.
    Returns (effective_min, detail) for logging.
    """
    base = float(sp.simple_ev_min_edge)
    detail: dict[str, Any] = {
        "ultra_short": False,
        "horizon_mult": 1.0,
        "late_mult": 1.0,
        "minutes_left": None,
        "base": base,
    }
    eff = base
    us = _is_ultra_short_horizon(market_or_event, yf_ticker, sp)
    detail["ultra_short"] = us
    if us and sp.ultra_short_min_edge_mult > 1.0:
        eff *= float(sp.ultra_short_min_edge_mult)
        detail["horizon_mult"] = float(sp.ultra_short_min_edge_mult)
    mins = _minutes_until_resolution_unified(market_or_event, yf_ticker)
    detail["minutes_left"] = mins
    if (
        us
        and float(sp.ultra_short_late_minutes_threshold) > 0.0
        and mins is not None
        and mins < float(sp.ultra_short_late_minutes_threshold)
        and float(sp.ultra_short_late_min_edge_mult) > 1.0
    ):
        eff *= float(sp.ultra_short_late_min_edge_mult)
        detail["late_mult"] = float(sp.ultra_short_late_min_edge_mult)
    return eff, detail


def _simple_ev_calibration_size_mult(calibration: dict[str, Any] | None, sp: _StrategyParams) -> float:
    if not sp.use_calibration or not calibration:
        return 1.0
    resolved = int(calibration.get("resolved_count", 0) or 0)
    wr = calibration.get("win_rate")
    if resolved < sp.min_calibration_samples_overall or wr is None:
        return 1.0
    if float(wr) < float(sp.simple_ev_calibration_wr_threshold):
        return float(sp.simple_ev_calibration_poor_size_mult)
    return 1.0


def _print_effective_edge_trace(
    sp: _StrategyParams,
    min_edge_cfg: float,
    *,
    simple_mode: bool,
    simple_ev_effective: float | None = None,
    ultra_short_detail: dict[str, Any] | None = None,
    em0: float | None = None,
    effective_min_core: float | None = None,
    effective_min_yes: float | None = None,
    effective_min_no: float | None = None,
) -> None:
    usd0 = ultra_short_detail or {}
    always_ultra_short_line = bool(simple_mode and usd0.get("ultra_short"))
    if not sp.log_effective_min_edge and not always_ultra_short_line:
        return
    if simple_mode:
        eff = float(simple_ev_effective) if simple_ev_effective is not None else float(sp.simple_ev_min_edge)
        usd = ultra_short_detail or {}
        if usd.get("ultra_short"):
            mins = usd.get("minutes_left")
            mins_s = f"{float(mins):.1f}m" if mins is not None else "?"
            hm = float(usd.get("horizon_mult", 1.0))
            late = float(usd.get("late_mult", 1.0))
            late_part = ""
            if late > 1.0 + 1e-9:
                late_part = (
                    f" late_edge_mult={late:.2f} (t<{float(sp.ultra_short_late_minutes_threshold):g}m)"
                )
            sz = float(sp.simple_ev_ultra_short_size_mult)
            print(
                f"    ├─ ultra_short: horizon_edge_mult={hm:.2f}{late_part} "
                f"min_left={mins_s} size_mult={sz:.2f} → net_min={eff:.2%} "
                f"(base={sp.simple_ev_min_edge:.2%}; YES/NO net edges vs bid+fee)"
            )
        elif sp.log_effective_min_edge:
            if abs(eff - float(sp.simple_ev_min_edge)) > 1e-9:
                print(
                    f"    ├─ Edge trace: simple_ev net_min={eff:.2%} "
                    f"(base={sp.simple_ev_min_edge:.2%} × ultra_short_min_edge_mult on this horizon; "
                    f"single gate; YES/NO net edges use each leg bid + fee)"
                )
            else:
                print(
                    f"    ├─ Edge trace: simple_ev net_min={sp.simple_ev_min_edge:.2%} "
                    f"(single gate; no stacked penalties; YES/NO net edges use each leg bid + fee)"
                )
        if sp.log_effective_min_edge:
            _px = float((ultra_short_detail or {}).get("proxy_basis_extra_min_edge") or 0.0)
            if _px > 0.0:
                print(
                    f"    ├─ proxy_basis_extra_min_edge: +{_px:.2%} "
                    f"(underlying proxy vs Kalshi settlement index; e.g. RBOB vs AAA retail)"
                )
        return
    if (
        em0 is not None
        and effective_min_core is not None
        and effective_min_yes is not None
        and effective_min_no is not None
    ):
        print(
            f"    ├─ Edge trace: cfg_min={min_edge_cfg:.2%} "
            f"after_calib_weekend={em0:.2%} → stacked_core={effective_min_core:.2%} "
            f"→ req_yes={effective_min_yes:.2%} req_no={effective_min_no:.2%}"
        )


def _dte_edge_scale_factor(dte_days: float, sp: _StrategyParams) -> float:
    """
    Scale required min_edge: easier hurdle when DTE is short (less time for model error),
    harder when DTE is long ("dead zone" contracts).
    """
    if not sp.dte_edge_scaling_enabled:
        return 1.0
    d = max(0.0, float(dte_days))
    s_days = float(sp.dte_edge_short_threshold_days)
    l_days = max(s_days + 1e-6, float(sp.dte_edge_long_threshold_days))
    sm = float(sp.dte_edge_mult_short)
    lm = float(sp.dte_edge_mult_long)
    if d <= s_days:
        return sm
    if d >= l_days:
        return lm
    t = (d - s_days) / (l_days - s_days)
    return sm + t * (lm - sm)


def _volatility_spike_metrics(closes: list[float]) -> tuple[float | None, float | None]:
    """
    Return (current_vol, baseline_vol) using recent log-return windows.
    Uses short-vs-long realized vol as a regime-break proxy.
    """
    if not closes or len(closes) < 30:
        return None, None
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev and prev > 0 and curr > 0:
            rets.append(math.log(curr / prev))
    if len(rets) < 25:
        return None, None
    curr_win = rets[-6:]
    base_win = rets[-24:]
    curr_vol = math.sqrt(sum(r * r for r in curr_win) / max(1, len(curr_win)))
    base_vol = math.sqrt(sum(r * r for r in base_win) / max(1, len(base_win)))
    return curr_vol, base_vol


def _vol_ratio(curr_vol: float | None, base_vol: float | None) -> float:
    if curr_vol is None or base_vol is None or base_vol <= 0:
        return 1.0
    return max(0.0, float(curr_vol) / float(base_vol))


def _dynamic_stability_seconds(sp: "_StrategyParams", vol_ratio: float) -> int:
    base = max(0, int(sp.min_stability_duration_seconds))
    if base <= 0 or not sp.enable_dynamic_stability_window:
        return base
    ref = max(1e-6, float(sp.stability_vol_ratio_ref))
    ratio = max(1.0, float(vol_ratio) / ref)
    mult = min(max(1.0, ratio), max(1.0, float(sp.stability_max_multiplier)))
    return int(round(base * mult))


def _cross_exchange_sanity_block_side(
    sp: "_StrategyParams",
    *,
    momentum_ret: float,
    local_yes: float,
    reference_yes: float,
) -> str | None:
    """
    If one venue lags repricing vs another during a sharp move, block the fragile side:
    - Sharp down move + local YES too high vs reference => block BUY YES.
    - Sharp up move + local YES too low vs reference => block BUY NO.
    """
    if not sp.enable_cross_exchange_sanity_gate:
        return None
    if not (0 < local_yes < 1 and 0 < reference_yes < 1):
        return None
    if abs(momentum_ret) < max(0.0, sp.cross_exchange_spot_move_threshold_pct):
        return None
    gap = float(local_yes) - float(reference_yes)
    max_gap = max(0.0, float(sp.cross_exchange_max_yes_gap))
    if momentum_ret < 0 and gap >= max_gap:
        return "yes"
    if momentum_ret > 0 and gap <= -max_gap:
        return "no"
    return None


def _compute_directional_parity_penalty(
    sp: _StrategyParams,
    cfg: dict,
) -> tuple[float, float]:
    """
    Return (yes_penalty, no_penalty) as extra min_edge.
    Penalize dominant side from recent resolved outcomes so YES/NO mix self-corrects.
    """
    if not sp.parity_penalty_enabled or not get_predictions_for_backtest:
        return 0.0, 0.0
    try:
        rows = get_predictions_for_backtest(resolved_only=True, config=cfg) or []
        if not rows:
            return 0.0, 0.0
        cutoff = time.time() - max(1, sp.parity_lookback_days) * 86400
        recent = [r for r in rows if (r.get("resolved_ts") or 0) >= cutoff and r.get("side") in ("yes", "no")]
        if len(recent) < 20:
            return 0.0, 0.0
        yes_rows = [r for r in recent if r.get("side") == "yes"]
        no_rows = [r for r in recent if r.get("side") == "no"]
        if not yes_rows or not no_rows:
            return 0.0, 0.0
        yes_n, no_n = len(yes_rows), len(no_rows)
        dominant = "yes" if yes_n >= no_n else "no"
        dom_n = max(yes_n, no_n)
        other_n = min(yes_n, no_n)
        imbalance = (dom_n - other_n) / max(1, len(recent))

        def _wr(rows_side: list[dict]) -> float:
            wins = sum(1 for r in rows_side if r.get("outcome") == "win")
            return wins / max(1, len(rows_side))

        yes_wr = _wr(yes_rows)
        no_wr = _wr(no_rows)
        # If dominant side is also weaker, increase penalty faster.
        underperf = 0.0
        if dominant == "yes":
            underperf = max(0.0, no_wr - yes_wr)
        else:
            underperf = max(0.0, yes_wr - no_wr)
        penalty = sp.parity_base_penalty * (1.0 + 2.0 * imbalance + 3.0 * underperf)
        penalty = max(0.0, min(sp.parity_max_penalty, penalty))
        if dominant == "yes":
            return penalty, 0.0
        return 0.0, penalty
    except Exception:
        return 0.0, 0.0


def _should_downgrade_buy_yes_above(
    rec: str,
    direction: str,
    hist_p: float,
    yes_bid: float,
    min_edge: float,
    calibration: dict[str, Any],
    sp: _StrategyParams,
    maker_fee_rate: float,
) -> bool:
    """If BUY YES on 'above' and above+yes underperforms, require extra edge or downgrade to SKIP."""
    if rec != REC_BUY_YES or direction != "above":
        return False
    if not sp.use_calibration or not calibration:
        return False
    above_yes = calibration.get("by_direction_side", {}).get("above+yes", {})
    n_ay = int(above_yes.get("count", 0) or 0)
    wr_ay = float(above_yes.get("win_rate", 1.0) or 1.0)
    hard_thr = float(sp.calibration_hard_block_above_yes_win_rate)
    hard_n = int(sp.calibration_hard_block_above_yes_min_samples)
    min_avg_e = float(sp.calibration_hard_block_above_yes_min_avg_entry)
    avg_e = above_yes.get("avg_entry_price")
    if hard_thr > 0 and n_ay >= hard_n and wr_ay < hard_thr:
        if min_avg_e <= 0:
            return True
        if avg_e is not None and float(avg_e) >= min_avg_e:
            return True
    if n_ay < sp.min_calibration_samples:
        return False
    if wr_ay >= 0.5:
        return False
    fee = _fee_per_contract(yes_bid, maker_fee_rate, sp.min_fee_per_order)
    required = min_edge + sp.calibration_extra_edge_above_yes
    net_edge = hist_p - yes_bid - fee
    return net_edge < required


def _gemini_no_bid_from_event(event: dict, yes_bid_fallback: float) -> float:
    """
    Prefer explicit NO-contract bestBid when Gemini exposes a separate NO leg; else 1 − YES bid.
    """
    no_c = _get_no_contract(event) or {}
    raw = float(get_yes_price(no_c)) if no_c else 0.0
    if raw > 0:
        return max(1e-6, min(0.99, raw))
    y = float(yes_bid_fallback)
    return max(1e-6, min(0.99, 1.0 - y))


def _barbell_yes_bucket(
    hist_p: float,
    yes_bid: float,
    maker_fee_rate: float,
    sp: _StrategyParams,
) -> str | None:
    """Classify a potential BUY YES leg: base (high-P consensus) | alpha (cheap + edge) | None."""
    if not sp.barbell_enabled:
        return None
    yb = max(0.0, min(0.99, float(yes_bid)))
    hp = max(0.0, min(1.0, float(hist_p)))
    gap = float(sp.barbell_base_max_model_vs_market_gap)
    if (
        yb >= sp.barbell_base_min_market_yes
        and hp >= sp.barbell_base_min_model_p
        and hp + gap >= yb
        and yb <= sp.barbell_base_max_yes_price
    ):
        return "base"
    fee_y = _fee_per_contract(yb, maker_fee_rate, sp.min_fee_per_order)
    net = hp - yb - fee_y
    if (
        yb <= sp.barbell_alpha_max_cheap_price
        and hp >= sp.barbell_alpha_min_model_p
        and net >= sp.barbell_alpha_min_net_edge
    ):
        return "alpha"
    return None


def _barbell_no_bucket(
    hist_p: float,
    no_bid: float,
    maker_fee_rate: float,
    sp: _StrategyParams,
) -> str | None:
    """Classify a potential BUY NO leg: base | alpha | None."""
    if not sp.barbell_enabled:
        return None
    nb = max(0.0, min(0.99, float(no_bid)))
    p_no = max(0.0, min(1.0, 1.0 - float(hist_p)))
    gap = float(sp.barbell_base_max_model_vs_market_gap)
    if (
        nb >= sp.barbell_base_min_market_yes
        and p_no >= sp.barbell_base_min_model_p
        and p_no + gap >= nb
        and nb <= sp.barbell_base_max_no_price
    ):
        return "base"
    fee_n = _fee_per_contract(nb, maker_fee_rate, sp.min_fee_per_order)
    net = p_no - nb - fee_n
    if (
        nb <= sp.barbell_alpha_max_cheap_price
        and p_no >= sp.barbell_alpha_min_model_p
        and net >= sp.barbell_alpha_min_net_edge
    ):
        return "alpha"
    return None


def _barbell_status_for_log(bucket: str, blocked: bool, rec: str) -> str:
    """Label for the Barbell: tree line when barbell_enabled (never bare em dash for SKIP-before-gate)."""
    if bucket:
        return bucket
    if blocked:
        return "blocked (not base/alpha)"
    if rec == REC_SKIP:
        return "n/a (SKIP before barbell; only classifies BUY YES/NO)"
    return "—"


def _maker_fee_rate_for_market(sp: _StrategyParams, platform: str, yf_ticker: str | None) -> float:
    """Platform-specific maker fee with index override support."""
    if platform == PLATFORM_KALSHI and (yf_ticker or "").strip().upper() in _KALSHI_INDEX_MAKER_FEE_YF:
        return max(0.0, float(sp.kalshi_sp500_maker_fee_rate))
    if platform == PLATFORM_GEMINI:
        return max(0.0, float(sp.gemini_maker_fee_rate))
    return max(0.0, float(sp.kalshi_maker_fee_rate))


def _compute_p_lose(
    hist_p: float,
    yes_bid: float,
    no_bid: float,
    side: str,
    days_to_expiry: float | None,
    sp: _StrategyParams,
) -> float:
    """
    P(our position loses). Blends model (hist_p) with market odds; market gets more
    weight as we approach resolution (closer = more accurate). Half-life decay.
    """
    p_model = hist_p if side == "no" else (1.0 - hist_p)
    p_market = yes_bid if side == "no" else no_bid
    if sp.p_lose_blend_market_max_weight <= 0 or not (0 < yes_bid < 1):
        return p_model
    dte = max(0.01, days_to_expiry) if days_to_expiry is not None else 1.0
    half = max(0.01, sp.p_lose_blend_market_half_life_days)
    # Reduce market influence in low-liquidity regimes using bid/ask consistency proxy.
    consistency = 1.0 - abs((yes_bid + no_bid) - 1.0)
    consistency = max(0.0, min(1.0, consistency))
    weight_market = sp.p_lose_blend_market_max_weight * consistency * (1.0 / (1.0 + dte / half))
    return (1.0 - weight_market) * p_model + weight_market * p_market


def _ev_sell_threshold(
    sell_price: float,
    maker_fee_rate: float,
    min_fee_per_order: float,
) -> float:
    """
    EV-optimal sell threshold for binary contracts.

    Hold-vs-sell breakeven: sell when EV(hold) < net(sell).
      EV(hold)  = 1 - p_lose
      net(sell)  = sell_price - exit_fee
    Rearranged: sell when p_lose > 1 - (sell_price - exit_fee).

    At high bids (90¢) → threshold ~11%: sell unless very confident.
    At low bids (20¢) → threshold ~81%: only sell if almost certain to lose.
    """
    exit_fee = _fee_per_contract(sell_price, maker_fee_rate, min_fee_per_order)
    return max(0.01, min(0.99, 1.0 - (sell_price - exit_fee)))


def _position_held_side_model_edge(
    hist_p: float,
    *,
    yes_bid: float,
    no_bid: float,
    side: str,
    maker_fee_rate: float,
    min_fee_per_order: float,
) -> float:
    """
    Signed edge for the *held* leg (same convention as _net_edges_buy_legs / BUY logic).
    """
    ey, en = _net_edges_buy_legs(
        float(hist_p),
        max(0.0, min(0.99, float(yes_bid))),
        max(1e-6, min(0.99, float(no_bid))),
        maker_fee_rate,
        min_fee_per_order,
    )
    return ey if str(side).lower() == "yes" else en


def _position_flip_sell_allowed(
    sp: _StrategyParams,
    *,
    hist_p: float,
    yes_bid: float,
    no_bid: float,
    side: str,
    maker_fee_rate: float,
    min_fee_per_order: float,
) -> bool:
    """Mean-reversion flip sell: 0 buffer = legacy (any rec flip may sell); >0 requires strong negative held-side edge."""
    buf = float(sp.position_flip_sell_min_negative_edge)
    if buf <= 0:
        return True
    edge = _position_held_side_model_edge(
        hist_p,
        yes_bid=yes_bid,
        no_bid=no_bid,
        side=side,
        maker_fee_rate=maker_fee_rate,
        min_fee_per_order=min_fee_per_order,
    )
    return edge < -buf


def _hold_winner_skip_discretionary_sells(
    sp: _StrategyParams,
    *,
    current_price: float,
    threshold: float,
    direction: str,
    side: str,
    dte_days: float | None,
) -> bool:
    """
    When enabled, skip REC_LIKELY_LOSE and rec-flip sells for deep ITM winners with short DTE.
    Does not affect STOP_LOSS or REC_RULE_VIOLATION.
    """
    if not sp.hold_winner_to_expiry_enabled:
        return False
    if dte_days is None or float(dte_days) > float(sp.hold_winner_max_dte_days):
        return False
    if current_price <= 0 or threshold <= 0:
        return False
    pct = max(0.0, float(sp.hold_winner_min_spot_vs_strike_pct))
    cp, th = float(current_price), float(threshold)
    d = str(direction or "").strip().lower()
    s = str(side or "").strip().lower()
    if d == "above" and s == "yes":
        return cp >= th * (1.0 + pct)
    if d == "above" and s == "no":
        return cp <= th * (1.0 - pct)
    if d == "below" and s == "yes":
        return cp <= th * (1.0 - pct)
    if d == "below" and s == "no":
        return cp >= th * (1.0 + pct)
    return False


def _maybe_add_reentry_order(
    sp: _StrategyParams,
    min_edge: float,
    pending_orders: list[dict],
    platform: str,
    ticker: str,
    title: str,
    side: str,
    hist_p: float,
    yes_bid: float,
    no_bid: float,
    threshold: float | None,
    direction: str | None,
    yf_ticker: str | None,
    vol_p: float | None,
    maker_rate: float,
    position_count: int,
    extra: dict | None = None,
    *,
    current_price: float | None = None,
    dte_days: float | None = None,
) -> None:
    """
    When selling a losing position, optionally add a re-entry BUY at current price
    if the model still sees edge (p_win - price - fee >= min_edge).
    Re-entry bypasses min_yes_price/min_no_price (can buy below 5¢) when profitable.
    """
    if not sp.enable_sell_and_rebuy or position_count <= 0:
        return
    # Block re-entry on ultra-short contracts where the strike guard would block a normal buy
    if current_price is not None and current_price > 0 and threshold is not None and threshold > 0 and direction:
        if side == "yes" and direction == "above" and sp.max_buy_yes_strike_above_pct > 0:
            eff_pct = _dte_adjusted_strike_pct(sp.max_buy_yes_strike_above_pct, dte_days) if dte_days is not None else sp.max_buy_yes_strike_above_pct
            if threshold > current_price * (1 + eff_pct):
                return
        if side == "no" and direction == "below" and sp.max_buy_no_strike_above_pct > 0:
            eff_pct = _dte_adjusted_strike_pct(sp.max_buy_no_strike_above_pct, dte_days) if dte_days is not None else sp.max_buy_no_strike_above_pct
            if threshold > current_price * (1 + eff_pct):
                return
    re_entry_price = yes_bid if side == "yes" else no_bid
    p_win = hist_p if side == "yes" else (1.0 - hist_p)
    fee = _fee_per_contract(re_entry_price, maker_rate, sp.min_fee_per_order)
    edge = p_win - re_entry_price - fee
    if edge < min_edge:
        return
    rec = REC_BUY_YES if side == "yes" else REC_BUY_NO
    order: dict[str, Any] = {
        "platform": platform,
        "ticker": ticker,
        "title": title,
        "rec": rec,
        "side": side,
        "price": re_entry_price,
        "edge": edge,
        "count": position_count,
        "hist_p": hist_p,
        "vol_p": vol_p,
        "yf_ticker": yf_ticker,
        "threshold": threshold,
        "direction": direction,
        "dte": dte_days,
        "re_entry": True,
    }
    if extra:
        order.update(extra)
    pending_orders.append(order)


def _recommend(
    historical_p_yes: float,
    market_yes_price: float,
    min_edge: float,
    maker_fee_rate: float = 0.0,
    *,
    min_edge_easy_win: float | None = None,
    easy_win_min_p_win: float = 0.90,
    easy_win_min_market_p: float = 0.85,
    min_fee_per_order: float = 0.01,
    min_edge_yes: float | None = None,
    min_edge_no: float | None = None,
) -> str:
    """
    Recommend BUY YES / BUY NO / SKIP based on historical vs market.
    Buy YES when historical says P(YES) > market_price + edge (market underpriced).
    Buy NO when historical says P(NO) > (1 - market_yes_price) + edge.
    When both have edge, prefer whichever yields the larger net return.
    min_edge is the minimum *net* edge after fees (e.g. 0.01 = 1¢ profit per $1 contract).

    Easy win: when model and market both say ~90%+ (aligned high confidence), use
    min_edge_easy_win instead — allows buying profitable contracts even with small edge
    (e.g. 1¢ profit after fees) when both agree the outcome is likely.
    """
    if market_yes_price <= 0:
        return REC_SKIP  # No liquidity
    no_price = 1.0 - market_yes_price
    fee_yes = _fee_per_contract(market_yes_price, maker_fee_rate, min_fee_per_order)
    fee_no = _fee_per_contract(no_price, maker_fee_rate, min_fee_per_order)
    net_edge_yes = historical_p_yes - market_yes_price - fee_yes
    net_edge_no = (1.0 - historical_p_yes) - no_price - fee_no

    req_yes = min_edge if min_edge_yes is None else float(min_edge_yes)
    req_no = min_edge if min_edge_no is None else float(min_edge_no)
    if min_edge_easy_win is not None:
        if historical_p_yes >= easy_win_min_p_win and market_yes_price >= easy_win_min_market_p:
            req_yes = min_edge_easy_win
        if (1.0 - historical_p_yes) >= easy_win_min_p_win and no_price >= easy_win_min_market_p:
            req_no = min_edge_easy_win

    has_yes = net_edge_yes >= req_yes
    has_no = net_edge_no >= req_no
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
    min_fee_per_order: float = 0.01,
    min_edge_yes: float | None = None,
    min_edge_no: float | None = None,
) -> str:
    """
    For other intraday (e.g. 8pm ET): only BUY when ALMOST ABSOLUTELY CERTAIN we win.
    Requires P(win) >= min_p_win (e.g. 0.90) AND net edge after fees >= min_edge.
    When both YES and NO qualify, prefer whichever yields the larger net return.
    """
    if market_yes_price <= 0:
        return REC_SKIP
    no_price = 1.0 - market_yes_price
    fee_yes = _fee_per_contract(market_yes_price, maker_fee_rate, min_fee_per_order)
    fee_no = _fee_per_contract(no_price, maker_fee_rate, min_fee_per_order)
    net_edge_yes = historical_p_yes - market_yes_price - fee_yes
    net_edge_no = (1.0 - historical_p_yes) - no_price - fee_no
    p_no = 1.0 - historical_p_yes
    req_yes = min_edge if min_edge_yes is None else float(min_edge_yes)
    req_no = min_edge if min_edge_no is None else float(min_edge_no)
    has_yes = historical_p_yes >= min_p_win and net_edge_yes >= req_yes
    has_no = p_no >= min_p_win and net_edge_no >= req_no
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
            return f"Price {_fmt_price(current_price)} above strike → favor YES (edge {edge_pct:.1%} vs market {market_yes:.1%})"
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
            parts.append(f"strike {_fmt_price(threshold)} >{max_buy_yes_strike_above_pct:.0%} above {_fmt_price(current_price)} (vol {vol_str} < 95%)")
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
    min_fee_per_order: float = 0.01,
    no_bid: float | None = None,
) -> float:
    """
    Net edge after fees on the *chosen* leg. For BUY NO uses explicit no_bid when provided
    (Kalshi no_bid_dollars / Gemini NO book); otherwise falls back to parity 1 − market_yes.
    Returns 0 for SKIP. Caller may use abs(diff) for display legacy; prefer _net_edges_buy_legs for truth.
    """
    if rec == REC_SKIP:
        return 0.0
    no_px = (
        float(no_bid)
        if no_bid is not None and float(no_bid) > 0
        else max(1e-6, min(0.99, 1.0 - float(market_yes)))
    )
    fee_yes = _fee_per_contract(market_yes, maker_fee_rate, min_fee_per_order)
    fee_no = _fee_per_contract(no_px, maker_fee_rate, min_fee_per_order)
    if rec == REC_BUY_YES:
        if violates_no_above:
            raw = 1.0 - market_yes  # favor YES when price above strike
        else:
            raw = hist_p_yes - market_yes
        return raw - fee_yes
    # REC_BUY_NO — must use NO leg bid, not (1 − yes) unless that is all we have
    raw = (1.0 - hist_p_yes) - no_px
    return raw - fee_no


def _fmt_dte(dte_days: float | None) -> str:
    """Format days-to-expiry as a human-readable string."""
    if dte_days is None:
        return "unknown"
    if dte_days < 1 / 24:
        return f"{dte_days * 24 * 60:.0f} min"
    if dte_days < 1:
        h = dte_days * 24
        return f"{h:.1f} hr" if h >= 1.05 else f"{dte_days * 24 * 60:.0f} min"
    if dte_days < 2:
        return f"{dte_days * 24:.0f} hr"
    return f"{dte_days:.1f} days"


def _fmt_price(v: float) -> str:
    """Format a price/threshold with appropriate decimals: $81,250 vs $1.24."""
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:,.4f}"


def _fmt_distance(current_price: float, threshold: float, direction: str) -> str:
    """Human-readable distance between current price and the threshold."""
    if current_price <= 0 or threshold <= 0:
        return ""
    diff = threshold - current_price
    pct = abs(diff / current_price) * 100
    d = _fmt_price(abs(diff)) if abs(diff) >= 0.005 else "$0.00"
    if direction == "above":
        if diff > 0:
            return f"{d} below strike ({pct:.2f}% gap to cover)"
        return f"{d} above strike (already in-the-money)"
    else:
        if diff < 0:
            return f"{d} above strike ({pct:.2f}% gap to cover)"
        return f"{d} below strike (already in-the-money)"


def _fmt_rec_plain(rec: str, side: str | None, direction: str, threshold: float,
                   current_price: float, dte_days: float | None) -> str:
    """One-line plain-English explanation of a recommendation."""
    dte_str = _fmt_dte(dte_days)
    thresh = _fmt_price(threshold)
    if rec == REC_BUY_YES and direction == "above":
        return f"Betting the price WILL be above {thresh} (resolves in {dte_str})"
    if rec == REC_BUY_YES and direction == "below":
        return f"Betting the price WILL be below {thresh} (resolves in {dte_str})"
    if rec == REC_BUY_NO and direction == "above":
        return f"Betting the price will NOT be above {thresh} (resolves in {dte_str})"
    if rec == REC_BUY_NO and direction == "below":
        return f"Betting the price will NOT be below {thresh} (resolves in {dte_str})"
    return f"No trade — insufficient edge or safety block (resolves in {dte_str})"


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
    *,
    net_edge_yes: float | None = None,
    net_edge_no: float | None = None,
    req_yes: float | None = None,
    req_no: float | None = None,
) -> str:
    """Build human-readable reasoning for the recommendation.

    When SKIP is due to thresholding, pass net_edge_* and req_* so logs match
    DefaultThresholdStrategy (fee-adjusted edge vs effective min_edge_yes/no), not raw P−market gaps.
    """
    if rec == REC_BUY_YES:
        if violates_rule:
            return f"Price {_fmt_price(current_price)} above strike {_fmt_price(threshold)} → favor YES (edge {hist_p - market_yes:.1%})"
        return f"Model P(YES) {hist_p:.1%} > market {market_yes:.1%} by {hist_p - market_yes:.1%} (min {min_edge:.0%})"
    if rec == REC_BUY_NO:
        p_no = 1 - hist_p
        mkt_no = 1 - market_yes
        return f"Model P(NO) {p_no:.1%} > market NO {mkt_no:.1%} by {p_no - mkt_no:.1%} (min {min_edge:.0%})"
    # SKIP — explain why
    parts: list[str] = []
    if market_yes < min_yes_price and rec == REC_SKIP:
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
            f"strike {_fmt_price(threshold)} >{max_buy_yes_strike_above_pct:.0%} above current {_fmt_price(current_price)} "
            f"(vol model {vol_conf} < 95% required)"
        )
    if violates_rule and hist_p < market_yes + min_edge:
        parts.append(f"would favor YES (price above strike) but no edge: model {hist_p:.1%} vs market {market_yes:.1%}")
    # Raw P−market checks ignore fees; skip when caller passes net_edge_* (fee-adjusted truth).
    if (
        net_edge_yes is None
        and hist_p >= market_yes + min_edge
        and (1 - hist_p) < (1 - market_yes) + min_edge
        and not parts
    ):
        parts.append(f"no edge either way: model {hist_p:.1%} vs market {market_yes:.1%} (gap < {min_edge:.0%})")
    if not parts:
        edge_yes = hist_p - market_yes
        edge_no = (1 - hist_p) - (1 - market_yes)
        if (
            net_edge_yes is not None
            and net_edge_no is not None
            and req_yes is not None
            and req_no is not None
        ):
            parts.append(
                f"net edge vs hurdle (fee-adjusted): YES {net_edge_yes:.1%} vs req {req_yes:.1%}; "
                f"NO {net_edge_no:.1%} vs req {req_no:.1%} "
                f"(raw gaps {edge_yes:.1%} / {edge_no:.1%})"
            )
        elif abs(edge_yes) < min_edge and abs(edge_no) < min_edge:
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


def _extract_actual_fill_price(result: dict | None, fallback: float) -> float:
    """Best-effort extraction of actual fill/limit price from broker response."""
    if not isinstance(result, dict):
        return float(fallback)
    order = result.get("order", result)
    if isinstance(order, dict):
        for key in (
            "price",
            "limit_price",
            "yes_price_dollars",
            "no_price_dollars",
            "yes_price",
            "no_price",
        ):
            val = order.get(key)
            if val is not None:
                try:
                    f = float(val)
                    if 0 < f < 1.01:
                        return f
                    if 1 < f <= 100:
                        return f / 100.0
                except (TypeError, ValueError):
                    pass
    return float(fallback)


def _fill_slippage_bps(actual_fill: float, theoretical: float | None) -> float | None:
    """Positive = worse fill than theoretical for buy orders."""
    if theoretical is None or theoretical <= 0:
        return None
    return ((actual_fill - theoretical) / theoretical) * 10_000.0


def _gemini_yes_ask(contract: dict | None) -> float | None:
    if not contract:
        return None
    prices = contract.get("prices") or {}
    val = prices.get("bestAsk")
    if val is None:
        val = (prices.get("sell") or {}).get("yes")
    try:
        f = float(val) if val is not None else None
        return f if (f is not None and 0 < f < 1) else None
    except (TypeError, ValueError):
        return None


def _gemini_no_ask(event: dict | None) -> float | None:
    if not event:
        return None
    no_c = _get_no_contract(event) or {}
    prices = no_c.get("prices") or {}
    val = prices.get("bestAsk")
    if val is None:
        val = (prices.get("sell") or {}).get("no")
    try:
        f = float(val) if val is not None else None
        return f if (f is not None and 0 < f < 1) else None
    except (TypeError, ValueError):
        return None


def _liquidity_size_multiplier(
    bid: float,
    ask: float | None,
    sp: _StrategyParams,
) -> tuple[float, float | None]:
    """
    Return (size_mult, spread_bps). Wide spreads reduce size.
    Missing ask gets a conservative haircut.
    """
    b = float(bid) if bid is not None else 0.0
    if b <= 0:
        return 0.0, None
    if ask is None or ask <= 0:
        return max(0.0, min(1.0, sp.liquidity_missing_ask_multiplier)), None
    a = float(ask)
    spread = max(0.0, a - b)
    spread_bps = spread / max(0.01, b) * 10_000.0
    soft = max(0.0, sp.liquidity_spread_bps_soft)
    hard = max(soft + 1.0, sp.liquidity_spread_bps_hard)
    floor = max(0.0, min(1.0, sp.liquidity_min_size_multiplier))
    if spread_bps <= soft:
        return 1.0, spread_bps
    if spread_bps >= hard:
        return floor, spread_bps
    frac = (spread_bps - soft) / (hard - soft)
    return 1.0 - frac * (1.0 - floor), spread_bps


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
    *,
    kalshi_balance_cents: int = 0,
    gemini_balance_cents: int = 0,
    max_pct_of_balance: float = 0.90,
    balance_aware: bool = True,
) -> tuple[str, str, float, float, float, float] | None:
    """
    Inter-exchange arb: buy YES on one platform + NO on other when sum < 1 - fee.
    Returns (best_yes_platform, best_no_platform, yes_price, no_price, other_yes, other_no) or None.
    In balance-aware mode, prefer routes that maximize paired affordability and
    place the cheaper leg on the lower-balance account when possible.
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

    candidates: list[dict[str, Any]] = []
    if cost_k_yes_g_no < (1.0 - min_arb_fee):
        candidates.append(
            {
                "yes_platform": PLATFORM_KALSHI,
                "no_platform": PLATFORM_GEMINI,
                "yes_price": yes_k,
                "no_price": no_g,
                "other_yes": yes_g,
                "other_no": no_k,
                "cost": cost_k_yes_g_no,
            }
        )
    if cost_g_yes_k_no < (1.0 - min_arb_fee):
        candidates.append(
            {
                "yes_platform": PLATFORM_GEMINI,
                "no_platform": PLATFORM_KALSHI,
                "yes_price": yes_g,
                "no_price": no_k,
                "other_yes": yes_k,
                "other_no": no_g,
                "cost": cost_g_yes_k_no,
            }
        )
    if not candidates:
        return None

    if not balance_aware:
        chosen = min(candidates, key=lambda c: c["cost"])
        return (
            chosen["yes_platform"],
            chosen["no_platform"],
            chosen["yes_price"],
            chosen["no_price"],
            chosen["other_yes"],
            chosen["other_no"],
        )

    low_balance_platform = (
        PLATFORM_KALSHI if kalshi_balance_cents <= gemini_balance_cents else PLATFORM_GEMINI
    )
    max_pct = max(0.0, float(max_pct_of_balance))
    kalshi_spend = int(max(0, int(kalshi_balance_cents)) * max_pct)
    gemini_spend = int(max(0, int(gemini_balance_cents)) * max_pct)

    def _pair_affordable_count(c: dict[str, Any]) -> int:
        yes_price_c = max(1, int(round(float(c["yes_price"]) * 100)))
        no_price_c = max(1, int(round(float(c["no_price"]) * 100)))
        if c["yes_platform"] == PLATFORM_KALSHI:
            yes_max = kalshi_spend // yes_price_c
        else:
            yes_max = gemini_spend // yes_price_c
        if c["no_platform"] == PLATFORM_KALSHI:
            no_max = kalshi_spend // no_price_c
        else:
            no_max = gemini_spend // no_price_c
        return max(0, min(yes_max, no_max))

    def _cheap_leg_on_low_balance(c: dict[str, Any]) -> int:
        if float(c["yes_price"]) <= float(c["no_price"]):
            cheap_leg_platform = c["yes_platform"]
        else:
            cheap_leg_platform = c["no_platform"]
        return 1 if cheap_leg_platform == low_balance_platform else 0

    chosen = max(
        candidates,
        key=lambda c: (
            _pair_affordable_count(c),
            _cheap_leg_on_low_balance(c),
            -float(c["cost"]),
        ),
    )
    return (
        chosen["yes_platform"],
        chosen["no_platform"],
        chosen["yes_price"],
        chosen["no_price"],
        chosen["other_yes"],
        chosen["other_no"],
    )


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
    """Format proposed trades for Ollama — limit price per share vs barrier/strike spelled out."""
    lines: list[str] = [
        "Note: LIMIT = dollars/share for the contract (0–1). BARRIER = settlement threshold if listed.",
        "",
    ]
    if pending_sells:
        lines.append(f"PROPOSED SELLs ({len(pending_sells)}):")
        for i, s in enumerate(pending_sells, 1):
            th = s.get("threshold")
            bar = f" | barrier {th}" if th is not None else ""
            lines.append(
                f"  {i}. [{s['platform'].upper()}] SELL {s['side'].upper()} {s['ticker']} | "
                f"{s.get('title', '')[:60]} | Count: {s['count']} | LIMIT ${s['price']:.2f}/share{bar}"
            )
        lines.append("")
    if pending_orders:
        lines.append(f"PROPOSED BUYs ({len(pending_orders)}):")
        for i, o in enumerate(pending_orders, 1):
            title = (o.get("title") or "")[:60]
            th = o.get("threshold")
            d = str(o.get("direction") or "")
            bar = ""
            if th is not None:
                bar = f" | BARRIER {d + ' ' if d else ''}{th}"
            lines.append(
                f"  {i}. [{o['platform'].upper()}] {o['rec']} {o['ticker']} | "
                f"{title} | Edge: {o['edge']:.1%} | LIMIT ${o['price']:.2f}/share | Count: {o.get('count', 10)}{bar}"
            )
    if not lines:
        return "No proposed trades."
    return "\n".join(lines).strip()


def _allocate_budget_to_orders(
    pending_orders: list[dict],
    balance_cents: int,
    max_pct: float,
    filter_zero: bool = True,
) -> None:
    """
    Adjust order counts so total cost fits budget. Prefer buying 1 of many
    contracts over blowing the budget on a few. Modifies pending_orders in place.
    filter_zero: if True, remove orders with count 0 from the list; if False, keep them.
    """
    if not pending_orders:
        return

    def _capital_efficiency_score(o: dict) -> float:
        """
        Prioritize expected net return per dollar-at-risk per hour until resolution.
        This lifts account-level capital efficiency over simple edge-only ranking.
        """
        edge = max(0.0, float(o.get("edge", 0.0)))
        price = max(0.01, float(o.get("price", 0.01)))
        ttl_h = o.get("expected_hours_to_resolution")
        if ttl_h is None:
            title = str(o.get("title") or "")
            yf = str(o.get("yf_ticker") or "")
            parsed = _hours_until_resolution(title, yf) if title else None
            ttl_h = parsed
        ttl_h = max(1.0, float(ttl_h) if ttl_h is not None else 24.0)
        return (edge / price) / ttl_h

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
    # Can't afford 1 of each: keep highest capital-efficiency trades first
    by_edge = sorted(
        pending_orders,
        key=lambda x: (
            -_capital_efficiency_score(x),
            -float(x.get("edge", 0)),
        ),
    )
    running_cents = 0
    for o in by_edge:
        cost_1 = int(float(o.get("price", 0)) * 100)
        if running_cents + cost_1 <= available_cents:
            o["count"] = 1
            running_cents += cost_1
        else:
            o["count"] = 0
    if filter_zero:
        pending_orders[:] = [o for o in by_edge if o.get("count", 0) > 0]


def _promote_zero_count_buys_if_affordable(
    orders: list[dict],
    *,
    balance_cents: int,
    max_total_buy_pct: float,
    enabled: bool,
    logger=print,
    log_tag: str = "[Recovery]",
) -> None:
    """
    After Kelly/budget zeroed count, promote the best edge BUY YES/NO to 1 contract if it fits max_total_buy_pct slice.
    """
    if not enabled or balance_cents <= 0 or not orders:
        return
    budget = max(0, int(balance_cents * max_total_buy_pct))
    used = 0
    for o in orders:
        cnt = int(o.get("count", 0) or 0)
        if cnt <= 0:
            continue
        pc = int(max(1, round(float(o.get("price", 0)) * 100)))
        used += pc * cnt
    candidates = [
        o
        for o in orders
        if str(o.get("rec", "")) in (REC_BUY_YES, REC_BUY_NO) and int(o.get("count", 0) or 0) <= 0
    ]
    candidates.sort(key=lambda x: -float(x.get("edge", 0) or 0))
    for o in candidates:
        pc = int(max(1, round(float(o.get("price", 0)) * 100)))
        if pc <= 0 or used + pc > budget:
            continue
        o["count"] = 1
        used += pc
        logger(
            f"{log_tag} promote x1: {o.get('platform')} {o.get('ticker')} "
            f"@ ${float(o.get('price', 0)):.2f} (edge {float(o.get('edge', 0)):.1%})"
        )


def _micro_balance_force_min_one_before_budget(
    pending_orders: list[dict],
    *,
    kalshi_balance_cents: int,
    gemini_balance_cents: int,
    trading: dict,
    logger=print,
) -> None:
    """
    When venue balance is at/below a micro cap, set count=1 on any directional BUY that
    still has count 0 *before* budget allocation (Kelly/vol-Kelly edge cases, etc.).
    Uses `micro_balance_max_balance_cents`; if unset/0, falls back to `dust_trade_max_balance_cents`.
    """
    if not bool(trading.get("micro_balance_force_min_one_contract", False)):
        return
    max_bc = int(trading.get("micro_balance_max_balance_cents") or 0)
    if max_bc <= 0:
        max_bc = int(trading.get("dust_trade_max_balance_cents", 0) or 0)
    if max_bc <= 0:
        return
    for o in pending_orders:
        if str(o.get("rec", "")) not in (REC_BUY_YES, REC_BUY_NO):
            continue
        if int(o.get("count", 0) or 0) > 0:
            continue
        plat = str(o.get("platform") or "")
        bal = int(gemini_balance_cents) if plat == PLATFORM_GEMINI else int(kalshi_balance_cents)
        if bal <= 0 or bal > max_bc:
            continue
        try:
            pc = int(max(1, round(float(o.get("price", 0)) * 100)))
        except (TypeError, ValueError):
            continue
        if pc > bal:
            continue
        o["count"] = 1
        logger(
            f"[MicroBalance] force x1 (pre-budget): {plat} {o.get('ticker')} "
            f"@ ${float(o.get('price', 0)):.2f} (bal {bal}¢ ≤ {max_bc}¢ cap)"
        )


def _filter_pending_buys_by_max_limit_price(
    pending_orders: list[dict],
    *,
    cap: float,
    logger=print,
) -> None:
    """
    Drop proposed BUYs whose limit price exceeds cap (e.g. block 99¢ NO "steamrollers" on micro balance).
    cap <= 0 disables. Applies to all pending buy-shaped orders (main recs, arb, lottery, dips).
    """
    if cap <= 0 or not pending_orders:
        return
    cap_f = float(cap)
    if cap_f >= 0.999:
        return
    kept: list[dict] = []
    dropped = 0
    for o in pending_orders:
        try:
            px = float(o.get("price") or 0)
        except (TypeError, ValueError):
            kept.append(o)
            continue
        if px > cap_f + 1e-9:
            dropped += 1
            t = str(o.get("ticker") or "?")
            plat = str(o.get("platform") or "?")
            rc = str(o.get("rec") or "")
            logger(
                f"[Risk] max_buy_limit_price ${cap_f:.2f}: skip {plat} {t} {rc} @ ${px:.2f} "
                f"(lottery-style / tail-risk cap; raise cap or lower passive quote if intentional)"
            )
            continue
        kept.append(o)
    if dropped:
        pending_orders[:] = kept
        logger(f"[Risk] Dropped {dropped} BUY(s) above max limit price ${cap_f:.2f}.")
    else:
        pending_orders[:] = kept


def _filter_pending_buys_by_min_payout_ratio(
    pending_orders: list[dict],
    *,
    min_ratio: float,
    logger=print,
) -> None:
    """
    Skip BUYs where profit-if-win / stake is below min_ratio: (1-price)/price for the limit price (YES or NO leg).
    min_ratio <= 0 disables. Complements max_buy_limit_price / max_yes_price_for_buy with an explicit ROI floor.
    """
    if min_ratio <= 0 or not pending_orders:
        return
    kept: list[dict] = []
    dropped = 0
    for o in pending_orders:
        rec = str(o.get("rec") or "")
        if rec not in (REC_BUY_YES, REC_BUY_NO):
            kept.append(o)
            continue
        try:
            px = float(o.get("price") or 0)
        except (TypeError, ValueError):
            kept.append(o)
            continue
        if px <= 0 or px >= 1:
            kept.append(o)
            continue
        payout_ratio = (1.0 - px) / px
        if payout_ratio + 1e-12 < float(min_ratio):
            dropped += 1
            logger(
                f"[Risk] min_payout_ratio {min_ratio:.0%}: skip {o.get('platform')} {o.get('ticker')} {rec} "
                f"@ ${px:.2f} (ROI if win {payout_ratio:.1%} < floor)"
            )
            continue
        kept.append(o)
    if dropped:
        pending_orders[:] = kept
        logger(f"[Risk] Dropped {dropped} BUY(s) below min payout ratio {min_ratio:.0%}.")
    else:
        pending_orders[:] = kept


def _apply_max_open_contracts_per_underlying(
    pending_orders: list[dict],
    cfg: dict,
    cap: int,
    get_sums: Any,
    logger: Any = print,
) -> None:
    """
    Drop pending BUYs that would push unresolved contract count for (platform, yf_ticker)
    above `cap`. Orders without yf_ticker pass through (caller should set yf on normal paths).
    """
    if cap <= 0 or not pending_orders or not get_sums:
        return
    try:
        sums: dict[tuple[str, str], int] = dict(get_sums(cfg) or {})
    except Exception:
        return
    kept: list[dict] = []
    blocked_keys: set[tuple[str, str]] = set()
    for o in pending_orders:
        yf = (o.get("yf_ticker") or "").strip()
        plat = str(o.get("platform") or "")
        if not yf or not plat:
            kept.append(o)
            continue
        cnt = int(o.get("count", 0) or 0)
        if cnt <= 0:
            kept.append(o)
            continue
        key = (plat, yf)
        cur = int(sums.get(key, 0) or 0)
        if cur + cnt > cap:
            blocked_keys.add(key)
            continue
        sums[key] = cur + cnt
        kept.append(o)
    if blocked_keys:
        pending_orders[:] = kept
        logger(
            f"[Risk] Per-underlying open-contract cap ({cap}): blocked order(s) for "
            f"{len(blocked_keys)} (platform, yf_ticker) bucket(s) already at or over limit."
        )
        for plat, yf in sorted(blocked_keys):
            logger(f"         {plat} + {yf}")
        logger("")
    else:
        pending_orders[:] = kept


def _cap_cross_platform_exposure(
    pending_orders: list[dict],
    total_balance_cents: int,
    max_cross_platform_exposure: float,
) -> None:
    """
    Cap combined (Kalshi + Gemini) exposure per (yf_ticker, resolution key).
    Prevents over-concentrating the same thesis across both exchanges.
    """
    if not pending_orders or total_balance_cents <= 0:
        return
    cap_pct = max(0.0, min(1.0, float(max_cross_platform_exposure)))
    if cap_pct <= 0:
        return
    cap_cents = int(total_balance_cents * cap_pct)
    if cap_cents <= 0:
        return

    grouped: dict[tuple[str, str], list[dict]] = {}
    for o in pending_orders:
        yf = o.get("yf_ticker")
        res_key = _get_resolution_key_for_order(o)
        if not yf or not res_key:
            continue
        grouped.setdefault((str(yf), str(res_key)), []).append(o)

    def _capital_efficiency_score(o: dict) -> float:
        edge = max(0.0, float(o.get("edge", 0.0)))
        price = max(0.01, float(o.get("price", 0.01)))
        ttl_h = o.get("expected_hours_to_resolution")
        if ttl_h is None:
            title = str(o.get("title") or "")
            yf = str(o.get("yf_ticker") or "")
            parsed = _hours_until_resolution(title, yf) if title else None
            ttl_h = parsed
        ttl_h = max(1.0, float(ttl_h) if ttl_h is not None else 24.0)
        return (edge / price) / ttl_h

    for _, orders in grouped.items():
        total_cost = sum(
            int(float(o.get("price", 0)) * 100) * int(o.get("count", 0))
            for o in orders
        )
        if total_cost <= cap_cents:
            continue
        # Keep highest capital-efficiency orders/count first within this grouped thesis.
        ranked = sorted(
            orders,
            key=lambda x: (
                -_capital_efficiency_score(x),
                -float(x.get("edge", 0)),
            ),
        )
        used = 0
        for o in ranked:
            price_c = max(1, int(float(o.get("price", 0)) * 100))
            max_count = max(0, (cap_cents - used) // price_c)
            curr = int(o.get("count", 0))
            if curr > max_count:
                o["count"] = max_count
            used += price_c * int(o.get("count", 0))


def _apply_inter_arb_group_preflight(
    pending_orders: list[dict],
    *,
    sp: _StrategyParams,
    kalshi_balance_cents: int,
    gemini_balance_cents: int,
    max_pct_of_balance: float,
    gemini_ready: bool,
    logger=print,
) -> None:
    """
    Ensure REC_ARB_INTER orders execute as balanced pairs.
    By default, drops incomplete/unfunded groups; optional fallback can keep
    one funded highest-likelihood leg when pair funding is not possible.
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
    single_leg_fallback_groups: dict[str, dict] = {}
    resized_legs = 0
    max_pct = max(0.0, float(max_pct_of_balance))

    def _leg_max_count(leg: dict) -> int:
        price_c = int(max(1, round(float(leg.get("price", 0.0)) * 100)))
        if price_c <= 0:
            return 0
        platform = str(leg.get("platform") or "")
        bal = gemini_balance_cents if platform == PLATFORM_GEMINI else kalshi_balance_cents
        spend = int(max(0, int(bal)) * max_pct)
        return max(0, spend // price_c)

    def _select_single_leg_fallback(gid: str, legs: list[dict], reason: str) -> bool:
        if not sp.arb_inter_allow_single_leg_fallback:
            dropped_groups.add(gid)
            logger(f"[Arb Preflight] Drop {gid}: {reason}.")
            return False
        candidates: list[tuple[float, int, float, dict]] = []
        for leg in legs:
            requested = max(0, int(leg.get("count", 0)))
            affordable = _leg_max_count(leg)
            keep_count = min(requested, affordable)
            if keep_count <= 0:
                continue
            # "Most likely bid": higher contract price implies higher implied win probability.
            implied_win = max(0.0, min(1.0, float(leg.get("price", 0.0))))
            edge = float(leg.get("edge", 0.0))
            candidates.append((implied_win, keep_count, edge, leg))
        if not candidates:
            dropped_groups.add(gid)
            logger(f"[Arb Preflight] Drop {gid}: {reason}; no funded fallback leg available.")
            return False
        _, keep_count, _, keep_leg = max(candidates, key=lambda t: (t[0], t[1], t[2]))
        keep_leg["count"] = keep_count
        keep_leg["rec"] = REC_BUY_YES if str(keep_leg.get("side", "")) == "yes" else REC_BUY_NO
        keep_leg["arb_single_leg_fallback"] = True
        keep_leg["arb_fallback_reason"] = reason
        for leg in legs:
            if leg is keep_leg:
                continue
            leg["count"] = 0
        single_leg_fallback_groups[gid] = keep_leg
        logger(
            f"[Arb Preflight] Fallback {gid}: keeping single leg "
            f"{keep_leg.get('platform')} {str(keep_leg.get('side')).upper()} "
            f"@ ${float(keep_leg.get('price', 0.0)):.2f} x {keep_count} ({reason})."
        )
        return True

    for gid, legs in groups.items():
        platforms = {str(l.get("platform") or "") for l in legs}
        if platforms != {PLATFORM_KALSHI, PLATFORM_GEMINI}:
            _select_single_leg_fallback(
                gid,
                legs,
                f"missing cross-exchange pair (platforms={sorted(platforms)})",
            )
            continue
        if not gemini_ready:
            _select_single_leg_fallback(
                gid,
                legs,
                "Gemini client unavailable for paired inter-arb execution",
            )
            continue
        requested = [max(0, int(l.get("count", 0))) for l in legs]
        affordable = [_leg_max_count(l) for l in legs]
        paired_count = min(requested + affordable)
        if paired_count <= 0:
            _select_single_leg_fallback(
                gid,
                legs,
                f"insufficient paired balance (requested={requested}, affordable={affordable})",
            )
            continue
        for leg in legs:
            old = max(0, int(leg.get("count", 0)))
            if old != paired_count:
                resized_legs += 1
            leg["count"] = paired_count

    if dropped_groups:
        before = len(pending_orders)
        pending_orders[:] = [
            o
            for o in pending_orders
            if not (o.get("rec") == REC_ARB_INTER and str(o.get("arb_group") or "") in dropped_groups)
        ]
        removed = before - len(pending_orders)
        if removed > 0:
            logger(f"[Arb Preflight] Removed {removed} inter-arb legs that could not be executed as full pairs.")
    # Remove zero-count inter-arb legs, including the dropped side of fallback groups.
    pending_orders[:] = [
        o
        for o in pending_orders
        if not (o.get("rec") == REC_ARB_INTER and int(o.get("count", 0) or 0) <= 0)
    ]
    if single_leg_fallback_groups:
        logger(
            f"[Arb Preflight] Enabled single-leg fallback for {len(single_leg_fallback_groups)} inter-arb group(s)."
        )
    if resized_legs > 0:
        logger(f"[Arb Preflight] Resized {resized_legs} inter-arb legs to matched paired counts.")


def _maybe_add_two_sided_quotes(
    pending_orders: list[dict],
    candidates: list[dict],
    *,
    sp: _StrategyParams,
    balance_cents: int,
    use_kelly: bool,
    kelly_fraction: float,
    min_contracts: int,
) -> None:
    if not sp.enable_two_sided_quotes or not candidates:
        return
    grouped: dict[tuple[str, str, str, str], list[dict]] = {}
    for c in candidates:
        key = (c["platform"], c["yf_ticker"], c["resolution_key"], c["direction"])
        grouped.setdefault(key, []).append(c)
    existing = {(o.get("platform"), o.get("ticker"), o.get("side")) for o in pending_orders}

    for key, rows in grouped.items():
        if len(rows) < 3:
            continue
        fair = min(rows, key=lambda r: abs(float(r.get("hist_p", 0.5)) - 0.5)).get("threshold")
        if fair is None:
            continue
        direction = key[3]

        def _is_yes_side(r: dict) -> bool:
            t = float(r["threshold"])
            return t <= float(fair) if direction == "above" else t >= float(fair)

        def _is_no_side(r: dict) -> bool:
            t = float(r["threshold"])
            return t >= float(fair) if direction == "above" else t <= float(fair)

        yes_cands = [r for r in rows if _is_yes_side(r)]
        no_cands = [r for r in rows if _is_no_side(r)]
        best_yes = max(yes_cands, key=lambda r: float(r.get("edge_yes_net", -9e9)), default=None)
        best_no = max(no_cands, key=lambda r: float(r.get("edge_no_net", -9e9)), default=None)
        picks: list[tuple[str, dict]] = []
        if best_yes and float(best_yes.get("edge_yes_net", -1.0)) >= sp.two_sided_quote_min_edge:
            picks.append(("yes", best_yes))
        if best_no and float(best_no.get("edge_no_net", -1.0)) >= sp.two_sided_quote_min_edge:
            picks.append(("no", best_no))

        for side, c in picks:
            ex_key = (c["platform"], c["ticker"], side)
            if ex_key in existing:
                continue
            bid = float(c["yes_bid"] if side == "yes" else c["no_bid"])
            if bid <= 0:
                continue
            p_win = float(c["hist_p"] if side == "yes" else (1.0 - c["hist_p"]))
            dip = max(0.0, min(sp.max_dip_cap, float(c.get("arr_dip", sp.limit_dip_pct))))
            if p_win >= 0.90 and sp.limit_dip_pct_high_confidence > 0:
                dip = max(dip, sp.limit_dip_pct_high_confidence)
            dip = _dte_confidence_dip(dip, p_win, c.get("dte"), sp)
            ask_side = c.get("yes_ask") if side == "yes" else c.get("no_ask")
            dip_price = max(0.01, min(0.99, bid * (1.0 - dip))) if dip else bid
            mf_q = _maker_fee_rate_for_market(sp, str(c.get("platform") or ""), str(c.get("yf_ticker") or ""))
            fair_cap_q = _clip_price(float(p_win) - _fee_per_contract(float(p_win), mf_q, sp.min_fee_per_order))
            ask_f: float | None = None
            if ask_side is not None:
                try:
                    af = float(ask_side)
                    ask_f = af if af > 0 else None
                except (TypeError, ValueError):
                    pass
            price = _resolve_passive_quote_price(
                passive_quote=_effective_passive_limit_quote(sp, c.get("dte")),
                bid=bid,
                ask=ask_f,
                dip_based_price=dip_price,
                fair_cap=fair_cap_q,
            )
            count_base = _compute_buy_count(
                p_win, price, balance_cents, sp, use_kelly, kelly_fraction, min_contracts
            )
            liq_mult, spread_bps = _liquidity_size_multiplier(bid, ask_side, sp)
            count = int(max(min_contracts, round(count_base * liq_mult))) if count_base > 0 else 0
            _bsz = c.get("yes_bid_size") if side == "yes" else c.get("no_bid_size")
            _bdv: float | None = None
            if _bsz is not None:
                try:
                    _bdv = float(_bsz)
                except (TypeError, ValueError):
                    _bdv = None
            count = _cap_buy_count_by_book_pct(count, _bdv, sp.max_pct_of_book_volume)
            if count <= 0:
                continue
            order = {
                "platform": c["platform"],
                "ticker": c["ticker"],
                "title": c["title"],
                "rec": REC_BUY_YES if side == "yes" else REC_BUY_NO,
                "side": side,
                "price": price,
                "bid_at_place": bid,
                "theoretical_price": bid,
                "spread_bps_at_place": spread_bps,
                "liquidity_mult": liq_mult,
                "dip_pct_used": dip,
                "edge": float(c["edge_yes_net"] if side == "yes" else c["edge_no_net"]),
                "count": count,
                "hist_p": float(c["hist_p"]),
                "vol_p": c.get("vol_p"),
                "yf_ticker": c["yf_ticker"],
                "threshold": float(c["threshold"]),
                "direction": c["direction"],
                "dte": c.get("dte"),
                "paired_quote": True,
            }
            if c.get("gemini_evt") is not None:
                order["gemini_evt"] = c.get("gemini_evt")
            if c.get("instrument_symbol"):
                order["instrument_symbol"] = c.get("instrument_symbol")
            pending_orders.append(order)
            existing.add(ex_key)


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
    sp_sync = _load_strategy_params(_effective_strategy_dict(cfg or {}))
    min_fee = float(((cfg or {}).get("strategy", {}) or {}).get("min_fee_per_order", 0.01))
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
        yf_ticker = p.get("yf_ticker")
        fee_rate = _maker_fee_rate_for_market(
            sp_sync,
            PLATFORM_KALSHI,
            str(yf_ticker or ""),
        )
        fee_cents = int(round(_fee_per_contract(entry, fee_rate, min_fee) * 100 * cnt))
        gross_cents = int(round((100 - entry * 100) * cnt)) if won else int(round(-entry * 100 * cnt))
        pnl_cents = gross_cents - fee_cents
        buy_ts = p.get("ts")
        min_after = max_after = None
        if buy_ts is not None and get_price_after_buy_from_snapshots:
            min_after, max_after = get_price_after_buy_from_snapshots(
                t, float(buy_ts), side, PLATFORM_KALSHI, cfg
            )
        res_price: float | None = None
        yf_t = p.get("yf_ticker") or ""
        if yf_t:
            try:
                res_price = yf_get_snapshot(yf_t)
            except Exception:
                pass
        record_prediction_outcome(
            p["id"], yes_settled, pnl_cents, cfg,
            min_price_after_buy=min_after,
            max_price_after_buy=max_after,
            resolution_price=res_price,
        )
        if resolve_position_evaluations:
            resolve_position_evaluations(
                t, yes_settled, cfg,
                maker_fee_rate=fee_rate, min_fee_per_order=min_fee,
            )
        if expire_unfilled_orders:
            expire_unfilled_orders(t, platform=PLATFORM_KALSHI, config=cfg)
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


def _sync_gemini_settlements(gemini: GeminiPredictionClient, cfg: dict) -> None:
    """Fetch recently settled Gemini events and resolve our unresolved Gemini predictions.

    Resolution strategy: match predictions to settled events by event ticker,
    then find the contract whose strike threshold matches the prediction's threshold.
    The matched contract's resolutionSide tells us if YES or NO won at that strike.
    """
    if not get_unresolved_predictions or not record_prediction_outcome:
        return
    unresolved = get_unresolved_predictions(cfg)
    gemini_preds = [p for p in unresolved if p.get("platform") == PLATFORM_GEMINI]
    if not gemini_preds:
        return
    event_tickers_needed = {p["ticker"] for p in gemini_preds}
    # Store all contracts for each matched event so we can resolve by threshold.
    # event_ticker → [{"threshold": float, "res_side": "yes"|"no", "evt": dict}, ...]
    events_contracts: dict[str, list[dict]] = {}

    def _parse_contract_threshold(contract: dict) -> float | None:
        """Extract threshold from a Gemini contract label like 'BTC > $73,900' or 'ETH > $2,240'."""
        label = contract.get("label") or contract.get("abbreviatedName") or ""
        parsed_thresh, _ = _extract_threshold_from_title(label)
        return parsed_thresh

    def _scan_events(data):
        for evt in data:
            evt_ticker = evt.get("ticker") or ""
            if evt_ticker not in event_tickers_needed:
                continue
            if evt_ticker not in events_contracts:
                events_contracts[evt_ticker] = []
            for c in evt.get("contracts") or []:
                rs = (c.get("resolutionSide") or "").lower()
                if rs not in ("yes", "no"):
                    continue
                thresh = _parse_contract_threshold(c)
                events_contracts[evt_ticker].append({
                    "threshold": thresh,
                    "res_side": rs,
                    "contract_ticker": c.get("ticker") or "",
                })

    try:
        for cat in ("crypto", None):
            offset = 0
            for _ in range(10):
                try:
                    r = gemini.get_events(category=cat, status="settled", limit=200, offset=offset)
                except Exception:
                    break
                data = r.get("data") or []
                _scan_events(data)
                if len(data) < 200 or len(events_contracts) >= len(event_tickers_needed):
                    break
                offset += len(data)
            if len(events_contracts) >= len(event_tickers_needed):
                break
    except Exception:
        return
    if not events_contracts:
        return
    resolved_count = 0
    sp_sync = _load_strategy_params(_effective_strategy_dict(cfg or {}))
    min_fee = float(((cfg or {}).get("strategy", {}) or {}).get("min_fee_per_order", 0.01))
    for p in gemini_preds:
        t = p.get("ticker")
        contracts = events_contracts.get(t) if t else None
        if not contracts:
            continue
        pred_threshold = p.get("threshold")
        if pred_threshold is None:
            continue
        pred_threshold = float(pred_threshold)
        # Find the contract whose threshold matches our prediction's threshold.
        best_match = None
        best_dist = float("inf")
        for c in contracts:
            ct = c.get("threshold")
            if ct is None:
                continue
            dist = abs(ct - pred_threshold)
            rel_dist = dist / max(1e-6, abs(pred_threshold))
            if rel_dist < 0.001 and dist < best_dist:
                best_match = c
                best_dist = dist
        if not best_match:
            continue
        yes_settled = best_match["res_side"] == "yes"
        entry = float(p.get("entry_price") or 0)
        cnt = int(p.get("count") or 0)
        if cnt <= 0:
            continue
        side = (p.get("side") or "").lower()
        won = (side == "yes" and yes_settled) or (side == "no" and not yes_settled)
        yf_ticker = p.get("yf_ticker")
        fee_rate = _maker_fee_rate_for_market(
            sp_sync,
            PLATFORM_GEMINI,
            str(yf_ticker or ""),
        )
        fee_cents = int(round(_fee_per_contract(entry, fee_rate, min_fee) * 100 * cnt))
        gross_cents = int(round(((100 - entry * 100) * cnt) if won else (-entry * 100 * cnt)))
        pnl_cents = gross_cents - fee_cents
        res_price: float | None = None
        yf_t = p.get("yf_ticker") or ""
        if yf_t:
            try:
                res_price = yf_get_snapshot(yf_t)
            except Exception:
                pass
        record_prediction_outcome(
            p["id"], yes_settled, pnl_cents, cfg,
            resolution_price=res_price,
        )
        if resolve_position_evaluations:
            resolve_position_evaluations(
                t, yes_settled, cfg,
                maker_fee_rate=fee_rate, min_fee_per_order=min_fee,
            )
        if expire_unfilled_orders:
            expire_unfilled_orders(t, platform=PLATFORM_GEMINI, config=cfg)
        if record_resolved and get_last_snapshot_for_ticker:
            snap = get_last_snapshot_for_ticker(t, platform=PLATFORM_GEMINI, config=cfg)
            record_resolved(
                ticker=t,
                yes_settled=yes_settled,
                platform=PLATFORM_GEMINI,
                title=snap.get("title") if snap else None,
                our_last_rec=snap.get("rec") if snap else None,
                our_last_hist_p=snap.get("hist_p") if snap else None,
                market_yes_at_last=snap.get("yes_bid") if snap else None,
                config=cfg,
            )


def _show_config(args) -> int:
    """Print resolved config (Freqtrade show-config style). No API calls."""
    cfg = get_config()
    strategy = _effective_strategy_dict(cfg)
    trading = cfg.get("trading", {})
    markets = cfg.get("markets", [])
    yf = cfg.get("yfinance_tickers", {})
    # Effective min_edge: config or --edge
    min_edge = float(strategy.get("min_edge", args.edge))
    print("=" * 60)
    print("  RESOLVED CONFIG (config.yaml + env overrides)")
    print("=" * 60)
    print(f"\nmarkets: {markets}")
    print(f"yfinance_tickers: {list(yf.keys()) if isinstance(yf, dict) else yf}")
    rec = cfg.get("recovery") or {}
    if rec:
        print(f"\nrecovery (merged into strategy for hurdle / sizing):")
        for k, v in rec.items():
            print(f"  {k}: {v}")
    ex = cfg.get("execution") or {}
    if ex:
        print(f"\nexecution (merged into strategy for pricing):")
        for k, v in ex.items():
            print(f"  {k}: {v}")
    print(f"\nstrategy (key params):")
    for k in ("strategy_engine", "min_edge", "kelly_fraction", "limit_order_mode", "vol_model",
              "enable_disagreement_dip", "enable_lottery_limits", "lottery_limit_price_mode",
              "lottery_limit_max_price", "lottery_penny_jump_apply_cap_bid_below",
              "lottery_market_yes_band_enabled", "lottery_market_yes_band_lo", "lottery_market_yes_band_hi",
              "lottery_limit_min_p_win", "yes_overconfidence_extra_edge",
              "max_dip_cap", "limit_dip_pct",
              "passive_limit_quote", "blacklist_extra_file", "max_buy_limit_price", "min_payout_ratio",
              "dte_edge_scaling_enabled", "dte_edge_short_threshold_days", "dte_edge_long_threshold_days",
              "dte_edge_mult_short", "dte_edge_mult_long", "max_new_buy_dte_days",
              "passive_bid_chase_enabled", "passive_bid_chase_refresh_minutes",
              "empirical_weighting", "empirical_exponential_halflife_bars",
              "enable_model_prob_hard_cap", "model_prob_hard_lo", "model_prob_hard_hi",
              "adaptive_edge_max_extra", "loss_streak_max_extra", "force_min_buy_contracts_one",
              "dynamic_sell_threshold", "position_flip_sell_min_negative_edge",
              "position_loser_sell_p_lose_margin", "hold_winner_to_expiry_enabled",
              "hold_winner_min_spot_vs_strike_pct", "hold_winner_max_dte_days",
              "ultra_short_dte_hours", "ultra_short_min_p_win", "ultra_short_min_p_win_with_vol",
              "ultra_short_min_edge_mult",
              "ultra_short_late_minutes_threshold", "ultra_short_late_min_edge_mult",
              "position_exit_min_net_edge", "resting_cancel_if_market_bid_above_limit_by",
              "limit_buy_edge_aggression_enabled", "limit_buy_cross_spread_min_edge",
              "limit_buy_near_market_min_edge", "skip_new_buy_when_unresolved_same_side"):
        v = strategy.get(k)
        if v is not None:
            print(f"  {k}: {v}")
    print(f"\n  (effective min_edge: {min_edge})")
    print(f"\ntrading:")
    for k in ("dry_run", "max_pct_of_balance", "max_total_buy_pct", "min_contracts",
              "dust_trade_enabled", "dust_trade_max_balance_cents",
              "micro_balance_force_min_one_contract", "micro_balance_max_balance_cents",
              "min_balance_for_limit_fallback_cents"):
        v = trading.get(k)
        if v is not None:
            print(f"  {k}: {v}")
    print("\n" + "=" * 60)
    return 0


def main():
    cfg = get_config()
    strategy = _effective_strategy_dict(cfg)
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
    parser.add_argument("--log", metavar="DIR", nargs="?", const="~/Desktop/trading-bot-logs", default=None, help="Log run output to ~/Desktop/trading-bot-logs/ or specified dir (eval_YYYYMMDDHH.log per hour, append within hour)")
    parser.add_argument("--limit-analysis", action="store_true", help="Print analysis of unfilled limit orders (why they didn't hit; inform limit_dip_pct tuning)")
    parser.add_argument("--position-eval-analysis", action="store_true", help="Print position evaluation stats (KEEP vs SELL outcomes, win rate by hist_p bucket)")
    parser.add_argument("--reconcile-diagnostics", action="store_true", help="Print fill reconciliation counters (seen/inserted/duplicate/skipped) per platform")
    parser.add_argument("--reconcile-only", action="store_true", help="Run fill reconciliation pass only, print diagnostics, then exit")
    parser.add_argument("--show-config", action="store_true", help="Print resolved config (with env overrides) and exit (Freqtrade-style)")
    parser.add_argument("--ticker", type=str, default="", help="Evaluate only this ticker (Kalshi or Gemini); skip all others (Gnosis pick-one pattern)")
    parser.add_argument(
        "--compare-strategies",
        type=str,
        default="",
        help="Comma-separated strategy_engine names; print alternate recommendations vs same model P (e.g. vol_regime,default_threshold,strict_threshold)",
    )
    args = parser.parse_args()

    if args.show_config:
        return _show_config(args)
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
    strategy = _effective_strategy_dict(cfg)
    trading = cfg.get("trading", {})
    min_edge = float(strategy.get("min_edge", args.edge))
    use_kelly = strategy.get("use_kelly", False)
    kelly_fraction = float(strategy.get("kelly_fraction", 0.25))
    whitelist = strategy.get("whitelist")  # if set, only these series
    blacklist = set((strategy.get("blacklist") or []))  # exclude these
    blacklist |= _load_blacklist_extra_file(strategy.get("blacklist_extra_file"))
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
        log_path = log_dir_resolved / f"eval_{datetime.utcnow().strftime('%Y%m%d%H')}.log"
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
    trading_cfg = cfg.get("trading", {})
    max_pct_for_arb_routing = float(trading_cfg.get("max_pct_of_balance", 0.90))
    balance_cents = 100_00  # default $100 for Kelly when balance unavailable
    try:
        bal = kalshi.get_balance()
        balance_cents = int(bal.get("balance", 100_00))
    except Exception:
        pass
    gemini_balance_for_arb = 0
    try:
        gt_route = GeminiTradingClient()
        gemini_balance_for_arb = int(gt_route.get_balance().get("balance", 0) or 0)
    except Exception:
        gemini_balance_for_arb = 0
    to_date = datetime.utcnow()
    from_date = to_date - timedelta(days=args.days)
    from_ts, to_ts = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")

    print()
    print("=" * 75)
    print("  TRADING BOT — Contract Evaluation Run")
    print("=" * 75)
    print(f"  Time:        {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  History:     {from_ts} → {to_ts} ({args.days} days)")
    print(f"  Min edge:    {min_edge:.0%}")
    print(f"  Volatility:  {'enabled' if bool(getattr(args, 'use_volatility', False) or strategy.get('use_volatility_by_default')) else 'disabled'}")
    _dry_banner = bool(trading_cfg.get("dry_run", True))
    _ex = bool(getattr(args, "execute", False))
    _pv = bool(getattr(args, "preview", False))
    if _ex and not _dry_banner:
        _mode = "LIVE — will place orders (Kalshi/Gemini)"
    elif _ex and _dry_banner:
        _mode = "EXECUTE requested but trading.dry_run is true — fix config to place orders"
    elif _pv:
        _mode = "PREVIEW — shows proposed orders only (no API order placement)"
    else:
        _mode = "EVALUATE ONLY — no order placement (use --preview to see API-style plan, --execute to trade)"
    print(f"  Mode:        {_mode}")
    print(f"  CLI flags:   --execute={_ex}  --preview={_pv}  config trading.dry_run={_dry_banner}")
    print("=" * 75)
    print()

    # Sync Kalshi settlements → update unresolved predictions with outcomes
    try:
        _sync_kalshi_settlements(kalshi, cfg)
    except Exception:
        pass

    # Fetch Gemini events and Kalshi markets in parallel
    gemini = GeminiPredictionClient()

    # Sync Gemini settlements → update unresolved predictions with outcomes
    try:
        _sync_gemini_settlements(gemini, cfg)
    except Exception:
        pass
    gemini_events_by_asset: dict[str, list] = {}
    kalshi_markets_by_series: dict[str, list] = {}

    _gemini_fetch_error: list[BaseException | None] = [None]

    def _fetch_gemini_events() -> dict[str, list]:
        out: dict[str, list] = {}
        try:
            events = gemini.get_crypto_events(limit=500)
            for evt in events:
                asset = _event_to_asset(evt)
                if asset:
                    out.setdefault(asset, []).append(evt)
        except Exception as e:
            _gemini_fetch_error[0] = e
        return out

    def _fetch_kalshi_markets() -> dict[str, list]:
        out: dict[str, list] = {}

        def _one_series(series_raw: str) -> tuple[str, list]:
            s = series_raw.strip()
            try:
                r = kalshi.get_markets(series_ticker=s.upper(), status="open")
                mkt_list = r.get("markets", []) or []
                if args.markets > 0:
                    mkt_list = mkt_list[: args.markets]
                return s, mkt_list
            except Exception:
                return s, []

        if len(markets_list) <= 1:
            for s in markets_list:
                k, v = _one_series(s)
                out[k] = v
            return out
        n_k = min(8, len(markets_list))
        with ThreadPoolExecutor(max_workers=n_k) as k_ex:
            futs = [k_ex.submit(_one_series, s) for s in markets_list]
            for fut in as_completed(futs):
                k, v = fut.result()
                out[k] = v
        return out

    with ThreadPoolExecutor(max_workers=2) as executor:
        fut_gemini = executor.submit(_fetch_gemini_events)
        fut_kalshi = executor.submit(_fetch_kalshi_markets)
        gemini_events_by_asset = fut_gemini.result()
        kalshi_markets_by_series = fut_kalshi.result()

    # --ticker: evaluate only this ticker (Gnosis pick-one pattern)
    filter_ticker = (getattr(args, "ticker", "") or "").strip().upper()
    if filter_ticker:
        for series in list(kalshi_markets_by_series.keys()):
            kalshi_markets_by_series[series] = [
                m for m in kalshi_markets_by_series[series]
                if str(m.get("ticker") or "").upper() == filter_ticker
            ]
        for asset in list(gemini_events_by_asset.keys()):
            gemini_events_by_asset[asset] = [
                e for e in gemini_events_by_asset[asset]
                if str(e.get("ticker") or "").upper() == filter_ticker
            ]
        total_filt = sum(len(v) for v in kalshi_markets_by_series.values()) + sum(len(v) for v in gemini_events_by_asset.values())
        if total_filt == 0:
            print(f"[--ticker {filter_ticker}] No market found matching this ticker. Exiting.")
            return 0
        print(f"[--ticker] Filtering to ticker {filter_ticker} ({total_filt} market(s))")
        print()

    # Diagnostic: how many Gemini events per asset
    total_gem = sum(len(v) for v in gemini_events_by_asset.values())
    if total_gem > 0:
        per_asset = ", ".join(f"{a}:{len(v)}" for a, v in sorted(gemini_events_by_asset.items()))
        print(f"[Gemini] Fetched {total_gem} events ({per_asset})")
    elif _gemini_fetch_error[0]:
        err = _gemini_fetch_error[0]
        print(f"[Gemini] No events fetched: {err}")
        if "proxy" in str(err).lower() or "403" in str(err):
            print("[Gemini] Tip: If using a proxy (HTTP_PROXY/HTTPS_PROXY), api.gemini.com may be blocked. Try: NO_PROXY=api.gemini.com,*.gemini.com")

    # Reconcile confirmed fills -> predictions (source of truth), not accepted order responses.
    try:
        gemini_trading_recon = None
        try:
            gemini_trading_recon = GeminiTradingClient()
        except ValueError:
            gemini_trading_recon = None
            if getattr(args, "verbose", False) or getattr(args, "reconcile_diagnostics", False):
                print("[Reconcile] Gemini client unavailable (missing GEMINI_API_KEY/SECRET); Kalshi-only fill sync.")
        recon_stats = reconcile_confirmed_fills(
            kalshi=kalshi,
            gemini_trading=gemini_trading_recon,
            cfg=cfg,
            gemini_events_by_asset=gemini_events_by_asset,
            get_instrument_symbol=get_instrument_symbol,
            record_prediction_from_fill=record_prediction_from_fill,
            platform_kalshi=PLATFORM_KALSHI,
            platform_gemini=PLATFORM_GEMINI,
            logger=print,
            lookback_hours=48,
            diagnostics=bool(getattr(args, "reconcile_diagnostics", False) or getattr(args, "reconcile_only", False)),
        )
        if bool(getattr(args, "reconcile_only", False)):
            print("\n" + "=" * 75)
            print("Reconciliation-only run complete.")
            print(
                f"  Kalshi: seen={recon_stats.get('kalshi_seen', 0)}, inserted={recon_stats.get('kalshi_inserted', 0)}, "
                f"dup={recon_stats.get('kalshi_duplicate', 0)}, skipped={recon_stats.get('kalshi_skipped', 0)}"
            )
            print(
                f"  Gemini: seen={recon_stats.get('gemini_seen', 0)}, inserted={recon_stats.get('gemini_inserted', 0)}, "
                f"dup={recon_stats.get('gemini_duplicate', 0)}, skipped={recon_stats.get('gemini_skipped', 0)}"
            )
            kr = recon_stats.get("kalshi_raw_rows")
            if kr is not None:
                print(f"  Kalshi raw fill rows fetched: {kr}")
            gr = recon_stats.get("gemini_raw_rows")
            gp = recon_stats.get("gemini_fills_endpoint")
            if gr is not None:
                print(f"  Gemini raw fill rows fetched: {gr} (endpoint={gp!r})")
            print("=" * 75)
            return 0
    except Exception as exc:
        print(f"[Reconcile] Fill reconciliation failed (continuing run): {exc}")

    pending_orders: list[dict] = []
    quote_candidates: list[dict] = []
    pending_sells: list[dict] = []
    inventory_by_key: dict[tuple[str, str, str], int] = {}
    if get_unresolved_predictions:
        try:
            for p in get_unresolved_predictions(cfg) or []:
                key = (
                    str(p.get("platform") or ""),
                    str(p.get("ticker") or ""),
                    str(p.get("side") or ""),
                )
                cnt = int(p.get("count") or 0)
                if cnt > 0:
                    inventory_by_key[key] = inventory_by_key.get(key, 0) + cnt
        except Exception:
            pass
    sp = _load_strategy_params(strategy)
    strategy_engine = _build_strategy_engine(strategy)
    print(f"[Strategy] Engine: {strategy.get('strategy_engine', 'vol_regime')}")
    if (sp.use_sklearn_calibration or sp.enable_auto_signal_weight_fit) and (
        IsotonicRegression is None or LogisticRegression is None
    ):
        venv_py = _ROOT / ".venv" / "bin" / "python3"
        wrong_interp = venv_py.is_file() and Path(sys.executable).resolve() != venv_py.resolve()
        tip = ""
        if wrong_interp:
            tip = (
                f" This interpreter is {sys.executable}; launchd uses {venv_py}. "
                f"Install with: {venv_py} -m pip install -r {_ROOT / 'requirements.txt'}"
            )
        print(
            "[Learning] sklearn not available: adaptive calibration/weight fitting disabled. "
            "Install: python3 -m pip install 'scikit-learn>=1.3.0'  (or pip install -r requirements.txt "
            "using the same interpreter as this run)."
            + tip
        )
        print()

    # Calibration: learn from resolved predictions (e.g. "above+YES" underperforming)
    calibration: dict[str, Any] = {}
    if get_calibration_stats:
        try:
            calibration = get_calibration_stats(cfg) or {}
            if calibration.get("enabled") and calibration.get("resolved_count", 0) > 0:
                wr = calibration.get("win_rate")
                wr_str = f"{wr:.1%}" if wr is not None else "N/A"
                by_ds = calibration.get("by_direction_side", {})
                above_yes = by_ds.get("above+yes", {})
                above_yes_str = ""
                if above_yes:
                    ae = above_yes.get("avg_entry_price")
                    ae_s = f", avg entry {float(ae):.1%}" if ae is not None else ""
                    above_yes_str = (
                        f" | above+YES: {above_yes.get('count',0)} trades, "
                        f"{above_yes.get('win_rate',0):.1%} win rate{ae_s}"
                    )
                print(f"[Calibration] {calibration.get('resolved_count')} resolved | overall win rate {wr_str}{above_yes_str}")
                pab = calibration.get("price_after_buy")
                if pab and pab.get("count", 0) > 0:
                    print(f"[Calibration] Price-after-buy: {pab['count']} w/ data | "
                          f"{pab['pct_dipped']:.0f}% dipped lower after buy (avg {pab['avg_dip_pct']:.1f}%) | "
                          f"{pab['pct_rose']:.0f}% rose after buy (avg {pab['avg_rise_pct']:.1f}%)")
                print()
        except Exception:
            pass

    if (
        sp.empirical_stress_recent_days > 0
        and sp.empirical_stress_calibration_max_wr > 0
        and calibration
        and int(calibration.get("resolved_count", 0) or 0) >= sp.empirical_stress_min_samples
    ):
        wr_c = calibration.get("win_rate")
        if wr_c is not None and float(wr_c) < float(sp.empirical_stress_calibration_max_wr):
            new_days = min(int(sp.empirical_recent_days), int(sp.empirical_stress_recent_days))
            if new_days < sp.empirical_recent_days:
                print(
                    f"[Empirical] Stressed calibration (overall WR {float(wr_c):.1%}) → "
                    f"rolling recent_days {sp.empirical_recent_days} → {new_days}"
                )
                print()
                sp.empirical_recent_days = new_days

    # Adaptive learning: recency-weighted performance, loss streak, per-series cooldown.
    adaptive_edge_bump: float = 0.0
    series_performance: dict[str, dict[str, Any]] = {}
    if sp.adaptive_learning_enabled:
        try:
            if get_recent_performance:
                recent = get_recent_performance(
                    lookback_hours=sp.adaptive_edge_lookback_hours, config=cfg,
                )
                recent_total = recent.get("total", 0)
                recent_wr = recent.get("win_rate")
                if recent_total >= sp.adaptive_edge_min_trades and recent_wr is not None and recent_wr < 0.5:
                    shortfall = 0.5 - recent_wr
                    adaptive_edge_bump += min(
                        shortfall * 100 * sp.adaptive_edge_scale_per_pct,
                        sp.adaptive_edge_max_extra,
                    )
                    print(
                        f"[Adaptive] Recent {sp.adaptive_edge_lookback_hours:.0f}h: "
                        f"{recent_total} trades, {recent_wr:.1%} win rate → "
                        f"+{adaptive_edge_bump:.3f} edge bump"
                    )
            if get_loss_streak:
                streak_info = get_loss_streak(config=cfg)
                streak = streak_info.get("streak", 0)
                if streak >= sp.loss_streak_threshold:
                    excess = streak - sp.loss_streak_threshold
                    streak_bump = min(
                        sp.loss_streak_extra_edge * (1 + excess * 0.5),
                        sp.loss_streak_max_extra,
                    )
                    adaptive_edge_bump += streak_bump
                    print(
                        f"[Adaptive] Loss streak: {streak} consecutive losses → "
                        f"+{streak_bump:.3f} extra edge (total bump now {adaptive_edge_bump:.3f})"
                    )
            if get_ewma_win_rate:
                ewma_info = get_ewma_win_rate(
                    halflife_trades=sp.ewma_halflife_trades, config=cfg,
                )
                ewma_wr = ewma_info.get("ewma_win_rate")
                if ewma_wr is not None and ewma_info.get("n_trades", 0) >= 20 and ewma_wr < 0.5:
                    ewma_shortfall = 0.5 - ewma_wr
                    ewma_bump = min(ewma_shortfall * 100 * sp.ewma_edge_scale_per_pct, 0.10)
                    adaptive_edge_bump += ewma_bump
                    print(
                        f"[Adaptive] EWMA win rate: {ewma_wr:.1%} "
                        f"(halflife={sp.ewma_halflife_trades}) → +{ewma_bump:.3f} edge"
                    )
            if get_recent_performance_by_series:
                series_performance = get_recent_performance_by_series(
                    lookback_hours=sp.adaptive_edge_lookback_hours * 2, config=cfg,
                )
                for series, perf in series_performance.items():
                    cl = perf.get("consecutive_losses", 0)
                    if cl >= sp.series_cooldown_threshold:
                        print(
                            f"[Adaptive] Series {series}: {cl} consecutive losses → "
                            f"+{sp.series_cooldown_extra_edge:.3f} extra edge for this series"
                        )
            adaptive_edge_bump = min(adaptive_edge_bump, sp.adaptive_edge_max_extra + sp.loss_streak_max_extra)
            if (
                adaptive_edge_bump > 0
                and get_seconds_since_last_resolved_trade
                and sp.adaptive_edge_idle_decay_halflife_hours > 0
            ):
                try:
                    sec_idle = get_seconds_since_last_resolved_trade(config=cfg)
                    if sec_idle is not None:
                        idle_h = sec_idle / 3600.0
                        start = float(sp.adaptive_edge_idle_decay_start_hours)
                        if idle_h > start:
                            half = float(sp.adaptive_edge_idle_decay_halflife_hours)
                            extra_h = idle_h - start
                            factor = 0.5 ** (extra_h / max(1e-6, half))
                            if factor < 1.0:
                                prev_b = adaptive_edge_bump
                                adaptive_edge_bump *= factor
                                print(
                                    f"[Adaptive] Idle decay: {idle_h:.1f}h since last resolution "
                                    f"→ edge bump {prev_b:.3f} → {adaptive_edge_bump:.3f}"
                                )
                except Exception:
                    pass
            if adaptive_edge_bump > 0:
                print(f"[Adaptive] Total adaptive edge bump: +{adaptive_edge_bump:.3f}")
                print()
        except Exception:
            pass

    kelly_fraction_live = _adaptive_kelly_fraction(kelly_fraction, calibration, sp)
    stress_sz = _adaptive_stress_size_multiplier(adaptive_edge_bump, sp)
    if stress_sz < 1.0:
        kelly_fraction_live = max(0.01, kelly_fraction_live * stress_sz)
    if use_kelly:
        extra_risk = f" (adaptive stress ×{stress_sz:.2f})" if stress_sz < 1.0 else ""
        print(f"[Risk] Kelly fraction: base={kelly_fraction:.2f} adaptive={kelly_fraction_live:.2f}{extra_risk}")
        print()
    macro_signal_24h = _compute_macro_signal_24h(cfg, sp)
    if sp.macro_signal_enabled:
        print(f"[Macro] 24h macro signal={macro_signal_24h:+.2%} from {', '.join(sp.macro_signal_tickers)}")
        print()
    # --- Lead-lag signals: precompute momentum for leader assets ---
    lead_lag_signals: dict[str, float] = {}
    if sp.ae_cfg.lead_lag_enabled:
        for leader_ticker, follower_prefix in sp.ae_cfg.lead_lag_pairs.items():
            try:
                leader_closes = yf_get_aggs(leader_ticker, days_back=5, config=cfg)
                if leader_closes and len(leader_closes) >= sp.ae_cfg.lead_lag_lookback_bars + 1:
                    sig = lead_lag_signal(
                        leader_closes,
                        lookback=sp.ae_cfg.lead_lag_lookback_bars,
                        min_move_pct=sp.ae_cfg.lead_lag_min_move_pct,
                    )
                    if sig != 0.0:
                        lead_lag_signals[follower_prefix] = sig
                        print(f"[Lead-Lag] {leader_ticker}→{follower_prefix}: {sig:+.3%}")
            except Exception:
                continue
        if lead_lag_signals:
            print()
    emergency_stop = False
    if get_realized_pnl_stats and (sp.max_daily_loss_pct > 0 or sp.max_daily_loss_cents > 0):
        try:
            since_ts = time.time() - 86400
            pnl_stats = get_realized_pnl_stats(since_ts=since_ts, config=cfg) or {}
            pnl_24h = int(pnl_stats.get("total_pnl_cents", 0) or 0)
            loss_24h = max(0, -pnl_24h)
            # Use min balance floor ($25) so low-balance accounts don't get permanently locked out
            balance_for_pct = max(balance_cents, 2500)
            loss_pct = (loss_24h / max(1, balance_for_pct))
            breach_pct = sp.max_daily_loss_pct > 0 and loss_pct >= sp.max_daily_loss_pct
            breach_abs = sp.max_daily_loss_cents > 0 and loss_24h >= sp.max_daily_loss_cents
            if breach_pct or breach_abs:
                emergency_stop = True
                print(
                    "[Risk] EMERGENCY_STOP active: "
                    f"24h realized loss=${loss_24h/100:.2f} ({loss_pct:.1%} of balance). "
                    "New buys will be blocked until losses recover or limits are adjusted."
                )
                print()
        except Exception:
            pass
    probability_calibrator = _fit_probability_calibrator(sp, cfg)
    learned_signal_weights = _fit_signal_model_weights(sp, cfg)
    if learned_signal_weights:
        merged = dict(sp.signal_model_weights or {})
        merged.update(learned_signal_weights)
        sp.signal_model_weights = merged
        print(
            f"[Signal Model] Learned weights from resolved data "
            f"({len(learned_signal_weights) - 1} features + bias)"
        )
        print()

    # Fill attribution diagnostics (actual vs theoretical execution quality)
    if get_fill_attribution_stats:
        try:
            fill_stats = get_fill_attribution_stats(cfg)
            if fill_stats.get("enabled") and fill_stats.get("count", 0) > 0:
                avg_slip = fill_stats.get("avg_fill_slippage_bps")
                avg_spread = fill_stats.get("avg_spread_bps_at_place")
                with_th = fill_stats.get("with_theoretical", 0)
                msg = (
                    f"[Fill Attribution] {fill_stats['count']} fills ({with_th} with theoretical) | "
                    f"avg slippage {avg_slip:.1f} bps | avg spread {avg_spread:.1f} bps"
                    if avg_slip is not None and avg_spread is not None
                    else f"[Fill Attribution] {fill_stats['count']} fills ({with_th} with theoretical)"
                )
                print(msg)
                print()
        except Exception:
            pass

    yes_parity_penalty, no_parity_penalty = _compute_directional_parity_penalty(sp, cfg)
    if yes_parity_penalty > 0 or no_parity_penalty > 0:
        print(
            "[Parity] Dynamic side penalty active | "
            f"YES +{yes_parity_penalty:.1%}, NO +{no_parity_penalty:.1%} "
            f"(lookback {sp.parity_lookback_days}d)"
        )
        print()
    if sp.yes_overconfidence_extra_edge > 0 or sp.model_probability_shrinkage < 1.0:
        print(
            f"[YES Bias Correction] overconfidence_edge +{sp.yes_overconfidence_extra_edge:.1%} | "
            f"probability_shrinkage {sp.model_probability_shrinkage:.0%}"
        )
        print()

    # Limit order analysis: learn from unfilled limit orders (why they didn't hit)
    if getattr(args, "limit_analysis", False) and get_limit_order_analysis:
        try:
            analysis = get_limit_order_analysis(cfg, days=90)
            orders = analysis.get("orders", [])
            summary = analysis.get("summary", {})
            if orders:
                print("[Limit Order Analysis] Unfilled limit orders (last 90 days):")
                for o in orders[:15]:  # Show up to 15
                    status = o.get("status", "?")
                    cancel_reason = o.get("cancel_reason") or ""
                    would = "would have filled" if o.get("would_have_filled") else "never touched"
                    print(f"  {o.get('ticker','?')} {o.get('side','?')} @ {o.get('limit_price',0):.2f} ({status}) — {would}"
                          f"{' — cancelled: ' + cancel_reason if cancel_reason else ''}")
                total = summary.get("total", 0)
                would_fill = summary.get("would_have_filled", 0)
                if total > 0:
                    print(f"  Summary: {total} unfilled | {would_fill} would have filled if we'd kept/waiting — "
                          f"inform limit_dip_pct tuning")
                print()
        except Exception:
            pass

    # Position evaluation analysis: KEEP vs SELL outcome stats
    if getattr(args, "position_eval_analysis", False) and get_position_evaluation_stats:
        try:
            pe_stats = get_position_evaluation_stats(cfg, days=90)
            if pe_stats.get("enabled") and pe_stats.get("total", 0) > 0:
                keep = pe_stats.get("keep", {})
                sell = pe_stats.get("sell", {})
                print("[Position Eval Analysis] KEEP vs SELL outcomes (last 90 days, last eval per position):")
                if keep.get("count", 0) > 0:
                    wr = f"{keep['win_rate']:.0%}" if keep.get("win_rate") is not None else "N/A"
                    print(f"  KEEP: {keep['count']} positions | {keep.get('wins',0)} won, {keep.get('losses',0)} lost | win rate {wr} | PnL {keep.get('pnl_cents',0):+d}¢")
                if sell.get("count", 0) > 0:
                    wr = f"{sell['win_rate']:.0%}" if sell.get("win_rate") is not None else "N/A"
                    print(f"  SELL: {sell['count']} positions | {sell.get('wins',0)} won, {sell.get('losses',0)} lost | win rate {wr} | PnL {sell.get('pnl_cents',0):+d}¢")
                buckets = pe_stats.get("keep_buckets", [])
                if buckets:
                    print("  KEEP win rate by hist_p bucket:")
                    for b in buckets:
                        wr = f"{b['win_rate']:.0%}" if b.get("win_rate") is not None else "N/A"
                        print(f"    {b['hist_p_range']}: {b['count']} positions, win rate {wr}")
                print()
            elif pe_stats.get("enabled"):
                print("[Position Eval Analysis] No resolved position evaluations yet.\n")
        except Exception:
            pass

    # Build ticker -> (series, yf_ticker, closes, market_data) for sell evaluation
    market_data_cache: dict[str, tuple[str, str, list[float], dict, float]] = {}
    _eval_count_total = 0
    _eval_count_skip = 0
    _eval_count_buy = 0
    any_momentum_crash = False
    any_vol_spike_active = False
    any_defensive_mode = False

    use_vol = bool(getattr(args, "use_volatility", False) or sp.use_volatility_by_default)

    # Parallel Yahoo/Gemini spot history + snapshot prefetch (one thread per series)
    def _prefetch_series_yf(series_raw: str) -> tuple[str, str | None, list[float] | None, float | None, str]:
        s = series_raw.strip()
        yt = yf_mapping.get(s.upper())
        if not yt:
            return s, None, None, None, "no_map"
        try:
            cl = yf_get_aggs(yt, days_back=args.days, config=cfg)
            if not cl:
                return s, yt, None, None, "no_closes"
            cl = apply_underlying_closes(cfg, s, cl)
            snap = yf_get_snapshot(yt) if sp.use_live_price else None
            if snap is not None:
                snap = apply_underlying_price(cfg, s, snap)
            return s, yt, cl, snap, "ok"
        except Exception:
            return s, yt, None, None, "err"

    _yf_series_cache: dict[str, tuple[str | None, list[float] | None, float | None, str]] = {}
    _yf_workers = min(8, max(1, len(markets_list)))
    with ThreadPoolExecutor(max_workers=_yf_workers) as _yf_ex:
        _yf_futs = [_yf_ex.submit(_prefetch_series_yf, s) for s in markets_list]
        for _fut in as_completed(_yf_futs):
            s_key, yt, cl, snap, st = _fut.result()
            _yf_series_cache[s_key] = (yt, cl, snap, st)

    for series in markets_list:
        series = series.strip()
        _cached = _yf_series_cache.get(series, (None, None, None, "no_map"))
        yf_ticker, closes, snap_pref, _pst = _cached
        if _pst == "no_map" or not yf_ticker:
            print(f"[{series}] No yfinance_tickers mapping - skip")
            continue
        if _pst in ("no_closes", "err") or not closes:
            print(f"[{series}] No close data for {yf_ticker}")
            continue

        # Blend live + closes[-1] when both available — use all signals, slightly prioritize live
        last_close = float(closes[-1])
        _snap = snap_pref if (sp.use_live_price and snap_pref is not None and snap_pref > 0) else None
        if _snap is None and sp.use_live_price:
            _snap = apply_underlying_price(cfg, series, yf_get_snapshot(yf_ticker))
        if _snap is not None and _snap > 0:
            live_f = float(_snap)
            w = sp.live_price_weight
            current_price = w * live_f + (1 - w) * last_close
            live_note = " (blend live+close)"
        else:
            current_price = last_close
            live_note = ""
        momentum_ret = ((current_price - last_close) / last_close) if last_close > 0 else 0.0
        momentum_crash = (
            sp.enable_momentum_protection
            and yf_ticker in GEMINI_CRYPTO_YF
            and momentum_ret <= sp.momentum_crash_threshold_pct
        )
        if momentum_crash:
            any_momentum_crash = True
        curr_vol, base_vol = _volatility_spike_metrics(closes)
        vol_ratio = _vol_ratio(curr_vol, base_vol)
        edge_regime_mult, dip_regime_mult, size_regime_mult = VolRegimeStrategy.volatility_regime_mults(
            sp, vol_ratio
        )
        dynamic_stability_seconds = _dynamic_stability_seconds(sp, vol_ratio)
        vol_spike_active = (
            sp.enable_vol_spike_safety
            and curr_vol is not None
            and base_vol is not None
            and base_vol > 0
            and (curr_vol / base_vol) >= sp.vol_spike_threshold_mult
        )
        if vol_spike_active:
            any_vol_spike_active = True
        defensive_mode_active = (
            sp.enable_defensive_mode
            and vol_ratio >= max(1.0, sp.defensive_vol_ratio_threshold)
        )
        if defensive_mode_active:
            any_defensive_mode = True
        _daily_chg = ((current_price - last_close) / last_close * 100) if last_close > 0 else 0.0
        _chg_sign = "+" if _daily_chg >= 0 else ""
        _hi = max(closes[-min(20, len(closes)):])
        _lo = min(closes[-min(20, len(closes)):])
        print(f"\n{'─' * 75}")
        print(f"  {series} ({yf_ticker})")
        _adj_m, _adj_a = underlying_adjust_params(cfg, series)
        if _adj_a != 0.0 or _adj_m != 1.0:
            _gas_note = (
                "; Kalshi settles on AAA national retail (gasprices.aaa.com), not RBOB"
                if series.upper() == "KXAAAGASW"
                else ""
            )
            print(
                f"  Underlying adjust: Yahoo × {_adj_m:.4f} + {_adj_a:.2f} $/gal "
                f"(align wholesale proxy to retail-style Kalshi thresholds){_gas_note}"
            )
        _px_cfg = proxy_basis_extra_min_edge(cfg, series)
        if _px_cfg > 0.0:
            print(
                f"  Proxy basis: +{_px_cfg:.2%} extra min edge (model vs settlement source)"
            )
        print(f"  Current Price:  ${current_price:,.2f}{live_note}  ({_chg_sign}{_daily_chg:.2f}% vs last close)")
        print(f"  20-Day Range:   ${_lo:,.2f} – ${_hi:,.2f}")
        print(f"  History:        {len(closes)} daily closes loaded")
        if sp.enable_vol_regime_scaling and vol_ratio > 1.0:
            print(
                f"  [Regime] vol ratio={vol_ratio:.2f}x | edge x{edge_regime_mult:.2f} "
                f"| dip x{dip_regime_mult:.2f} | size x{size_regime_mult:.2f}"
            )
        if dynamic_stability_seconds != sp.min_stability_duration_seconds:
            print(
                f"  [Execution] stability window scaled to {dynamic_stability_seconds}s "
                f"(base {sp.min_stability_duration_seconds}s)"
            )
        if vol_spike_active and curr_vol is not None and base_vol is not None:
            print(
                f"  [Safety] Vol spike mode: current_vol/base_vol={curr_vol / base_vol:.2f}x "
                f"(threshold {sp.vol_spike_threshold_mult:.2f}x); "
                f"edge x{sp.vol_spike_edge_multiplier:.2f}"
            )
        if defensive_mode_active:
            print(
                f"  [Defensive] Sells-only mode active (vol ratio {vol_ratio:.2f}x >= "
                f"{sp.defensive_vol_ratio_threshold:.2f}x)"
            )

        mkt_list = kalshi_markets_by_series.get(series, [])

        gemini_used_in_arb: set[str] = set()
        for mkt in mkt_list or []:
            barbell_bucket = ""
            barbell_blocked = False
            joint_blend_pre: float | None = None
            joint_blend_post: float | None = None
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
            if sp.enable_strike_validation and not _validate_threshold_with_market(
                threshold=threshold,
                direction=direction,
                current_price=current_price,
                yes_bid=yes_bid,
                tolerance_pct=proxy_strike_validation_tolerance_pct(
                    cfg, series, sp.strike_validation_tolerance_pct
                ),
            ):
                print(f"  {ticker}: SKIP (threshold validation failed) | {title}")
                continue

            market_data_cache[ticker] = (series, yf_ticker, closes, m, current_price)

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
                gemini_match = _find_matching_gemini_event(
                    _gemini_events_list_for_yf_ticker(gemini_events_by_asset, yf_ticker),
                    threshold,
                    direction,
                )
                if gemini_match:
                    inter = _check_inter_arb(
                        m,
                        gemini_match,
                        sp.min_arb_fee,
                        kalshi_balance_cents=balance_cents,
                        gemini_balance_cents=gemini_balance_for_arb,
                        max_pct_of_balance=max_pct_for_arb_routing,
                        balance_aware=sp.inter_arb_balance_aware_routing,
                    )
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

            closes_for_emp = _get_closes_for_empirical(yf_ticker, args.days, closes, m, sp, cfg, kalshi_series=series)
            recent_w = _regime_adjusted_recent_weight(closes_for_emp, sp.empirical_recent_weight)
            emp = _empirical_probability(
                closes_for_emp,
                threshold,
                direction,
                recent_days=sp.empirical_recent_days,
                recent_weight=recent_w,
                use_rolling_window=sp.empirical_use_rolling_window,
                weighting=sp.empirical_weighting,
                exponential_halflife_bars=sp.empirical_exponential_halflife_bars,
            )
            hist_p = emp
            dte = _get_days_to_expiry_from_market(m, yf_ticker, sp.days_to_expiry)
            ewma_lambda_used = _ewma_lambda_for_market(sp, m, yf_ticker)
            vol_p = (
                _volatility_probability_with_mc(
                    closes,
                    threshold,
                    direction,
                    dte,
                    use_ewma=sp.use_ewma_vol,
                    ewma_lambda=ewma_lambda_used,
                    current_price=current_price,
                    vol_model=sp.vol_model,
                    garch_min_returns=sp.garch_min_returns,
                    enable_otm_smile_adjustment=sp.enable_otm_smile_adjustment,
                    otm_smile_strength=sp.otm_smile_strength,
                    otm_smile_max_mult=sp.otm_smile_max_mult,
                    distribution=sp.vol_distribution,
                    t_df=sp.vol_t_df,
                    mc_paths=sp.mc_paths if sp.mc_enabled else 0,
                    mc_blend_weight=sp.mc_blend_weight,
                )
                if use_vol else None
            )
            if vol_p is not None and use_vol:
                _is_intra = _is_intraday_or_ultra_short(m, yf_ticker, sp)
                if _is_intra:
                    vol_p = _cap_intraday_vol_p(vol_p, sp.intraday_vol_cap)
                if _is_intra and not sp.use_intraday_empirical:
                    hist_p = vol_p
                elif _is_intra and sp.use_intraday_empirical:
                    w_emp = _dynamic_empirical_weight(sp.vol_blend_weight, len(closes_for_emp), sp.vol_dynamic_n0)
                    w_emp = _dte_adjusted_empirical_weight(w_emp, dte)
                    w_emp = max(w_emp, sp.intraday_min_empirical_weight)
                    joint_blend_pre, joint_blend_post = _vol_emp_blend_hist_p_with_joint_trace(
                        emp, vol_p, w_emp, sp
                    )
                    hist_p = joint_blend_post
                elif sp.vol_blend_weight is not None:
                    w_emp = _dynamic_empirical_weight(sp.vol_blend_weight, len(closes_for_emp), sp.vol_dynamic_n0)
                    w_emp = _dte_adjusted_empirical_weight(w_emp, dte)
                    if _is_intra:
                        w_emp = max(w_emp, sp.intraday_min_empirical_weight)
                    joint_blend_pre, joint_blend_post = _vol_emp_blend_hist_p_with_joint_trace(
                        emp, vol_p, w_emp, sp
                    )
                    hist_p = joint_blend_post
                else:
                    hist_p = max(emp, vol_p)
            # Anti-gambler sanity buffer on vol/emp blend (before bucket & signal layers)
            if sp.enable_model_prob_hard_cap:
                hist_p = max(sp.model_prob_hard_lo, min(sp.model_prob_hard_hi, float(hist_p)))
            # Bucket penalty + optional sklearn probability calibration.
            hist_p = strategy_engine.apply_bucket_penalty(
                hist_p=hist_p,
                calibration=calibration,
                use_bucket_penalty=sp.use_bucket_penalty and not sp.simple_ev_decision_enabled,
                use_calibration=sp.use_calibration,
                min_bucket_samples=sp.min_bucket_samples,
            )
            if probability_calibrator:
                hist_p = probability_calibrator(hist_p)
            if not sp.simple_ev_decision_enabled and sp.model_probability_shrinkage < 1.0:
                hist_p = 0.5 + sp.model_probability_shrinkage * (hist_p - 0.5)
            no_bid_signal = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
            yes_ask_signal = float(m.get("yes_ask_dollars") or 0) or None
            spread_bps_signal = None
            if yes_ask_signal and yes_bid > 0 and yes_ask_signal >= yes_bid:
                spread_bps_signal = ((yes_ask_signal - yes_bid) / max(0.01, yes_bid)) * 10_000.0
            if sp.enable_signal_model:
                rsi_for_signal = _compute_rsi(closes)
                feats = strategy_engine.build_signal_features(
                    emp_p=emp,
                    vol_p=vol_p,
                    market_yes=yes_bid,
                    threshold=threshold,
                    current_price=current_price,
                    dte_days=dte,
                    spread_bps=spread_bps_signal,
                    no_bid=no_bid_signal,
                    macro_24h_ret=macro_signal_24h,
                    rsi_14=rsi_for_signal,
                )
                hist_p = strategy_engine.score_signal_probability(
                    features=feats,
                    signal_model_weights=sp.signal_model_weights,
                )
            # --- Anti-hallucination pipeline: correct model P before recommendation ---
            hist_p_raw = hist_p
            if not sp.simple_ev_decision_enabled:
                hist_p = apply_anti_hallucination_pipeline(
                    model_p=hist_p,
                    market_p=yes_bid,
                    closes=closes,
                    threshold=threshold,
                    direction=direction,
                    current_price=current_price,
                    momentum_ret=momentum_ret,
                    cfg=sp.ah_cfg,
                )
            hist_p = _sanitize_model_probability(
                hist_p,
                lo=sp.model_prob_hard_lo,
                hi=sp.model_prob_hard_hi,
                enabled=sp.enable_model_prob_hard_cap,
            )
            maker_fee_rate = _maker_fee_rate_for_market(sp, PLATFORM_KALSHI, yf_ticker)
            no_bid_early = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
            effective_min_core_trace: float | None = None
            _us_edge_detail: dict[str, Any] | None = None
            _eff_after_simple_bundle: float | None = None
            _proxy_edge_simple = 0.0
            if sp.simple_ev_decision_enabled:
                _eff_after_simple_bundle, _us_edge_detail = _simple_ev_net_min_edge_bundle(sp, m, yf_ticker)
                _proxy_edge_simple = float(proxy_basis_extra_min_edge(cfg, series))
                effective_min = float(_eff_after_simple_bundle) + _proxy_edge_simple
                if _proxy_edge_simple > 0.0 and _us_edge_detail is not None:
                    _us_edge_detail = {**_us_edge_detail, "proxy_basis_extra_min_edge": _proxy_edge_simple}
                effective_min_yes = effective_min
                effective_min_no = effective_min
                obi_adj_yes = obi_adj_no = 0.0
                obi_val = None
                ll_adj_yes = ll_adj_no = 0.0
                _print_effective_edge_trace(
                    sp,
                    min_edge,
                    simple_mode=True,
                    simple_ev_effective=effective_min,
                    ultra_short_detail=_us_edge_detail,
                )
            else:
                effective_min = _get_effective_min_edge(min_edge, direction, calibration, sp)
                em0_trace = effective_min
                emp_n = min(sp.empirical_recent_days, len(closes_for_emp)) if sp.empirical_use_rolling_window else len(closes_for_emp)
                ci_half = _empirical_ci_halfwidth(emp, emp_n)
                effective_min += sp.uncertainty_edge_mult * ci_half
                _proxy_edge_leg = proxy_basis_extra_min_edge(cfg, series)
                if _proxy_edge_leg > 0.0:
                    effective_min += _proxy_edge_leg
                    if sp.log_effective_min_edge:
                        print(
                            f"    ├─ proxy_basis_extra_min_edge: +{_proxy_edge_leg:.2%} "
                            f"(underlying proxy vs settlement index)"
                        )
                effective_min *= edge_regime_mult
                if vol_spike_active:
                    effective_min *= max(1.0, sp.vol_spike_edge_multiplier)
                if vol_p is not None and abs(emp - vol_p) > sp.vol_disagree_threshold:
                    effective_min += sp.vol_disagree_extra_edge
                # --- Adaptive edge: scale required edge with contract price and trend ---
                if sp.ah_cfg.adaptive_edge_enabled:
                    effective_min = adaptive_min_edge(
                        effective_min,
                        yes_bid,
                        current_price=current_price,
                        threshold=threshold,
                        direction=direction,
                        momentum_ret=momentum_ret,
                        cheap_threshold=sp.ah_cfg.adaptive_edge_cheap_threshold,
                        expensive_threshold=sp.ah_cfg.adaptive_edge_expensive_threshold,
                        otm_base_mult=sp.ah_cfg.adaptive_edge_otm_base_mult,
                        trend_opposed_mult=sp.ah_cfg.adaptive_edge_trend_opposed_mult,
                        strike_dist_mult=sp.ah_cfg.adaptive_edge_strike_dist_mult,
                    )
                # --- Alpha engine: toxic flow penalty ---
                if sp.ae_cfg.markout_enabled and get_markout_history:
                    _markout_hist = get_markout_history(PLATFORM_KALSHI, ticker, window_minutes=5, config=cfg)
                    _toxic_penalty = toxic_flow_edge_penalty(
                        _markout_hist,
                        threshold_bps=sp.ae_cfg.markout_toxic_threshold_bps,
                        min_samples=sp.ae_cfg.markout_min_samples,
                        penalty=sp.ae_cfg.markout_edge_penalty,
                    )
                    effective_min += _toxic_penalty
                # --- Alpha engine: OBI edge adjustment ---
                obi_val = None
                if sp.ae_cfg.obi_enabled:
                    _book_obi = _extract_book_fields(m, yes_bid, float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid))
                    obi_val = compute_obi(
                        _book_obi.get("yes_bid_size"), _book_obi.get("yes_ask_size"),
                        min_total_size=sp.ae_cfg.obi_min_total_size,
                    )
                    if obi_val is not None:
                        obi_adj_yes = obi_edge_adjustment(
                            obi_val, "BUY YES",
                            strong_threshold=sp.ae_cfg.obi_strong_threshold,
                            max_bonus=sp.ae_cfg.obi_edge_bonus_max,
                        )
                        obi_adj_no = obi_edge_adjustment(
                            obi_val, "BUY NO",
                            strong_threshold=sp.ae_cfg.obi_strong_threshold,
                            max_bonus=sp.ae_cfg.obi_edge_bonus_max,
                        )
                    else:
                        obi_adj_yes = obi_adj_no = 0.0
                else:
                    obi_adj_yes = obi_adj_no = 0.0
                # --- Alpha engine: lead-lag edge adjustment ---
                ll_adj_yes = ll_adj_no = 0.0
                if sp.ae_cfg.lead_lag_enabled:
                    for _ll_prefix, _ll_sig in lead_lag_signals.items():
                        if ticker.startswith(_ll_prefix):
                            ll_adj_yes = lead_lag_edge_adjustment(
                                _ll_sig, direction, "BUY YES",
                                edge_bonus=sp.ae_cfg.lead_lag_edge_bonus,
                            )
                            ll_adj_no = lead_lag_edge_adjustment(
                                _ll_sig, direction, "BUY NO",
                                edge_bonus=sp.ae_cfg.lead_lag_edge_bonus,
                            )
                            break
                # --- Adaptive learning: bump edge from recent performance & loss streaks ---
                if sp.adaptive_learning_enabled and adaptive_edge_bump > 0:
                    effective_min += adaptive_edge_bump
                if sp.adaptive_learning_enabled and series_performance:
                    _series_prefix = ticker.split("-")[0] if ticker else ""
                    _sp_info = series_performance.get(_series_prefix)
                    if _sp_info and _sp_info.get("consecutive_losses", 0) >= sp.series_cooldown_threshold:
                        effective_min += sp.series_cooldown_extra_edge
                if sp.dte_edge_scaling_enabled and dte is not None:
                    effective_min *= _dte_edge_scale_factor(float(dte), sp)
                if _is_ultra_short_horizon(m, yf_ticker, sp) and sp.ultra_short_min_edge_mult > 1.0:
                    effective_min *= float(sp.ultra_short_min_edge_mult)
                effective_min_core_trace = float(effective_min)
                effective_min_yes = effective_min + yes_parity_penalty + obi_adj_yes + ll_adj_yes + sp.yes_overconfidence_extra_edge
                effective_min_no = effective_min + no_parity_penalty + obi_adj_no + ll_adj_no
                _print_effective_edge_trace(
                    sp,
                    min_edge,
                    simple_mode=False,
                    em0=em0_trace,
                    effective_min_core=effective_min_core_trace,
                    effective_min_yes=effective_min_yes,
                    effective_min_no=effective_min_no,
                )
            # For non-standard intraday (e.g. 8pm ET): use relaxed threshold when volatility model available
            min_p_win = sp.intraday_other_min_p_win_with_vol if (use_vol and vol_p is not None) else sp.intraday_other_min_p_win
            if _is_ultra_short_horizon(m, yf_ticker, sp):
                _up = (
                    sp.ultra_short_min_p_win_with_vol
                    if (use_vol and vol_p is not None)
                    else sp.ultra_short_min_p_win
                )
                min_p_win = max(min_p_win, _up)
            cross_sanity_block_side = None
            cross_sanity_reason = ""
            if sp.enable_cross_exchange_sanity_gate and yf_ticker in GEMINI_CRYPTO_YF:
                try:
                    gem_ref = _find_matching_gemini_event(
                        _gemini_events_list_for_yf_ticker(gemini_events_by_asset, yf_ticker),
                        threshold,
                        direction,
                    )
                    if gem_ref:
                        gem_ref_yes = get_yes_price(_get_yes_contract(gem_ref) or {})
                        cross_sanity_block_side = _cross_exchange_sanity_block_side(
                            sp,
                            momentum_ret=momentum_ret,
                            local_yes=yes_bid,
                            reference_yes=gem_ref_yes,
                        )
                        if cross_sanity_block_side:
                            cross_sanity_reason = (
                                "cross-exchange sanity gate: "
                                f"Kalshi YES {yes_bid:.1%} vs Gemini YES {gem_ref_yes:.1%} during "
                                f"{momentum_ret:+.2%} spot move"
                            )
                except Exception:
                    pass
            if sp.simple_ev_decision_enabled:
                rec = _simple_ev_recommend(
                    hist_p,
                    yes_bid,
                    no_bid_early,
                    effective_min,
                    maker_fee_rate,
                    sp.min_fee_per_order,
                    sp,
                )
            else:
                rec = (
                    strategy_engine.recommend_strict(
                        p_yes=hist_p,
                        market_yes=yes_bid,
                        min_p_win=min_p_win,
                        min_edge=effective_min,
                        maker_fee_rate=maker_fee_rate,
                        min_fee_per_order=sp.min_fee_per_order,
                        min_edge_yes=effective_min_yes,
                        min_edge_no=effective_min_no,
                        **_strategy_recommend_strict_extras(sp),
                    )
                    if _strict_intraday_evaluation(m, yf_ticker, sp)
                    else strategy_engine.recommend(
                        p_yes=hist_p,
                        market_yes=yes_bid,
                        min_edge=effective_min,
                        maker_fee_rate=maker_fee_rate,
                        min_fee_per_order=sp.min_fee_per_order,
                        min_edge_yes=effective_min_yes,
                        min_edge_no=effective_min_no,
                        min_edge_easy_win=sp.min_edge_easy_win if sp.enable_easy_win else None,
                        easy_win_min_p_win=sp.easy_win_min_p_win,
                        easy_win_min_market_p=sp.easy_win_min_market_p,
                        **_strategy_recommend_extras(sp),
                    )
                )
            if rec in (REC_BUY_YES, REC_BUY_NO) and sp.max_new_buy_dte_days > 0 and dte is not None and float(dte) > sp.max_new_buy_dte_days:
                rec = REC_SKIP
            # Skip BUY NO when price already above strike (risky to bet NO)
            violates_rule = _violates_no_above_rule(current_price, threshold, direction, sp.no_above_cushion)
            if rec == REC_BUY_NO and sp.enforce_no_above_rule and violates_rule:
                rec = REC_SKIP
            # Favor BUY YES when price is well above strike — but only if we have edge (after fees) and calibration allows it
            if rec == REC_SKIP and violates_rule:
                _favor_fee = _fee_per_contract(yes_bid, maker_fee_rate, sp.min_fee_per_order)
                if 0 < yes_bid < (1 - effective_min) and hist_p >= yes_bid + effective_min + _favor_fee:
                    should_favor = sp.favor_yes_when_above_strike
                    if should_favor and sp.use_calibration and calibration:
                        above_yes = calibration.get("by_direction_side", {}).get("above+yes", {})
                        cnt = above_yes.get("count", 0)
                        wr = above_yes.get("win_rate", 1.0)
                        if cnt >= sp.min_calibration_samples and wr < sp.calibration_min_win_rate_above_yes:
                            should_favor = False  # Historical "above+YES" underperforming
                    if should_favor:
                        rec = REC_BUY_YES
            # Skip BUY YES when market prices it very low (e.g. 1¢): market thinks <5% chance
            if rec == REC_BUY_YES and yes_bid < sp.min_yes_price:
                rec = REC_SKIP
            # Steamroller: skip expensive YES unless barbell base layer (high-P consensus up to barbell_base_max_yes_price)
            if rec == REC_BUY_YES and sp.max_yes_price_for_buy > 0 and yes_bid > sp.max_yes_price_for_buy:
                if sp.simple_ev_decision_enabled or not sp.barbell_enabled:
                    rec = REC_SKIP
                elif sp.barbell_enabled:
                    if _barbell_yes_bucket(hist_p, yes_bid, maker_fee_rate, sp) != "base":
                        rec = REC_SKIP
            # Skip BUY YES when strike is far above current (far OTM) unless vol model shows min_vol_confidence_far_otm
            # DTE-aware: tighten allowed gap for short-dated contracts (price can't move far in minutes)
            # When DTE < 0.1 days (~2.4h), vol override is blocked — not enough time for large moves
            _eff_max_above = _dte_adjusted_strike_pct(sp.max_buy_yes_strike_above_pct, dte)
            _dte_too_short = dte is not None and dte < 0.1
            if rec == REC_BUY_YES and direction == "above" and _eff_max_above > 0:
                if threshold > current_price * (1 + _eff_max_above):
                    vol_override = use_vol and vol_p is not None and vol_p >= sp.min_vol_confidence_far_otm and not _dte_too_short
                    if not vol_override:
                        rec = REC_SKIP
            # Skip BUY NO when market prices NO very low (market thinks our side unlikely); same override when vol shows high P(NO)
            if rec == REC_BUY_NO and no_bid_early < sp.min_no_price:
                if not (use_vol and vol_p is not None and (1 - hist_p) >= 0.95):
                    rec = REC_SKIP
            # Skip BUY NO on "below X" when strike is far above current (BUY NO = "price > X" needs big rally) unless vol shows P(NO) ≥95%
            _eff_max_no_above = _dte_adjusted_strike_pct(sp.max_buy_no_strike_above_pct, dte)
            if rec == REC_BUY_NO and direction == "below" and _eff_max_no_above > 0:
                if threshold > current_price * (1 + _eff_max_no_above):
                    vol_override_no = use_vol and vol_p is not None and (1 - hist_p) >= 0.95 and not _dte_too_short
                    if not vol_override_no:
                        rec = REC_SKIP
            if (
                not sp.simple_ev_decision_enabled
                and rec == REC_BUY_YES
                and _should_downgrade_buy_yes_above(
                    rec, direction, hist_p, yes_bid, min_edge, calibration, sp, maker_fee_rate
                )
            ):
                rec = REC_SKIP
            if rec == REC_BUY_YES and momentum_crash:
                rec = REC_SKIP
            # --- Regime filter: block BUY YES when EMA trend opposes trade direction ---
            if (
                not sp.simple_ev_decision_enabled
                and sp.ah_cfg.regime_filter_enabled
                and regime_filter(
                    closes, current_price, direction, rec, ema_period=sp.ah_cfg.regime_ema_period,
                )
            ):
                rec = REC_SKIP
            if rec in (REC_BUY_YES, REC_BUY_NO) and defensive_mode_active:
                rec = REC_SKIP
            if rec in (REC_BUY_YES, REC_BUY_NO):
                if rec == REC_BUY_YES and cross_sanity_block_side == "yes":
                    rec = REC_SKIP
                elif rec == REC_BUY_NO and cross_sanity_block_side == "no":
                    rec = REC_SKIP
            if sp.barbell_enabled and not sp.simple_ev_decision_enabled and rec in (REC_BUY_YES, REC_BUY_NO):
                _bb = (
                    _barbell_yes_bucket(hist_p, yes_bid, maker_fee_rate, sp)
                    if rec == REC_BUY_YES
                    else _barbell_no_bucket(hist_p, no_bid_early, maker_fee_rate, sp)
                )
                if _bb is None:
                    rec = REC_SKIP
                    barbell_blocked = True
                else:
                    barbell_bucket = _bb
            elif sp.simple_ev_decision_enabled and rec in (REC_BUY_YES, REC_BUY_NO):
                barbell_bucket = "simple_ev"
            diff = _compute_edge_diff(
                yes_bid,
                hist_p,
                rec,
                violates_rule,
                maker_fee_rate,
                min_fee_per_order=sp.min_fee_per_order,
                no_bid=no_bid_early,
            )
            no_bid_for_quote = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
            fee_yes_q = _fee_per_contract(yes_bid, maker_fee_rate, sp.min_fee_per_order)
            fee_no_q = _fee_per_contract(no_bid_for_quote, maker_fee_rate, sp.min_fee_per_order)
            book_q = _extract_book_fields(m, yes_bid, no_bid_for_quote)
            quote_candidates.append(
                {
                    "platform": PLATFORM_KALSHI,
                    "ticker": ticker,
                    "title": title,
                    "yf_ticker": yf_ticker,
                    "resolution_key": _extract_resolution_key_from_ticker(ticker) or "",
                    "direction": direction,
                    "threshold": threshold,
                    "hist_p": hist_p,
                    "vol_p": vol_p,
                    "yes_bid": yes_bid,
                    "no_bid": no_bid_for_quote,
                    "yes_ask": book_q.get("yes_ask"),
                    "no_ask": book_q.get("no_ask"),
                    "edge_yes_net": hist_p - yes_bid - fee_yes_q,
                    "edge_no_net": (1.0 - hist_p) - no_bid_for_quote - fee_no_q,
                    "arr_dip": min(sp.max_dip_cap, _adaptive_limit_dip(sp.limit_dip_pct, closes_for_emp, cap=sp.max_dip_cap) * dip_regime_mult),
                    "dte": dte,
                    "barbell": barbell_bucket,
                }
            )

            _dist_str = _fmt_distance(current_price, threshold, direction)
            _dte_str = _fmt_dte(dte)
            _rec_plain = _fmt_rec_plain(rec, "yes" if rec == REC_BUY_YES else "no", direction, threshold, current_price, dte)
            print(f"\n    {'·' * 60}")
            print(f"    {ticker}  [Kalshi]")
            print(f"    {(m.get('title') or title)[:70]}")
            print(f"    ├─ Strike:       {_fmt_price(threshold)} (pays if {direction})")
            print(f"    ├─ Current:      ${current_price:,.2f}  →  {_dist_str}")
            print(f"    ├─ Resolves in:  {_dte_str}")
            if vol_p is not None:
                emp_kalshi = _empirical_probability(
                    closes,
                    threshold,
                    direction,
                    recent_days=sp.empirical_recent_days,
                    recent_weight=sp.empirical_recent_weight,
                    use_rolling_window=sp.empirical_use_rolling_window,
                    weighting=sp.empirical_weighting,
                    exponential_halflife_bars=sp.empirical_exponential_halflife_bars,
                )
                print(f"    ├─ Empirical P:  {emp_kalshi:.1%}  (historical frequency)")
                _vp_k = _display_model_probability(vol_p, sp)
                _vp_note = "" if _vp_k is None or abs(float(_vp_k) - float(vol_p)) < 1e-9 else " (display capped)"
                print(f"    ├─ Volatility P: {_vp_k:.1%}{_vp_note}  (model based on current price + DTE)")
                ah_delta = abs(hist_p - hist_p_raw)
                ah_tag = f"  (AH: {hist_p_raw:.1%}→{hist_p:.1%})" if ah_delta >= 0.02 else ""
                print(f"    ├─ Blended P:    {hist_p:.1%}  (final model probability){ah_tag}")
                _print_barbell_joint_blend_diagnostic(
                    sp,
                    emp=emp,
                    vol_p=float(vol_p),
                    yes_bid=yes_bid,
                    no_bid=no_bid_early,
                    maker_fee_rate=maker_fee_rate,
                    joint_pre=joint_blend_pre,
                    joint_post=joint_blend_post,
                    final_p_yes=hist_p,
                )
            else:
                print(f"    ├─ Model P(YES): {hist_p:.1%}")
            print(f"    ├─ Market YES:   {yes_bid:.1%}  (what the market is pricing)")
            if _is_ultra_short_horizon(m, yf_ticker, sp):
                _usm = float(sp.ultra_short_min_edge_mult)
                _us_note = " (no extra ×)" if _usm <= 1.0 else ""
                print(
                    f"    ├─ Ultra-short:  min_edge ×{_usm:.2f} "
                    f"(ultra_short_min_edge_mult){_us_note}"
                )
            _alpha_tags = []
            if obi_val is not None:
                _alpha_tags.append(f"OBI={obi_val:+.2f}")
            for _ll_prefix, _ll_sig in lead_lag_signals.items():
                if ticker.startswith(_ll_prefix):
                    _alpha_tags.append(f"LL={_ll_sig:+.3%}")
            if _alpha_tags:
                print(f"    ├─ Alpha:        {' | '.join(_alpha_tags)}")
            print(f"    ├─ Verdict:      {rec}")
            if sp.log_effective_min_edge:
                _eys, _ens = _net_edges_buy_legs(
                    hist_p, yes_bid, no_bid_early, maker_fee_rate, sp.min_fee_per_order
                )
                print(
                    f"    ├─ Leg edges:     YES net {_eys:+.1%}  |  NO net {_ens:+.1%}  "
                    f"(bids {yes_bid:.1%} / {no_bid_early:.1%})"
                )
            if sp.log_decision_diagnostics:
                _edy, _edn = _net_edges_buy_legs(
                    hist_p, yes_bid, no_bid_early, maker_fee_rate, sp.min_fee_per_order
                )
                print_decision_diagnostics_line(
                    {
                        "ticker": ticker,
                        "platform": "kalshi",
                        "strategy_engine": str(strategy.get("strategy_engine", "vol_regime")),
                        "simple_ev": bool(sp.simple_ev_decision_enabled),
                        "p": {
                            "emp": float(emp),
                            "vol": float(vol_p) if vol_p is not None else None,
                            "joint_pre": float(joint_blend_pre) if joint_blend_pre is not None else None,
                            "joint_post": float(joint_blend_post) if joint_blend_post is not None else None,
                            "hist_p_raw": float(hist_p_raw),
                            "hist_p": float(hist_p),
                        },
                        "market": {"yes_bid": float(yes_bid), "no_bid": float(no_bid_early)},
                        "net_edge": {"yes": float(_edy), "no": float(_edn)},
                        "required": build_required_edge_breakdown(
                            simple_ev=bool(sp.simple_ev_decision_enabled),
                            effective_min_final=float(effective_min),
                            effective_min_yes=float(effective_min_yes),
                            effective_min_no=float(effective_min_no),
                            min_p_win=float(min_p_win),
                            us_detail=_us_edge_detail,
                            eff_after_simple_ev_bundle=_eff_after_simple_bundle,
                            proxy_basis_add=float(_proxy_edge_simple),
                            legacy_core_before_parity_obi=effective_min_core_trace,
                        ),
                        "regime": {"vol_ratio": float(vol_ratio), "edge_mult": float(edge_regime_mult)},
                        "verdict": rec,
                    }
                )
            if sp.barbell_enabled:
                if sp.simple_ev_decision_enabled:
                    print("    ├─ Barbell:      gating off (simple EV mode)")
                else:
                    print(
                        f"    ├─ Barbell:      "
                        f"{_barbell_status_for_log(barbell_bucket, barbell_blocked, rec)}"
                    )
            print(f"    │    → {_rec_plain}")
            _print_multi_strategy_compare_line(
                compare_strategies_arg=getattr(args, "compare_strategies", "") or "",
                hist_p=hist_p,
                market_yes=yes_bid,
                min_p_win=min_p_win,
                effective_min=effective_min,
                maker_fee_rate=maker_fee_rate,
                min_fee_per_order=sp.min_fee_per_order,
                effective_min_yes=effective_min_yes,
                effective_min_no=effective_min_no,
                is_strict_intraday=bool(
                    _strict_intraday_evaluation(m, yf_ticker, sp) and not sp.simple_ev_decision_enabled
                ),
                sp=sp,
            )
            _fee_yes_r = _fee_per_contract(yes_bid, maker_fee_rate, sp.min_fee_per_order)
            _fee_no_r = _fee_per_contract(no_bid_early, maker_fee_rate, sp.min_fee_per_order)
            _net_yes_r = hist_p - yes_bid - _fee_yes_r
            _net_no_r = (1.0 - hist_p) - no_bid_early - _fee_no_r
            reason = _format_reason(
                rec, hist_p, yes_bid, threshold, current_price, direction,
                violates_rule, vol_p, sp.min_yes_price, sp.min_no_price,
                sp.max_buy_yes_strike_above_pct, no_bid_early, effective_min, use_vol,
                net_edge_yes=_net_yes_r,
                net_edge_no=_net_no_r,
                req_yes=effective_min_yes,
                req_no=effective_min_no,
            )
            if rec == REC_SKIP and momentum_crash:
                reason = (
                    f"momentum protection: live-vs-close return {momentum_ret:.2%} <= "
                    f"{sp.momentum_crash_threshold_pct:.2%} threshold"
                )
            elif rec == REC_SKIP and defensive_mode_active:
                reason = (
                    f"defensive mode: vol ratio {vol_ratio:.2f}x >= "
                    f"{sp.defensive_vol_ratio_threshold:.2f}x (sells-only)"
                )
            elif rec == REC_SKIP and cross_sanity_reason:
                reason = cross_sanity_reason
            elif rec == REC_SKIP and barbell_blocked:
                reason = (
                    "barbell: directional trade not in base (high-P consensus) or alpha "
                    "(cheap leg + large net edge) bucket"
                )
            print(f"    └─ Reason:       {reason}")
            if getattr(args, "verbose", False):
                mn, mx = min(closes), max(closes)
                n = sum(1 for c in closes if (c > threshold if direction == "above" else c < threshold))
                print(f"       (closes {_fmt_price(mn)}–{_fmt_price(mx)}; {n}/{len(closes)} days {'above' if direction == 'above' else 'below'} threshold)")
            try:
                from storage import record_market_snapshot
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                book = _extract_book_fields(m, yes_bid, no_bid)
                record_market_snapshot(
                    PLATFORM_KALSHI,
                    ticker,
                    yes_bid,
                    no_bid,
                    hist_p,
                    rec,
                    threshold,
                    direction,
                    vol_p,
                    abs(diff),
                    title,
                    yf_ticker,
                    yes_ask=book.get("yes_ask"),
                    no_ask=book.get("no_ask"),
                    yes_bid_size=book.get("yes_bid_size"),
                    no_bid_size=book.get("no_bid_size"),
                    yes_ask_size=book.get("yes_ask_size"),
                    no_ask_size=book.get("no_ask_size"),
                    config=cfg,
                )
            except ImportError:
                pass
            added_dip = False
            if rec != REC_SKIP:
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                bid = yes_bid if rec == REC_BUY_YES else no_bid
                side = "yes" if rec == REC_BUY_YES else "no"
                if sp.skip_new_buy_when_unresolved_same_side and inventory_by_key.get(
                    (PLATFORM_KALSHI, ticker, side), 0
                ) > 0:
                    print(
                        "       Note: skip new BUY — same-side position already tracked in unresolved predictions "
                        "(skip_new_buy_when_unresolved_same_side)"
                    )
                    print()
                    continue
                p_win = hist_p if rec == REC_BUY_YES else (1 - hist_p)
                _ey_o, _en_o = _net_edges_buy_legs(
                    hist_p, yes_bid, no_bid, maker_fee_rate, sp.min_fee_per_order
                )
                _chosen_net_k = _ey_o if rec == REC_BUY_YES else _en_o
                print(
                    f"       Edge: {_chosen_net_k:.1%}  |  P(win): {p_win:.1%}  |  "
                    f"Side: {side.upper()}  |  Cost/contract: ${bid:.2f}"
                )
                dip = _adaptive_limit_dip(sp.limit_dip_pct, closes_for_emp, cap=sp.max_dip_cap)
                dip = min(sp.max_dip_cap, dip * dip_regime_mult)
                price_might_decline = (use_vol and vol_p is not None) or (dte is not None and dte >= 2)
                if price_might_decline and sp.limit_dip_pct_price_decline > 0:
                    dip = max(dip, sp.limit_dip_pct_price_decline)
                if p_win >= 0.90 and sp.limit_dip_pct_high_confidence > 0:
                    dip = max(dip, sp.limit_dip_pct_high_confidence)
                dip = _dte_confidence_dip(dip, p_win, dte, sp)
                rsi_val = _compute_rsi(closes)
                dip = _rsi_dip_adjustment(dip, rsi_val, sp.max_dip_cap)
                book = _extract_book_fields(m, yes_bid, no_bid)
                bid_size_side = book.get("yes_bid_size") if side == "yes" else book.get("no_bid_size")
                if sp.simple_ev_decision_enabled and sp.simple_ev_min_bid_size > 0:
                    _dv_ev = float(bid_size_side) if bid_size_side is not None else 0.0
                    if _dv_ev < float(sp.simple_ev_min_bid_size):
                        print(
                            f"    Why: simple_ev min bid size {sp.simple_ev_min_bid_size:.0f} "
                            f"not met ({_dv_ev:.0f} on {side.upper()})"
                        )
                        print()
                        continue
                depth_mult = 1.0
                if sp.min_order_book_depth > 0:
                    depth_val = float(bid_size_side) if bid_size_side is not None else 0.0
                    depth_req = float(sp.min_order_book_depth) * min(2.5, max(1.0, vol_ratio))
                    if depth_val < depth_req:
                        # Layered mode: allow thin-book participation but scale size down.
                        if sp.limit_order_mode == "layered":
                            depth_mult = max(0.20, min(1.0, depth_val / depth_req)) if depth_val > 0 else 0.20
                            print(
                                f"    Note: thin book for {side.upper()} ({depth_val:.0f} < {depth_req:.0f}); "
                                f"layered mode scales size by {depth_mult:.2f}"
                            )
                        else:
                            print(
                                f"    Why: insufficient book depth for {side.upper()} "
                                f"({depth_val:.0f} < {depth_req:.0f})"
                            )
                            print()
                            continue
                if not _is_midprice_stable(
                    ticker=ticker,
                    platform=PLATFORM_KALSHI,
                    cfg=cfg,
                    min_stability_seconds=dynamic_stability_seconds,
                    skip_in_preview=getattr(args, "preview", False),
                ):
                    print(
                        f"    Why: mid-price stability guard failed "
                        f"(<{dynamic_stability_seconds}s stable window)"
                    )
                    print()
                    continue
                held_same = inventory_by_key.get((PLATFORM_KALSHI, ticker, side), 0)
                held_opp = inventory_by_key.get((PLATFORM_KALSHI, ticker, "no" if side == "yes" else "yes"), 0)
                ask_side = book.get("yes_ask") if side == "yes" else book.get("no_ask")
                _fee_le = _fee_per_contract(bid, maker_fee_rate, sp.min_fee_per_order)
                _net_edge_le = _chosen_net_k
                price, dip_used = _compute_limit_price(
                    bid=bid,
                    ask=ask_side,
                    p_win=p_win,
                    maker_fee_rate=maker_fee_rate,
                    min_fee_per_order=sp.min_fee_per_order,
                    mode=sp.limit_order_mode,
                    base_dip=dip,
                    tick_offset=sp.tick_offset,
                    held_same_side=held_same,
                    held_opp_side=held_opp,
                    max_inventory_skew_pct=sp.max_inventory_skew_pct,
                    layered_order_levels=sp.layered_order_levels,
                    order_count=inventory_by_key.get((PLATFORM_KALSHI, ticker, side), 0) + 1,
                    max_dip_cap=sp.max_dip_cap,
                    passive_quote=_effective_passive_limit_quote(sp, dte),
                    net_edge=_net_edge_le,
                    edge_aggression_enabled=sp.limit_buy_edge_aggression_enabled,
                    cross_spread_min_edge=sp.limit_buy_cross_spread_min_edge,
                    near_market_min_edge=sp.limit_buy_near_market_min_edge,
                )
                _bb_k = 1.0
                if sp.barbell_enabled and not sp.simple_ev_decision_enabled and barbell_bucket == "base":
                    _bb_k = sp.barbell_base_kelly_mult
                elif sp.barbell_enabled and not sp.simple_ev_decision_enabled and barbell_bucket == "alpha":
                    _bb_k = sp.barbell_alpha_kelly_mult
                _k0 = kelly_fraction_live * _bb_k
                _kelly_frac_ticker = _k0
                if sp.ae_cfg.vol_kelly_enabled and use_kelly and closes and not sp.simple_ev_decision_enabled:
                    _kelly_frac_ticker = vol_adjusted_kelly(
                        _k0,
                        closes,
                        lookback=sp.ae_cfg.vol_kelly_lookback_bars,
                        baseline_vol=sp.ae_cfg.vol_kelly_baseline_vol,
                        max_shrink=sp.ae_cfg.vol_kelly_max_shrink,
                    )
                if sp.simple_ev_decision_enabled:
                    _ev_n = _chosen_net_k
                    _sf = simple_ev_spend_fraction(
                        _ev_n,
                        bid,
                        kelly_dampen=sp.simple_ev_kelly_dampen,
                        max_bankroll_fraction=sp.simple_ev_max_bankroll_fraction,
                    )
                    if _is_ultra_short_horizon(m, yf_ticker, sp):
                        _sf *= float(sp.simple_ev_ultra_short_size_mult)
                    _sf *= _simple_ev_calibration_size_mult(calibration, sp)
                    if vol_p is not None and abs(float(emp) - float(vol_p)) > float(
                        sp.simple_ev_emp_vol_disagree_threshold
                    ):
                        _sf *= float(sp.simple_ev_disagree_size_mult)
                    _spr_f = 0.0
                    if ask_side is not None and bid > 0 and float(ask_side) >= bid:
                        _spr_f = (float(ask_side) - bid) / bid
                    if _spr_f > float(sp.simple_ev_spread_wide_fraction):
                        _sf *= float(sp.simple_ev_spread_wide_size_mult)
                    _cost_c = max(bid * 100.0, 1.0)
                    count_base = int(balance_cents * _sf / _cost_c)
                    count_base = max(min_contracts, count_base) if count_base > 0 else 0
                    liq_mult, spread_bps = _liquidity_size_multiplier(bid, ask_side, sp)
                    liq_mult = 1.0
                    liq_mult *= depth_mult
                    liq_mult *= size_regime_mult
                else:
                    count_base = _compute_buy_count(
                        p_win, price, balance_cents, sp, use_kelly, _kelly_frac_ticker, min_contracts
                    )
                    liq_mult, spread_bps = _liquidity_size_multiplier(bid, ask_side, sp)
                    liq_mult *= depth_mult
                    liq_mult *= size_regime_mult
                count = int(max(min_contracts, round(count_base * liq_mult))) if count_base > 0 else 0
                _bdv_k: float | None = None
                if bid_size_side is not None:
                    try:
                        _bdv_k = float(bid_size_side)
                    except (TypeError, ValueError):
                        _bdv_k = None
                count = _cap_buy_count_by_book_pct(count, _bdv_k, sp.max_pct_of_book_volume)
                pending_orders.append({
                    "platform": PLATFORM_KALSHI,
                    "ticker": ticker,
                    "title": title,
                    "rec": rec,
                    "side": side,
                    "price": price,
                    "bid_at_place": bid,
                    "theoretical_price": bid,
                    "spread_bps_at_place": spread_bps,
                    "liquidity_mult": liq_mult,
                    "dip_pct_used": dip_used,
                    "edge": _chosen_net_k,
                    "count": count,
                    "hist_p": hist_p,
                    "vol_p": vol_p,
                    "yf_ticker": yf_ticker,
                    "threshold": threshold,
                    "direction": direction,
                    "limit_order_mode": sp.limit_order_mode,
                    "dte": dte,
                    "barbell": barbell_bucket,
                })
            elif sp.enable_disagreement_dip and rec == REC_SKIP:
                # Strong disagreement: market 95% vs we 45% → place low limit to catch dips
                gap = abs(hist_p - yes_bid)
                if gap >= sp.disagreement_dip_min_gap:
                    no_bid_dip = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                    if yes_bid - hist_p >= sp.disagreement_dip_min_gap:
                        rec_dip = REC_BUY_NO
                        bid_dip = no_bid_dip
                        side_dip = "no"
                        p_win_dip = 1.0 - hist_p
                    elif hist_p - yes_bid >= sp.disagreement_dip_min_gap:
                        rec_dip = REC_BUY_YES
                        bid_dip = yes_bid
                        side_dip = "yes"
                        p_win_dip = hist_p
                    else:
                        rec_dip = None
                        bid_dip = 0
                        p_win_dip = 0.0
                    if rec_dip is not None and bid_dip > 0 and p_win_dip >= sp.disagreement_dip_min_p_win:
                        price_dip = max(
                            sp.disagreement_dip_min_price,
                            min(sp.disagreement_dip_max_price, bid_dip * sp.disagreement_dip_pct_of_bid),
                        )
                        price_dip = round(price_dip, 2)
                        if price_dip >= 0.01 and _is_midprice_stable(
                            ticker=ticker, platform=PLATFORM_KALSHI, cfg=cfg, min_stability_seconds=dynamic_stability_seconds,
                            skip_in_preview=getattr(args, "preview", False),
                        ):
                            pending_orders.append({
                                "platform": PLATFORM_KALSHI,
                                "ticker": ticker,
                                "title": title,
                                "rec": rec_dip,
                                "side": side_dip,
                                "price": price_dip,
                                "bid_at_place": bid_dip,
                                "theoretical_price": bid_dip,
                                "spread_bps_at_place": 0,
                                "dip_pct_used": sp.disagreement_dip_pct_of_bid,
                                "edge": gap,
                                "count": sp.disagreement_dip_contracts,
                                "hist_p": hist_p,
                                "vol_p": vol_p,
                                "yf_ticker": yf_ticker,
                                "threshold": threshold,
                                "direction": direction,
                                "limit_order_mode": "fixed_pct",
                                "dte": dte,
                                "disagreement_dip": True,
                            })
                            print(f"       [Disagreement Dip] {side_dip.upper()} | gap={gap:.1%} | limit ${price_dip:.2f} (below ${bid_dip:.2f}) x {sp.disagreement_dip_contracts}")
                            added_dip = True
            # Lottery limits: micro bid on high-P(win) SKIPs — fixed 1¢ or penny_jump (bid+1¢) for queue priority
            _lot_band_ok = True
            if sp.lottery_market_yes_band_enabled:
                _lo, _hi = sp.lottery_market_yes_band_lo, sp.lottery_market_yes_band_hi
                if _hi > _lo:
                    _lot_band_ok = _lo <= float(yes_bid) <= _hi
            if not added_dip and rec == REC_SKIP and sp.enable_lottery_limits and _lot_band_ok:
                p_win_yes, p_win_no = hist_p, 1.0 - hist_p
                if p_win_yes >= sp.lottery_limit_min_p_win or p_win_no >= sp.lottery_limit_min_p_win:
                    side_lot = "yes" if p_win_yes >= p_win_no else "no"
                    p_win_lot = p_win_yes if side_lot == "yes" else p_win_no
                    if p_win_lot >= sp.lottery_limit_min_p_win and _is_midprice_stable(
                        ticker=ticker, platform=PLATFORM_KALSHI, cfg=cfg, min_stability_seconds=dynamic_stability_seconds,
                        skip_in_preview=getattr(args, "preview", False),
                    ):
                        bid_lot = yes_bid if side_lot == "yes" else (float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid))
                        _book_lot = _extract_book_fields(m, yes_bid, float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid))
                        _lot_px = _compute_lottery_limit_price(
                            sp=sp,
                            side=side_lot,
                            yes_bid=yes_bid,
                            no_bid=float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid),
                            yes_ask=_book_lot.get("yes_ask"),
                            no_ask=_book_lot.get("no_ask"),
                        )
                        pending_orders.append({
                            "platform": PLATFORM_KALSHI,
                            "ticker": ticker,
                            "title": title,
                            "rec": REC_BUY_YES if side_lot == "yes" else REC_BUY_NO,
                            "side": side_lot,
                            "price": _lot_px,
                            "bid_at_place": bid_lot,
                            "theoretical_price": bid_lot,
                            "spread_bps_at_place": 0,
                            "edge": p_win_lot,
                            "count": sp.lottery_limit_contracts,
                            "hist_p": hist_p,
                            "vol_p": vol_p,
                            "yf_ticker": yf_ticker,
                            "threshold": threshold,
                            "direction": direction,
                            "limit_order_mode": "fixed_pct",
                            "dte": dte,
                            "lottery_limit": True,
                        })
                        print(
                            f"       [Lottery Limit] {side_lot.upper()} | P(win)={p_win_lot:.1%} | "
                            f"limit ${_lot_px:.2f} ({sp.lottery_limit_price_mode}) x {sp.lottery_limit_contracts}"
                        )
                        added_dip = True
            _eval_count_total += 1
            if rec != REC_SKIP or added_dip:
                _eval_count_buy += 1
            else:
                _eval_count_skip += 1
            print()

        # Gemini crypto — expand to individual markets (binary: 1 per event; categorical: 1 per strike contract)
        if yf_ticker in GEMINI_CRYPTO_YF:
            gemini_evts = _gemini_events_list_for_yf_ticker(gemini_events_by_asset, yf_ticker)
            gemini_markets = expand_gemini_events_to_markets(gemini_evts)
            if args.markets > 0:
                gemini_markets = gemini_markets[: args.markets]
            use_vol = bool(getattr(args, "use_volatility", False) or sp.use_volatility_by_default)
            for mkt in gemini_markets:
                barbell_bucket_g = ""
                barbell_blocked_g = False
                joint_blend_pre_g: float | None = None
                joint_blend_post_g: float | None = None
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
                if sp.enable_strike_validation and not _validate_threshold_with_market(
                    threshold=threshold_g,
                    direction=direction_g,
                    current_price=current_price,
                    yes_bid=impl,
                    tolerance_pct=sp.strike_validation_tolerance_pct,
                ):
                    print(f"  [Gemini] {g_ticker}: SKIP (threshold validation failed) | {title}")
                    continue
                closes_for_emp_g = _get_closes_for_empirical(yf_ticker, args.days, closes, evt, sp, cfg)
                recent_w_g = _regime_adjusted_recent_weight(closes_for_emp_g, sp.empirical_recent_weight)
                emp = _empirical_probability(
                    closes_for_emp_g,
                    threshold_g,
                    direction_g,
                    recent_days=sp.empirical_recent_days,
                    recent_weight=recent_w_g,
                    use_rolling_window=sp.empirical_use_rolling_window,
                    weighting=sp.empirical_weighting,
                    exponential_halflife_bars=sp.empirical_exponential_halflife_bars,
                )
                dte_g = _get_days_to_expiry_from_market(evt, yf_ticker, sp.days_to_expiry)
                ewma_lambda_used_g = _ewma_lambda_for_market(sp, evt, yf_ticker)
                vol_p_gem = (
                    _volatility_probability_with_mc(
                        closes,
                        threshold_g,
                        direction_g,
                        dte_g,
                        use_ewma=sp.use_ewma_vol,
                        ewma_lambda=ewma_lambda_used_g,
                        current_price=current_price,
                        vol_model=sp.vol_model,
                        garch_min_returns=sp.garch_min_returns,
                        enable_otm_smile_adjustment=sp.enable_otm_smile_adjustment,
                        otm_smile_strength=sp.otm_smile_strength,
                        otm_smile_max_mult=sp.otm_smile_max_mult,
                        distribution=sp.vol_distribution,
                        t_df=sp.vol_t_df,
                        mc_paths=sp.mc_paths if sp.mc_enabled else 0,
                        mc_blend_weight=sp.mc_blend_weight,
                    )
                    if use_vol else None
                )
                if vol_p_gem is not None and use_vol:
                    _is_intra_g = _is_intraday_or_ultra_short(evt, yf_ticker, sp)
                    if _is_intra_g:
                        vol_p_gem = _cap_intraday_vol_p(vol_p_gem, sp.intraday_vol_cap)
                    if _is_intra_g and not sp.use_intraday_empirical:
                        hist_p_gem = vol_p_gem
                    elif sp.vol_blend_weight is not None:
                        w_emp_g = _dynamic_empirical_weight(sp.vol_blend_weight, len(closes_for_emp_g), sp.vol_dynamic_n0)
                        w_emp_g = _dte_adjusted_empirical_weight(w_emp_g, dte_g)
                        if _is_intra_g:
                            w_emp_g = max(w_emp_g, sp.intraday_min_empirical_weight)
                        joint_blend_pre_g, joint_blend_post_g = _vol_emp_blend_hist_p_with_joint_trace(
                            emp, vol_p_gem, w_emp_g, sp
                        )
                        hist_p_gem = joint_blend_post_g
                    else:
                        hist_p_gem = max(emp, vol_p_gem)
                else:
                    hist_p_gem = emp
                if sp.enable_model_prob_hard_cap:
                    hist_p_gem = max(sp.model_prob_hard_lo, min(sp.model_prob_hard_hi, float(hist_p_gem)))
                hist_p_gem = strategy_engine.apply_bucket_penalty(
                    hist_p=hist_p_gem,
                    calibration=calibration,
                    use_bucket_penalty=sp.use_bucket_penalty and not sp.simple_ev_decision_enabled,
                    use_calibration=sp.use_calibration,
                    min_bucket_samples=sp.min_bucket_samples,
                )
                if probability_calibrator:
                    hist_p_gem = probability_calibrator(hist_p_gem)
                if not sp.simple_ev_decision_enabled and sp.model_probability_shrinkage < 1.0:
                    hist_p_gem = 0.5 + sp.model_probability_shrinkage * (hist_p_gem - 0.5)
                yes_ask_sig_g = _gemini_yes_ask(contract if contract else (_get_yes_contract(evt) or {}))
                spread_bps_sig_g = None
                if yes_ask_sig_g and impl > 0 and yes_ask_sig_g >= impl:
                    spread_bps_sig_g = ((yes_ask_sig_g - impl) / max(0.01, impl)) * 10_000.0
                if sp.enable_signal_model:
                    rsi_for_signal_g = _compute_rsi(closes)
                    feats_g = strategy_engine.build_signal_features(
                        emp_p=emp,
                        vol_p=vol_p_gem,
                        market_yes=impl,
                        threshold=threshold_g,
                        current_price=current_price,
                        dte_days=dte_g,
                        spread_bps=spread_bps_sig_g,
                        no_bid=(1.0 - impl),
                        macro_24h_ret=macro_signal_24h,
                        rsi_14=rsi_for_signal_g,
                    )
                    hist_p_gem = strategy_engine.score_signal_probability(
                        features=feats_g,
                        signal_model_weights=sp.signal_model_weights,
                    )
                # --- Anti-hallucination pipeline (Gemini) ---
                hist_p_gem_raw = hist_p_gem
                if not sp.simple_ev_decision_enabled:
                    hist_p_gem = apply_anti_hallucination_pipeline(
                        model_p=hist_p_gem,
                        market_p=impl,
                        closes=closes,
                        threshold=threshold_g,
                        direction=direction_g,
                        current_price=current_price,
                        momentum_ret=momentum_ret,
                        cfg=sp.ah_cfg,
                    )
                hist_p_gem = _sanitize_model_probability(
                    hist_p_gem,
                    lo=sp.model_prob_hard_lo,
                    hi=sp.model_prob_hard_hi,
                    enabled=sp.enable_model_prob_hard_cap,
                )
                maker_fee_rate_gem = _maker_fee_rate_for_market(sp, PLATFORM_GEMINI, yf_ticker)
                no_bid_gem_early = _gemini_no_bid_from_event(evt, impl)
                effective_min_core_trace_g: float | None = None
                _us_edge_detail_g: dict[str, Any] | None = None
                _eff_after_simple_bundle_g: float | None = None
                _proxy_edge_simple_g = 0.0
                if sp.simple_ev_decision_enabled:
                    _eff_after_simple_bundle_g, _us_edge_detail_g = _simple_ev_net_min_edge_bundle(
                        sp, evt, yf_ticker
                    )
                    _proxy_edge_simple_g = float(proxy_basis_extra_min_edge(cfg, series))
                    effective_min_gem = float(_eff_after_simple_bundle_g) + _proxy_edge_simple_g
                    if _proxy_edge_simple_g > 0.0 and _us_edge_detail_g is not None:
                        _us_edge_detail_g = {
                            **_us_edge_detail_g,
                            "proxy_basis_extra_min_edge": _proxy_edge_simple_g,
                        }
                    effective_min_yes_g = effective_min_gem
                    effective_min_no_g = effective_min_gem
                    obi_adj_yes_g = obi_adj_no_g = 0.0
                    obi_val_g = None
                    _print_effective_edge_trace(
                        sp,
                        min_edge,
                        simple_mode=True,
                        simple_ev_effective=effective_min_gem,
                        ultra_short_detail=_us_edge_detail_g,
                    )
                else:
                    effective_min_gem = _get_effective_min_edge(min_edge, direction_g, calibration, sp)
                    em0_trace_g = effective_min_gem
                    emp_n_g = min(sp.empirical_recent_days, len(closes_for_emp_g)) if sp.empirical_use_rolling_window else len(closes_for_emp_g)
                    effective_min_gem += sp.uncertainty_edge_mult * _empirical_ci_halfwidth(emp, emp_n_g)
                    effective_min_gem *= edge_regime_mult
                    if vol_spike_active:
                        effective_min_gem *= max(1.0, sp.vol_spike_edge_multiplier)
                    if vol_p_gem is not None and abs(emp - vol_p_gem) > sp.vol_disagree_threshold:
                        effective_min_gem += sp.vol_disagree_extra_edge
                    # --- Adaptive edge (Gemini) ---
                    if sp.ah_cfg.adaptive_edge_enabled:
                        effective_min_gem = adaptive_min_edge(
                            effective_min_gem,
                            impl,
                            current_price=current_price,
                            threshold=threshold_g,
                            direction=direction_g,
                            momentum_ret=momentum_ret,
                            cheap_threshold=sp.ah_cfg.adaptive_edge_cheap_threshold,
                            expensive_threshold=sp.ah_cfg.adaptive_edge_expensive_threshold,
                            otm_base_mult=sp.ah_cfg.adaptive_edge_otm_base_mult,
                            trend_opposed_mult=sp.ah_cfg.adaptive_edge_trend_opposed_mult,
                            strike_dist_mult=sp.ah_cfg.adaptive_edge_strike_dist_mult,
                        )
                    # --- Alpha engine: toxic flow penalty (Gemini) ---
                    if sp.ae_cfg.markout_enabled and get_markout_history:
                        _markout_hist_g = get_markout_history(PLATFORM_GEMINI, evt.get("ticker", "?"), window_minutes=5, config=cfg)
                        _toxic_penalty_g = toxic_flow_edge_penalty(
                            _markout_hist_g,
                            threshold_bps=sp.ae_cfg.markout_toxic_threshold_bps,
                            min_samples=sp.ae_cfg.markout_min_samples,
                            penalty=sp.ae_cfg.markout_edge_penalty,
                        )
                        effective_min_gem += _toxic_penalty_g
                    # --- Alpha engine: OBI edge adjustment (Gemini) ---
                    obi_val_g = None
                    obi_adj_yes_g = obi_adj_no_g = 0.0
                    if sp.ae_cfg.obi_enabled:
                        _market_for_obi_g = contract if isinstance(contract, dict) else evt
                        _book_obi_g = _extract_book_fields(_market_for_obi_g or {}, impl, 1.0 - impl)
                        obi_val_g = compute_obi(
                            _book_obi_g.get("yes_bid_size"), _book_obi_g.get("yes_ask_size"),
                            min_total_size=sp.ae_cfg.obi_min_total_size,
                        )
                        if obi_val_g is not None:
                            obi_adj_yes_g = obi_edge_adjustment(
                                obi_val_g, "BUY YES",
                                strong_threshold=sp.ae_cfg.obi_strong_threshold,
                                max_bonus=sp.ae_cfg.obi_edge_bonus_max,
                            )
                            obi_adj_no_g = obi_edge_adjustment(
                                obi_val_g, "BUY NO",
                                strong_threshold=sp.ae_cfg.obi_strong_threshold,
                                max_bonus=sp.ae_cfg.obi_edge_bonus_max,
                            )
                    # --- Adaptive learning: bump edge from recent performance & loss streaks (Gemini) ---
                    if sp.adaptive_learning_enabled and adaptive_edge_bump > 0:
                        effective_min_gem += adaptive_edge_bump
                    if sp.adaptive_learning_enabled and series_performance:
                        _series_prefix_g = (evt.get("ticker") or "").split("-")[0] if evt else ""
                        _sp_info_g = series_performance.get(_series_prefix_g)
                        if _sp_info_g and _sp_info_g.get("consecutive_losses", 0) >= sp.series_cooldown_threshold:
                            effective_min_gem += sp.series_cooldown_extra_edge
                    if sp.dte_edge_scaling_enabled and dte_g is not None:
                        effective_min_gem *= _dte_edge_scale_factor(float(dte_g), sp)
                    if _is_ultra_short_horizon(evt, yf_ticker, sp) and sp.ultra_short_min_edge_mult > 1.0:
                        effective_min_gem *= float(sp.ultra_short_min_edge_mult)
                    effective_min_core_trace_g = float(effective_min_gem)
                    effective_min_yes_g = effective_min_gem + yes_parity_penalty + obi_adj_yes_g + sp.yes_overconfidence_extra_edge
                    effective_min_no_g = effective_min_gem + no_parity_penalty + obi_adj_no_g
                    _print_effective_edge_trace(
                        sp,
                        min_edge,
                        simple_mode=False,
                        em0=em0_trace_g,
                        effective_min_core=effective_min_core_trace_g,
                        effective_min_yes=effective_min_yes_g,
                        effective_min_no=effective_min_no_g,
                    )
                # For non-standard intraday: use relaxed threshold when volatility model available
                min_p_win_gem = sp.intraday_other_min_p_win_with_vol if (use_vol and vol_p_gem is not None) else sp.intraday_other_min_p_win
                if _is_ultra_short_horizon(evt, yf_ticker, sp):
                    _ug = (
                        sp.ultra_short_min_p_win_with_vol
                        if (use_vol and vol_p_gem is not None)
                        else sp.ultra_short_min_p_win
                    )
                    min_p_win_gem = max(min_p_win_gem, _ug)
                cross_sanity_block_side_g = None
                cross_sanity_reason_g = ""
                if sp.enable_cross_exchange_sanity_gate:
                    try:
                        kalshi_ref = next(
                            (
                                mk
                                for mk in mkt_list
                                if _extract_strike(mk)[0] == threshold_g and _extract_strike(mk)[1] == direction_g
                            ),
                            None,
                        )
                        if kalshi_ref:
                            kalshi_ref_yes = float(kalshi_ref.get("yes_bid_dollars") or 0) or float(
                                kalshi_ref.get("last_price_dollars") or kalshi_ref.get("yes_ask_dollars") or 0
                            )
                            cross_sanity_block_side_g = _cross_exchange_sanity_block_side(
                                sp,
                                momentum_ret=momentum_ret,
                                local_yes=impl,
                                reference_yes=kalshi_ref_yes,
                            )
                            if cross_sanity_block_side_g:
                                cross_sanity_reason_g = (
                                    "cross-exchange sanity gate: "
                                    f"Gemini YES {impl:.1%} vs Kalshi YES {kalshi_ref_yes:.1%} during "
                                    f"{momentum_ret:+.2%} spot move"
                                )
                    except Exception:
                        pass
                if sp.simple_ev_decision_enabled:
                    rec = _simple_ev_recommend(
                        hist_p_gem,
                        impl,
                        no_bid_gem_early,
                        effective_min_gem,
                        maker_fee_rate_gem,
                        sp.min_fee_per_order,
                        sp,
                    )
                else:
                    rec = (
                        strategy_engine.recommend_strict(
                            p_yes=hist_p_gem,
                            market_yes=impl,
                            min_p_win=min_p_win_gem,
                            min_edge=effective_min_gem,
                            maker_fee_rate=maker_fee_rate_gem,
                            min_fee_per_order=sp.min_fee_per_order,
                            min_edge_yes=effective_min_yes_g,
                            min_edge_no=effective_min_no_g,
                            **_strategy_recommend_strict_extras(sp),
                        )
                        if _strict_intraday_evaluation(evt, yf_ticker, sp)
                        else strategy_engine.recommend(
                            p_yes=hist_p_gem,
                            market_yes=impl,
                            min_edge=effective_min_gem,
                            maker_fee_rate=maker_fee_rate_gem,
                            min_fee_per_order=sp.min_fee_per_order,
                            min_edge_yes=effective_min_yes_g,
                            min_edge_no=effective_min_no_g,
                            min_edge_easy_win=sp.min_edge_easy_win if sp.enable_easy_win else None,
                            easy_win_min_p_win=sp.easy_win_min_p_win,
                            easy_win_min_market_p=sp.easy_win_min_market_p,
                            **_strategy_recommend_extras(sp),
                        )
                    )
                if rec in (REC_BUY_YES, REC_BUY_NO) and sp.max_new_buy_dte_days > 0 and dte_g is not None and float(dte_g) > sp.max_new_buy_dte_days:
                    rec = REC_SKIP
                violates_rule_g = _violates_no_above_rule(current_price, threshold_g, direction_g, sp.no_above_cushion)
                if rec == REC_BUY_NO and sp.enforce_no_above_rule and violates_rule_g:
                    rec = REC_SKIP
                # Favor BUY YES when price is well above strike — only if we have edge (after fees) and calibration allows it
                if rec == REC_SKIP and violates_rule_g:
                    _favor_fee_g = _fee_per_contract(impl, maker_fee_rate_gem, sp.min_fee_per_order)
                    if 0 < impl < (1 - effective_min_gem) and hist_p_gem >= impl + effective_min_gem + _favor_fee_g:
                        should_favor = sp.favor_yes_when_above_strike
                        if should_favor and sp.use_calibration and calibration:
                            above_yes = calibration.get("by_direction_side", {}).get("above+yes", {})
                            cnt = above_yes.get("count", 0)
                            wr = above_yes.get("win_rate", 1.0)
                            if cnt >= sp.min_calibration_samples and wr < sp.calibration_min_win_rate_above_yes:
                                should_favor = False  # Historical "above+YES" underperforming
                        if should_favor:
                            rec = REC_BUY_YES
                # Skip BUY YES when market prices it very low (e.g. 1¢): market thinks <5% chance
                if rec == REC_BUY_YES and impl < sp.min_yes_price:
                    rec = REC_SKIP
                if rec == REC_BUY_YES and sp.max_yes_price_for_buy > 0 and impl > sp.max_yes_price_for_buy:
                    if sp.simple_ev_decision_enabled or not sp.barbell_enabled:
                        rec = REC_SKIP
                    elif sp.barbell_enabled:
                        if _barbell_yes_bucket(hist_p_gem, impl, maker_fee_rate_gem, sp) != "base":
                            rec = REC_SKIP
                # Skip BUY YES when strike is far above current (far OTM) unless vol model shows min_vol_confidence_far_otm
                # DTE-aware: tighten allowed gap for short-dated contracts
                # When DTE < 0.1 days (~2.4h), vol override is blocked
                _eff_max_above_g = _dte_adjusted_strike_pct(sp.max_buy_yes_strike_above_pct, dte_g)
                _dte_too_short_g = dte_g is not None and dte_g < 0.1
                if rec == REC_BUY_YES and direction_g == "above" and _eff_max_above_g > 0:
                    if threshold_g > current_price * (1 + _eff_max_above_g):
                        vol_override_g = use_vol and vol_p_gem is not None and vol_p_gem >= sp.min_vol_confidence_far_otm and not _dte_too_short_g
                        if not vol_override_g:
                            rec = REC_SKIP
                # Skip BUY NO when market prices NO very low; override when vol model shows P(NO) ≥ 95%
                no_price_gem = no_bid_gem_early
                if rec == REC_BUY_NO and no_price_gem < sp.min_no_price:
                    if not (use_vol and vol_p_gem is not None and (1 - hist_p_gem) >= 0.95):
                        rec = REC_SKIP
                # Skip BUY NO on "below X" when strike is far above current (BUY NO = "price > X" needs big rally)
                _eff_max_no_above_g = _dte_adjusted_strike_pct(sp.max_buy_no_strike_above_pct, dte_g)
                if rec == REC_BUY_NO and direction_g == "below" and _eff_max_no_above_g > 0:
                    if threshold_g > current_price * (1 + _eff_max_no_above_g):
                        vol_override_no_g = use_vol and vol_p_gem is not None and (1 - hist_p_gem) >= 0.95 and not _dte_too_short_g
                        if not vol_override_no_g:
                            rec = REC_SKIP
                if (
                    not sp.simple_ev_decision_enabled
                    and rec == REC_BUY_YES
                    and _should_downgrade_buy_yes_above(
                        rec, direction_g, hist_p_gem, impl, min_edge, calibration, sp, maker_fee_rate_gem
                    )
                ):
                    rec = REC_SKIP
                if rec == REC_BUY_YES and momentum_crash:
                    rec = REC_SKIP
                # --- Regime filter (Gemini): block BUY YES when EMA trend opposes direction ---
                if (
                    not sp.simple_ev_decision_enabled
                    and sp.ah_cfg.regime_filter_enabled
                    and regime_filter(
                        closes, current_price, direction_g, rec, ema_period=sp.ah_cfg.regime_ema_period,
                    )
                ):
                    rec = REC_SKIP
                if rec in (REC_BUY_YES, REC_BUY_NO) and defensive_mode_active:
                    rec = REC_SKIP
                if rec in (REC_BUY_YES, REC_BUY_NO):
                    if rec == REC_BUY_YES and cross_sanity_block_side_g == "yes":
                        rec = REC_SKIP
                    elif rec == REC_BUY_NO and cross_sanity_block_side_g == "no":
                        rec = REC_SKIP
                if sp.barbell_enabled and not sp.simple_ev_decision_enabled and rec in (REC_BUY_YES, REC_BUY_NO):
                    _bbg = (
                        _barbell_yes_bucket(hist_p_gem, impl, maker_fee_rate_gem, sp)
                        if rec == REC_BUY_YES
                        else _barbell_no_bucket(hist_p_gem, no_price_gem, maker_fee_rate_gem, sp)
                    )
                    if _bbg is None:
                        rec = REC_SKIP
                        barbell_blocked_g = True
                    else:
                        barbell_bucket_g = _bbg
                elif sp.simple_ev_decision_enabled and rec in (REC_BUY_YES, REC_BUY_NO):
                    barbell_bucket_g = "simple_ev"
                diff_gem = _compute_edge_diff(
                    impl,
                    hist_p_gem,
                    rec,
                    violates_rule_g,
                    maker_fee_rate_gem,
                    min_fee_per_order=sp.min_fee_per_order,
                    no_bid=no_bid_gem_early,
                )
                no_price_gem_q = no_bid_gem_early
                fee_yes_gq = _fee_per_contract(impl, maker_fee_rate_gem, sp.min_fee_per_order)
                fee_no_gq = _fee_per_contract(no_price_gem_q, maker_fee_rate_gem, sp.min_fee_per_order)
                market_for_book_q = contract if isinstance(contract, dict) else evt
                book_gq = _extract_book_fields(market_for_book_q or {}, impl, no_price_gem_q)
                quote_candidates.append(
                    {
                        "platform": PLATFORM_GEMINI,
                        "ticker": evt.get("ticker", "?"),
                        "title": title,
                        "yf_ticker": yf_ticker,
                        "resolution_key": _extract_resolution_key_from_gemini_ticker(evt.get("ticker", "")) or "",
                        "direction": direction_g,
                        "threshold": threshold_g,
                        "hist_p": hist_p_gem,
                        "vol_p": vol_p_gem,
                        "yes_bid": impl,
                        "no_bid": no_price_gem_q,
                        "yes_ask": book_gq.get("yes_ask"),
                        "no_ask": book_gq.get("no_ask"),
                        "edge_yes_net": hist_p_gem - impl - fee_yes_gq,
                        "edge_no_net": (1.0 - hist_p_gem) - no_price_gem_q - fee_no_gq,
                        "arr_dip": min(sp.max_dip_cap, _adaptive_limit_dip(sp.limit_dip_pct, closes_for_emp_g, cap=sp.max_dip_cap) * dip_regime_mult),
                        "dte": dte_g,
                        "gemini_evt": evt,
                        "instrument_symbol": instrument_symbol,
                        "barbell": barbell_bucket_g,
                    }
                )
                _dist_str_g = _fmt_distance(current_price, threshold_g, direction_g)
                _dte_str_g = _fmt_dte(dte_g)
                _rec_plain_g = _fmt_rec_plain(rec, "yes" if rec == REC_BUY_YES else "no", direction_g, threshold_g, current_price, dte_g)
                print(f"\n    {'·' * 60}")
                print(f"    {evt.get('ticker','?')}  [Gemini]")
                print(f"    {title[:70]}")
                print(f"    ├─ Strike:       {_fmt_price(threshold_g)} (pays if {direction_g})")
                print(f"    ├─ Current:      ${current_price:,.2f}  →  {_dist_str_g}")
                print(f"    ├─ Resolves in:  {_dte_str_g}")
                if vol_p_gem is not None:
                    print(f"    ├─ Empirical P:  {emp:.1%}  (historical frequency)")
                    _vp_g = _display_model_probability(vol_p_gem, sp)
                    _vp_g_note = "" if _vp_g is None or abs(float(_vp_g) - float(vol_p_gem)) < 1e-9 else " (display capped)"
                    print(f"    ├─ Volatility P: {_vp_g:.1%}{_vp_g_note}  (model based on current price + DTE)")
                    print(f"    ├─ Blended P:    {hist_p_gem:.1%}  (final model probability)")
                    _print_barbell_joint_blend_diagnostic(
                        sp,
                        emp=emp,
                        vol_p=float(vol_p_gem),
                        yes_bid=impl,
                        no_bid=no_bid_gem_early,
                        maker_fee_rate=maker_fee_rate_gem,
                        joint_pre=joint_blend_pre_g,
                        joint_post=joint_blend_post_g,
                        final_p_yes=hist_p_gem,
                    )
                else:
                    print(f"    ├─ Model P(YES): {hist_p_gem:.1%}")
                print(f"    ├─ Market YES:   {impl:.1%}  (what the market is pricing)")
                if _is_ultra_short_horizon(evt, yf_ticker, sp):
                    _usm_g = float(sp.ultra_short_min_edge_mult)
                    _us_note_g = " (no extra ×)" if _usm_g <= 1.0 else ""
                    print(
                        f"    ├─ Ultra-short:  min_edge ×{_usm_g:.2f} "
                        f"(ultra_short_min_edge_mult){_us_note_g}"
                    )
                print(f"    ├─ Verdict:      {rec}")
                if sp.log_effective_min_edge:
                    _eys_g, _ens_g = _net_edges_buy_legs(
                        hist_p_gem, impl, no_bid_gem_early, maker_fee_rate_gem, sp.min_fee_per_order
                    )
                    print(
                        f"    ├─ Leg edges:     YES net {_eys_g:+.1%}  |  NO net {_ens_g:+.1%}  "
                        f"(bids {impl:.1%} / {no_bid_gem_early:.1%})"
                    )
                if sp.log_decision_diagnostics:
                    _edyg, _edng = _net_edges_buy_legs(
                        hist_p_gem, impl, no_bid_gem_early, maker_fee_rate_gem, sp.min_fee_per_order
                    )
                    print_decision_diagnostics_line(
                        {
                            "ticker": evt.get("ticker", "?"),
                            "platform": "gemini",
                            "strategy_engine": str(strategy.get("strategy_engine", "vol_regime")),
                            "simple_ev": bool(sp.simple_ev_decision_enabled),
                            "p": {
                                "emp": float(emp),
                                "vol": float(vol_p_gem) if vol_p_gem is not None else None,
                                "joint_pre": float(joint_blend_pre_g) if joint_blend_pre_g is not None else None,
                                "joint_post": float(joint_blend_post_g) if joint_blend_post_g is not None else None,
                                "hist_p_raw": float(hist_p_gem_raw),
                                "hist_p": float(hist_p_gem),
                            },
                            "market": {"yes_bid": float(impl), "no_bid": float(no_bid_gem_early)},
                            "net_edge": {"yes": float(_edyg), "no": float(_edng)},
                            "required": build_required_edge_breakdown(
                                simple_ev=bool(sp.simple_ev_decision_enabled),
                                effective_min_final=float(effective_min_gem),
                                effective_min_yes=float(effective_min_yes_g),
                                effective_min_no=float(effective_min_no_g),
                                min_p_win=float(min_p_win_gem),
                                us_detail=_us_edge_detail_g,
                                eff_after_simple_ev_bundle=_eff_after_simple_bundle_g,
                                proxy_basis_add=float(_proxy_edge_simple_g),
                                legacy_core_before_parity_obi=effective_min_core_trace_g,
                            ),
                            "regime": {"vol_ratio": float(vol_ratio), "edge_mult": float(edge_regime_mult)},
                            "verdict": rec,
                        }
                    )
                if sp.barbell_enabled:
                    if sp.simple_ev_decision_enabled:
                        print("    ├─ Barbell:      gating off (simple EV mode)")
                    else:
                        print(
                            f"    ├─ Barbell:      "
                            f"{_barbell_status_for_log(barbell_bucket_g, barbell_blocked_g, rec)}"
                        )
                print(f"    │    → {_rec_plain_g}")
                _print_multi_strategy_compare_line(
                    compare_strategies_arg=getattr(args, "compare_strategies", "") or "",
                    hist_p=hist_p_gem,
                    market_yes=impl,
                    min_p_win=min_p_win_gem,
                    effective_min=effective_min_gem,
                    maker_fee_rate=maker_fee_rate_gem,
                    min_fee_per_order=sp.min_fee_per_order,
                    effective_min_yes=effective_min_yes_g,
                    effective_min_no=effective_min_no_g,
                    is_strict_intraday=bool(
                        _strict_intraday_evaluation(evt, yf_ticker, sp) and not sp.simple_ev_decision_enabled
                    ),
                    sp=sp,
                )
                _reason_g = ""
                if rec == REC_SKIP and defensive_mode_active:
                    _reason_g = (
                        f"defensive mode (sells-only) due to extreme volatility "
                        f"(ratio {vol_ratio:.2f}x)"
                    )
                elif rec == REC_SKIP and cross_sanity_reason_g:
                    _reason_g = cross_sanity_reason_g
                elif rec == REC_SKIP and momentum_crash:
                    _reason_g = (
                        f"momentum protection "
                        f"({momentum_ret:.2%} <= {sp.momentum_crash_threshold_pct:.2%})"
                    )
                elif rec == REC_SKIP and barbell_blocked_g:
                    _reason_g = (
                        "barbell: directional trade not in base (high-P consensus) or alpha "
                        "(cheap leg + large net edge) bucket"
                    )
                elif rec == REC_SKIP:
                    _fq_r = no_bid_gem_early
                    _fy_r = _fee_per_contract(impl, maker_fee_rate_gem, sp.min_fee_per_order)
                    _fn_r = _fee_per_contract(_fq_r, maker_fee_rate_gem, sp.min_fee_per_order)
                    _reason_g = _format_reason(
                        rec,
                        hist_p_gem,
                        impl,
                        threshold_g,
                        current_price,
                        direction_g,
                        violates_rule_g,
                        vol_p_gem,
                        sp.min_yes_price,
                        sp.min_no_price,
                        sp.max_buy_yes_strike_above_pct,
                        _fq_r,
                        effective_min_gem,
                        use_vol,
                        net_edge_yes=hist_p_gem - impl - _fy_r,
                        net_edge_no=(1.0 - hist_p_gem) - _fq_r - _fn_r,
                        req_yes=effective_min_yes_g,
                        req_no=effective_min_no_g,
                    )
                if _reason_g:
                    print(f"    └─ Reason:       {_reason_g}")
                try:
                    from storage import record_market_snapshot
                    market_for_book = contract if isinstance(contract, dict) else evt
                    book = _extract_book_fields(market_for_book or {}, impl, no_bid_gem_early)
                    record_market_snapshot(
                        PLATFORM_GEMINI,
                        evt.get("ticker", "?"),
                        impl,
                        no_bid_gem_early,
                        hist_p_gem,
                        rec,
                        threshold_g,
                        direction_g,
                        vol_p_gem,
                        abs(diff_gem),
                        title,
                        yf_ticker,
                        yes_ask=book.get("yes_ask"),
                        no_ask=book.get("no_ask"),
                        yes_bid_size=book.get("yes_bid_size"),
                        no_bid_size=book.get("no_bid_size"),
                        yes_ask_size=book.get("yes_ask_size"),
                        no_ask_size=book.get("no_ask_size"),
                        config=cfg,
                    )
                except ImportError:
                    pass
                added_dip = False
                if rec != REC_SKIP:
                    bid = impl if rec == REC_BUY_YES else no_bid_gem_early
                    side = "yes" if rec == REC_BUY_YES else "no"
                    _gt = evt.get("ticker", "?")
                    if sp.skip_new_buy_when_unresolved_same_side and inventory_by_key.get(
                        (PLATFORM_GEMINI, _gt, side), 0
                    ) > 0:
                        print(
                            "       Note: skip new BUY — same-side position already tracked in unresolved predictions "
                            "(skip_new_buy_when_unresolved_same_side)"
                        )
                        print()
                        continue
                    p_win = hist_p_gem if rec == REC_BUY_YES else (1 - hist_p_gem)
                    _ey_go, _en_go = _net_edges_buy_legs(
                        hist_p_gem, impl, no_bid_gem_early, maker_fee_rate_gem, sp.min_fee_per_order
                    )
                    _chosen_net_g = _ey_go if rec == REC_BUY_YES else _en_go
                    print(
                        f"       Edge: {_chosen_net_g:.1%}  |  P(win): {p_win:.1%}  |  "
                        f"Side: {side.upper()}  |  Cost/contract: ${bid:.2f}"
                    )
                    dip = _adaptive_limit_dip(sp.limit_dip_pct, closes_for_emp_g, cap=sp.max_dip_cap)
                    dip = min(sp.max_dip_cap, dip * dip_regime_mult)
                    price_might_decline = (use_vol and vol_p_gem is not None) or (dte_g is not None and dte_g >= 2)
                    if price_might_decline and sp.limit_dip_pct_price_decline > 0:
                        dip = max(dip, sp.limit_dip_pct_price_decline)
                    if p_win >= 0.90 and sp.limit_dip_pct_high_confidence > 0:
                        dip = max(dip, sp.limit_dip_pct_high_confidence)
                    dip = _dte_confidence_dip(dip, p_win, dte_g, sp)
                    rsi_val_g = _compute_rsi(closes)
                    dip = _rsi_dip_adjustment(dip, rsi_val_g, sp.max_dip_cap)
                    market_for_book = contract if isinstance(contract, dict) else evt
                    book = _extract_book_fields(market_for_book or {}, impl, no_bid_gem_early)
                    bid_size_side = book.get("yes_bid_size") if side == "yes" else book.get("no_bid_size")
                    if sp.simple_ev_decision_enabled and sp.simple_ev_min_bid_size > 0:
                        _dv_ev_g = float(bid_size_side) if bid_size_side is not None else 0.0
                        if _dv_ev_g < float(sp.simple_ev_min_bid_size):
                            print(
                                f"    Why: simple_ev min bid size {sp.simple_ev_min_bid_size:.0f} "
                                f"not met ({_dv_ev_g:.0f} on {side.upper()})"
                            )
                            print()
                            continue
                    depth_mult = 1.0
                    if sp.min_order_book_depth > 0:
                        depth_val = float(bid_size_side) if bid_size_side is not None else 0.0
                        depth_req = float(sp.min_order_book_depth) * min(2.5, max(1.0, vol_ratio))
                        if depth_val < depth_req:
                            if sp.limit_order_mode == "layered":
                                depth_mult = max(0.20, min(1.0, depth_val / depth_req)) if depth_val > 0 else 0.20
                                print(
                                    f"    Note: thin book for {side.upper()} ({depth_val:.0f} < {depth_req:.0f}); "
                                    f"layered mode scales size by {depth_mult:.2f}"
                                )
                            else:
                                print(
                                    f"    Why: insufficient book depth for {side.upper()} "
                                    f"({depth_val:.0f} < {depth_req:.0f})"
                                )
                                print()
                                continue
                    if not _is_midprice_stable(
                        ticker=evt.get("ticker", "?"),
                        platform=PLATFORM_GEMINI,
                        cfg=cfg,
                        min_stability_seconds=dynamic_stability_seconds,
                        skip_in_preview=getattr(args, "preview", False),
                    ):
                        print(
                            f"    Why: mid-price stability guard failed "
                            f"(<{dynamic_stability_seconds}s stable window)"
                        )
                        print()
                        continue
                    held_same = inventory_by_key.get((PLATFORM_GEMINI, evt.get("ticker", "?"), side), 0)
                    held_opp = inventory_by_key.get(
                        (PLATFORM_GEMINI, evt.get("ticker", "?"), "no" if side == "yes" else "yes"), 0
                    )
                    ask_side = book.get("yes_ask") if side == "yes" else book.get("no_ask")
                    _fee_leg = _fee_per_contract(bid, maker_fee_rate_gem, sp.min_fee_per_order)
                    _net_edge_leg = _chosen_net_g
                    price, dip_used = _compute_limit_price(
                        bid=bid,
                        ask=ask_side,
                        p_win=p_win,
                        maker_fee_rate=maker_fee_rate_gem,
                        min_fee_per_order=sp.min_fee_per_order,
                        mode=sp.limit_order_mode,
                        base_dip=dip,
                        tick_offset=sp.tick_offset,
                        held_same_side=held_same,
                        held_opp_side=held_opp,
                        max_inventory_skew_pct=sp.max_inventory_skew_pct,
                        layered_order_levels=sp.layered_order_levels,
                        order_count=inventory_by_key.get((PLATFORM_GEMINI, evt.get("ticker", "?"), side), 0) + 1,
                        max_dip_cap=sp.max_dip_cap,
                        passive_quote=_effective_passive_limit_quote(sp, dte_g),
                        net_edge=_net_edge_leg,
                        edge_aggression_enabled=sp.limit_buy_edge_aggression_enabled,
                        cross_spread_min_edge=sp.limit_buy_cross_spread_min_edge,
                        near_market_min_edge=sp.limit_buy_near_market_min_edge,
                    )
                    _bb_kg = 1.0
                    if sp.barbell_enabled and not sp.simple_ev_decision_enabled and barbell_bucket_g == "base":
                        _bb_kg = sp.barbell_base_kelly_mult
                    elif sp.barbell_enabled and not sp.simple_ev_decision_enabled and barbell_bucket_g == "alpha":
                        _bb_kg = sp.barbell_alpha_kelly_mult
                    _k0g = kelly_fraction_live * _bb_kg
                    _kelly_frac_gem = _k0g
                    if sp.ae_cfg.vol_kelly_enabled and use_kelly and closes and not sp.simple_ev_decision_enabled:
                        _kelly_frac_gem = vol_adjusted_kelly(
                            _k0g,
                            closes,
                            lookback=sp.ae_cfg.vol_kelly_lookback_bars,
                            baseline_vol=sp.ae_cfg.vol_kelly_baseline_vol,
                            max_shrink=sp.ae_cfg.vol_kelly_max_shrink,
                        )
                    if sp.simple_ev_decision_enabled:
                        _ev_ng = _chosen_net_g
                        _sf_g = simple_ev_spend_fraction(
                            _ev_ng,
                            bid,
                            kelly_dampen=sp.simple_ev_kelly_dampen,
                            max_bankroll_fraction=sp.simple_ev_max_bankroll_fraction,
                        )
                        if _is_ultra_short_horizon(evt, yf_ticker, sp):
                            _sf_g *= float(sp.simple_ev_ultra_short_size_mult)
                        _sf_g *= _simple_ev_calibration_size_mult(calibration, sp)
                        if vol_p_gem is not None and abs(float(emp) - float(vol_p_gem)) > float(
                            sp.simple_ev_emp_vol_disagree_threshold
                        ):
                            _sf_g *= float(sp.simple_ev_disagree_size_mult)
                        _spr_fg = 0.0
                        if ask_side is not None and bid > 0 and float(ask_side) >= bid:
                            _spr_fg = (float(ask_side) - bid) / bid
                        if _spr_fg > float(sp.simple_ev_spread_wide_fraction):
                            _sf_g *= float(sp.simple_ev_spread_wide_size_mult)
                        _cost_cg = max(bid * 100.0, 1.0)
                        count_base = int(balance_cents * _sf_g / _cost_cg)
                        count_base = max(min_contracts, count_base) if count_base > 0 else 0
                        liq_mult, spread_bps = _liquidity_size_multiplier(bid, ask_side, sp)
                        liq_mult = 1.0
                        liq_mult *= depth_mult
                        liq_mult *= size_regime_mult
                    else:
                        count_base = _compute_buy_count(
                            p_win, price, balance_cents, sp, use_kelly, _kelly_frac_gem, min_contracts
                        )
                        liq_mult, spread_bps = _liquidity_size_multiplier(bid, ask_side, sp)
                        liq_mult *= depth_mult
                        liq_mult *= size_regime_mult
                    count = int(max(min_contracts, round(count_base * liq_mult))) if count_base > 0 else 0
                    _bdv_g: float | None = None
                    if bid_size_side is not None:
                        try:
                            _bdv_g = float(bid_size_side)
                        except (TypeError, ValueError):
                            _bdv_g = None
                    count = _cap_buy_count_by_book_pct(count, _bdv_g, sp.max_pct_of_book_volume)
                    order_entry = {
                        "platform": PLATFORM_GEMINI,
                        "ticker": evt.get("ticker", "?"),
                        "title": title,
                        "rec": rec,
                        "side": side,
                        "price": price,
                        "bid_at_place": bid,
                        "theoretical_price": bid,
                        "spread_bps_at_place": spread_bps,
                        "liquidity_mult": liq_mult,
                        "dip_pct_used": dip_used,
                        "edge": _chosen_net_g,
                        "count": count,
                        "hist_p": hist_p_gem,
                        "vol_p": vol_p_gem,
                        "yf_ticker": yf_ticker,
                        "threshold": threshold_g,
                        "direction": direction_g,
                        "dte": dte_g,
                        "gemini_evt": evt,
                        "limit_order_mode": sp.limit_order_mode,
                        "barbell": barbell_bucket_g,
                    }
                    if instrument_symbol:
                        order_entry["instrument_symbol"] = instrument_symbol
                    pending_orders.append(order_entry)
                elif sp.enable_disagreement_dip and rec == REC_SKIP:
                    # Strong disagreement: market 95% vs we 45% → place low limit to catch dips
                    gap = abs(hist_p_gem - impl)
                    if gap >= sp.disagreement_dip_min_gap:
                        no_price_dip = 1.0 - impl
                        if impl - hist_p_gem >= sp.disagreement_dip_min_gap:
                            rec_dip = REC_BUY_NO
                            bid_dip = no_price_dip
                            side_dip = "no"
                            p_win_dip = 1.0 - hist_p_gem
                        elif hist_p_gem - impl >= sp.disagreement_dip_min_gap:
                            rec_dip = REC_BUY_YES
                            bid_dip = impl
                            side_dip = "yes"
                            p_win_dip = hist_p_gem
                        else:
                            rec_dip = None
                            bid_dip = 0
                            p_win_dip = 0.0
                        if rec_dip is not None and bid_dip > 0 and p_win_dip >= sp.disagreement_dip_min_p_win:
                            price_dip = max(
                                sp.disagreement_dip_min_price,
                                min(sp.disagreement_dip_max_price, bid_dip * sp.disagreement_dip_pct_of_bid),
                            )
                            price_dip = round(price_dip, 2)
                            if price_dip >= 0.01 and _is_midprice_stable(
                                ticker=evt.get("ticker", "?"),
                                platform=PLATFORM_GEMINI,
                                cfg=cfg,
                                min_stability_seconds=dynamic_stability_seconds,
                                skip_in_preview=getattr(args, "preview", False),
                            ):
                                dip_entry = {
                                    "platform": PLATFORM_GEMINI,
                                    "ticker": evt.get("ticker", "?"),
                                    "title": title,
                                    "rec": rec_dip,
                                    "side": side_dip,
                                    "price": price_dip,
                                    "bid_at_place": bid_dip,
                                    "theoretical_price": bid_dip,
                                    "spread_bps_at_place": 0,
                                    "dip_pct_used": sp.disagreement_dip_pct_of_bid,
                                    "edge": gap,
                                    "count": sp.disagreement_dip_contracts,
                                    "hist_p": hist_p_gem,
                                    "vol_p": vol_p_gem,
                                    "yf_ticker": yf_ticker,
                                    "threshold": threshold_g,
                                    "direction": direction_g,
                                    "limit_order_mode": "fixed_pct",
                                    "dte": dte_g,
                                    "gemini_evt": evt,
                                    "disagreement_dip": True,
                                }
                                if instrument_symbol:
                                    dip_entry["instrument_symbol"] = get_instrument_symbol(evt, side_dip) or instrument_symbol
                                pending_orders.append(dip_entry)
                                print(f"       [Disagreement Dip] {side_dip.upper()} | gap={gap:.1%} | limit ${price_dip:.2f} (below ${bid_dip:.2f}) x {sp.disagreement_dip_contracts}")
                                added_dip = True
                # Lottery limits: micro bid on high-P(win) SKIPs — penny_jump improves fill rate vs 1¢ stink bids
                _lot_band_ok_g = True
                if sp.lottery_market_yes_band_enabled:
                    _lo_g, _hi_g = sp.lottery_market_yes_band_lo, sp.lottery_market_yes_band_hi
                    if _hi_g > _lo_g:
                        _lot_band_ok_g = _lo_g <= float(impl) <= _hi_g
                if not added_dip and rec == REC_SKIP and sp.enable_lottery_limits and _lot_band_ok_g:
                    p_win_yes_g, p_win_no_g = hist_p_gem, 1.0 - hist_p_gem
                    if p_win_yes_g >= sp.lottery_limit_min_p_win or p_win_no_g >= sp.lottery_limit_min_p_win:
                        side_lot = "yes" if p_win_yes_g >= p_win_no_g else "no"
                        p_win_lot = p_win_yes_g if side_lot == "yes" else p_win_no_g
                        if p_win_lot >= sp.lottery_limit_min_p_win and _is_midprice_stable(
                            ticker=evt.get("ticker", "?"),
                            platform=PLATFORM_GEMINI,
                            cfg=cfg,
                            min_stability_seconds=dynamic_stability_seconds,
                            skip_in_preview=getattr(args, "preview", False),
                        ):
                            bid_lot = impl if side_lot == "yes" else (1.0 - impl)
                            _yes_ask_g = _gemini_yes_ask(contract if contract else (_get_yes_contract(evt) or {}))
                            _no_ask_g = _gemini_no_ask(evt)
                            _lot_px_g = _compute_lottery_limit_price(
                                sp=sp,
                                side=side_lot,
                                yes_bid=float(impl),
                                no_bid=float(1.0 - impl),
                                yes_ask=_yes_ask_g,
                                no_ask=_no_ask_g,
                            )
                            lot_entry = {
                                "platform": PLATFORM_GEMINI,
                                "ticker": evt.get("ticker", "?"),
                                "title": title,
                                "rec": REC_BUY_YES if side_lot == "yes" else REC_BUY_NO,
                                "side": side_lot,
                                "price": _lot_px_g,
                                "bid_at_place": bid_lot,
                                "theoretical_price": bid_lot,
                                "spread_bps_at_place": 0,
                                "edge": p_win_lot,
                                "count": sp.lottery_limit_contracts,
                                "hist_p": hist_p_gem,
                                "vol_p": vol_p_gem,
                                "yf_ticker": yf_ticker,
                                "threshold": threshold_g,
                                "direction": direction_g,
                                "limit_order_mode": "fixed_pct",
                                "dte": dte_g,
                                "gemini_evt": evt,
                                "lottery_limit": True,
                            }
                            if instrument_symbol:
                                lot_entry["instrument_symbol"] = get_instrument_symbol(evt, side_lot) or instrument_symbol
                            pending_orders.append(lot_entry)
                            print(
                                f"       [Lottery Limit] {side_lot.upper()} | P(win)={p_win_lot:.1%} | "
                                f"limit ${_lot_px_g:.2f} ({sp.lottery_limit_price_mode}) x {sp.lottery_limit_contracts}"
                            )
                            added_dip = True
                _eval_count_total += 1
                if rec != REC_SKIP or added_dip:
                    _eval_count_buy += 1
                else:
                    _eval_count_skip += 1
                print()

    # Latest unresolved entries for dynamic stop-loss checks: (platform, ticker, side) -> entry_price
    latest_entry_by_key: dict[tuple[str, str, str], float] = {}
    latest_entry_ts_by_key: dict[tuple[str, str, str], float] = {}
    if get_unresolved_predictions:
        try:
            for p in get_unresolved_predictions(cfg) or []:
                key = (
                    str(p.get("platform") or ""),
                    str(p.get("ticker") or ""),
                    str(p.get("side") or ""),
                )
                ep = float(p.get("entry_price") or 0)
                ts = float(p.get("ts") or 0)
                if ep <= 0:
                    continue
                prev_ts = latest_entry_ts_by_key.get(key)
                if prev_ts is None or ts >= prev_ts:
                    latest_entry_by_key[key] = ep
                    latest_entry_ts_by_key[key] = ts
        except Exception:
            pass

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
                                for prefix, s in [
                                    ("KXBTC15M", "KXBTC15M"),
                                    ("KXBTC", "KXBTCD"),
                                    ("KXETH", "KXETHD"),
                                    ("KXSOL", "KXSOLD"),
                                    ("KXXRP", "KXXRPD"),
                                    ("KXINX", "KXINXU"),
                                    # US gas weekly — RB=F proxy + yfinance_underlying_adjust vs AAA settlement
                                    ("KXAAAGASW", "KXAAAGASW"),
                                ]:
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
                                    closes = apply_underlying_closes(cfg, series or "", closes)
                                    threshold, direction = _extract_strike(m)
                                    if threshold is not None and direction is not None:
                                        yes_bid = float(m.get("yes_bid_dollars") or 0) or float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)
                                        no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                                        closes_for_emp_pos = _get_closes_for_empirical(
                                            pos_yf, args.days, closes, m, sp, cfg, kalshi_series=series or ""
                                        )
                                        recent_w_pos = _regime_adjusted_recent_weight(closes_for_emp_pos, sp.empirical_recent_weight)
                                        emp_pos = _empirical_probability(
                                            closes_for_emp_pos,
                                            threshold,
                                            direction,
                                            recent_days=sp.empirical_recent_days,
                                            recent_weight=recent_w_pos,
                                            use_rolling_window=sp.empirical_use_rolling_window,
                                            weighting=sp.empirical_weighting,
                                            exponential_halflife_bars=sp.empirical_exponential_halflife_bars,
                                        )
                                        hist_p = emp_pos
                                        dte_pos = _get_days_to_expiry_from_market(m, pos_yf, sp.days_to_expiry)
                                        ewma_lambda_pos = _ewma_lambda_for_market(sp, m, pos_yf)
                                        last_close_pos = float(closes[-1])
                                        _snap_pos = (
                                            apply_underlying_price(cfg, series or "", yf_get_snapshot(pos_yf))
                                            if sp.use_live_price
                                            else None
                                        )
                                        if _snap_pos is not None and _snap_pos > 0:
                                            current_price_pos = sp.live_price_weight * float(_snap_pos) + (1 - sp.live_price_weight) * last_close_pos
                                        else:
                                            current_price_pos = last_close_pos
                                        vol_p = (
                                            _volatility_probability_with_mc(
                                                closes,
                                                threshold,
                                                direction,
                                                dte_pos,
                                                use_ewma=sp.use_ewma_vol,
                                                ewma_lambda=ewma_lambda_pos,
                                                current_price=current_price_pos,
                                                vol_model=sp.vol_model,
                                                garch_min_returns=sp.garch_min_returns,
                                                enable_otm_smile_adjustment=sp.enable_otm_smile_adjustment,
                                                otm_smile_strength=sp.otm_smile_strength,
                                                otm_smile_max_mult=sp.otm_smile_max_mult,
                                                distribution=sp.vol_distribution,
                                                t_df=sp.vol_t_df,
                                                mc_paths=sp.mc_paths if sp.mc_enabled else 0,
                                                mc_blend_weight=sp.mc_blend_weight,
                                            )
                                            if use_vol else None
                                        )
                                        if vol_p is not None and use_vol:
                                            _is_intra_pos = _is_intraday_or_ultra_short(m, pos_yf, sp)
                                            if _is_intra_pos:
                                                vol_p = _cap_intraday_vol_p(vol_p, sp.intraday_vol_cap)
                                            if _is_intra_pos and not sp.use_intraday_empirical:
                                                hist_p = vol_p
                                            elif sp.vol_blend_weight is not None:
                                                w_emp_pos = _dynamic_empirical_weight(sp.vol_blend_weight, len(closes_for_emp_pos), sp.vol_dynamic_n0)
                                                w_emp_pos = _dte_adjusted_empirical_weight(w_emp_pos, dte_pos)
                                                if _is_intra_pos:
                                                    w_emp_pos = max(w_emp_pos, sp.intraday_min_empirical_weight)
                                                hist_p = _vol_emp_blend_hist_p(emp_pos, vol_p, w_emp_pos, sp)
                                            else:
                                                hist_p = max(emp_pos, vol_p)
                                        if sp.enable_model_prob_hard_cap:
                                            hist_p = max(sp.model_prob_hard_lo, min(sp.model_prob_hard_hi, float(hist_p)))
                                    momentum_ret_pos = ((current_price_pos - last_close_pos) / last_close_pos) if last_close_pos > 0 else 0.0
                                    hist_p = apply_anti_hallucination_pipeline(
                                        model_p=hist_p,
                                        market_p=yes_bid,
                                        closes=closes,
                                        threshold=threshold,
                                        direction=direction,
                                        current_price=current_price_pos,
                                        momentum_ret=momentum_ret_pos,
                                        cfg=sp.ah_cfg,
                                    )
                                    hist_p = _sanitize_model_probability(
                                        hist_p,
                                        lo=sp.model_prob_hard_lo,
                                        hi=sp.model_prob_hard_hi,
                                        enabled=sp.enable_model_prob_hard_cap,
                                    )
                                    maker_fee_pos = _maker_fee_rate_for_market(sp, PLATFORM_KALSHI, pos_yf)
                                    eff_pos = _get_effective_min_edge(min_edge, direction, calibration, sp)
                                    eff_pos += proxy_basis_extra_min_edge(cfg, series or "")
                                    if sp.dte_edge_scaling_enabled and dte_pos is not None:
                                        eff_pos *= _dte_edge_scale_factor(float(dte_pos), sp)
                                    if _is_ultra_short_horizon(m, pos_yf, sp) and sp.ultra_short_min_edge_mult > 1.0:
                                        eff_pos *= float(sp.ultra_short_min_edge_mult)
                                    if sp.adaptive_learning_enabled and adaptive_edge_bump > 0:
                                        eff_pos += adaptive_edge_bump
                                    eff_pos_yes = eff_pos + yes_parity_penalty + sp.yes_overconfidence_extra_edge
                                    eff_pos_no = eff_pos + no_parity_penalty
                                    min_p_win_pos = (
                                        sp.intraday_other_min_p_win_with_vol
                                        if (use_vol and vol_p is not None)
                                        else sp.intraday_other_min_p_win
                                    )
                                    if _is_ultra_short_horizon(m, pos_yf, sp):
                                        _upp = (
                                            sp.ultra_short_min_p_win_with_vol
                                            if (use_vol and vol_p is not None)
                                            else sp.ultra_short_min_p_win
                                        )
                                        min_p_win_pos = max(min_p_win_pos, _upp)
                                    rec = (
                                        strategy_engine.recommend_strict(
                                            p_yes=hist_p,
                                            market_yes=yes_bid,
                                            min_p_win=min_p_win_pos,
                                            min_edge=eff_pos,
                                            maker_fee_rate=maker_fee_pos,
                                            min_fee_per_order=sp.min_fee_per_order,
                                            min_edge_yes=eff_pos_yes,
                                            min_edge_no=eff_pos_no,
                                            **_strategy_recommend_strict_extras(sp),
                                        )
                                        if _strict_intraday_evaluation(m, pos_yf, sp)
                                        else strategy_engine.recommend(
                                            p_yes=hist_p,
                                            market_yes=yes_bid,
                                            min_edge=eff_pos,
                                            maker_fee_rate=maker_fee_pos,
                                            min_fee_per_order=sp.min_fee_per_order,
                                            min_edge_yes=eff_pos_yes,
                                            min_edge_no=eff_pos_no,
                                            min_edge_easy_win=sp.min_edge_easy_win if sp.enable_easy_win else None,
                                            easy_win_min_p_win=sp.easy_win_min_p_win,
                                            easy_win_min_market_p=sp.easy_win_min_market_p,
                                            **_strategy_recommend_extras(sp),
                                        )
                                    )
                                    sell_price = yes_bid if side == "yes" else no_bid
                                    # Persist orphan position eval for calibration (positions not in main market loop)
                                    if record_market_snapshot:
                                        diff = (hist_p - yes_bid) if rec == REC_BUY_YES else ((1 - hist_p) - no_bid) if rec == REC_BUY_NO else 0
                                        book = _extract_book_fields(m, yes_bid, no_bid)
                                        record_market_snapshot(
                                            PLATFORM_KALSHI, ticker, yes_bid, no_bid, hist_p, rec,
                                            threshold, direction, vol_p, abs(diff), m.get("title"), pos_yf,
                                            yes_ask=book.get("yes_ask"),
                                            no_ask=book.get("no_ask"),
                                            yes_bid_size=book.get("yes_bid_size"),
                                            no_bid_size=book.get("no_bid_size"),
                                            yes_ask_size=book.get("yes_ask_size"),
                                            no_ask_size=book.get("no_ask_size"),
                                            config=cfg,
                                        )
                                    # Dynamic stop-loss: cut losers before expiry if contract price moves against us.
                                    entry_price = latest_entry_by_key.get((PLATFORM_KALSHI, ticker, side))
                                    current_side_price = yes_bid if side == "yes" else no_bid
                                    if entry_price and current_side_price <= entry_price * (1 - sp.stop_loss_pct):
                                        pending_sells.append({
                                            "platform": PLATFORM_KALSHI,
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": sell_price,
                                            "rec": "STOP_LOSS",
                                        })
                                        continue
                                    _fee_hy_pe = _fee_per_contract(yes_bid, maker_fee_pos, sp.min_fee_per_order)
                                    _fee_hn_pe = _fee_per_contract(no_bid, maker_fee_pos, sp.min_fee_per_order)
                                    _held_ne_pe = (
                                        (hist_p - yes_bid - _fee_hy_pe)
                                        if side == "yes"
                                        else ((1.0 - hist_p) - no_bid - _fee_hn_pe)
                                    )
                                    if sp.position_exit_min_net_edge > 0 and _held_ne_pe < sp.position_exit_min_net_edge:
                                        pending_sells.append({
                                            "platform": PLATFORM_KALSHI,
                                            "ticker": ticker,
                                            "title": (m.get("title") or "")[:55],
                                            "side": side,
                                            "action": "sell",
                                            "count": position_count,
                                            "price": sell_price,
                                            "rec": REC_EDGE_EXIT,
                                        })
                                        continue
                                    # SELL when definitely a loser
                                    p_lose = strategy_engine.compute_p_lose(
                                        hist_p=hist_p,
                                        yes_bid=yes_bid,
                                        no_bid=no_bid,
                                        side=side,
                                        days_to_expiry=dte_pos,
                                        p_lose_blend_market_max_weight=sp.p_lose_blend_market_max_weight,
                                        p_lose_blend_market_half_life_days=sp.p_lose_blend_market_half_life_days,
                                    )
                                    sell_threshold = (
                                        _ev_sell_threshold(sell_price, maker_fee_pos, sp.min_fee_per_order)
                                        if sp.dynamic_sell_threshold
                                        else sp.sell_losing_pct
                                    )
                                    eff_sell_thresh = sell_threshold + max(
                                        0.0, float(sp.position_loser_sell_p_lose_margin)
                                    )
                                    hold_skip = _hold_winner_skip_discretionary_sells(
                                        sp,
                                        current_price=current_price_pos,
                                        threshold=threshold,
                                        direction=direction,
                                        side=side,
                                        dte_days=dte_pos,
                                    )
                                    flip_cond = (side == "yes" and rec == REC_BUY_NO) or (
                                        side == "no" and rec == REC_BUY_YES
                                    )
                                    flip_ok = flip_cond and _position_flip_sell_allowed(
                                        sp,
                                        hist_p=hist_p,
                                        yes_bid=yes_bid,
                                        no_bid=no_bid,
                                        side=side,
                                        maker_fee_rate=maker_fee_pos,
                                        min_fee_per_order=sp.min_fee_per_order,
                                    )
                                    if not hold_skip and p_lose >= eff_sell_thresh:
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
                                        _maybe_add_reentry_order(
                                            sp, min_edge, pending_orders, PLATFORM_KALSHI, ticker, (m.get("title") or "")[:55],
                                            side, hist_p, yes_bid, no_bid, threshold, direction, pos_yf, vol_p,
                                            maker_fee_pos, position_count,
                                            current_price=current_price_pos, dte_days=dte_pos,
                                        )
                                    elif not hold_skip and flip_ok:
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
                                        _maybe_add_reentry_order(
                                            sp, min_edge, pending_orders, PLATFORM_KALSHI, ticker, (m.get("title") or "")[:55],
                                            side, hist_p, yes_bid, no_bid, threshold, direction, pos_yf, vol_p,
                                            maker_fee_pos, position_count,
                                            current_price=current_price_pos, dte_days=dte_pos,
                                        )
                                    elif (
                                        side == "no"
                                        and sp.enforce_no_above_rule
                                        and _violates_no_above_rule(current_price_pos, threshold, direction, sp.no_above_cushion)
                                    ):
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
                                        if record_position_evaluation:
                                            record_position_evaluation(
                                                platform=PLATFORM_KALSHI, ticker=ticker, side=side, action="keep",
                                                hist_p=hist_p, vol_p=vol_p, p_lose=p_lose, yes_bid=yes_bid, no_bid=no_bid,
                                                threshold=threshold, direction=direction, yf_ticker=pos_yf, rec=rec,
                                                position_count=position_count,
                                                entry_price=latest_entry_by_key.get((PLATFORM_KALSHI, ticker, side)),
                                                current_price=current_side_price, config=cfg,
                                            )
                            elif getattr(args, "verbose", False):
                                print(f"    (no hist data for {ticker}, skip sell eval)")
                    except Exception as ex:
                        if getattr(args, "verbose", False):
                            print(f"    (could not evaluate {ticker}: {ex})")
                    continue
                _series, _yf, closes, m = cached[:4]
                current_price_cached = cached[4] if len(cached) > 4 else closes[-1]
                threshold, direction = _extract_strike(m)
                if threshold is None or direction is None:
                    continue
                yes_bid = float(m.get("yes_bid_dollars") or 0) or float(m.get("last_price_dollars") or m.get("yes_ask_dollars") or 0)
                closes_for_emp_cached = _get_closes_for_empirical(_yf, args.days, closes, m, sp, cfg, kalshi_series=_series)
                recent_w_cached = _regime_adjusted_recent_weight(closes_for_emp_cached, sp.empirical_recent_weight)
                emp_cached = _empirical_probability(
                    closes_for_emp_cached,
                    threshold,
                    direction,
                    recent_days=sp.empirical_recent_days,
                    recent_weight=recent_w_cached,
                    use_rolling_window=sp.empirical_use_rolling_window,
                    weighting=sp.empirical_weighting,
                    exponential_halflife_bars=sp.empirical_exponential_halflife_bars,
                )
                hist_p = emp_cached
                dte_cached = _get_days_to_expiry_from_market(m, _yf, sp.days_to_expiry)
                ewma_lambda_cached = _ewma_lambda_for_market(sp, m, _yf)
                vol_p = (
                    _volatility_probability_with_mc(
                        closes,
                        threshold,
                        direction,
                        dte_cached,
                        use_ewma=sp.use_ewma_vol,
                        ewma_lambda=ewma_lambda_cached,
                        current_price=current_price_cached,
                        vol_model=sp.vol_model,
                        garch_min_returns=sp.garch_min_returns,
                        enable_otm_smile_adjustment=sp.enable_otm_smile_adjustment,
                        otm_smile_strength=sp.otm_smile_strength,
                        otm_smile_max_mult=sp.otm_smile_max_mult,
                        distribution=sp.vol_distribution,
                        t_df=sp.vol_t_df,
                        mc_paths=sp.mc_paths if sp.mc_enabled else 0,
                        mc_blend_weight=sp.mc_blend_weight,
                    )
                    if use_vol else None
                )
                if vol_p is not None and use_vol:
                    _is_intra_c = _is_intraday_or_ultra_short(m, _yf, sp)
                    if _is_intra_c:
                        vol_p = _cap_intraday_vol_p(vol_p, sp.intraday_vol_cap)
                    if _is_intra_c and not sp.use_intraday_empirical:
                        hist_p = vol_p
                    elif sp.vol_blend_weight is not None:
                        w_emp_cached = _dynamic_empirical_weight(sp.vol_blend_weight, len(closes_for_emp_cached), sp.vol_dynamic_n0)
                        w_emp_cached = _dte_adjusted_empirical_weight(w_emp_cached, dte_cached)
                        if _is_intra_c:
                            w_emp_cached = max(w_emp_cached, sp.intraday_min_empirical_weight)
                        hist_p = _vol_emp_blend_hist_p(emp_cached, vol_p, w_emp_cached, sp)
                    else:
                        hist_p = max(emp_cached, vol_p)
                maker_fee_cached = _maker_fee_rate_for_market(sp, PLATFORM_KALSHI, _yf)
                eff_cached = _get_effective_min_edge(min_edge, direction, calibration, sp)
                if sp.dte_edge_scaling_enabled and dte_cached is not None:
                    eff_cached *= _dte_edge_scale_factor(float(dte_cached), sp)
                if _is_ultra_short_horizon(m, _yf, sp) and sp.ultra_short_min_edge_mult > 1.0:
                    eff_cached *= float(sp.ultra_short_min_edge_mult)
                if sp.adaptive_learning_enabled and adaptive_edge_bump > 0:
                    eff_cached += adaptive_edge_bump
                eff_cached_yes = eff_cached + yes_parity_penalty + sp.yes_overconfidence_extra_edge
                eff_cached_no = eff_cached + no_parity_penalty
                min_p_win_c = (
                    sp.intraday_other_min_p_win_with_vol
                    if (use_vol and vol_p is not None)
                    else sp.intraday_other_min_p_win
                )
                if _is_ultra_short_horizon(m, _yf, sp):
                    _uc = (
                        sp.ultra_short_min_p_win_with_vol
                        if (use_vol and vol_p is not None)
                        else sp.ultra_short_min_p_win
                    )
                    min_p_win_c = max(min_p_win_c, _uc)
                rec = (
                    strategy_engine.recommend_strict(
                        p_yes=hist_p,
                        market_yes=yes_bid,
                        min_p_win=min_p_win_c,
                        min_edge=eff_cached,
                        maker_fee_rate=maker_fee_cached,
                        min_fee_per_order=sp.min_fee_per_order,
                        min_edge_yes=eff_cached_yes,
                        min_edge_no=eff_cached_no,
                        **_strategy_recommend_strict_extras(sp),
                    )
                    if _strict_intraday_evaluation(m, _yf, sp)
                    else strategy_engine.recommend(
                        p_yes=hist_p,
                        market_yes=yes_bid,
                        min_edge=eff_cached,
                        maker_fee_rate=maker_fee_cached,
                        min_fee_per_order=sp.min_fee_per_order,
                        min_edge_yes=eff_cached_yes,
                        min_edge_no=eff_cached_no,
                        min_edge_easy_win=sp.min_edge_easy_win if sp.enable_easy_win else None,
                        easy_win_min_p_win=sp.easy_win_min_p_win,
                        easy_win_min_market_p=sp.easy_win_min_market_p,
                        **_strategy_recommend_extras(sp),
                    )
                )
                current_price_pos = current_price_cached
                no_bid = float(m.get("no_bid_dollars") or 0) or (1.0 - yes_bid)
                sell_price = yes_bid if side == "yes" else no_bid

                # Dynamic stop-loss: cut losers before expiry if contract price moves against us.
                entry_price = latest_entry_by_key.get((PLATFORM_KALSHI, ticker, side))
                current_side_price = yes_bid if side == "yes" else no_bid
                if entry_price and current_side_price <= entry_price * (1 - sp.stop_loss_pct):
                    pending_sells.append({
                        "platform": PLATFORM_KALSHI,
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": sell_price,
                        "rec": "STOP_LOSS",
                    })
                    continue
                _fee_hy_pc = _fee_per_contract(yes_bid, maker_fee_cached, sp.min_fee_per_order)
                _fee_hn_pc = _fee_per_contract(no_bid, maker_fee_cached, sp.min_fee_per_order)
                _held_ne_pc = (
                    (hist_p - yes_bid - _fee_hy_pc)
                    if side == "yes"
                    else ((1.0 - hist_p) - no_bid - _fee_hn_pc)
                )
                if sp.position_exit_min_net_edge > 0 and _held_ne_pc < sp.position_exit_min_net_edge:
                    pending_sells.append({
                        "platform": PLATFORM_KALSHI,
                        "ticker": ticker,
                        "title": (m.get("title") or "")[:55],
                        "side": side,
                        "action": "sell",
                        "count": position_count,
                        "price": sell_price,
                        "rec": REC_EDGE_EXIT,
                    })
                    continue
                # SELL when EV of holding < net proceeds from selling at current bid
                p_lose = strategy_engine.compute_p_lose(
                    hist_p=hist_p,
                    yes_bid=yes_bid,
                    no_bid=no_bid,
                    side=side,
                    days_to_expiry=dte_cached,
                    p_lose_blend_market_max_weight=sp.p_lose_blend_market_max_weight,
                    p_lose_blend_market_half_life_days=sp.p_lose_blend_market_half_life_days,
                )
                sell_threshold = (
                    _ev_sell_threshold(sell_price, maker_fee_cached, sp.min_fee_per_order)
                    if sp.dynamic_sell_threshold
                    else sp.sell_losing_pct
                )
                eff_sell_thresh = sell_threshold + max(0.0, float(sp.position_loser_sell_p_lose_margin))
                hold_skip = _hold_winner_skip_discretionary_sells(
                    sp,
                    current_price=current_price_pos,
                    threshold=threshold,
                    direction=direction,
                    side=side,
                    dte_days=dte_cached,
                )
                flip_cond = (side == "yes" and rec == REC_BUY_NO) or (side == "no" and rec == REC_BUY_YES)
                flip_ok = flip_cond and _position_flip_sell_allowed(
                    sp,
                    hist_p=hist_p,
                    yes_bid=yes_bid,
                    no_bid=no_bid,
                    side=side,
                    maker_fee_rate=maker_fee_cached,
                    min_fee_per_order=sp.min_fee_per_order,
                )
                if not hold_skip and p_lose >= eff_sell_thresh:
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
                    _maybe_add_reentry_order(
                        sp, min_edge, pending_orders, PLATFORM_KALSHI, ticker, (m.get("title") or "")[:55],
                        side, hist_p, yes_bid, no_bid, threshold, direction, _yf, vol_p,
                        maker_fee_cached, position_count,
                        current_price=current_price_pos, dte_days=dte_cached,
                    )
                # SELL YES when we'd now recommend BUY NO; SELL NO when we'd recommend BUY YES
                elif not hold_skip and flip_ok:
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
                    _maybe_add_reentry_order(
                        sp, min_edge, pending_orders, PLATFORM_KALSHI, ticker, (m.get("title") or "")[:55],
                        side, hist_p, yes_bid, no_bid, threshold, direction, _yf, vol_p,
                        maker_fee_cached, position_count,
                        current_price=current_price_pos, dte_days=dte_cached,
                    )
                # SELL NO when holding NO on "above X" but price already > X * (1 + cushion)
                elif (
                    side == "no"
                    and sp.enforce_no_above_rule
                    and _violates_no_above_rule(current_price_pos, threshold, direction, sp.no_above_cushion)
                ):
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
                    if record_position_evaluation:
                        record_position_evaluation(
                            platform=PLATFORM_KALSHI, ticker=ticker, side=side, action="keep",
                            hist_p=hist_p, vol_p=vol_p, p_lose=p_lose, yes_bid=yes_bid, no_bid=no_bid,
                            threshold=threshold, direction=direction, yf_ticker=_yf, rec=rec,
                            position_count=position_count,
                            entry_price=latest_entry_by_key.get((PLATFORM_KALSHI, ticker, side)),
                            current_price=current_side_price, config=cfg,
                        )

            # Evaluate Gemini positions for sell when bet looks like a loser (like Kalshi)
            gemini_symbol_to_market: dict[str, dict] = {}
            for yf_ticker, evts in gemini_events_by_asset.items():
                for mkt in expand_gemini_events_to_markets(evts):
                    evt = mkt["event"]
                    sym = mkt.get("instrument_symbol")
                    if sym:
                        gemini_symbol_to_market[str(sym)] = {**mkt, "yf_ticker": yf_ticker}
                    else:
                        for out in ("yes", "no"):
                            s = get_instrument_symbol(evt, out)
                            if s:
                                gemini_symbol_to_market[str(s)] = {**mkt, "yf_ticker": yf_ticker}

            gemini_trading_eval = None
            try:
                gemini_trading_eval = GeminiTradingClient()
            except ValueError:
                pass
            if gemini_trading_eval and gemini_symbol_to_market:
                try:
                    pos_resp = gemini_trading_eval.get_positions()
                    gemini_positions = pos_resp.get("positions") or []
                    gemini_eval_count = len(gemini_positions)
                    if gemini_eval_count:
                        if eval_count:
                            print(f"  (plus {gemini_eval_count} Gemini position(s))")
                        else:
                            print(f"\nEvaluating {gemini_eval_count} Gemini position(s) for keep vs sell...")
                    for pos in gemini_positions:
                        sym = pos.get("symbol") or pos.get("instrumentSymbol")
                        outcome = (pos.get("outcome") or "").lower()
                        qty_raw = pos.get("totalQuantity") or pos.get("quantity") or "0"
                        try:
                            position_count = int(float(qty_raw))
                        except (ValueError, TypeError):
                            position_count = 0
                        if not sym or outcome not in ("yes", "no") or position_count <= 0:
                            continue
                        info = gemini_symbol_to_market.get(str(sym))
                        if not info:
                            if getattr(args, "verbose", False):
                                print(f"    (skip Gemini {sym} — not in our market list)")
                            continue
                        evt = info["event"]
                        threshold_g = info["threshold"]
                        direction_g = info["direction"]
                        pos_yf = info.get("yf_ticker")
                        title = (info.get("title") or evt.get("title") or "")[:55]
                        ticker_g = info.get("ticker") or evt.get("ticker", "?")
                        side = outcome
                        if _should_skip_by_resolution(evt, pos_yf or ""):
                            impl = get_yes_price(info.get("contract") or _get_yes_contract(evt) or {})
                            no_price_g = 1.0 - impl if 0 < impl < 1 else impl
                            sell_price = impl if side == "yes" else no_price_g
                            pending_sells.append({
                                "platform": PLATFORM_GEMINI,
                                "ticker": ticker_g,
                                "title": title,
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": sell_price,
                                "rec": "INTRADAY_MARKET",
                                "instrument_symbol": sym,
                                "gemini_evt": evt,
                            })
                            continue
                        if not pos_yf or pos_yf not in GEMINI_CRYPTO_YF:
                            continue
                        closes = yf_get_aggs(pos_yf, days_back=args.days, config=cfg)
                        if not closes:
                            if getattr(args, "verbose", False):
                                print(f"    (no hist data for Gemini {ticker_g}, skip sell eval)")
                            continue
                        last_close_g = float(closes[-1])
                        _snap_g = yf_get_snapshot(pos_yf) if sp.use_live_price else None
                        if _snap_g is not None and _snap_g > 0:
                            current_price_g = sp.live_price_weight * float(_snap_g) + (1 - sp.live_price_weight) * last_close_g
                        else:
                            current_price_g = last_close_g
                        impl = get_yes_price(info.get("contract") or _get_yes_contract(evt) or {})
                        no_price_g = 1.0 - impl if 0 < impl < 1 else impl
                        closes_for_emp_g = _get_closes_for_empirical(pos_yf, args.days, closes, evt, sp, cfg)
                        recent_w_gp = _regime_adjusted_recent_weight(closes_for_emp_g, sp.empirical_recent_weight)
                        emp_g = _empirical_probability(
                            closes_for_emp_g,
                            threshold_g,
                            direction_g,
                            recent_days=sp.empirical_recent_days,
                            recent_weight=recent_w_gp,
                            use_rolling_window=sp.empirical_use_rolling_window,
                            weighting=sp.empirical_weighting,
                            exponential_halflife_bars=sp.empirical_exponential_halflife_bars,
                        )
                        dte_g = _get_days_to_expiry_from_market(evt, pos_yf, sp.days_to_expiry)
                        ewma_lambda_g = _ewma_lambda_for_market(sp, evt, pos_yf)
                        vol_p_g = (
                            _volatility_probability_with_mc(
                                closes,
                                threshold_g,
                                direction_g,
                                dte_g,
                                use_ewma=sp.use_ewma_vol,
                                ewma_lambda=ewma_lambda_g,
                                current_price=current_price_g,
                                vol_model=sp.vol_model,
                                garch_min_returns=sp.garch_min_returns,
                                enable_otm_smile_adjustment=sp.enable_otm_smile_adjustment,
                                otm_smile_strength=sp.otm_smile_strength,
                                otm_smile_max_mult=sp.otm_smile_max_mult,
                                distribution=sp.vol_distribution,
                                t_df=sp.vol_t_df,
                                mc_paths=sp.mc_paths if sp.mc_enabled else 0,
                                mc_blend_weight=sp.mc_blend_weight,
                            )
                            if use_vol else None
                        )
                        hist_p_g = emp_g
                        if vol_p_g is not None and use_vol:
                            _is_intra_gp = _is_intraday_or_ultra_short(evt, pos_yf, sp)
                            if _is_intra_gp:
                                vol_p_g = _cap_intraday_vol_p(vol_p_g, sp.intraday_vol_cap)
                            if _is_intra_gp and not sp.use_intraday_empirical:
                                hist_p_g = vol_p_g
                            elif sp.vol_blend_weight is not None:
                                w_emp_g = _dynamic_empirical_weight(sp.vol_blend_weight, len(closes_for_emp_g), sp.vol_dynamic_n0)
                                w_emp_g = _dte_adjusted_empirical_weight(w_emp_g, dte_g)
                                if _is_intra_gp:
                                    w_emp_g = max(w_emp_g, sp.intraday_min_empirical_weight)
                                hist_p_g = _vol_emp_blend_hist_p(emp_g, vol_p_g, w_emp_g, sp)
                            else:
                                hist_p_g = max(emp_g, vol_p_g)
                        min_p_win_g = sp.intraday_other_min_p_win_with_vol if (use_vol and vol_p_g is not None) else sp.intraday_other_min_p_win
                        if _is_ultra_short_horizon(evt, pos_yf, sp):
                            _ugp = (
                                sp.ultra_short_min_p_win_with_vol
                                if (use_vol and vol_p_g is not None)
                                else sp.ultra_short_min_p_win
                            )
                            min_p_win_g = max(min_p_win_g, _ugp)
                        maker_fee_pos_g = _maker_fee_rate_for_market(sp, PLATFORM_GEMINI, pos_yf)
                        pos_min_edge_g = _get_effective_min_edge(min_edge, direction_g, calibration, sp)
                        if sp.dte_edge_scaling_enabled and dte_g is not None:
                            pos_min_edge_g *= _dte_edge_scale_factor(float(dte_g), sp)
                        if _is_ultra_short_horizon(evt, pos_yf, sp) and sp.ultra_short_min_edge_mult > 1.0:
                            pos_min_edge_g *= float(sp.ultra_short_min_edge_mult)
                        if sp.adaptive_learning_enabled and adaptive_edge_bump > 0:
                            pos_min_edge_g += adaptive_edge_bump
                        rec = (
                            strategy_engine.recommend_strict(
                                p_yes=hist_p_g,
                                market_yes=impl,
                                min_p_win=min_p_win_g,
                                min_edge=pos_min_edge_g,
                                maker_fee_rate=maker_fee_pos_g,
                                min_fee_per_order=sp.min_fee_per_order,
                                min_edge_yes=pos_min_edge_g + yes_parity_penalty + sp.yes_overconfidence_extra_edge,
                                min_edge_no=pos_min_edge_g + no_parity_penalty,
                                **_strategy_recommend_strict_extras(sp),
                            )
                            if _strict_intraday_evaluation(evt, pos_yf, sp)
                            else strategy_engine.recommend(
                                p_yes=hist_p_g,
                                market_yes=impl,
                                min_edge=pos_min_edge_g,
                                maker_fee_rate=maker_fee_pos_g,
                                min_fee_per_order=sp.min_fee_per_order,
                                min_edge_yes=pos_min_edge_g + yes_parity_penalty + sp.yes_overconfidence_extra_edge,
                                min_edge_no=pos_min_edge_g + no_parity_penalty,
                                min_edge_easy_win=sp.min_edge_easy_win if sp.enable_easy_win else None,
                                easy_win_min_p_win=sp.easy_win_min_p_win,
                                easy_win_min_market_p=sp.easy_win_min_market_p,
                                **_strategy_recommend_extras(sp),
                            )
                        )
                        violates_g = _violates_no_above_rule(current_price_g, threshold_g, direction_g, sp.no_above_cushion)
                        if rec == REC_BUY_NO and sp.enforce_no_above_rule and violates_g:
                            rec = REC_SKIP
                        sell_price = impl if side == "yes" else no_price_g
                        # Persist position eval for calibration (same as Kalshi orphan positions)
                        if record_market_snapshot:
                            diff_g = (hist_p_g - impl) if rec == REC_BUY_YES else ((1 - hist_p_g) - no_price_g) if rec == REC_BUY_NO else 0
                            book_g = _extract_book_fields(evt, impl, no_price_g)
                            record_market_snapshot(
                                PLATFORM_GEMINI, ticker_g, impl, no_price_g, hist_p_g, rec,
                                threshold_g, direction_g, vol_p_g, abs(diff_g), title, pos_yf,
                                yes_ask=book_g.get("yes_ask"),
                                no_ask=book_g.get("no_ask"),
                                yes_bid_size=book_g.get("yes_bid_size"),
                                no_bid_size=book_g.get("no_bid_size"),
                                yes_ask_size=book_g.get("yes_ask_size"),
                                no_ask_size=book_g.get("no_ask_size"),
                                config=cfg,
                            )
                        entry_price_g = latest_entry_by_key.get((PLATFORM_GEMINI, ticker_g, side))
                        current_side_price_g = impl if side == "yes" else no_price_g
                        if entry_price_g and current_side_price_g <= entry_price_g * (1 - sp.stop_loss_pct):
                            pending_sells.append({
                                "platform": PLATFORM_GEMINI,
                                "ticker": ticker_g,
                                "title": title,
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": sell_price,
                                "rec": "STOP_LOSS",
                                "instrument_symbol": sym,
                                "gemini_evt": evt,
                            })
                            continue
                        _fee_hy_pg = _fee_per_contract(impl, maker_fee_pos_g, sp.min_fee_per_order)
                        _fee_hn_pg = _fee_per_contract(no_price_g, maker_fee_pos_g, sp.min_fee_per_order)
                        _held_ne_pg = (
                            (hist_p_g - impl - _fee_hy_pg)
                            if side == "yes"
                            else ((1.0 - hist_p_g) - no_price_g - _fee_hn_pg)
                        )
                        if sp.position_exit_min_net_edge > 0 and _held_ne_pg < sp.position_exit_min_net_edge:
                            pending_sells.append({
                                "platform": PLATFORM_GEMINI,
                                "ticker": ticker_g,
                                "title": title,
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": sell_price,
                                "rec": REC_EDGE_EXIT,
                                "instrument_symbol": sym,
                                "gemini_evt": evt,
                            })
                            continue
                        p_lose = strategy_engine.compute_p_lose(
                            hist_p=hist_p_g,
                            yes_bid=impl,
                            no_bid=no_price_g,
                            side=side,
                            days_to_expiry=dte_g,
                            p_lose_blend_market_max_weight=sp.p_lose_blend_market_max_weight,
                            p_lose_blend_market_half_life_days=sp.p_lose_blend_market_half_life_days,
                        )
                        sell_threshold = (
                            _ev_sell_threshold(sell_price, maker_fee_pos_g, sp.min_fee_per_order)
                            if sp.dynamic_sell_threshold
                            else sp.sell_losing_pct
                        )
                        eff_sell_thresh = sell_threshold + max(0.0, float(sp.position_loser_sell_p_lose_margin))
                        hold_skip = _hold_winner_skip_discretionary_sells(
                            sp,
                            current_price=current_price_g,
                            threshold=threshold_g,
                            direction=direction_g,
                            side=side,
                            dte_days=dte_g,
                        )
                        flip_cond = (side == "yes" and rec == REC_BUY_NO) or (
                            side == "no" and rec == REC_BUY_YES
                        )
                        flip_ok = flip_cond and _position_flip_sell_allowed(
                            sp,
                            hist_p=hist_p_g,
                            yes_bid=impl,
                            no_bid=no_price_g,
                            side=side,
                            maker_fee_rate=maker_fee_pos_g,
                            min_fee_per_order=sp.min_fee_per_order,
                        )
                        if not hold_skip and p_lose >= eff_sell_thresh:
                            pending_sells.append({
                                "platform": PLATFORM_GEMINI,
                                "ticker": ticker_g,
                                "title": title,
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": sell_price,
                                "rec": REC_LIKELY_LOSE,
                                "instrument_symbol": sym,
                                "gemini_evt": evt,
                            })
                            _maybe_add_reentry_order(
                                sp, min_edge, pending_orders, PLATFORM_GEMINI, ticker_g, title,
                                side, hist_p_g, impl, no_price_g, threshold_g, direction_g, pos_yf, vol_p_g,
                                maker_fee_pos_g, position_count,
                                extra={"instrument_symbol": sym, "gemini_evt": evt},
                                current_price=current_price_g, dte_days=dte_g,
                            )
                        elif not hold_skip and flip_ok:
                            pending_sells.append({
                                "platform": PLATFORM_GEMINI,
                                "ticker": ticker_g,
                                "title": title,
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": sell_price,
                                "rec": rec,
                                "instrument_symbol": sym,
                                "gemini_evt": evt,
                            })
                            _maybe_add_reentry_order(
                                sp, min_edge, pending_orders, PLATFORM_GEMINI, ticker_g, title,
                                side, hist_p_g, impl, no_price_g, threshold_g, direction_g, pos_yf, vol_p_g,
                                maker_fee_pos_g, position_count,
                                extra={"instrument_symbol": sym, "gemini_evt": evt},
                                current_price=current_price_g, dte_days=dte_g,
                            )
                        elif side == "no" and violates_g:
                            pending_sells.append({
                                "platform": PLATFORM_GEMINI,
                                "ticker": ticker_g,
                                "title": title,
                                "side": side,
                                "action": "sell",
                                "count": position_count,
                                "price": no_price_g,
                                "rec": REC_RULE_VIOLATION,
                                "instrument_symbol": sym,
                                "gemini_evt": evt,
                            })
                        else:
                            res_key_g = _extract_resolution_key_from_gemini_ticker(ticker_g)
                            if res_key_g and pos_yf:
                                key = (pos_yf, res_key_g, PLATFORM_GEMINI)
                                existing_by_underlying.setdefault(key, []).append((threshold_g, direction_g, side))
                            if record_position_evaluation:
                                record_position_evaluation(
                                    platform=PLATFORM_GEMINI, ticker=ticker_g, side=side, action="keep",
                                    hist_p=hist_p_g, vol_p=vol_p_g, p_lose=p_lose, yes_bid=impl, no_bid=no_price_g,
                                    threshold=threshold_g, direction=direction_g, yf_ticker=pos_yf, rec=rec,
                                    position_count=position_count,
                                    entry_price=latest_entry_by_key.get((PLATFORM_GEMINI, ticker_g, side)),
                                    current_price=current_side_price_g, config=cfg,
                                )
                except Exception as gex:
                    if getattr(args, "verbose", False):
                        print(f"    (Gemini position eval failed: {gex})")
        except Exception:
            pass

    trading = cfg.get("trading", {})
    max_pct = float(trading.get("max_pct_of_balance", 0.90))
    max_total_buy_pct = float(trading.get("max_total_buy_pct", 0.50))
    min_balance_fallback = int(trading.get("min_balance_for_limit_fallback_cents", 0))
    dry_run = trading.get("dry_run", True)
    force_cancel_buy_orders = bool(any_vol_spike_active and sp.cancel_on_vol_spike)
    regime_shift_active = bool(
        sp.cancel_all_on_regime_shift and (force_cancel_buy_orders or any_momentum_crash or any_defensive_mode)
    )

    if emergency_stop and pending_orders:
        blocked = len(pending_orders)
        pending_orders[:] = []
        print(f"[Risk] EMERGENCY_STOP blocked {blocked} pending BUY orders (SELL logic still active).")
        print()
    elif force_cancel_buy_orders and pending_orders:
        blocked = len(pending_orders)
        pending_orders[:] = []
        print(
            f"[Risk] Vol-spike guard blocked {blocked} pending BUY orders and will cancel resting BUY limits."
        )
        print()
    elif any_defensive_mode and pending_orders:
        blocked = len(pending_orders)
        pending_orders[:] = []
        print(
            f"[Risk] Defensive-mode guard blocked {blocked} pending BUY orders (sells-only while volatility is extreme)."
        )
        print()
    if regime_shift_active:
        print("[Risk] Regime-shift cleanup active: stale/risky resting BUY limits will be cancelled.")
        print()
    elif sp.enable_two_sided_quotes:
        before_n = len(pending_orders)
        _maybe_add_two_sided_quotes(
            pending_orders,
            quote_candidates,
            sp=sp,
            balance_cents=balance_cents,
            use_kelly=use_kelly,
            kelly_fraction=kelly_fraction_live,
            min_contracts=min_contracts,
        )
        added = len(pending_orders) - before_n
        if added > 0:
            print(f"[Strategy] Added {added} paired YES/NO quote orders around inferred fair levels.")
            print()

    # Skip BUYs that contradict kept positions or other pending BUYs (e.g. NO on SOL>$87 + YES on SOL>$93).
    # Per-platform only: cross-platform bets are allowed (e.g. NO Kalshi + YES Gemini on same underlying).
    _filter_contradictory_buys(pending_orders, existing_by_underlying)

    # Micro-balance: never place 99¢-style limits; wait for cheaper tail bids (set max_buy_limit_price: 0 in config to disable)
    _filter_pending_buys_by_max_limit_price(
        pending_orders, cap=sp.max_buy_limit_price, logger=print,
    )
    _filter_pending_buys_by_min_payout_ratio(
        pending_orders, min_ratio=sp.min_payout_ratio, logger=print,
    )

    gemini_bal_for_micro = 0
    if any(o.get("platform") == PLATFORM_GEMINI for o in pending_orders):
        try:
            gemini_bal_for_micro = int(
                GeminiTradingClient().get_balance().get("balance", 0) or 0
            )
        except Exception:
            gemini_bal_for_micro = 0
    _micro_balance_force_min_one_before_budget(
        pending_orders,
        kalshi_balance_cents=balance_cents,
        gemini_balance_cents=gemini_bal_for_micro,
        trading=trading,
        logger=print,
    )

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
    _allocate_budget_to_orders(gemini_orders, gemini_balance_cents, max_total_buy_pct, filter_zero=False)
    dust_on = bool(trading.get("dust_trade_enabled", False))
    dust_max_bc = int(trading.get("dust_trade_max_balance_cents", 0) or 0)
    if dust_on and dust_max_bc > 0:
        if balance_cents <= dust_max_bc:
            _promote_zero_count_buys_if_affordable(
                kalshi_orders,
                balance_cents=balance_cents,
                max_total_buy_pct=max_total_buy_pct,
                enabled=True,
                logger=print,
                log_tag="[DustTrade]",
            )
        if gemini_balance_cents <= dust_max_bc:
            _promote_zero_count_buys_if_affordable(
                gemini_orders,
                balance_cents=gemini_balance_cents,
                max_total_buy_pct=max_total_buy_pct,
                enabled=True,
                logger=print,
                log_tag="[DustTrade]",
            )
    if sp.force_min_buy_contracts_one:
        _promote_zero_count_buys_if_affordable(
            kalshi_orders,
            balance_cents=balance_cents,
            max_total_buy_pct=max_total_buy_pct,
            enabled=True,
            logger=print,
            log_tag="[Recovery]",
        )
        _promote_zero_count_buys_if_affordable(
            gemini_orders,
            balance_cents=gemini_balance_cents,
            max_total_buy_pct=max_total_buy_pct,
            enabled=True,
            logger=print,
            log_tag="[Recovery]",
        )
    # Keep Gemini orders with count=0 so we can re-allocate after cancels free up balance
    pending_orders[:] = [o for o in kalshi_orders if o.get("count", 0) > 0] + gemini_orders
    _cap_cross_platform_exposure(
        pending_orders,
        int(balance_cents) + int(gemini_balance_cents),
        sp.max_cross_platform_exposure,
    )

    max_entries_per_ticker = int(strategy.get("max_entries_per_ticker", 3))
    if max_entries_per_ticker > 0 and get_ticker_entry_counts and pending_orders:
        ticker_counts = get_ticker_entry_counts(cfg)
        before_cap = len(pending_orders)
        capped_tickers: list[str] = []
        kept: list[dict] = []
        for o in pending_orders:
            t = o.get("ticker", "")
            if ticker_counts.get(t, 0) >= max_entries_per_ticker:
                capped_tickers.append(t)
            else:
                kept.append(o)
        if capped_tickers:
            pending_orders[:] = kept
            unique_capped = set(capped_tickers)
            print(f"[Risk] Per-ticker cap ({max_entries_per_ticker}): blocked {len(capped_tickers)} order(s) across {len(unique_capped)} ticker(s) already at entry limit.")
            for t in sorted(unique_capped):
                print(f"         {t} ({ticker_counts.get(t, 0)} existing entries)")
            print()

    max_open_cu = int(strategy.get("max_open_contracts_per_underlying", 0) or 0)
    _apply_max_open_contracts_per_underlying(
        pending_orders,
        cfg,
        max_open_cu,
        get_unresolved_contract_sums_by_platform_yf,
        logger=print,
    )

    loss_cb_pct = float(sp.adaptive_loss_block_max_balance_loss_pct)
    block_hours = float(sp.adaptive_loss_block_lookback_hours)
    min_tr_cb = max(1, int(sp.adaptive_loss_block_min_trades))
    if loss_cb_pct > 0 and block_hours > 0 and get_recent_performance and pending_orders:
        # Independent per-venue breakers: Kalshi PnL vs Kalshi balance; Gemini PnL vs Gemini balance.
        if any(o.get("platform") == PLATFORM_KALSHI for o in pending_orders):
            k_bal = int(balance_cents)
            if k_bal > 0:
                perf_k = get_recent_performance(
                    lookback_hours=block_hours, platform=PLATFORM_KALSHI, config=cfg
                ) or {}
                total_k = int(perf_k.get("total", 0))
                pnl_k = int(perf_k.get("pnl_cents", 0) or 0)
                if total_k >= min_tr_cb and pnl_k < 0:
                    loss_k = -pnl_k
                    loss_pct_k = loss_k / float(max(1, k_bal))
                    if loss_pct_k >= loss_cb_pct:
                        nk = sum(1 for o in pending_orders if o.get("platform") == PLATFORM_KALSHI)
                        pending_orders[:] = [
                            o for o in pending_orders if o.get("platform") != PLATFORM_KALSHI
                        ]
                        print(
                            f"[Risk] Circuit breaker (Kalshi): realized loss ${loss_k / 100:.2f} in last {block_hours:.1f}h "
                            f"({loss_pct_k:.1%} of ~${k_bal / 100:.2f} Kalshi balance) ≥ {loss_cb_pct:.1%}; "
                            f"blocking {nk} Kalshi buy(s) (Gemini unchanged)."
                        )

        if any(o.get("platform") == PLATFORM_GEMINI for o in pending_orders):
            g_bal_cb = int(gemini_balance_cents or 0)
            if g_bal_cb <= 0:
                try:
                    g_bal_cb = int(
                        GeminiTradingClient().get_balance().get("balance", 0) or 0
                    )
                except Exception:
                    g_bal_cb = 0
            if g_bal_cb > 0:
                perf_g = get_recent_performance(
                    lookback_hours=block_hours, platform=PLATFORM_GEMINI, config=cfg
                ) or {}
                total_g = int(perf_g.get("total", 0))
                pnl_g = int(perf_g.get("pnl_cents", 0) or 0)
                if total_g >= min_tr_cb and pnl_g < 0:
                    loss_g = -pnl_g
                    loss_pct_g = loss_g / float(max(1, g_bal_cb))
                    if loss_pct_g >= loss_cb_pct:
                        ng = sum(1 for o in pending_orders if o.get("platform") == PLATFORM_GEMINI)
                        pending_orders[:] = [
                            o for o in pending_orders if o.get("platform") != PLATFORM_GEMINI
                        ]
                        print(
                            f"[Risk] Circuit breaker (Gemini): realized loss ${loss_g / 100:.2f} in last {block_hours:.1f}h "
                            f"({loss_pct_g:.1%} of ~${g_bal_cb / 100:.2f} Gemini balance) ≥ {loss_cb_pct:.1%}; "
                            f"blocking {ng} Gemini buy(s) (Kalshi unchanged)."
                        )

    # Proposed trades + per-trade Ollama: print each trade and Ollama's interpretation
    if pending_orders or pending_sells:
        print("\n" + "=" * 75)
        print("PROPOSED TRADES")
        print("=" * 75)
        _total_buy_cost = 0
        use_llm = getattr(args, "use_llm", True)
        if pending_sells:
            print(f"\n  --- SELLS ({len(pending_sells)}) ---")
        for i, s in enumerate(pending_sells, 1):
            _sell_total = s['count'] * s['price']
            _entry = latest_entry_by_key.get((s['platform'], s['ticker'], s['side']))
            _pnl_str = ""
            if _entry:
                _pnl = (s['price'] - _entry) * s['count']
                _pnl_str = f"  |  Est. P&L: {'+' if _pnl >= 0 else ''}${_pnl:.2f} (entry ${_entry:.2f})"
            print(f"\n  SELL {i}. [{s['platform'].upper()}] {s['ticker']}")
            print(f"         {s.get('title', '')[:65]}")
            print(f"         Side: {s['side'].upper()}  |  Count: {s['count']}  |  Price: ${s['price']:.2f}  |  Gross: ~${_sell_total:.2f}{_pnl_str}")
            print(f"         Trigger: {s.get('rec', 'unknown')}")
            if use_llm:
                analysis = analyze_single_trade(s, "sell")
                if analysis:
                    print(f"         [Ollama] {analysis}")
        if pending_orders:
            print(f"\n  --- BUYS ({len(pending_orders)} proposed; {sum(1 for o in pending_orders if int(o.get('count', 0) or 0) > 0)} with count > 0) ---")
        buys_printable = [o for o in pending_orders if int(o.get("count", 0) or 0) > 0]
        buys_zero = [o for o in pending_orders if int(o.get("count", 0) or 0) <= 0]
        for i, o in enumerate(buys_printable, 1):
            _count = int(o.get("count", 0) or 0)
            _cost = _count * o["price"]
            _total_buy_cost += _cost
            _p_win = o.get("hist_p", 0) if o.get("rec") == REC_BUY_YES else (1 - o.get("hist_p", 1))
            _ev = _p_win * (1.0 - o["price"]) - (1 - _p_win) * o["price"]
            _thresh = o.get("threshold")
            _dir = o.get("direction", "")
            _dte_o = o.get("dte")
            _side = o.get("side", "")
            _bet_dir = _dir if _side == "yes" else ("below" if _dir == "above" else "above") if _dir else ""
            print(f"\n  BUY {i}. [{o['platform'].upper()}] {o['ticker']}")
            print(f"         {(o.get('title') or '')[:65]}")
            if _thresh and _bet_dir:
                print(f"         Bet: price {_bet_dir} {_fmt_price(_thresh)}  |  Resolves in: {_fmt_dte(_dte_o)}")
            print(f"         Side: {o['rec'].replace('BUY_', '')}  |  Count: {_count}  |  Limit: ${o['price']:.2f}  |  Total cost: ${_cost:.2f}")
            print(f"         Edge: {o['edge']:.1%}  |  P(win): {_p_win:.1%}  |  EV/contract: {'+' if _ev >= 0 else ''}${_ev:.2f}")
            if use_llm:
                analysis = analyze_single_trade(o, "buy")
                if analysis:
                    print(f"         [Ollama] {analysis}")
        if buys_zero:
            for o in buys_zero:
                print(
                    f"\n  BUY (deferred) [{str(o.get('platform') or '').upper()}] {o.get('ticker')} "
                    f"@ ${float(o.get('price', 0)):.2f} — Count: 0 after budget cap (insufficient balance for ≥1 contract; "
                    f"may re-size after --execute sync/cancels)"
                )
        print()
        if buys_printable:
            print(f"  Total buy cost: ${_total_buy_cost:.2f} across {len(buys_printable)} funded order(s)")
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
        # Always try to create Gemini client when executing — review/cancel stale orders even if no new Gemini orders
        try:
            gemini_trading = GeminiTradingClient()
        except ValueError:
            pass  # No API keys; skip Gemini (no message unless we had pending Gemini items)
        if gemini_trading is None and (any(o["platform"] == PLATFORM_GEMINI for o in pending_orders) or any(s["platform"] == PLATFORM_GEMINI for s in pending_sells)):
            print("  (Gemini: GEMINI_API_KEY/GEMINI_API_SECRET not set, skipping Gemini orders)")

        sync_result = sync_and_cancel_resting_orders(
            kalshi=kalshi,
            gemini_trading=gemini_trading,
            pending_orders=pending_orders,
            pending_sells=pending_sells,
            regime_shift_active=regime_shift_active,
            limit_order_ttl_minutes=sp.limit_order_ttl_minutes,
            passive_bid_chase_enabled=sp.passive_bid_chase_enabled,
            passive_bid_chase_refresh_minutes=sp.passive_bid_chase_refresh_minutes,
            resting_cancel_if_market_bid_above_limit_by=sp.resting_cancel_if_market_bid_above_limit_by,
            platform_kalshi=PLATFORM_KALSHI,
            platform_gemini=PLATFORM_GEMINI,
            gemini_events_by_asset=gemini_events_by_asset,
            find_gemini_event=_find_gemini_event,
            get_instrument_symbol=get_instrument_symbol,
            record_order_cancel=record_order_cancel,
            mark_prediction_cancelled=mark_prediction_cancelled,
            cfg=cfg,
            logger=print,
        )
        kalshi_keep_buys = sync_result.kalshi_keep_buys
        kalshi_keep_sells = sync_result.kalshi_keep_sells
        gemini_keep_buys = sync_result.gemini_keep_buys
        gemini_keep_sells = sync_result.gemini_keep_sells
        if sync_result.cancels_done:
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
                        if record_prediction_early_exit:
                            sell_rate = _maker_fee_rate_for_market(
                                sp, PLATFORM_KALSHI, str(s.get("yf_ticker") or "")
                            )
                            record_prediction_early_exit(
                                platform=PLATFORM_KALSHI,
                                ticker=str(s["ticker"]),
                                side=str(s["side"]),
                                sell_price=float(s["price"]),
                                count_sold=int(s.get("count") or 0),
                                maker_fee_rate=float(sell_rate),
                                min_fee_per_order=float(sp.min_fee_per_order),
                                exit_reason=str(s.get("rec") or "early_sell"),
                                config=cfg,
                            )
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
                        if record_prediction_early_exit:
                            sell_rate = _maker_fee_rate_for_market(
                                sp, PLATFORM_GEMINI, str(s.get("yf_ticker") or "")
                            )
                            record_prediction_early_exit(
                                platform=PLATFORM_GEMINI,
                                ticker=str(s["ticker"]),
                                side=str(s["side"]),
                                sell_price=float(s["price"]),
                                count_sold=int(s.get("count") or 0),
                                maker_fee_rate=float(sell_rate),
                                min_fee_per_order=float(sp.min_fee_per_order),
                                exit_reason=str(s.get("rec") or "early_sell"),
                                config=cfg,
                            )
                    else:
                        print(f"  (skipped SELL (Gemini) {s['ticker']} - balance/API constraint)")

        # Re-allocate Gemini budget after cancels+sells — freed balance may now allow buys
        gemini_orders_in_pending = [o for o in pending_orders if o.get("platform") == PLATFORM_GEMINI]
        gemini_balance_after = 0  # Used by _apply_inter_arb_group_preflight; 0 when no Gemini
        if gemini_trading and gemini_orders_in_pending:
            try:
                gemini_balance_after = int(gemini_trading.get_balance().get("balance", 0) or 0)
                _allocate_budget_to_orders(
                    gemini_orders_in_pending, gemini_balance_after, max_total_buy_pct, filter_zero=False
                )
                _cap_cross_platform_exposure(
                    pending_orders,
                    int(balance_cents) + int(gemini_balance_after),
                    sp.max_cross_platform_exposure,
                )
            except Exception:
                pass

        # Inter-exchange ARB safety: only keep groups that can execute both legs together.
        _apply_inter_arb_group_preflight(
            pending_orders,
            sp=sp,
            kalshi_balance_cents=balance_cents,
            gemini_balance_cents=gemini_balance_after,
            max_pct_of_balance=max_pct,
            gemini_ready=bool(gemini_trading),
            logger=print,
        )

        # Then BUY — balance now reflects sell proceeds
        for o in pending_orders:
            if o["platform"] == PLATFORM_KALSHI:
                if (o["ticker"], o["side"]) in kalshi_keep_buys:
                    print(f"  (skip BUY {o['ticker']} — already have open limit order)")
                    continue
                cached = market_data_cache.get(o["ticker"])
                price_ranges = cached[3].get("price_ranges") if cached and len(cached) > 3 else None
                price_str = f"{o['price']:.4f}"  # round_price_to_tick in create_order_safe
                count = int(o.get("count", 10) or 0)
                if count <= 0:
                    continue
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
                        ord_obj = result.get("order", result)
                        oid = ord_obj.get("order_id") if isinstance(ord_obj, dict) else None
                        if record_limit_order and oid:
                            record_limit_order(
                                platform=PLATFORM_KALSHI,
                                ticker=o["ticker"],
                                order_id=oid,
                                side=o["side"],
                                action="buy",
                                limit_price=o["price"],
                                bid_at_place=o.get("bid_at_place"),
                                dip_pct_used=o.get("dip_pct_used"),
                                hist_p=o.get("hist_p"),
                                vol_p=o.get("vol_p"),
                                edge=o.get("edge"),
                                rec=o.get("rec"),
                                count=count,
                                yf_ticker=o.get("yf_ticker"),
                                threshold=o.get("threshold"),
                                direction=o.get("direction"),
                                config=cfg,
                            )
                    else:
                        print(f"  (skipped BUY {o['ticker']} - balance/API constraint)")
                except Exception as ex:
                    print(f"  BUY {o['ticker']} failed: {ex}")
            else:
                count = o.get("count", 10)
                if count <= 0:
                    continue
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
                        min_balance_for_limit_fallback_cents=min_balance_fallback,
                    )
                    if result:
                        print(f"  Placed BUY {o['side'].upper()} (Gemini) {o['ticker']} x {count}")
                        buys_placed += 1
                        oid = str(result.get("orderId") or result.get("order_id", "")) if isinstance(result, dict) else None
                        if record_limit_order and oid:
                            record_limit_order(
                                platform=PLATFORM_GEMINI,
                                ticker=o["ticker"],
                                order_id=oid,
                                side=o["side"],
                                action="buy",
                                limit_price=o["price"],
                                bid_at_place=o.get("bid_at_place"),
                                dip_pct_used=o.get("dip_pct_used"),
                                hist_p=o.get("hist_p"),
                                vol_p=o.get("vol_p"),
                                edge=o.get("edge"),
                                rec=o.get("rec"),
                                count=count,
                                yf_ticker=o.get("yf_ticker"),
                                threshold=o.get("threshold"),
                                direction=o.get("direction"),
                                config=cfg,
                            )
                    else:
                        _gem_err = getattr(gemini_trading, "last_place_order_error", None) or "balance/API constraint"
                        print(f"  (skipped BUY (Gemini) {o['ticker']} - {_gem_err})")
                elif sym and not gemini_trading:
                    print(f"  (Gemini {o['ticker']}: GEMINI_API_KEY/SECRET not set - skip)")

        print("\n" + "=" * 75)
        print(f"Live run complete. {buys_placed} buys, {sells_placed} sells placed.")
        print("=" * 75)
    elif getattr(args, "preview", False) and (pending_orders or pending_sells):
        print("\n" + "=" * 75)
        print("PREVIEW: Orders that would be placed (--execute to go live)")
        print("=" * 75)

        if pending_sells:
            print(f"\n  --- SELLS ({len(pending_sells)}) — executed first so balance reflects proceeds ---")
        for i, s in enumerate(pending_sells, 1):
            _sell_total = s['count'] * s['price']
            _entry = latest_entry_by_key.get((s['platform'], s['ticker'], s['side']))
            _pnl_str = ""
            if _entry:
                _pnl = (s['price'] - _entry) * s['count']
                _pnl_str = f"  |  Est. P&L: {'+' if _pnl >= 0 else ''}${_pnl:.2f} (entry ${_entry:.2f})"
            print(f"\n  SELL {i}. [{s['platform'].upper()}] {s['ticker']}")
            print(f"         {s['title']}")
            print(f"         Side: {s['side'].upper()}  |  Count: {s['count']}  |  Price: ${s['price']:.2f}  |  Gross: ~${_sell_total:.2f}{_pnl_str}")
            print(f"         Trigger: {s['rec']}")
            if s["platform"] == PLATFORM_KALSHI:
                payload = {
                    "ticker": s["ticker"],
                    "side": s["side"],
                    "action": "sell",
                    "count": s["count"],
                }
                payload["yes_price_dollars" if s["side"] == "yes" else "no_price_dollars"] = f"{s['price']:.2f}"
                print(f"         API: POST /portfolio/orders → {payload}")
            elif s["platform"] == PLATFORM_GEMINI:
                print(f"         API: POST /v1/prediction-markets/order (side=sell)")

        if pending_orders:
            print(f"\n  --- BUYS ({len(pending_orders)}) ---")
        for i, o in enumerate(pending_orders, 1):
            _count = o.get("count", 10)
            _cost = _count * o["price"]
            _p_win_prev = o.get("hist_p", 0) if o.get("rec") == REC_BUY_YES else (1 - o.get("hist_p", 1))
            _thresh = o.get("threshold")
            _dir = o.get("direction", "")
            _dte_prev = o.get("dte")
            _side_prev = o.get("side", "")
            _bet_dir_prev = _dir if _side_prev == "yes" else ("below" if _dir == "above" else "above") if _dir else ""
            print(f"\n  BUY {i}. [{o['platform'].upper()}] {o['ticker']}")
            print(f"         {(o.get('title') or '')[:65]}")
            if _thresh and _bet_dir_prev:
                print(f"         Bet: price {_bet_dir_prev} {_fmt_price(_thresh)}  |  Resolves in: {_fmt_dte(_dte_prev)}")
            print(f"         Side: {o['rec'].replace('BUY_', '')}  |  Count: {_count}  |  Limit: ${o['price']:.2f}  |  Total cost: ${_cost:.2f}")
            print(f"         Edge: {o['edge']:.1%}  |  P(win): {_p_win_prev:.1%}")
            if o["platform"] == PLATFORM_KALSHI:
                side, action = o["side"], "buy"
                price_str = f"{o['price']:.2f}"
                count = o.get("count", 10)
                payload = {"ticker": o["ticker"], "side": side, "action": action, "count": count}
                if side == "yes":
                    payload["yes_price_dollars"] = price_str
                else:
                    payload["no_price_dollars"] = price_str
                print(f"         API: POST /portfolio/orders → {payload}")
            else:
                count = o.get("count", 10)
                if count <= 0:
                    continue
                sym = o.get("instrument_symbol") or (
                    get_instrument_symbol(
                        o.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, o["ticker"]),
                        o["side"],
                    )
                    if o.get("gemini_evt") or _find_gemini_event(gemini_events_by_asset, o["ticker"])
                    else None
                )
                if sym:
                    print(f"         API: POST /v1/prediction-markets/order → symbol={sym}, side=buy, outcome={o['side']}, qty={count}, price={o['price']:.2f}")
                else:
                    print(f"         Would: {o['rec']} at {o['price']:.2f} (side={o['side']}, count={count})")

        print("\n" + "=" * 75)

    print("\n" + "=" * 75)
    print("  RUN SUMMARY")
    print("=" * 75)
    print(f"  Markets evaluated:   {_eval_count_total}")
    print(f"  Recommendations:     {_eval_count_buy} BUY  |  {_eval_count_skip} SKIP")
    print(f"  Proposed buys:       {len(pending_orders)}")
    print(f"  Proposed sells:      {len(pending_sells)}")
    if pending_sells and not (getattr(args, "preview", False) or getattr(args, "execute", False)):
        print(f"\n  Note: {len(pending_sells)} position(s) look like losers.")
        print("  Run with --preview to see details, --execute to sell.")
    if getattr(args, "execute", False):
        print("\n  Run with --execute for future live runs (or set up cron).")
    else:
        if not getattr(args, "preview", False):
            print("\n  Run every 15 min (cron) for continuous evaluation. See README.")
            if not pending_sells:
                print("  Use --preview to see what orders would be placed.")
    print("=" * 75)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
