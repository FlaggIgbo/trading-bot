"""Backward-compat shim — actual implementation in models/strategies/"""
from models.strategies import BaseStrategy, DefaultThresholdStrategy, VolRegimeStrategy

__all__ = ["BaseStrategy", "DefaultThresholdStrategy", "VolRegimeStrategy"]
