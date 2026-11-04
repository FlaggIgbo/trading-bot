"""
Tests for main bot entry point against Kalshi demo environment.
"""

from pathlib import Path
from main import main


def test_main_single_cycle(tmp_path: Path) -> None:
    # Run one full cycle against the demo environment configured by conftest.py using a temporary SQLite DB
    db_file = str(tmp_path / "test_bot.db")
    main(single_cycle=True, sleep_seconds=0, db_path=db_file)
