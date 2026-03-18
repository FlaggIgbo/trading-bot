"""
Data layer: persistent storage for predictions, snapshots, and calibration.

SQLite-backed storage that powers backtesting, calibration, and fill tracking.
Acts as the memory of the ML pipeline — stores historical outcomes to improve
future predictions.

Submodules:
    data.storage - SQLite database operations (predictions, snapshots, cache)
"""
