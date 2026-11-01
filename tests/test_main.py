"""
Tests for main bot entry point against Kalshi demo environment.
"""

from main import main


def test_main_single_cycle():
    # Run one full cycle against the demo environment configured by conftest.py
    main(single_cycle=True, sleep_seconds=0)

