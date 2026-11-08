"""
Tests for main bot entry point and clock alignment (:00:30, :15:30, :30:30, :45:30).
"""

from main import main, seconds_until_next_slot


def test_seconds_until_next_slot():
    # (current_timestamp_seconds, expected_wait_seconds)
    tests = [
        (10, 20),   # 00:00:10 -> wait 20s until 00:00:30
        (30, 0),    # 00:00:30 -> run immediately (0s wait)
        (35, 895),  # 00:00:35 -> wait 895s until 00:15:30
        (900, 30),  # 00:15:00 -> wait 30s until 00:15:30
    ]
    for now, expected_wait in tests:
        assert seconds_until_next_slot(now=now, interval=900, offset=30) == expected_wait


def test_main_single_cycle(tmp_path):
    db_file = str(tmp_path / "test_bot.db")
    main(single_cycle=True, sleep_seconds=0, db_path=db_file)


def test_parse_instances():
    from scripts.run_parallel import parse_instances
    specs = ["sell_1.5n_35p", "sell_1.5n_35p:prod:live", "sell_3n", "sell_2.5n:demo", "sell_1.5n_5m:prod:live:sl"]
    instances = parse_instances(specs)
    assert len(instances) == 5
    assert instances[0]["live"] is False
    assert instances[0]["env"] == "demo"
    assert instances[0]["strategy"] == "sell_1.5n_35p"
    assert instances[0]["stop_loss"] is False
    assert instances[1]["live"] is True
    assert instances[1]["env"] == "prod"
    assert instances[1]["tag"] == "sell_1.5n_35p (prod, LIVE)"
    assert instances[1]["stop_loss"] is False
    assert instances[2]["live"] is False
    assert instances[3]["env"] == "demo"
    assert instances[4]["live"] is True
    assert instances[4]["env"] == "prod"
    assert instances[4]["stop_loss"] is True
    assert instances[4]["tag"] == "sell_1.5n_5m (prod, LIVE, SL)"
