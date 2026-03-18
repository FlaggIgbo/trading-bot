"""
Models layer: ML strategies, signal scoring, and calibration.

Contains the COMPUTE layer of the ML pipeline — takes features derived from
signals and produces trade recommendations (BUY YES / BUY NO / SKIP).
"""
from models.strategies import BaseStrategy, DefaultThresholdStrategy, VolRegimeStrategy

__all__ = ["BaseStrategy", "DefaultThresholdStrategy", "VolRegimeStrategy"]
