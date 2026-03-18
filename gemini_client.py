"""Backward-compat shim — actual implementation in signals/gemini_client.py"""
# Re-export everything including underscore-prefixed helpers used by compare_historical
from signals.gemini_client import *  # noqa: F401,F403
from signals.gemini_client import (  # noqa: F401
    _event_to_asset,
    _get_no_contract,
    _get_yes_contract,
)
