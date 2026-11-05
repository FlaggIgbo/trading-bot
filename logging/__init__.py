"""
Logging & Analytics Layer
=========================
Provides structured analytics and event logging for the trading bot,
while bridging standard library logging to maintain seamless interoperability
with external packages (e.g. urllib3, requests, pytest).
"""

import importlib.util
import os
import sys

# In Python, 'logging' is also the name of Python's built-in logging library.
# To keep external libraries (like requests and pytest) working normally,
# we find Python's built-in logging library and copy its functions here.
_stdlib_logging = None
for _p in sys.path:
    # Look in Python system library folders, not this project directory
    if _p and _p != "." and not os.path.abspath(_p).startswith(os.path.abspath(".")):
        _cand = os.path.join(_p, "logging", "__init__.py")
        if os.path.isfile(_cand):
            _stdlib_dir = os.path.join(_p, "logging")
            if _stdlib_dir not in __path__:
                __path__.append(_stdlib_dir)
            _spec = importlib.util.spec_from_file_location("_stdlib_logging", _cand)
            if _spec and _spec.loader:
                _mod = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                # Copy standard logging functions (like getLogger, info, error) into this module
                globals().update(
                    {
                        k: v
                        for k, v in _mod.__dict__.items()
                        if not (k.startswith("__") and k.endswith("__"))
                    }
                )
                _stdlib_logging = _mod
            break

# Export analytics library
from logging.analytics import Analytics, AnalyticsEvent, ClickHouseBackend, analytics

__all__ = [
    "Analytics",
    "AnalyticsEvent",
    "ClickHouseBackend",
    "analytics",
]
if _stdlib_logging and hasattr(_stdlib_logging, "__all__"):
    __all__.extend(_stdlib_logging.__all__)
