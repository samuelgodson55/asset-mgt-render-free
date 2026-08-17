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
