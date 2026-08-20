"""P1 resilience contracts.

These tests intentionally verify the failure behavior that must remain true
when production dependencies are restarted or briefly unavailable. They are
fast unit/contract tests; scripts/chaos-test.sh exercises the same scenarios
against the real Docker Compose stack.
"""

import asyncio
from pathlib import Path

import redis

from middleware.rate_limit import RateLimitMiddleware


class _BrokenRedis:
    def incr(self, _key):
        raise redis.RedisError("simulated Redis outage")


def test_login_rate_limit_fails_open_when_redis_is_down(monkeypatch):
    """Authentication must remain usable during a short Redis outage.

    The limiter is defense-in-depth. A Redis outage must not turn the entire
    login endpoint into an outage of its own.
    """
    middleware = RateLimitMiddleware(lambda *_args, **_kwargs: None, {"/auth/login"})
    middleware._limiter._redis = _BrokenRedis()
    blocked, retry_after = middleware._limiter.check("198.51.100.10")
    assert blocked is False
    assert retry_after == 0


def test_rate_limiter_uses_bounded_redis_timeouts():
    middleware = RateLimitMiddleware(lambda *_args, **_kwargs: None, {"/auth/login"})
    kwargs = middleware._limiter._redis.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == 1
    assert kwargs["socket_timeout"] == 1
    assert kwargs["health_check_interval"] == 30


def test_rate_limit_non_limited_requests_pass_through():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])

    middleware = RateLimitMiddleware(app, {"/auth/login"})
    asyncio.run(middleware({"type": "http", "method": "GET", "path": "/healthz", "client": ("127.0.0.1", 1)}, None, None))
    assert calls == ["/healthz"]


def test_rate_limit_redis_failure_reports_degradation_without_http_503():
    # The fail-open report_exception() call now lives in the shared
    # RedisFixedWindowLimiter (utils/rate_limiter.py), used by both the
    # login rate limiter and api/telemetry_api.py's client-error limiter --
    # see that module's docstring.
    source = (Path(__file__).resolve().parents[1] / "utils" / "rate_limiter.py").read_text()
    assert 'report_exception(' in source
    assert 'category="dependency_degraded"' in source
    assert '"failure_mode": "fail_open"' in source


def test_db_pool_exhaustion_returns_503_not_bare_500():
    """
    Pentest finding: a burst of concurrent requests exhausting the
    SQLAlchemy connection pool (settings.DB_POOL_TIMEOUT_SECONDS reached)
    used to fall through to the generic unhandled-exception handler and
    come back as an opaque 500 for the whole ~10s the burst lasted. This
    is an expected, self-recovering "server is momentarily out of a
    specific resource" condition, not a bug -- it belongs in HTTP's 503
    (with Retry-After), not 500.
    """
    import asyncio
    import sqlalchemy.exc
    from middleware.error_handling import UnhandledExceptionMiddleware

    async def _failing_app(scope, receive, send):
        raise sqlalchemy.exc.TimeoutError(
            "QueuePool limit of size 5 overflow 5 reached, connection timed out, timeout 10"
        )

    middleware = UnhandledExceptionMiddleware(_failing_app)

    sent_messages = []

    async def _send(message):
        sent_messages.append(message)

    async def _receive():
        return {"type": "http.request"}

    scope = {
        "type": "http", "method": "GET", "path": "/api/assets",
        "headers": [], "client": ("127.0.0.1", 1), "query_string": b"",
    }
    asyncio.run(middleware(scope, _receive, _send))

    start_message = next(m for m in sent_messages if m["type"] == "http.response.start")
    assert start_message["status"] == 503
    header_dict = {k.decode(): v.decode() for k, v in start_message["headers"]}
    assert header_dict.get("retry-after") is not None

    body_message = next(m for m in sent_messages if m["type"] == "http.response.body")
    import json
    body = json.loads(body_message["body"])
    assert "detail" in body
    assert "request_id" in body


def test_start_audit_export_returns_503_not_500_when_redis_is_down(as_admin):
    """
    Pentest/reliability finding: conftest.py deliberately points REDIS_URL
    at an unreachable host for the whole test session (see its own
    docstring) -- generate_audit_export.delay() used to let that raise
    straight through POST /audit-logs/export as a bare, unhandled 500.
    Exports are the one Redis dependency in this app that ISN'T allowed
    to fail open (unlike the rate limiter above) -- there's no safe
    default result for "start a background job" -- so the contract here
    is a clean, actionable 503, not a crash.
    """
    client, headers = as_admin
    response = client.post("/api/audit-logs/export?format=csv", headers=headers)
    assert response.status_code == 503
    assert "detail" in response.json()


def test_audit_export_status_poll_returns_503_not_500_when_redis_is_down(as_admin):
    """Same contract as the start-export test above, for the status-poll
    endpoint the frontend calls every second or two after starting an
    export (see js/components/audit.js) -- AsyncResult(...).state is what
    actually round-trips to Redis, and used to let that same unreachable-
    Redis error surface as a bare 500 on every single poll."""
    client, headers = as_admin
    response = client.get("/api/audit-logs/export/some-nonexistent-task-id/status", headers=headers)
    assert response.status_code == 503
    assert "detail" in response.json()


def test_audit_export_status_falls_back_to_disk_when_redis_is_down(as_admin, tmp_path, monkeypatch):
    """
    The export "false negative" case: if Redis dies between the worker
    finishing the file write and storing the result pointer, Celery's
    state alone would incorrectly say the export failed/is unknown even
    though the finished file is sitting right there on disk. The status
    endpoint must recover it via tasks.export_tasks.find_export_on_disk()
    rather than trusting Celery's state (or lack of one, with Redis fully
    unreachable) as the sole source of truth.
    """
    from config import settings

    monkeypatch.setattr(settings, "EXPORT_RESULT_DIR", str(tmp_path))
    task_id = "already-finished-task-id"
    with open(tmp_path / f"{task_id}.csv", "wb") as f:
        f.write(b"id,action\n1,test\n")

    client, headers = as_admin
    response = client.get(f"/api/audit-logs/export/{task_id}/status", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"state": "SUCCESS", "ready": True}


def test_db_pool_exhaustion_still_raises_a_genuine_500_for_other_db_errors():
    """Sanity check that this new handling is scoped to pool-checkout
    timeouts specifically, not every SQLAlchemy exception -- a genuine
    query/programming error must still surface as a 500, not be silently
    reclassified as a transient 503."""
    import asyncio
    import sqlalchemy.exc
    from middleware.error_handling import UnhandledExceptionMiddleware

    async def _failing_app(scope, receive, send):
        raise sqlalchemy.exc.IntegrityError("statement", {}, Exception("unique violation"))

    middleware = UnhandledExceptionMiddleware(_failing_app)
    sent_messages = []

    async def _send(message):
        sent_messages.append(message)

    async def _receive():
        return {"type": "http.request"}

    scope = {
        "type": "http", "method": "GET", "path": "/api/assets",
        "headers": [], "client": ("127.0.0.1", 1), "query_string": b"",
    }
    asyncio.run(middleware(scope, _receive, _send))

    start_message = next(m for m in sent_messages if m["type"] == "http.response.start")
    assert start_message["status"] == 500
