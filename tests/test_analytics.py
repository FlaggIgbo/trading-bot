"""
Tests for logging and analytics library.
"""

from logging.analytics import Analytics, ClickHouseBackend


def test_analytics_event_levels():
    tracker = Analytics(console_output=False)

    # (method_name, message, event_name, expected_level)
    tests = [
        ("info", "Bot started", "bot_start", "INFO"),
        ("warning", "High latency", "slow_request", "WARNING"),
        ("error", "Failed request", "api_error", "ERROR"),
        ("debug", "Trace details", "debug_trace", "DEBUG"),
    ]
    for method, msg, evt_name, expected_level in tests:
        entry = getattr(tracker, method)(msg, event=evt_name)
        assert entry.level == expected_level
        assert entry.event == evt_name
        assert entry.message == msg


def test_analytics_filtering():
    tracker = Analytics(console_output=False)
    tracker.info("Order 1", event="order")
    tracker.warning("Warning 1", event="alert")
    tracker.error("Error 1", event="alert")
    tracker.info("Order 2", event="order")

    # (query_filters, expected_count)
    tests = [
        ({}, 4),
        ({"event": "order"}, 2),
        ({"event": "alert"}, 2),
        ({"level": "ERROR"}, 1),
        ({"limit": 2}, 2),
    ]
    for query, expected_count in tests:
        assert len(tracker.get_events(**query)) == expected_count


def test_analytics_summary_metrics():
    tracker = Analytics(console_output=False)

    events = [
        ("markets_scanned", {"count": 10}),
        ("order_simulated", {"count": 1}),
        ("order_live", {"count": 2}),
        ("settlement", {"pnl_cents": 60}),   # win (+60)
        ("settlement", {"pnl_cents": -20}),  # loss (-20)
        ("settlement", {"pnl_cents": 0}),    # break-even (0)
        ("api_failure", {"level": "ERROR"}),
    ]
    for evt, kwargs in events:
        tracker.track(evt, **kwargs)

    summary = tracker.summary()

    # (metric_name, expected_value)
    tests = [
        ("total_events", 7),
        ("markets_scanned", 10),
        ("orders_placed", 3),
        ("positions_settled", 3),
        ("total_pnl_cents", 40),
        ("wins", 1),
        ("losses", 1),
        ("break_evens", 1),
        ("win_rate", 0.5),
        ("errors_count", 1),
    ]
    for key, expected_val in tests:
        assert summary[key] == expected_val


def test_clickhouse_backend():
    class DummyClient:
        def command(self, sql):
            pass

        def insert(self, table, rows, column_names=None):
            pass

    # (backend_instance, expected_connected, can_insert)
    tests = [
        (ClickHouseBackend(host=None, auto_init=False), False, False),
        (ClickHouseBackend(client=DummyClient()), True, True),
    ]
    for backend, expected_connected, can_insert in tests:
        assert backend.is_connected == expected_connected
        assert backend.insert_event("test_evt", "msg") == can_insert
