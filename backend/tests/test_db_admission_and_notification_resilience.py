

import pytest


def test_background_db_slot_fails_open_when_redis_is_unavailable(monkeypatch):
    import db_admission

    class BrokenClient:
        def set(self, *args, **kwargs):
            raise db_admission.redis.ConnectionError("redis down")

        def close(self):
            pass

    monkeypatch.setattr(db_admission, "_client", lambda: BrokenClient())
    monkeypatch.setattr(db_admission.settings, "DB_BACKGROUND_CONCURRENCY_LIMIT", 1)
    monkeypatch.setattr(db_admission.settings, "DB_BACKGROUND_CONNECTION_RESERVE", 1)
    monkeypatch.setattr(db_admission.settings, "DB_BACKGROUND_ADMISSION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(db_admission.settings, "PGBOUNCER_SERVER_POOL_SIZE", 5)

    with db_admission.background_db_slot():
        pass


def test_background_db_slot_caps_limit_at_pgbouncer_effective_budget(monkeypatch):
    """Regression test for the drift bug documented in background_db_slot()'s
    own "BUG FIX" comment: the admission ceiling MUST be capped against
    database.PGBOUNCER_EFFECTIVE_BUDGET (the live-probed, safety-margin-
    adjusted number database.py itself sizes the API pool's share against)
    rather than against the raw, un-probed DB_BACKGROUND_CONCURRENCY_LIMIT/
    DB_BACKGROUND_CONNECTION_RESERVE settings. If a future change reintroduces
    that drift, this test fails instead of the two budgets silently being
    able to jointly over-admit past what PgBouncer/Postgres can actually
    grant.

    Exercised by observing behavior, not internals: `limit` itself is a
    local variable inside background_db_slot(), so this configures a fake
    Redis client that always fails to acquire a slot and records every slot
    key it was asked to set. The highest slot index ever attempted reveals
    the effective `limit` the function actually used.
    """
    import db_admission

    attempted_keys = []

    class AlwaysBusyClient:
        def set(self, key, *args, **kwargs):
            attempted_keys.append(key)
            return False  # every slot always "already taken"

        def close(self):
            pass

    monkeypatch.setattr(db_admission, "_client", lambda: AlwaysBusyClient())
    monkeypatch.setattr(db_admission.settings, "DB_BACKGROUND_ADMISSION_TIMEOUT_SECONDS", 0)
    # Deliberately large/generous raw settings -- if the bug ever comes back
    # (limit derived from these instead of the live-probed budget), the
    # function would try slots far past index 0.
    monkeypatch.setattr(db_admission.settings, "DB_BACKGROUND_CONCURRENCY_LIMIT", 100)
    monkeypatch.setattr(db_admission.settings, "DB_BACKGROUND_CONNECTION_RESERVE", 100)
    # A small, live-probed effective budget -- also deliberately smaller than
    # PGBOUNCER_SERVER_POOL_SIZE below, so this also proves USE_PGBOUNCER=true
    # prefers PGBOUNCER_EFFECTIVE_BUDGET over the static fallback setting.
    monkeypatch.setattr(db_admission.settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(db_admission.database, "PGBOUNCER_EFFECTIVE_BUDGET", 2)
    monkeypatch.setattr(db_admission.settings, "PGBOUNCER_SERVER_POOL_SIZE", 50)

    with pytest.raises(RuntimeError, match="background DB admission capacity is busy"):
        with db_admission.background_db_slot():
            pass

    # server_budget=2 -> limit = min(100, 100, server_budget - 1) = 1, so only
    # slot 0 should ever have been attempted.
    assert attempted_keys == ["db:background-admission:slot:0"]


def test_email_publish_is_scheduled_off_commit_thread(monkeypatch):
    import services.notification_service as ns

    submitted = []

    class FakeFuture:
        pass

    class FakeExecutor:
        def submit(self, fn, item):
            submitted.append((fn, item))
            return FakeFuture()

    class FakeSemaphore:
        def acquire(self, blocking=False):
            return True

        def release(self):
            pass

    monkeypatch.setattr(ns, "_EMAIL_DISPATCH_EXECUTOR", FakeExecutor())
    monkeypatch.setattr(ns, "_EMAIL_DISPATCH_SLOTS", FakeSemaphore())

    ns._dispatch_pending_email_notifications_payloads(
        [{"to": "ops@example.com", "subject": "test", "body": "body"}]
    )

    assert len(submitted) == 1
    assert submitted[0][1]["subject"] == "test"


def test_email_publish_disables_celery_retry(monkeypatch):
    import services.notification_service as ns

    captured = {}

    class FakeTask:
        def apply_async(self, **kwargs):
            captured.update(kwargs)

    class FakeModule:
        send_email_task = FakeTask()

    class FakeSemaphore:
        def acquire(self, blocking=False):
            return True

        def release(self):
            pass

    import sys
    monkeypatch.setitem(sys.modules, "tasks.notification_tasks", FakeModule())
    # _publish_email_task() only ever runs after
    # _dispatch_pending_email_notifications_payloads() has already acquired a
    # slot on _EMAIL_DISPATCH_SLOTS (a BoundedSemaphore) -- its own `finally`
    # unconditionally releases that same slot. Calling it directly here,
    # bypassing that acquire, previously blew up with "ValueError: Semaphore
    # released too many times" before the assertion below ever ran. Stub the
    # semaphore out like test_email_publish_is_scheduled_off_commit_thread
    # does, since this test only cares about the apply_async(retry=False) call.
    monkeypatch.setattr(ns, "_EMAIL_DISPATCH_SLOTS", FakeSemaphore())
    ns._publish_email_task(
        {"to": "ops@example.com", "subject": "test", "body": "body"}
    )

    assert captured["retry"] is False
