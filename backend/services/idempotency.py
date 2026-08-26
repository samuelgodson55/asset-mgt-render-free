"""
services/idempotency.py
------------------------
Makes a client-supplied `Idempotency-Key` header safe to retry for
mutating "action" endpoints (checkout, submit cart, create pool).

THE GAP THIS CLOSES
------------------------------------------------------------------
A burst of duplicate requests from one source (a double-click that
outraces a submit button's `disabled` state, a flaky connection causing
a browser-level replay, someone impatiently clicking "Try again" twice)
is already handled SAFELY by two existing layers:

  - middleware/db_concurrency.py's per-process admission semaphore keeps
    a burst from overwhelming the DB pool.
  - `with_for_update()` row-level locking in e.g.
    services/asset_service/checkout.py serializes two concurrent
    requests for the same row so the second one re-reads
    already-updated stock instead of overselling it.

Both of those make a duplicate request FAIL safely (a lock wait, then a
correctly-rejected 400/409) -- but the second request still fully runs:
it still consumes a DB connection, an admission-queue slot, a row lock,
and a query/commit cycle, purely to be told "someone already did that."
That's correct, but wasteful under load, and it's also why a double-
clicked checkout can show the person an unfriendly "someone already
grabbed it" 409 for an action THEY THEMSELVES already completed a
moment earlier, rather than just quietly returning that same success.

run_idempotent() below lets an endpoint SKIP re-running its logic
entirely for a genuine duplicate, and return the exact same result the
first request already computed -- no second DB round trip, and no
confusing "conflict" error for what was actually just one action.

WHY THIS IS AN IN-HANDLER HELPER, NOT MIDDLEWARE
------------------------------------------------------------------
The cache key below is scoped as `idem:{action}:{user_id}:{key}`,
deliberately keyed by the AUTHENTICATED user, not just the raw
client-supplied key. This function is only ever called from inside an
endpoint that has already resolved `user` via `Depends(get_current_user)`.
A raw ASGI-level middleware version of this feature (short-circuiting
before FastAPI's routing/auth dependencies even run, the way
middleware/db_concurrency.py does for its own admission check) was
deliberately rejected: doing the cache lookup before authentication
would let one person supply another person's guessed or reused
Idempotency-Key value and receive THAT PERSON's cached response body --
a real cross-account data leak. Scoping by `user_id` and only running
after auth has already resolved is what rules that out by construction.

REDIS: AN EFFICIENCY OPTIMIZATION, NOT A CORRECTNESS DEPENDENCY
------------------------------------------------------------------
Backed by Redis (already a hard Celery dependency -- see
db_admission.py's `background_db_slot()`, whose fail-open philosophy
this mirrors exactly) using SETNX for the atomic "claim" and a bounded
poll for a concurrent in-flight duplicate. If Redis is unreachable, this
fails OPEN (just runs the action normally) rather than blocking a real
user action on a cache outage. The row-level lock inside the action
itself remains the final correctness backstop either way -- this module
only ever makes the SAFE case (a genuine duplicate) faster and friendlier;
it is never the only thing standing between the app and a double-spend.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable, TypeVar

import redis
from fastapi import HTTPException

from config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Placeholder value stored while the ORIGINAL request for a key is still
# running, so a racing duplicate can tell "still in flight, worth waiting
# for" apart from "no claim exists yet" (client.set(..., nx=True) already
# handles that second case) apart from "finished -- here's the real result".
_PENDING_SENTINEL = "__pending__"

# How long a claimed key (and, once set, its cached result) survives.
# Generous enough to cover a slow request plus a person re-clicking after
# stepping away for a bit, short enough that it doesn't matter this cache
# is never explicitly invalidated by anything else in the app.
_DEFAULT_TTL_SECONDS = 300

_POLL_INTERVAL_SECONDS = 0.1
# A duplicate waits at most this long for the original request to finish
# before giving up and telling the caller to retry, rather than either
# hanging indefinitely or running a second copy of the action.
_POLL_TIMEOUT_SECONDS = 8.0


def _client() -> redis.Redis:
    return redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def run_idempotent(
    *,
    action: str,
    user_id: int,
    key: str | None,
    fn: Callable[[], dict],
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> dict:
    """Run `fn()` -- a service function returning a JSON-serializable
    dict, exactly what every call site below already returns -- at most
    once per (action, user_id, key), replaying the same result for any
    retry that reuses the same key within `ttl_seconds`.

    `key` is expected to be a client-generated, per-click/per-submit
    token (a UUID minted once per button click -- see the frontend's
    `lib/idempotency.ts`), NOT a fresh value per HTTP attempt. Two
    requests racing in from the exact same click carry the SAME key and
    are deduplicated here; two separate, legitimate clicks (e.g.
    submitting two different carts back to back) carry DIFFERENT keys
    and both run normally.

    `key=None` (no Idempotency-Key header sent) is a full no-op
    passthrough -- this is opt-in per request, and any caller that
    doesn't send the header keeps its exact existing behavior.
    """
    if not key:
        return fn()

    redis_key = f"idem:{action}:{user_id}:{key}"
    try:
        client = _client()
    except Exception as exc:
        logger.warning("idempotency: Redis client construction failed for action=%s; running without dedupe: %s", action, exc)
        return fn()

    try:
        claimed = client.set(redis_key, _PENDING_SENTINEL, nx=True, ex=ttl_seconds)
    except redis.RedisError as exc:
        # Fail open: an unreachable Redis must not block a real user
        # action. The row-level lock inside `fn()` remains the correctness
        # backstop for any genuine concurrent duplicate that slips through.
        logger.warning("idempotency: Redis unavailable for action=%s; running without dedupe: %s", action, exc)
        return fn()

    if claimed:
        try:
            result = fn()
        except Exception:
            # Don't let a failed attempt permanently occupy this key --
            # release it so a retry (same key or a fresh one) isn't forced
            # to wait out the full TTL for something that never actually
            # happened.
            try:
                client.delete(redis_key)
            except redis.RedisError:
                pass
            raise
        try:
            client.set(redis_key, json.dumps(result), ex=ttl_seconds)
        except redis.RedisError:
            # The result already reached THIS caller either way; losing
            # the cached copy only means a LATER retry with this same key
            # re-runs fn() instead of replaying it -- degraded, not
            # broken, and no worse than the key simply having expired.
            pass
        return result

    # Someone else already claimed this key -- either that request is
    # still in flight, or it already finished and cached a result. Poll
    # briefly rather than immediately erroring: the common case (a
    # near-simultaneous double-click) resolves in well under a second.
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            cached = client.get(redis_key)
        except redis.RedisError as exc:
            logger.warning("idempotency: Redis unavailable while polling action=%s; running without dedupe: %s", action, exc)
            return fn()
        if cached is not None and cached != _PENDING_SENTINEL:
            try:
                return json.loads(cached)
            except (TypeError, ValueError):
                # Cached value wasn't valid JSON for some reason -- don't
                # fail the request over a caching bug, just recompute.
                return fn()
        time.sleep(_POLL_INTERVAL_SECONDS)

    # The original request is taking unusually long. Ask the caller to
    # retry (their No-op-safe "Try again" button already does exactly
    # this -- see frontend's ErrorState) rather than either running a
    # second copy of the action or hanging this request indefinitely.
    raise HTTPException(
        status_code=409,
        detail="This action is already being processed. Please wait a moment and try again.",
    )
