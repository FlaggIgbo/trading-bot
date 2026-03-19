"""
Alpha-generation and risk-management upgrades.

Provides four composable modules:
  1. Order Book Imbalance (OBI) — directional signal from bid/ask depth
  2. Markout PnL / Toxic Flow Detection — post-fill adverse selection tracking
  3. Volatility-Adjusted Kelly — scale Kelly sizing inversely with realised vol
  4. Lead-Lag Signal — use correlated asset moves (e.g. SPY→KXINXU) as a leading indicator
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class AlphaEngineConfig:
    """All tunables for the alpha / risk engine."""

    # --- Order Book Imbalance (OBI) ---
    obi_enabled: bool = True
    obi_edge_bonus_max: float = 0.04
    obi_strong_threshold: float = 0.40
    obi_min_total_size: float = 5.0

    # --- Markout PnL / Toxic Flow ---
    markout_enabled: bool = True
    markout_windows_minutes: list[int] = field(default_factory=lambda: [1, 5, 15])
    markout_toxic_threshold_bps: float = -50.0
    markout_edge_penalty: float = 0.03
    markout_min_samples: int = 5

    # --- Volatility-Adjusted Kelly ---
    vol_kelly_enabled: bool = True
    vol_kelly_baseline_vol: float = 0.02
    vol_kelly_max_shrink: float = 0.25
    vol_kelly_lookback_bars: int = 24

    # --- Lead-Lag Signal ---
    lead_lag_enabled: bool = True
    lead_lag_pairs: dict[str, str] = field(default_factory=lambda: {
        "^GSPC": "KXINXU",
        "^IXIC": "KXINXU",
    })
    lead_lag_min_move_pct: float = 0.003
    lead_lag_edge_bonus: float = 0.02
    lead_lag_lookback_bars: int = 5


_DEFAULT_LEAD_LAG_PAIRS = {"^GSPC": "KXINXU", "^IXIC": "KXINXU"}
_DEFAULT_MARKOUT_WINDOWS = [1, 5, 15]


def load_alpha_engine_config(strategy: dict) -> AlphaEngineConfig:
    ae = strategy.get("alpha_engine", {})
    pairs_raw = ae.get("lead_lag_pairs")
    pairs = dict(pairs_raw) if isinstance(pairs_raw, dict) else dict(_DEFAULT_LEAD_LAG_PAIRS)
    windows_raw = ae.get("markout_windows_minutes")
    windows = list(windows_raw) if isinstance(windows_raw, (list, tuple)) else list(_DEFAULT_MARKOUT_WINDOWS)
    return AlphaEngineConfig(
        obi_enabled=bool(ae.get("obi_enabled", True)),
        obi_edge_bonus_max=float(ae.get("obi_edge_bonus_max", 0.04)),
        obi_strong_threshold=float(ae.get("obi_strong_threshold", 0.40)),
        obi_min_total_size=float(ae.get("obi_min_total_size", 5.0)),
        markout_enabled=bool(ae.get("markout_enabled", True)),
        markout_windows_minutes=[int(w) for w in windows],
        markout_toxic_threshold_bps=float(ae.get("markout_toxic_threshold_bps", -50.0)),
        markout_edge_penalty=float(ae.get("markout_edge_penalty", 0.03)),
        markout_min_samples=int(ae.get("markout_min_samples", 5)),
        vol_kelly_enabled=bool(ae.get("vol_kelly_enabled", True)),
        vol_kelly_baseline_vol=float(ae.get("vol_kelly_baseline_vol", 0.02)),
        vol_kelly_max_shrink=float(ae.get("vol_kelly_max_shrink", 0.25)),
        vol_kelly_lookback_bars=int(ae.get("vol_kelly_lookback_bars", 24)),
        lead_lag_enabled=bool(ae.get("lead_lag_enabled", True)),
        lead_lag_pairs=pairs,
        lead_lag_min_move_pct=float(ae.get("lead_lag_min_move_pct", 0.003)),
        lead_lag_edge_bonus=float(ae.get("lead_lag_edge_bonus", 0.02)),
        lead_lag_lookback_bars=int(ae.get("lead_lag_lookback_bars", 5)),
    )


# ---------------------------------------------------------------------------
# Layer 1: Order Book Imbalance (OBI)
# ---------------------------------------------------------------------------

def compute_obi(
    bid_size: float | None,
    ask_size: float | None,
    min_total_size: float = 5.0,
) -> float | None:
    """
    Order Book Imbalance = (V_bid - V_ask) / (V_bid + V_ask).

    Returns a value in [-1, +1]:
      +1 = all depth on the bid (upward pressure)
      -1 = all depth on the ask (downward pressure)
      None = insufficient data

    When bid_size >> ask_size, the price is likely to tick up because
    there is strong demand relative to supply at the top of the book.
    """
    if bid_size is None or ask_size is None:
        return None
    bid_size = max(0.0, float(bid_size))
    ask_size = max(0.0, float(ask_size))
    total = bid_size + ask_size
    if total < min_total_size:
        return None
    return (bid_size - ask_size) / total


def obi_edge_adjustment(
    obi: float | None,
    rec: str,
    *,
    strong_threshold: float = 0.40,
    max_bonus: float = 0.04,
) -> float:
    """
    Adjust effective edge requirement based on order book imbalance.

    When OBI strongly favors our trade direction, reduce the required edge
    (negative return = bonus). When OBI opposes us, increase it (positive return = penalty).

    For BUY YES: positive OBI (buy pressure) → bonus; negative OBI → penalty.
    For BUY NO: negative OBI (sell pressure) → bonus; positive OBI → penalty.
    """
    if obi is None:
        return 0.0

    if rec == "BUY YES":
        aligned = obi
    elif rec == "BUY NO":
        aligned = -obi
    else:
        return 0.0

    if abs(aligned) < strong_threshold:
        return 0.0

    scale = min(1.0, (abs(aligned) - strong_threshold) / (1.0 - strong_threshold))
    if aligned > 0:
        return -max_bonus * scale
    else:
        return max_bonus * scale


# ---------------------------------------------------------------------------
# Layer 2: Markout PnL / Toxic Flow Detection
# ---------------------------------------------------------------------------

def compute_markout_bps(
    entry_price: float,
    price_after: float,
    side: str,
) -> float:
    """
    Markout in basis points from our perspective.

    For YES buys: if price dropped after our fill, markout is negative (toxic).
    For NO buys: inverted — price rising after our fill is bad for us.

    Returns the PnL in basis points relative to the contract price.
    """
    if entry_price <= 0:
        return 0.0
    if side.lower() in ("yes", "buy_yes"):
        return (price_after - entry_price) / entry_price * 10_000
    else:
        return (entry_price - price_after) / entry_price * 10_000


def is_ticker_toxic(
    markout_history: list[float],
    threshold_bps: float = -50.0,
    min_samples: int = 5,
) -> bool:
    """
    A ticker is "toxic" if the average markout across recent fills is
    below the threshold. This means we are consistently getting filled
    right before the price moves against us — a sign of adverse selection.
    """
    if len(markout_history) < min_samples:
        return False
    avg = sum(markout_history) / len(markout_history)
    return avg < threshold_bps


def toxic_flow_edge_penalty(
    markout_history: list[float],
    *,
    threshold_bps: float = -50.0,
    min_samples: int = 5,
    penalty: float = 0.03,
) -> float:
    """
    If a ticker is toxic, return a positive edge penalty so the strategy
    demands more edge before entering again. This forces the model's
    min_edge to exceed the adverse selection cost.
    """
    if is_ticker_toxic(markout_history, threshold_bps, min_samples):
        return penalty
    return 0.0


# ---------------------------------------------------------------------------
# Layer 3: Volatility-Adjusted Kelly
# ---------------------------------------------------------------------------

def vol_adjusted_kelly(
    base_kelly: float,
    closes: list[float],
    lookback: int = 24,
    baseline_vol: float = 0.02,
    max_shrink: float = 0.25,
) -> float:
    """
    Scale Kelly fraction inversely with realized volatility.

    f_adjusted = base_kelly * (baseline_vol / current_vol)

    In high-volatility regimes (current_vol >> baseline_vol), the Kelly
    fraction shrinks automatically, preserving capital for cleaner signals.
    Capped so that in extreme vol the fraction never goes below max_shrink * base_kelly.

    In low vol, the Kelly fraction is NOT inflated beyond 1x to avoid overconfidence.
    """
    if not closes or len(closes) < max(2, lookback):
        return base_kelly

    recent = closes[-lookback:]
    rets = []
    for i in range(1, len(recent)):
        if recent[i - 1] > 0 and recent[i] > 0:
            rets.append(math.log(recent[i] / recent[i - 1]))
    if not rets:
        return base_kelly

    current_vol = math.sqrt(sum(r * r for r in rets) / len(rets))
    if current_vol <= 0:
        return base_kelly

    ratio = baseline_vol / current_vol
    ratio = min(1.0, ratio)
    ratio = max(max_shrink, ratio)

    return base_kelly * ratio


# ---------------------------------------------------------------------------
# Layer 4: Lead-Lag Signal
# ---------------------------------------------------------------------------

def lead_lag_signal(
    leader_closes: list[float],
    lookback: int = 5,
    min_move_pct: float = 0.003,
) -> float:
    """
    Compute short-term momentum of a leading asset.

    Returns the return over the last `lookback` bars.
    If the absolute return exceeds min_move_pct, the signal is live;
    otherwise it returns 0.0 (no actionable signal).

    Positive return on SPY → bullish for KXINXU "above" contracts.
    Negative return on SPY → bearish for KXINXU "above" contracts.
    """
    if not leader_closes or len(leader_closes) < lookback + 1:
        return 0.0

    old = leader_closes[-(lookback + 1)]
    new = leader_closes[-1]
    if old <= 0:
        return 0.0
    ret = (new - old) / old
    if abs(ret) < min_move_pct:
        return 0.0
    return ret


def lead_lag_edge_adjustment(
    signal: float,
    direction: str,
    rec: str,
    edge_bonus: float = 0.02,
) -> float:
    """
    Adjust edge requirement when a leading asset has moved.

    If SPY is sharply up and we're considering BUY YES on "above" contract,
    reduce required edge (bonus). If SPY is sharply down and we're buying
    YES on "above", increase edge (penalty).

    Returns edge adjustment: negative = bonus, positive = penalty.
    """
    if signal == 0.0 or rec not in ("BUY YES", "BUY NO"):
        return 0.0

    if rec == "BUY YES":
        if direction == "above":
            return -edge_bonus if signal > 0 else edge_bonus
        elif direction == "below":
            return -edge_bonus if signal < 0 else edge_bonus
    elif rec == "BUY NO":
        if direction == "above":
            return -edge_bonus if signal < 0 else edge_bonus
        elif direction == "below":
            return -edge_bonus if signal > 0 else edge_bonus
    return 0.0
