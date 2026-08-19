"""
Signals module: Prediction market exchange API clients (Kalshi & Gemini).
"""
from signals.base_client import (  # noqa: F401
    BaseTradingClient,
    LogTag,
    is_5min_market,
    is_15m_market,
    parse_market_close_datetime,
    validate_market_closure_time,
)
from signals.gemini_client import (  # noqa: F401
    GEMINI_CATEGORIES,
    GeminiClient,
    GeminiPredictionClient,
    GeminiTradingClient,
)
from signals.gemini_client import (
    SERIES_15M_ALL as GEMINI_SERIES_15M_ALL,
)
from signals.gemini_client import (
    SERIES_15M_COMMODITIES as GEMINI_SERIES_15M_COMMODITIES,
)
from signals.gemini_client import (
    SERIES_15M_CRYPTO as GEMINI_SERIES_15M_CRYPTO,
)
from signals.gemini_client import (
    SERIES_HOURLY_ALL as GEMINI_SERIES_HOURLY_ALL,
)
from signals.gemini_client import (
    SERIES_HOURLY_COMMODITIES as GEMINI_SERIES_HOURLY_COMMODITIES,
)
from signals.gemini_client import (
    SERIES_HOURLY_CRYPTO as GEMINI_SERIES_HOURLY_CRYPTO,
)
from signals.gemini_client import (
    SERIES_HOURLY_INDICES as GEMINI_SERIES_HOURLY_INDICES,
)
from signals.kalshi_client import (  # noqa: F401
    SERIES_15M_ALL,
    SERIES_15M_COMMODITIES,
    SERIES_15M_CRYPTO,
    SERIES_HOURLY_ALL,
    SERIES_HOURLY_COMMODITIES,
    SERIES_HOURLY_CRYPTO,
    SERIES_HOURLY_INDICES,
    KalshiClient,
)

