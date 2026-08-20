"""Distributed admission control for background database work.

Celery worker processes do not pass through FastAPI's HTTP DB concurrency
middleware. This module gives those DB-using tasks a small deployment-wide
Redis semaphore so they consume only the connection budget reserved for
background work. Redis is already a hard dependency of Celery. Redis is used only as a
best-effort distributed admission layer: if it is unreachable, tasks fail
open and rely on the bounded local SQLAlchemy pool rather than turning a
Redis outage into a background-job outage.

The size of that reserved budget (`server_budget` in `background_db_slot()`
below) is read from `database.PGBOUNCER_EFFECTIVE_BUDGET` when PgBouncer is
in use -- the exact same live-probed, safety-margin-adjusted number
database.py itself used to carve `background_reserve` out of the API pool's
share (see database.py's `pgbouncer_effective_budget()` docstring). Reading
the same cached value here, instead of independently recomputing it from
raw settings, is what keeps the API pool and the background-task admission
ceiling from being able to drift apart and jointly over-admit past what
PgBouncer/Postgres can actually grant.
"""

import time
import uuid
from contextlib import contextmanager

import redis

import database
from config import settings

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _client():
    return redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


@contextmanager
def background_db_slot():
    """Acquire one deployment-wide background DB slot.

    If Redis is unavailable, fail open so a transient broker/cache outage does
    not turn otherwise healthy background work into an application outage.
    The local DB pool and task-level DB budget remain the final safeguards.

    Each slot is an independent Redis key. SET NX is atomic, so two workers
    cannot claim the same slot and the configured number of live leases is a
    hard ceiling across replicas. A bounded TTL prevents a killed worker from
    permanently stranding capacity.
    """
    # Never let the configurable task limit exceed the DB capacity that
    # database.py actually removed from the API budget.  If operators set
    # the two values inconsistently, the smaller value wins safely.
    #
    # BUG FIX: this used to recompute its own "how big is the PgBouncer
    # server pool" number straight from the static
    # PGBOUNCER_SERVER_POOL_SIZE / DEFAULT_POOL_SIZE+RESERVE_POOL_SIZE
    # settings -- unlike database.py's own reserve calculation, it never
    # accounted for the live Postgres probe or the PGBOUNCER_SAFETY_MARGIN_PERCENT
    # reduction those settings go through in database.py. If the live
    # server turned out smaller than configured, database.py correctly
    # shrunk the API pool's share (and its own background reserve) to
    # match, while this function kept admitting Celery/Beat DB work
    # against the old, larger, un-probed number -- so the two sides could
    # together exceed what PgBouncer/Postgres could actually grant. Read
    # database.PGBOUNCER_EFFECTIVE_BUDGET (see that module's
    # pgbouncer_effective_budget() docstring) instead: it's the exact same
    # number database.py itself capped `background_reserve` against,
    # computed once at process startup rather than duplicated here.
    if settings.USE_PGBOUNCER and database.PGBOUNCER_EFFECTIVE_BUDGET is not None:
        server_budget = database.PGBOUNCER_EFFECTIVE_BUDGET
    elif settings.PGBOUNCER_SERVER_POOL_SIZE is not None:
        server_budget = max(int(settings.PGBOUNCER_SERVER_POOL_SIZE), 1)
    else:
        server_budget = max(
            int(settings.PGBOUNCER_DEFAULT_POOL_SIZE) + int(settings.PGBOUNCER_RESERVE_POOL_SIZE),
            1,
        )
    limit = min(
        max(int(settings.DB_BACKGROUND_CONCURRENCY_LIMIT), 0),
        max(int(settings.DB_BACKGROUND_CONNECTION_RESERVE), 0),
        max(server_budget - 1, 0),
    )
    if limit == 0:
        yield
        return

    timeout = max(float(settings.DB_BACKGROUND_ADMISSION_TIMEOUT_SECONDS), 0.0)
    client = _client()
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    acquired_key = None
    try:
        while True:
            try:
                for slot in range(limit):
                    key = f"db:background-admission:slot:{slot}"
                    if client.set(key, token, nx=True, ex=300):
                        acquired_key = key
                        break
            except redis.RedisError as exc:
                # Redis is an admission-control dependency, not the database
                # itself. Do not make a transient Redis outage prevent the
                # task from running. The bounded SQLAlchemy pool remains the
                # final local safeguard when the distributed limiter is down.
                logger = __import__("logging").getLogger(__name__)
                logger.warning(
                    "Redis unavailable for background DB admission; failing open: %s",
                    exc,
                )
                yield
                return

            if acquired_key is not None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("background DB admission capacity is busy")
            time.sleep(0.01)

        yield
    finally:
        if acquired_key is not None:
            try:
                client.eval(_RELEASE_SCRIPT, 1, acquired_key, token)
            except redis.RedisError:
                # The 5-minute lease is the safety net if Redis disappears during
                # cleanup; never let cleanup hide the task's real outcome.
                pass
        try:
            client.close()
        except Exception:
            pass
