"""
Configuration loader for the trading bot.
Loads config.yaml, deep-merges config/strategy_minimal_overlay.yaml by default, then env overrides.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# Path to main config file (next to this module)
CONFIG_PATH = Path(__file__).parent / "config.yaml"
# Minimal baseline overlay (merged after config.yaml; overlay wins on conflicts)
STRATEGY_MINIMAL_OVERLAY_PATH = Path(__file__).parent / "config" / "strategy_minimal_overlay.yaml"
# Set to 0 / false / no / off to load only config.yaml
MINIMAL_OVERLAY_ENV = "TRADING_BOT_MINIMAL_OVERLAY"


def deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay into base (overlay values win). Lists/scalars replace."""
    out = copy.deepcopy(base)
    for key, val in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge_dict(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _minimal_overlay_enabled() -> bool:
    raw = os.getenv(MINIMAL_OVERLAY_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def get_config() -> dict:
    """
    Load config from config.yaml and apply env var overrides.
    Returns a dict with keys: markets, poll_interval_seconds, yfinance_tickers, strategy, trading, etc.
    """
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

    if _minimal_overlay_enabled() and STRATEGY_MINIMAL_OVERLAY_PATH.exists():
        with open(STRATEGY_MINIMAL_OVERLAY_PATH) as f:
            overlay = yaml.safe_load(f) or {}
        if overlay:
            cfg = deep_merge_dict(cfg, overlay)

    # Env overrides (useful for cron, CI, or per-run overrides)
    # Example: MARKETS=KXBTCD,KXETHD  POLL_INTERVAL_SECONDS=120
    if markets_str := os.getenv("MARKETS"):
        cfg["markets"] = [m.strip() for m in markets_str.split(",") if m.strip()]
    if pi := os.getenv("POLL_INTERVAL_SECONDS"):
        try:
            cfg["poll_interval_seconds"] = int(pi)
        except ValueError:
            pass

    return cfg
