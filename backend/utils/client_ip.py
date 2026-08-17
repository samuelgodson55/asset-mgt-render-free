"""
utils/client_ip.py
-------------------
Shared "what's the real caller's IP" resolution for anything in `backend/`
that needs it for a security purpose (per-IP rate limiting, brute-force
lockouts, audit logging).

WHY THIS IS ITS OWN MODULE
---------------------------
This logic used to be implemented twice: once correctly in
middleware/rate_limit.py's `_client_ip()` (login/MFA throttle), and once
naively in api/telemetry_api.py (`request.headers.get('x-real-ip') or
request.client.host`) for the browser-telemetry throttle. The second copy
was never updated when the first one was fixed, so it kept trusting
`X-Real-IP` alone -- exactly the header proven unstable in production (see
below) -- which meant an attacker (or just a browser retried across
replicas) could dodge the telemetry rate limit while the login one worked
correctly a few files away. One implementation now, so a future fix here
can't silently miss a second call site again.

WHY X-FORWARDED-FOR BEFORE X-REAL-IP
--------------------------------------
`backend` has ingress.external: false (main.bicep) -- the ONLY way to reach
it is through the `frontend` nginx container app, which sets X-Real-IP /
X-Forwarded-For on every proxied /api/ request (see
nginx/default.conf.template). Trusting those headers is safe here
specifically because backend can't be hit directly from the internet to
spoof them.

Without this, the ASGI-level `scope["client"]` / Starlette `request.client`
is the IP of whichever `frontend` REPLICA proxied the request -- not the
real caller's IP. With frontendMaxReplicas > 1 in prod, successive requests
from the same real client can land on different frontend replicas and
therefore different `client` values, splitting one attacker/user across
several rate-limit keys and silently defeating a fixed-window counter (it
never reaches max_requests on any single key). This bit us in production
validation: session-redis-test.py sent 6 rapid login attempts and never
got a 429.

BUG FIX -- round 2: the first fix checked X-Real-IP BEFORE
X-Forwarded-For, which turned out to be exactly backwards. `frontend`
(external: true) isn't the first hop for real traffic either -- Azure
Container Apps' own platform ingress (Envoy) sits in front of it too, the
same way it sits in front of `backend`. nginx's `X-Real-IP $remote_addr`
(see nginx/default.conf.template) reflects whatever IP ACA's OWN edge
proxy connected from, which can vary request-to-request depending on
which ACA ingress node happened to handle it -- the identical class of
bug as the scope["client"] issue above, just one hop further out.
Confirmed in production logs: after the first fix deployed, Redis's INCR
was succeeding on every single login attempt (no "Redis unavailable"
fail-open warning anywhere in the logs), yet session-redis-test.py's 6
rapid attempts still never got a 429 -- only explainable by each attempt
hashing to a DIFFERENT rate-limit key, i.e. a different perceived
client_ip per request, i.e. X-Real-IP (which was checked first) was not
stable. X-Forwarded-For IS stable: ACA's ingress prepends the true
external client IP onto it before nginx ever sees it, and nginx's
`$proxy_add_x_forwarded_for` only ever appends to that (never replaces
it) -- so the LEFTMOST entry is the one value that stays constant across
every hop and every request from the same real client, regardless of
which ACA/nginx internal node handled it. Checking it first (X-Real-IP
now only a fallback) is what actually fixes this.
"""

from __future__ import annotations

from typing import Callable, Optional

# A callable that returns a header's value (case-insensitively) given its
# lowercase name, or None if absent. Both call sites below already have
# something that satisfies this shape natively:
#   - Starlette's `Request.headers.get` (a case-insensitive Mapping's
#     `.get`) matches this signature exactly -- no adapter needed.
#   - Pure-ASGI middleware (which only has `scope["headers"]`, a list of
#     raw `(bytes, bytes)` pairs, not a Headers object) uses
#     `asgi_header_getter(scope)` below to build one.
HeaderGetter = Callable[[str], Optional[str]]


def resolve_client_ip(get_header: HeaderGetter, fallback_ip: Optional[str] = None) -> str:
    """
    Resolve the real caller's IP from proxy headers, preferring
    X-Forwarded-For's leftmost entry over X-Real-IP (see module docstring
    for why the order matters). Falls back to `fallback_ip` -- normally
    `scope["client"][0]` / `request.client.host` -- for local/docker-compose
    dev, where nginx isn't necessarily in front of the backend the same way,
    and finally to the literal string "unknown" if nothing at all is
    available.
    """
    forwarded_for = get_header("x-forwarded-for")
    if forwarded_for:
        # nginx appends via $proxy_add_x_forwarded_for, so the FIRST
        # (leftmost) entry is the original client -- everything after it
        # is a hop (ACA's ingress, nginx itself, ...) added later.
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first

    real_ip = get_header("x-real-ip")
    if real_ip:
        stripped = real_ip.strip()
        if stripped:
            return stripped

    return fallback_ip or "unknown"


def asgi_header_getter(scope) -> HeaderGetter:
    """
    Adapts a raw ASGI `scope["headers"]` list (`[(b"x-forwarded-for",
    b"1.2.3.4"), ...]`) into the `HeaderGetter` shape `resolve_client_ip`
    expects, for pure-ASGI middleware that runs before Starlette builds a
    `Request`/`Headers` object at all.
    """
    headers = dict(scope.get("headers") or [])

    def get(name: str) -> Optional[str]:
        value = headers.get(name.encode("latin-1"))
        return value.decode("latin-1") if value is not None else None

    return get
