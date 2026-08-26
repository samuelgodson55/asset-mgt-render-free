"""
Tests for services/idempotency.py -- see that module's docstring for the
full "why" (a burst of duplicate clicks should be cheap to dedupe, and
Redis must be a fail-open efficiency optimization, never a hard
dependency for the underlying action to succeed).

Uses the same monkeypatch-a-fake-Redis-client idiom as
test_db_admission_and_notification_resilience.py's tests for
db_admission.py's background_db_slot() -- there's no real Redis in the
test environment (see conftest.py's REDIS_URL pointing at an
unreachable host), so these fakes are in-memory stand-ins for the tiny
slice of the redis-py client API this module actually calls
(get/set/delete).
"""

import threading
import time

import pytest
from fastapi import HTTPException


class FakeRedis:
    """Minimal in-memory stand-in for the redis-py client calls
    services/idempotency.py actually makes: SET (with nx/ex), GET, DELETE.
    Thread-safe since some tests below exercise the two-concurrent-
    requests-racing-in scenario for real, on real threads."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            if nx and key in self._store:
                return False
            self._store[key] = value
            return True

    def get(self, key):
        with self._lock:
            return self._store.get(key)

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    import services.idempotency as idem

    client = FakeRedis()
    monkeypatch.setattr(idem, "_client", lambda: client)
    return client


def test_no_key_is_a_full_passthrough_and_never_touches_redis(monkeypatch):
    """key=None (no Idempotency-Key header sent) must not construct a
    Redis client at all -- every existing caller that doesn't send the
    header keeps its exact current behavior with zero new dependency."""
    import services.idempotency as idem

    def boom():
        raise AssertionError("must not construct a Redis client when key is None")
    monkeypatch.setattr(idem, "_client", boom)

    calls = []
    result = idem.run_idempotent(action="test.action", user_id=1, key=None, fn=lambda: calls.append(1) or {"ok": True})
    assert result == {"ok": True}
    assert calls == [1]


def test_second_call_with_same_key_replays_cached_result_without_rerunning_fn(fake_redis):
    import services.idempotency as idem

    calls = []

    def fn():
        calls.append(1)
        return {"message": "Checked out.", "id": 42}

    first = idem.run_idempotent(action="asset.checkout_advanced", user_id=7, key="click-abc", fn=fn)
    second = idem.run_idempotent(action="asset.checkout_advanced", user_id=7, key="click-abc", fn=fn)

    assert first == {"message": "Checked out.", "id": 42}
    assert second == first
    # The whole point: fn() -- the real checkout logic, a DB write -- only
    # ran once, even though run_idempotent() was called twice.
    assert calls == [1]


def test_different_keys_both_run_normally(fake_redis):
    import services.idempotency as idem

    calls = []
    fn = lambda: calls.append(1) or {"n": len(calls)}

    a = idem.run_idempotent(action="quotation.submit", user_id=1, key="click-1", fn=fn)
    b = idem.run_idempotent(action="quotation.submit", user_id=1, key="click-2", fn=fn)

    assert a != b
    assert calls == [1, 1]


def test_same_key_different_users_never_cross_contaminate(fake_redis):
    """The cache key is scoped by user_id specifically to rule out one
    person's guessed/reused Idempotency-Key replaying ANOTHER person's
    cached response -- see the module docstring's cross-account-leak
    reasoning. Two different users sending the identical key value must
    both run fn() independently."""
    import services.idempotency as idem

    calls = []
    fn = lambda: calls.append(1) or {"n": len(calls)}

    a = idem.run_idempotent(action="quotation.submit", user_id=1, key="same-key", fn=fn)
    b = idem.run_idempotent(action="quotation.submit", user_id=2, key="same-key", fn=fn)

    assert a != b
    assert calls == [1, 1]


def test_a_failed_attempt_releases_the_key_so_a_retry_is_not_stuck_waiting(fake_redis):
    """If fn() raises (e.g. the cart was empty, a genuine validation
    failure), the claim must be released -- not left as a permanently
    'pending' placeholder that would force a legitimate subsequent retry
    with the same key to wait out the full poll timeout for nothing."""
    import services.idempotency as idem

    def failing():
        raise HTTPException(status_code=400, detail="Your saved order is empty.")

    with pytest.raises(HTTPException):
        idem.run_idempotent(action="quotation.submit", user_id=1, key="k", fn=failing)

    def succeeding():
        return {"ok": True}

    # Retried promptly with the SAME key right after the failure -- must
    # run fn() again (not hang waiting for a claim that will never
    # resolve), since the failed attempt's own claim was released.
    result = idem.run_idempotent(action="quotation.submit", user_id=1, key="k", fn=succeeding)
    assert result == {"ok": True}


def test_redis_unavailable_fails_open_and_still_runs_the_action(monkeypatch):
    """Redis is an efficiency optimization, never a hard dependency --
    an outage must not block a real user action (mirrors
    db_admission.py's identical fail-open test)."""
    import services.idempotency as idem

    class BrokenClient:
        def set(self, *a, **k):
            raise idem.redis.ConnectionError("redis down")

    monkeypatch.setattr(idem, "_client", lambda: BrokenClient())

    calls = []
    result = idem.run_idempotent(action="asset.create", user_id=1, key="k", fn=lambda: calls.append(1) or {"id": 1})
    assert result == {"id": 1}
    assert calls == [1]


def test_a_concurrent_duplicate_waits_for_and_replays_the_in_flight_result(fake_redis):
    """The realistic double-click scenario: two requests carrying the
    SAME key arrive close enough together that the second one claims
    nothing (the first already holds the 'pending' placeholder) -- it
    must wait for the first to finish and then replay its result,
    rather than either erroring immediately or running fn() itself."""
    import services.idempotency as idem

    calls = []
    started = threading.Event()
    finish = threading.Event()

    def slow_fn():
        calls.append(1)
        started.set()
        finish.wait(timeout=2)
        return {"message": "Checked out.", "n": len(calls)}

    results = {}

    def run_first():
        results["first"] = idem.run_idempotent(action="asset.checkout_advanced", user_id=1, key="dup", fn=slow_fn)

    t = threading.Thread(target=run_first)
    t.start()
    assert started.wait(timeout=2), "first request never started"

    # The second, "duplicate" request -- fires while the first is still
    # mid-flight, and must NOT run the real (slow, DB-touching) logic again.
    def second_fn():
        calls.append("should-never-run")
        return {"should": "never-run"}

    def run_second():
        results["second"] = idem.run_idempotent(action="asset.checkout_advanced", user_id=1, key="dup", fn=second_fn)

    t2 = threading.Thread(target=run_second)
    t2.start()
    time.sleep(0.05)  # let the second thread actually start polling
    finish.set()
    t.join(timeout=2)
    t2.join(timeout=2)

    assert results["first"] == {"message": "Checked out.", "n": 1}
    assert results["second"] == results["first"]
    assert calls == [1]  # the duplicate's fn() never ran
