"""
Ingestion client module (re-exports from ingestion.api.client for backward compatibility).
"""

from ingestion.api.client import (
    DEFAULT_DEMO_API_KEY_ID,
    DEFAULT_SERIES,
    DEMO_BASE_URL,
    MULTI_STRIKE_SUFFIX_RE,
    KalshiClient,
    is_binary_market,
    load_private_key,
    sign_request,
)

__all__ = [
    "DEFAULT_DEMO_API_KEY_ID",
    "DEFAULT_SERIES",
    "DEMO_BASE_URL",
    "MULTI_STRIKE_SUFFIX_RE",
    "KalshiClient",
    "is_binary_market",
    "load_private_key",
    "sign_request",
]
