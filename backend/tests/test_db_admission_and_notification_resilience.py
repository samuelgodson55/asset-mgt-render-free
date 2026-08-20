

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
