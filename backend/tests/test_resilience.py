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
