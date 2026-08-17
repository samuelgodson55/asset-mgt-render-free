"""
utils/rate_limiter.py
----------------------
Shared Redis-backed, fixed-window rate limiter.

WHY THIS IS ITS OWN MODULE
---------------------------
Before this change, this app had TWO independent rate limiters with two
different (and very different) reliability properties:

  - middleware/rate_limit.py (POST /auth/login, MFA) -- Redis-backed, so
    every backend replica shares the same counter (see that module's own
    docstring for the full "why Redis instead of an in-process dict"
    writeup).
  - api/telemetry_api.py (POST /api/telemetry/client-error) -- a plain
    in-memory `deque` per IP, living in ONE process's memory. With more
    than one backend replica behind the load balancer, the effective
    limit silently multiplies by the replica count (each replica has its
    own independent 20-per-minute counter for the same IP), and the
    limiter resets completely on every deploy/restart.

Same problem (limit N requests per client per window), same fix (a
counter that lives in the ONE place every replica already shares --
Redis, already used as the Celery broker; see config.py's REDIS_URL),
implemented once here instead of twice with two different bugs.

FAIL-OPEN ON REDIS ERRORS
--------------------------
If Redis is briefly unreachable (a redeploy, a network blip), requests are
allowed through rather than blocked -- an endpoint that's unreachable
because the RATE LIMITER'S OWN dependency is down is a worse outcome than
temporarily losing throttling. Every fail-open is logged (and reported to
ErrorBeacon, best-effort) so it's visible in monitoring, not silent.

A fixed window (`INCR` a key namespaced by identity + current window,
`EXPIRE` it on the first hit) rather than a sliding-window log is
deliberate -- it maps onto two cheap Redis commands with no client-side
bookkeeping, keeping this fast on every request; the small extra burst it
allows right at a window boundary is an acceptable, well-documented
tradeoff for a throttle (not a hard security perimeter).
"""

from __future__ import annotations

import logging
import time

import redis

logger = logging.getLogger(__name__)


class RedisFixedWindowLimiter:
    """
    A single fixed-window counter, namespaced by `key_prefix`, shared by
    every process that constructs one with the same Redis URL + prefix.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str,
        max_requests: int,
        window_seconds: int,
    ) -> None:
        self.key_prefix = key_prefix
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # decode_responses=True: we only ever read back plain integers, no
        # point handling raw bytes everywhere below. A single shared
        # connection pool (redis-py manages this internally) is reused for
        # the lifetime of the process rather than reconnecting per request.
        self._redis = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=30,
        )

    def check(self, identity: str) -> tuple[bool, int]:
        """
        Returns (should_block, retry_after_seconds). The window is
        identified by `int(time.time() // window_seconds)`, so every
        caller sharing that same window bucket increments the same Redis
        key. `INCR` on a brand-new key both creates it (at 1) and returns
        the new value atomically -- no separate GET needed.
        """
        window_id = int(time.time() // self.window_seconds)
        key = f"{self.key_prefix}:{identity}:{window_id}"
        try:
            count = self._redis.incr(key)
            if count == 1:
                # First hit in this window -- set the key to expire once
                # the window ends, so Redis cleans it up on its own rather
                # than these keys accumulating forever.
                self._redis.expire(key, self.window_seconds)

            if count > self.max_requests:
                # Time remaining until this window rolls over -- the
                # soonest a retry could possibly succeed.
                retry_after = self.window_seconds - int(time.time() % self.window_seconds)
                return True, max(1, retry_after)
            return False, 0
        except redis.RedisError as exc:
            self._report_fail_open(exc)
            logger.warning(
                "rate_limiter(%s): Redis unavailable, allowing request through unlimited.",
                self.key_prefix,
                exc_info=True,
            )
            return False, 0

    def _report_fail_open(self, exc: redis.RedisError) -> None:
        try:
            from integrations.fastapi_errorbeacon import report_exception
            report_exception(
                exc,
                None,
                None,
                component="rate_limit",
                operation="redis_check",
                severity="warning",
                category="dependency_degraded",
                context={
                    "failure_mode": "fail_open",
                    "dependency": "redis",
                    "rate_limit_enforced": False,
                    "key_prefix": self.key_prefix,
                },
            )
        except Exception:
            # Reporting the degradation must never itself take the
            # request down -- see fastapi_errorbeacon.py's own module
            # docstring ("must never make application availability...
            # depend on ErrorBeacon being reachable").
            pass
