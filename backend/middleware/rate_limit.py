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

HOW IT WORKS (fixed-window counter, beginner-friendly explanation)
---------------------------------------------------------------------
For each client IP address, we keep a short list ("deque") of timestamps of
its recent login attempts. On every new attempt:
  1. Drop any timestamps older than `window_seconds` (they're outside the
     window we care about anymore).
  2. If there are still >= `max_requests` timestamps left in the window,
     the client has hit the limit -- reject with HTTP 429 ("Too Many
     Requests") and a `Retry-After` header telling it how long to wait.
  3. Otherwise, record this attempt's timestamp and let the request
     through to the real route handler.

This is a "sliding window log" limiter -- slightly more accurate than a
naive fixed window (which can allow a burst of 2x the limit right at a
window boundary), while still being simple enough to read in one sitting.

IMPORTANT LIMITATIONS (documented on purpose -- see README.md's "Suggested
Future Features" section too)
---------------------------------------------------------------------------
  - IN-MEMORY / PER-PROCESS: counts live in a plain Python dict inside this
    one running process. If you scale the backend to multiple
    uvicorn/gunicorn WORKERS or multiple CONTAINER REPLICAS, each one has
    its OWN independent counter -- an attacker distributing requests across
    replicas effectively gets `max_requests * replica_count` attempts. Fine
    for this project's single-container docker-compose setup; NOT fine for
    a horizontally-scaled production deployment.
  - KEYED BY IP ONLY: shared by everyone behind the same NAT/VPN/proxy. A
    more advanced version would rate-limit by IP AND by the submitted
    `identifier` (email/username) together, so one noisy IP can't lock out
    every other user behind the same office network, while still stopping
    an attacker hammering one specific account from many IPs.
  - Resets to zero on every backend restart/redeploy (it's not persisted
    anywhere) -- acceptable for slowing down brute force, not a substitute
    for account lockout policies.
  - For a real multi-replica production deployment, swap this for a
    Redis-backed limiter (e.g. the `slowapi` or `fastapi-limiter` packages)
    so every replica shares the same counters. See README.md's suggested
    features list.
"""

import logging
import time
from collections import defaultdict, deque
from threading import Lock

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


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
        # client_ip -> deque of monotonic timestamps of recent attempts.
        # `Lock` guards against two requests mutating the same client's
        # deque at the exact same instant (uvicorn can run requests
        # concurrently on the same event loop / across threads).
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def _client_ip(self, scope: Scope) -> str:
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _is_limited(self, client_ip: str) -> tuple[bool, int]:
        """Returns (should_block, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            timestamps = self._hits[client_ip]
            while timestamps and now - timestamps[0] > self.window_seconds:
                timestamps.popleft()

            if len(timestamps) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - timestamps[0])))
                return True, retry_after

            timestamps.append(now)
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
