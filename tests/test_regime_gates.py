"""Regime classification and directional gates (anti_hallucination)."""

from models.anti_hallucination import (
    AntiHallucinationConfig,
    classify_trend_regime,
    directional_regime_should_skip,
)


def _uptrend_closes(n: int = 60, start: float = 50.0, step: float = 0.8) -> list[float]:
    return [start + i * step for i in range(n)]


def test_classify_trend_regime_up_on_rising_series() -> None:
    closes = _uptrend_closes()
    cfg = AntiHallucinationConfig()
    assert classify_trend_regime(closes, closes[-1] + 1.0, cfg) == "up"


def test_buy_no_momentum_gate_blocks_high_rsi_above_ema() -> None:
    closes = _uptrend_closes()
    cfg = AntiHallucinationConfig(
        buy_no_momentum_gate_enabled=True,
        buy_no_momentum_rsi_min=55.0,
    )
    skip, reason = directional_regime_should_skip(
        "BUY NO", "above", closes, closes[-1], cfg
    )
    assert skip is True
    assert reason == "buy_no_momentum_gate"


def test_directional_regime_disabled_skips_nothing() -> None:
    closes = _uptrend_closes()
    cfg = AntiHallucinationConfig(regime_filter_enabled=False)
    assert directional_regime_should_skip("BUY NO", "above", closes, closes[-1], cfg) == (
        False,
        "",
    )
