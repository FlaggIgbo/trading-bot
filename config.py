"""
Configuration loader for the trading bot.
Loads config.yaml and overrides selected values from environment variables.
"""
import os
from pathlib import Path

import yaml

# Path to main config file (next to this module)
CONFIG_PATH = Path(__file__).parent / "config.yaml"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def get_config() -> dict:
    """
    Load config from config.yaml and apply env var overrides.
    Returns a dict with keys: markets, poll_interval_seconds, yfinance_tickers, strategy, trading, etc.
    """
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

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
