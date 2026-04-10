"""
Strategy Configuration: Definitions and loading logic for trading strategy parameters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.anti_hallucination import AntiHallucinationConfig, load_anti_hallucination_config
from models.alpha_engine import AlphaEngineConfig, load_alpha_engine_config


@dataclass
class StrategyParams:
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
    max_pct_of_book_volume: float
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
    kelly_fraction: float
    max_yes_price_for_buy: float
    max_buy_limit_price: float
    force_min_buy_contracts_one: bool
    min_payout_ratio: float
    dte_edge_scaling_enabled: bool
    dte_edge_short_threshold_days: float
    dte_edge_long_threshold_days: float
    dte_edge_mult_short: float
    dte_edge_mult_long: float
    max_new_buy_dte_days: float
    passive_bid_chase_enabled: bool
    taker_edge_threshold: float
    ultra_short_k_spread: float
    ultra_short_latency_risk: float
    passive_bid_chase_refresh_minutes: int
    market_prob_lo: float
    market_prob_hi: float
    max_contracts_per_underlying: int
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
    calibration_loss_weight: float
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
    position_flip_sell_min_negative_edge: float
    position_loser_sell_p_lose_margin: float
    hold_winner_to_expiry_enabled: bool
    hold_winner_min_spot_vs_strike_pct: float
    hold_winner_max_dte_days: float
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
    stop_loss_min_model_conf: float
    stop_loss_spot_itm_min_pct: float
    stop_loss_book_persist_seconds: float
    stop_loss_near_expiry_within_hours: float
    stop_loss_near_expiry_extra_pct: float
    stop_loss_near_expiry_persist_mult: float
    stop_loss_near_expiry_hard_disable: bool
    stop_loss_gradient_max_extra_pct: float
    stop_loss_gradient_per_edge: float
    weekend_edge_multiplier: float
    enable_momentum_protection: bool
    momentum_crash_threshold_pct: float
    max_cross_platform_exposure: float
    max_bankroll_fraction_per_underlying: float
    max_bankroll_fraction_per_direction: float
    max_daily_loss_pct: float
    max_daily_loss_cents: int
    max_trade_cents: float
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
    force_exploration_reduction: float
    force_exploration_hours: float
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
    tail_cutoff_min_p: float
    vol_model_zero_drift: bool
    position_exit_min_net_edge: float
    position_min_hold_minutes: float
    position_exit_strong_entry_edge: float
    position_exit_strong_entry_floor: float
    position_exit_stale_minutes: float
    position_exit_stale_edge_floor: float
    skip_new_buy_when_unresolved_same_side: bool
    starting_balance_cents: int
    balance_regime_medium_ratio: float
    balance_regime_low_ratio: float
    balance_regime_medium_kelly_mult: float
    balance_regime_low_kelly_mult: float
    balance_regime_medium_min_edge_add: float
    balance_regime_low_min_edge_add: float
    min_bet_size_bankroll_pct: float
    low_balance_min_price: float
    drawdown_stop_trading_pct: float
    drawdown_cooldown_hours: float
    limit_buy_edge_aggression_enabled: bool
    limit_buy_cross_spread_min_edge: float
    limit_buy_near_market_min_edge: float
    limit_buy_passive_only_below_edge: float
    max_spread_pct: float
    stop_loss_cooldown_hours: float
    tiered_min_edge_enabled: bool
    tiered_min_edge_bands: list[dict[str, float]]
    limit_buy_ev_tier_pricing: bool
    limit_buy_weak_bid_min_edge: float
    limit_buy_skip_below_weak_edge: bool
    limit_buy_medium_quote_mode: str
    limit_buy_medium_spread_fraction: float
    no_trade_zone_width: float
    buy_no_extra_min_edge: float
    limit_buy_escalation_price_enabled: bool
    limit_buy_escalate_after_minutes_1: float
    limit_buy_escalate_after_minutes_2: float
    limit_buy_escalation_cancel_minutes: int
    limit_buy_wide_book_coalesce_enabled: bool
    limit_buy_wide_book_spread_rel: float
    limit_buy_wide_book_anchor_fraction: float
    limit_buy_wide_book_conservative_substrings: list[str]
    limit_buy_wide_book_conservative_yf_tickers: list[str]
    resting_cancel_if_market_bid_above_limit_by: float
    simple_ev_decision_enabled: bool
    simple_ev_min_edge: float
    simple_ev_min_edge_full: float
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
    simple_ev_edge_size_boost_edge: float
    simple_ev_edge_size_boost_mult: float
    simple_ev_skip_p_win_mid_enabled: bool
    simple_ev_skip_p_win_mid_lo: float
    simple_ev_skip_p_win_mid_hi: float
    simple_ev_cheap_band_lo: float
    simple_ev_cheap_band_hi: float
    simple_ev_cheap_band_min_net_edge: float
    log_effective_min_edge: bool
    log_decision_diagnostics: bool
    filter_disjoint_underlying_buys: bool
    empirical_multi_horizon_enabled: bool
    empirical_horizon_short_days: int
    empirical_horizon_medium_days: int
    empirical_horizon_long_days: int
    empirical_horizon_w_short: float
    empirical_horizon_w_medium: float
    empirical_horizon_w_long: float
    dynamic_kill_switch_enabled: bool
    dynamic_kill_oil_series_prefix: str
    dynamic_kill_oil_block_buy_no_uptrend: bool
    dynamic_kill_series_pnl_lookback_hours: float
    dynamic_kill_pnl_cents_floor: int
    dynamic_kill_min_trades: int
    dynamic_kill_poor_pnl_size_mult: float
    stop_loss_regime_persist_enabled: bool
    stop_loss_book_persist_counter_trend_seconds: float
    stop_loss_book_persist_trend_aligned_seconds: float
    adaptive_edge_confidence_t_stat: float
    ah_cfg: AntiHallucinationConfig = None  # type: ignore[assignment]
    ae_cfg: AlphaEngineConfig = None  # type: ignore[assignment]


def sanitize_model_probability(p: float, *, lo: float, hi: float, enabled: bool) -> float:
    """Hard cap model P so Kelly / edge never assume certainty (e.g. vol model at 100%)."""
    if not enabled:
        return p
    x = float(p)
    lo_f = max(1e-6, min(0.99, float(lo)))
    hi_f = max(0.01, min(0.999, float(hi)))
    if lo_f >= hi_f:
        return x
    return max(lo_f, min(hi_f, x))


def display_model_probability(p: float | None, sp: StrategyParams) -> float | None:
    """Log-friendly P: same hard cap as sizing so logs don't show misleading '100%'."""
    if p is None:
        return None
    return sanitize_model_probability(
        float(p),
        lo=sp.model_prob_hard_lo,
        hi=sp.model_prob_hard_hi,
        enabled=sp.enable_model_prob_hard_cap,
    )


def apply_recovery_overrides(cfg: dict) -> dict:
    """
    Merge optional top-level `recovery:` into a copy of strategy (recovery injector / micro-balance).
    Keys: min_edge, adaptive_edge_max_extra, loss_streak_max_extra, force_min_buy_contracts_one,
    calibration_extra_edge_overall, calibration_extra_edge_above_yes (BUY YES above-X downgrade gate),
    joint_high_pull_to_half / joint_high_confidence_enabled,
    calibration_hard_block_above_yes_win_rate / calibration_hard_block_above_yes_min_avg_entry,
    parity_penalty_enabled, uncertainty_edge_mult,
    vol_disagree_extra_edge, max_buy_limit_price (0 = disable cap on bought-leg limit),
    enable_vol_regime_scaling, dte_edge_scaling_enabled, ultra_short_min_edge_mult (1.0 = no extra on 15m).
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


def merge_execution_into_strategy(cfg: dict) -> dict:
    """
    Apply optional top-level `execution:` overrides into a copy of `strategy:`.
    - price_strategy: passive → mid or bid+1¢ from limit_offset_cents (spread-aware maker quotes)
    - aggressive → restore deep dip style (passive_limit_quote=dip)
    """
    strategy = dict(cfg.get("strategy") or {})
    ex = cfg.get("execution") or {}
    if not ex:
        return strategy
    # Optional numeric/bool execution knobs merged into strategy for load_strategy_params
    _ex_float = (
        "resting_cancel_if_market_bid_above_limit_by",
        "position_exit_min_net_edge",
        "position_min_hold_minutes",
        "position_exit_strong_entry_edge",
        "position_exit_strong_entry_floor",
        "position_exit_stale_minutes",
        "position_exit_stale_edge_floor",
        "limit_buy_cross_spread_min_edge",
        "limit_buy_near_market_min_edge",
        "limit_buy_passive_only_below_edge",
        "limit_buy_weak_bid_min_edge",
        "limit_buy_medium_spread_fraction",
        "limit_buy_escalate_after_minutes_1",
        "limit_buy_escalate_after_minutes_2",
        "limit_buy_wide_book_spread_rel",
        "limit_buy_wide_book_anchor_fraction",
    )
    for k in _ex_float:
        if ex.get(k) is not None:
            try:
                strategy[k] = float(ex[k])
            except (TypeError, ValueError):
                pass
    if ex.get("limit_buy_escalation_cancel_minutes") is not None:
        try:
            strategy["limit_buy_escalation_cancel_minutes"] = max(0, int(ex["limit_buy_escalation_cancel_minutes"]))
        except (TypeError, ValueError):
            pass
    if ex.get("limit_buy_medium_quote_mode") is not None:
        strategy["limit_buy_medium_quote_mode"] = str(ex["limit_buy_medium_quote_mode"]).strip().lower()
    for k in (
        "limit_buy_edge_aggression_enabled",
        "skip_new_buy_when_unresolved_same_side",
        "limit_buy_ev_tier_pricing",
        "limit_buy_skip_below_weak_edge",
        "limit_buy_escalation_price_enabled",
        "limit_buy_wide_book_coalesce_enabled",
    ):
        if ex.get(k) is not None:
            strategy[k] = bool(ex[k])
    for _lk in (
        "limit_buy_wide_book_conservative_substrings",
        "limit_buy_wide_book_conservative_yf_tickers",
    ):
        if ex.get(_lk) is not None and isinstance(ex[_lk], list):
            strategy[_lk] = [str(x).strip() for x in ex[_lk] if str(x).strip()]
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


def get_effective_strategy_dict(cfg: dict) -> dict:
    """strategy.yaml + optional `recovery:` + optional `execution:` merges."""
    base = apply_recovery_overrides(cfg)
    return merge_execution_into_strategy({**cfg, "strategy": base})


def get_strategy_recommend_extras(sp: StrategyParams) -> dict[str, Any]:
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


def get_strategy_recommend_strict_extras(sp: StrategyParams) -> dict[str, Any]:
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
    out.update(get_strategy_recommend_extras(sp))
    return out


def get_effective_passive_limit_quote(sp: StrategyParams, dte_days: float | None) -> str:
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


def load_strategy_params(strategy: dict) -> StrategyParams:
    return StrategyParams(
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
        kelly_fraction=float(strategy.get("kelly_fraction", 0.25)),
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
        taker_edge_threshold=float(strategy.get("taker_edge_threshold", 0.35)),
        ultra_short_k_spread=float(strategy.get("ultra_short_k_spread", 0.4)),
        ultra_short_latency_risk=float(strategy.get("ultra_short_latency_risk", 0.01)),
        passive_bid_chase_refresh_minutes=max(0, int(strategy.get("passive_bid_chase_refresh_minutes", 0) or 0)),
        market_prob_lo=float(strategy.get("market_prob_lo", 0.10)),
        market_prob_hi=float(strategy.get("market_prob_hi", 0.90)),
        max_contracts_per_underlying=int(strategy.get("max_contracts_per_underlying", 5)),
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
        calibration_loss_weight=float(strategy.get("calibration_loss_weight", 1.0)),
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
        stop_loss_min_model_conf=float(strategy.get("stop_loss_min_model_conf", 1.0)),
        stop_loss_spot_itm_min_pct=max(0.0, float(strategy.get("stop_loss_spot_itm_min_pct", 0.01))),
        stop_loss_book_persist_seconds=max(0.0, float(strategy.get("stop_loss_book_persist_seconds", 60.0))),
        stop_loss_near_expiry_within_hours=max(
            0.0,
            float(
                strategy["stop_loss_near_expiry_within_hours"]
                if "stop_loss_near_expiry_within_hours" in strategy
                else strategy.get("stop_loss_disable_within_hours", 0.0)
            ),
        ),
        stop_loss_near_expiry_extra_pct=max(0.0, float(strategy.get("stop_loss_near_expiry_extra_pct", 0.12))),
        stop_loss_near_expiry_persist_mult=max(0.0, float(strategy.get("stop_loss_near_expiry_persist_mult", 2.0))),
        stop_loss_near_expiry_hard_disable=bool(strategy.get("stop_loss_near_expiry_hard_disable", False)),
        stop_loss_gradient_max_extra_pct=max(0.0, float(strategy.get("stop_loss_gradient_max_extra_pct", 0.25))),
        stop_loss_gradient_per_edge=max(0.0, float(strategy.get("stop_loss_gradient_per_edge", 10.0))),
        weekend_edge_multiplier=float(strategy.get("weekend_edge_multiplier", 1.0)),
        enable_momentum_protection=bool(strategy.get("enable_momentum_protection", False)),
        momentum_crash_threshold_pct=float(strategy.get("momentum_crash_threshold_pct", -0.02)),
        max_cross_platform_exposure=float(strategy.get("max_cross_platform_exposure", 0.30)),
        max_bankroll_fraction_per_underlying=max(
            0.0, min(1.0, float(strategy.get("max_bankroll_fraction_per_underlying", 0.0)))
        ),
        max_bankroll_fraction_per_direction=max(
            0.0, min(1.0, float(strategy.get("max_bankroll_fraction_per_direction", 0.0)))
        ),
        max_daily_loss_pct=float(strategy.get("max_daily_loss_pct", 0.10)),
        max_daily_loss_cents=int(strategy.get("max_daily_loss_cents", 0)),
        max_trade_cents=float(strategy.get("max_trade_cents", 5000.0)),
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
        force_exploration_reduction=max(0.0, min(1.0, float(strategy.get("force_exploration_reduction", 0.0)))),
        force_exploration_hours=max(0.0, float(strategy.get("force_exploration_hours", 0.0))),
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
        tail_cutoff_min_p=max(0.0, min(0.5, float(strategy.get("tail_cutoff_min_p", 0.0)))),
        vol_model_zero_drift=bool(strategy.get("vol_model_zero_drift", False)),
        position_exit_min_net_edge=max(-0.5, min(0.5, float(strategy.get("position_exit_min_net_edge", 0.0)))),
        position_min_hold_minutes=max(0.0, float(strategy.get("position_min_hold_minutes", 0.0))),
        position_exit_strong_entry_edge=max(0.0, min(1.0, float(strategy.get("position_exit_strong_entry_edge", 0.0)))),
        position_exit_strong_entry_floor=max(-0.5, min(0.0, float(strategy.get("position_exit_strong_entry_floor", 0.0)))),
        position_exit_stale_minutes=max(0.0, float(strategy.get("position_exit_stale_minutes", 0.0))),
        position_exit_stale_edge_floor=max(-0.5, min(0.5, float(strategy.get("position_exit_stale_edge_floor", 0.0)))),
        skip_new_buy_when_unresolved_same_side=bool(strategy.get("skip_new_buy_when_unresolved_same_side", False)),
        starting_balance_cents=int(strategy.get("starting_balance_cents", 100_00)),
        balance_regime_medium_ratio=float(strategy.get("balance_regime_medium_ratio", 0.5)),
        balance_regime_low_ratio=float(strategy.get("balance_regime_low_ratio", 0.3)),
        balance_regime_medium_kelly_mult=float(strategy.get("balance_regime_medium_kelly_mult", 0.5)),
        balance_regime_low_kelly_mult=float(strategy.get("balance_regime_low_kelly_mult", 0.1)),
        balance_regime_medium_min_edge_add=float(strategy.get("balance_regime_medium_min_edge_add", 0.05)),
        balance_regime_low_min_edge_add=float(strategy.get("balance_regime_low_min_edge_add", 0.10)),
        min_bet_size_bankroll_pct=float(strategy.get("min_bet_size_bankroll_pct", 0.005)),
        low_balance_min_price=float(strategy.get("low_balance_min_price", 0.05)),
        drawdown_stop_trading_pct=float(strategy.get("drawdown_stop_trading_pct", 0.10)),
        drawdown_cooldown_hours=float(strategy.get("drawdown_cooldown_hours", 24.0)),
        limit_buy_edge_aggression_enabled=bool(strategy.get("limit_buy_edge_aggression_enabled", False)),
        limit_buy_cross_spread_min_edge=max(0.0, min(0.95, float(strategy.get("limit_buy_cross_spread_min_edge", 0.10)))),
        limit_buy_near_market_min_edge=max(0.0, min(0.95, float(strategy.get("limit_buy_near_market_min_edge", 0.05)))),
        limit_buy_passive_only_below_edge=max(0.0, min(0.95, float(strategy.get("limit_buy_passive_only_below_edge", 0.0)))),
        max_spread_pct=max(0.01, min(1.0, float(strategy.get("max_spread_pct", 0.20)))),
        stop_loss_cooldown_hours=max(0.0, float(strategy.get("stop_loss_cooldown_hours", 0.0))),
        tiered_min_edge_enabled=bool((strategy.get("tiered_min_edge") or {}).get("enabled", False)),
        tiered_min_edge_bands=(strategy.get("tiered_min_edge") or {}).get("bands", []),
        limit_buy_ev_tier_pricing=bool(strategy.get("limit_buy_ev_tier_pricing", True)),
        limit_buy_weak_bid_min_edge=max(0.0, min(0.95, float(strategy.get("limit_buy_weak_bid_min_edge", 0.03)))),
        limit_buy_skip_below_weak_edge=bool(strategy.get("limit_buy_skip_below_weak_edge", True)),
        limit_buy_medium_quote_mode=str(strategy.get("limit_buy_medium_quote_mode", "spread_fraction")).strip().lower(),
        limit_buy_medium_spread_fraction=max(0.0, min(1.0, float(strategy.get("limit_buy_medium_spread_fraction", 0.25)))),
        no_trade_zone_width=max(0.0, min(0.5, float(strategy.get("no_trade_zone_width", 0.0)))),
        buy_no_extra_min_edge=max(0.0, min(0.5, float(strategy.get("buy_no_extra_min_edge", 0.0)))),
        limit_buy_escalation_price_enabled=bool(strategy.get("limit_buy_escalation_price_enabled", True)),
        limit_buy_escalate_after_minutes_1=max(0.0, float(strategy.get("limit_buy_escalate_after_minutes_1", 5.0))),
        limit_buy_escalate_after_minutes_2=max(0.0, float(strategy.get("limit_buy_escalate_after_minutes_2", 10.0))),
        limit_buy_escalation_cancel_minutes=max(0, int(strategy.get("limit_buy_escalation_cancel_minutes", 0))),
        limit_buy_wide_book_coalesce_enabled=bool(strategy.get("limit_buy_wide_book_coalesce_enabled", False)),
        limit_buy_wide_book_spread_rel=max(0.0, min(2.0, float(strategy.get("limit_buy_wide_book_spread_rel", 0.22)))),
        limit_buy_wide_book_anchor_fraction=max(0.0, min(1.0, float(strategy.get("limit_buy_wide_book_anchor_fraction", 0.38)))),
        limit_buy_wide_book_conservative_substrings=(
            [str(x).strip() for x in strategy["limit_buy_wide_book_conservative_substrings"] if str(x).strip()]
            if isinstance(strategy.get("limit_buy_wide_book_conservative_substrings"), list)
            else []
        ),
        limit_buy_wide_book_conservative_yf_tickers=(
            [str(x).strip() for x in strategy["limit_buy_wide_book_conservative_yf_tickers"] if str(x).strip()]
            if isinstance(strategy.get("limit_buy_wide_book_conservative_yf_tickers"), list)
            else []
        ),
        resting_cancel_if_market_bid_above_limit_by=max(0.0, min(0.5, float(strategy.get("resting_cancel_if_market_bid_above_limit_by", 0.0)))),
        simple_ev_decision_enabled=bool((strategy.get("simple_ev_decision") or {}).get("enabled", False)),
        simple_ev_min_edge=max(0.0, min(0.5, float((strategy.get("simple_ev_decision") or {}).get("min_edge", 0.03)))),
        simple_ev_min_edge_full=max(0.0, min(0.5, float((strategy.get("simple_ev_decision") or {}).get("min_edge_full", 0.08)))),
        simple_ev_kelly_dampen=max(0.001, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("kelly_dampen", 0.25)))),
        adaptive_edge_confidence_t_stat=max(0.0, float(strategy.get("adaptive_edge_confidence_t_stat", 1.5))),
        simple_ev_max_bankroll_fraction=max(0.001, min(0.25, float((strategy.get("simple_ev_decision") or {}).get("max_bankroll_fraction", 0.02)))),
        simple_ev_spread_wide_fraction=max(0.0, min(0.5, float((strategy.get("simple_ev_decision") or {}).get("spread_wide_fraction", 0.05)))),
        simple_ev_spread_wide_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("spread_wide_size_mult", 0.5)))),
        simple_ev_emp_vol_disagree_threshold=max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("emp_vol_disagree_threshold", 0.15)))),
        simple_ev_disagree_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("disagree_size_mult", 0.5)))),
        simple_ev_calibration_wr_threshold=max(0.0, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("calibration_win_rate_threshold", 0.40)))),
        simple_ev_calibration_poor_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("calibration_poor_size_mult", 0.7)))),
        simple_ev_min_bid_size=max(0.0, float((strategy.get("simple_ev_decision") or {}).get("min_bid_size", 0.0))),
        simple_ev_ultra_short_size_mult=max(0.05, min(1.0, float((strategy.get("simple_ev_decision") or {}).get("ultra_short_size_mult", 1.0)))),
        simple_ev_edge_size_boost_edge=max(
            0.0,
            min(0.5, float((strategy.get("simple_ev_decision") or {}).get("edge_size_boost_edge", 0.0))),
        ),
        simple_ev_edge_size_boost_mult=max(
            1.0,
            min(2.5, float((strategy.get("simple_ev_decision") or {}).get("edge_size_boost_mult", 1.0))),
        ),
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
        filter_disjoint_underlying_buys=bool(strategy.get("filter_disjoint_underlying_buys", False)),
        empirical_multi_horizon_enabled=bool(strategy.get("empirical_multi_horizon_enabled", False)),
        empirical_horizon_short_days=max(5, int(strategy.get("empirical_horizon_short_days", 21))),
        empirical_horizon_medium_days=max(5, int(strategy.get("empirical_horizon_medium_days", 90))),
        empirical_horizon_long_days=max(5, int(strategy.get("empirical_horizon_long_days", 365))),
        empirical_horizon_w_short=max(0.0, float(strategy.get("empirical_horizon_w_short", 0.5))),
        empirical_horizon_w_medium=max(0.0, float(strategy.get("empirical_horizon_w_medium", 0.3))),
        empirical_horizon_w_long=max(0.0, float(strategy.get("empirical_horizon_w_long", 0.2))),
        dynamic_kill_switch_enabled=bool((strategy.get("dynamic_kill_switch") or {}).get("enabled", False)),
        dynamic_kill_oil_series_prefix=str((strategy.get("dynamic_kill_switch") or {}).get("oil_series_prefix", "KXWTI")).strip(),
        dynamic_kill_oil_block_buy_no_uptrend=bool(
            (strategy.get("dynamic_kill_switch") or {}).get("oil_block_buy_no_uptrend", True)
        ),
        dynamic_kill_series_pnl_lookback_hours=max(
            1.0, float((strategy.get("dynamic_kill_switch") or {}).get("series_pnl_lookback_hours", 168.0))
        ),
        dynamic_kill_pnl_cents_floor=int((strategy.get("dynamic_kill_switch") or {}).get("pnl_cents_floor", -5000)),
        dynamic_kill_min_trades=max(1, int((strategy.get("dynamic_kill_switch") or {}).get("min_trades", 3))),
        dynamic_kill_poor_pnl_size_mult=max(
            0.05, min(1.0, float((strategy.get("dynamic_kill_switch") or {}).get("poor_pnl_size_mult", 0.5)))
        ),
        stop_loss_regime_persist_enabled=bool(strategy.get("stop_loss_regime_persist_enabled", True)),
        stop_loss_book_persist_counter_trend_seconds=max(
            0.0, float(strategy.get("stop_loss_book_persist_counter_trend_seconds", 45.0))
        ),
        stop_loss_book_persist_trend_aligned_seconds=max(
            0.0, float(strategy.get("stop_loss_book_persist_trend_aligned_seconds", 90.0))
        ),
        ah_cfg=load_anti_hallucination_config(strategy),
        ae_cfg=load_alpha_engine_config(strategy),
    )
