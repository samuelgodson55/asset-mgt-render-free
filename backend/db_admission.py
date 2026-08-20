"""Distributed admission control for background database work.

Celery worker processes do not pass through FastAPI's HTTP DB concurrency
middleware. This module gives those DB-using tasks a small deployment-wide
Redis semaphore so they consume only the connection budget reserved for
background work. Redis is already a hard dependency of Celery. Redis is used only as a
best-effort distributed admission layer: if it is unreachable, tasks fail
open and rely on the bounded local SQLAlchemy pool rather than turning a
Redis outage into a background-job outage.
"""

import time
import uuid
from contextlib import contextmanager

import redis

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
    if settings.PGBOUNCER_SERVER_POOL_SIZE is not None:
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
