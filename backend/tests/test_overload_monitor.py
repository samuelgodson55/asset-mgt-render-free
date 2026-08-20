"""Unit tests for overload_monitor.py.

Covers the two things the "503 shedding" feature request specifically
asked for:
  1. Per-route/per-reason rejection counts are recorded on every 503.
  2. ErrorBeacon only receives ONE degraded-dependency signal once
     rejections are SUSTAINED (>= threshold within the window), not one
     per rejected request, and respects a cooldown afterward.
"""

import overload_monitor


def _reset(monkeypatch):
    overload_monitor.reset_for_tests()
    monkeypatch.setattr(overload_monitor.settings, "OVERLOAD_ALERT_WINDOW_SECONDS", 30)
    monkeypatch.setattr(overload_monitor.settings, "OVERLOAD_ALERT_THRESHOLD_COUNT", 3)
    monkeypatch.setattr(overload_monitor.settings, "OVERLOAD_ALERT_COOLDOWN_SECONDS", 300)


def test_record_503_increments_the_per_route_per_reason_snapshot(monkeypatch):
    _reset(monkeypatch)
    overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.ADMISSION_QUEUE_FULL)
    overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.ADMISSION_QUEUE_FULL)
    overload_monitor.record_503(route="/api/checkouts", reason=overload_monitor.OverloadReason.POOL_TIMEOUT)

    snap = overload_monitor.snapshot()
    assert snap["/api/assets [db_admission_queue_full]"] == 2
    assert snap["/api/checkouts [db_pool_exhausted]"] == 1


def test_user_message_never_leaks_the_reason_code():
    for reason in (
        overload_monitor.OverloadReason.ADMISSION_QUEUE_FULL,
        overload_monitor.OverloadReason.POOL_TIMEOUT,
        "some_unknown_future_reason",
    ):
        message = overload_monitor.user_message(reason)
        assert reason not in message
        assert "retry" in message.lower()


def test_errorbeacon_is_not_called_below_the_sustained_threshold(monkeypatch):
    _reset(monkeypatch)
    calls = []
    monkeypatch.setattr(overload_monitor, "report_exception", lambda *a, **k: calls.append((a, k)))

    # threshold is 3 in _reset(); two rejections must not alert.
    overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.POOL_TIMEOUT)
    overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.POOL_TIMEOUT)

    assert calls == []


def test_errorbeacon_fires_exactly_once_when_threshold_is_crossed_then_respects_cooldown(monkeypatch):
    _reset(monkeypatch)
    calls = []
    monkeypatch.setattr(overload_monitor, "report_exception", lambda *a, **k: calls.append((a, k)))

    for _ in range(5):
        overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.POOL_TIMEOUT)

    # 5 rejections all within the window, threshold=3 -> crossed on the
    # 3rd call, then held below cooldown for the 4th/5th: exactly one
    # ErrorBeacon report despite 5 individual 503s.
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["category"] == "dependency_degraded"
    assert kwargs["severity"] == "warning"
    assert kwargs["context"]["failure_mode"] == overload_monitor.OverloadReason.POOL_TIMEOUT


def test_different_reasons_are_tracked_and_alerted_independently(monkeypatch):
    _reset(monkeypatch)
    calls = []
    monkeypatch.setattr(overload_monitor, "report_exception", lambda *a, **k: calls.append(k))

    for _ in range(3):
        overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.ADMISSION_QUEUE_FULL)
    for _ in range(3):
        overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.POOL_TIMEOUT)

    reasons_alerted = {k["context"]["failure_mode"] for k in calls}
    assert reasons_alerted == {
        overload_monitor.OverloadReason.ADMISSION_QUEUE_FULL,
        overload_monitor.OverloadReason.POOL_TIMEOUT,
    }


def test_record_503_never_raises_even_if_errorbeacon_reporting_blows_up(monkeypatch):
    _reset(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("errorbeacon is down too")

    monkeypatch.setattr(overload_monitor, "report_exception", _boom)

    for _ in range(5):
        overload_monitor.record_503(route="/api/assets", reason=overload_monitor.OverloadReason.POOL_TIMEOUT)
    # No exception propagated -- observability failures must never turn
    # an already-degraded request into a worse one.
