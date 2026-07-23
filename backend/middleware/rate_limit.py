"""
middleware/rate_limit.py
--------------------------
Rate limiting for POST /auth/login (Operations & Observability requirement
#3), to slow down password-guessing / brute-force / credential-stuffing
attacks.

WHY THIS MATTERS
-----------------
Before this change, `POST /auth/login` had NO limit on how many times a
single client could try a password. An attacker (or a buggy script) could
fire thousands of guesses per second against, say, "r.adeyemi@corp.io"
with no friction at all. Password complexity/hashing (see security.py)
protects the STORED credential, but does nothing to slow down someone
guessing common passwords one request at a time.

LOAD BALANCING: REDIS INSTEAD OF AN IN-PROCESS DICT
----------------------------------------------------
This used to keep per-IP hit counts in a plain Python dict inside one
running process -- fine for a single container, but it silently broke
down the moment this app was scaled to multiple backend replicas behind
nginx (see DEPLOYMENT.md's load balancing section): each replica had its
OWN independent counter, so an attacker distributing requests across N
replicas effectively got `max_requests * N` attempts before hitting a
limit anywhere, and a legitimate user bounced between replicas by the
load balancer could get rate-limited on one replica while a fresh
allowance quietly waited for them on another.

Counts now live in Redis (the same instance already used as the Celery
broker/result backend -- see config.py's REDIS_URL) using a fixed-window
counter: `INCR` a key namespaced by client IP + the current window, set
its expiry on the very first hit of that window, and compare the
returned count to `max_requests`. Every replica reads/writes the SAME
Redis key, so the limit is enforced consistently no matter which replica
a given request lands on. A fixed window (rather than the previous
sliding-window deque) is deliberate here -- it maps onto two Redis
commands (INCR + EXPIRE) with no client-side bookkeeping at all, which
keeps this middleware fast on every single request; the small extra
burst it allows right at a window boundary is an acceptable tradeoff for
a login-attempt limiter (not a hard security perimeter) and is a known,
well-documented property of fixed-window counters.

FAIL-OPEN ON REDIS ERRORS
--------------------------
If Redis is briefly unreachable (a redeploy, a network blip), requests
are allowed through rather than blocked -- a login endpoint that's
unreachable because the RATE LIMITER'S dependency is down is a worse
outcome than temporarily losing brute-force protection. Every fail-open
is logged so it's visible in monitoring, not silent.

REMAINING LIMITATION (documented on purpose)
---------------------------------------------
Still keyed by IP ONLY (shared by everyone behind the same NAT/VPN/proxy).
A more advanced version would rate-limit by IP AND by the submitted
`identifier` (email/username) together, so one noisy IP can't lock out
every other user behind the same office network, while still stopping an
attacker hammering one specific account from many IPs. Per-account
lockout already exists as a separate, DB-backed layer -- see
services/auth_service.py's ACCOUNT_LOCKOUT_* handling, which (unlike the
old in-memory version of THIS middleware) was already safe for multiple
replicas, since every replica shares the same Postgres database.
"""

import logging
import time

import redis
from starlette.types import ASGIApp, Receive, Scope, Send

from config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "rl:login"


class RateLimitMiddleware:
    """
    Pure ASGI middleware that rate-limits POST requests to a configurable
    set of paths (in this project: just `/auth/login`). Every other path
    passes through completely untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        limited_paths: set,
        max_requests: int = 5,
        window_seconds: int = 60,
    ) -> None:
        self.app = app
        self.limited_paths = limited_paths
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # decode_responses=True: we only ever read back plain integers, no
        # point handling raw bytes everywhere below. A single shared
        # connection pool (redis-py manages this internally) is reused for
        # the lifetime of the process rather than reconnecting per request.
        self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

    def _client_ip(self, scope: Scope) -> str:
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _is_limited(self, client_ip: str) -> tuple[bool, int]:
        """
        Returns (should_block, retry_after_seconds). Fixed-window counter:
        the window is identified by `int(time.time() // window_seconds)`,
        so every client sharing that same window bucket increments the
        same Redis key. `INCR` on a brand-new key both creates it (at 1)
        and returns the new value atomically -- no separate GET needed.
        """
        window_id = int(time.time() // self.window_seconds)
        key = f"{_KEY_PREFIX}:{client_ip}:{window_id}"
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
        except redis.RedisError:
            # Fail open -- see module docstring's "FAIL-OPEN ON REDIS
            # ERRORS" section for why this is the right tradeoff here.
            logger.warning("rate_limit: Redis unavailable, allowing request through unlimited.", exc_info=True)
            return False, 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in self.limited_paths
        ):
            await self.app(scope, receive, send)
            return

        client_ip = self._client_ip(scope)
        blocked, retry_after = self._is_limited(client_ip)

        if blocked:
            logger.warning(
                "Login rate limit exceeded",
                extra={"client_ip": client_ip, "path": scope.get("path"), "retry_after": retry_after},
            )
            body = (
                b'{"detail": "Too many login attempts. Please wait a moment and try again."}'
            )
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(retry_after).encode("latin-1")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
