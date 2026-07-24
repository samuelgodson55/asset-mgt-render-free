"""
tests/test_redbeat_scheduling.py
---------------------------------
Verifies the exact mechanism celery_app.py relies on to make it safe to run
the embedded Celery worker+beat in EVERY backend replica (Render's
render-start.sh, Azure's backend/start.sh) without an operator manually
pinning Beat to a single instance: RedBeat's Redis-backed distributed lock.

Without this lock, N replicas each running `celery -A celery_app worker -B`
would each independently fire every entry in celery_app.py's
`beat_schedule` (the overdue/due-soon notification digest) on its own
timer -- N copies of the same email, once per replica, forever. RedBeat's
`beat_scheduler`/`redbeat_redis_url` config (see celery_app.py's
`celery_app.conf.update(...)` block) makes only ONE replica at a time the
active scheduler; every other replica sits idle as a standby and
automatically takes over if the active one dies.

WHY THESE TESTS BUILD THEIR OWN THROWAWAY CELERY APPS
------------------------------------------------------
conftest.py deliberately points `REDIS_URL` at an unreachable host for the
rest of this suite (see its own module docstring: "keeps this test suite
dependency-free"), and `celery_app` is a process-wide singleton other test
files already import transitively (via `main` -> `api.audit`) by the time
any test runs -- so its config is fixed to that unreachable URL for this
whole pytest session. Building fresh `Celery(...)` app objects here,
configured with the exact same `beat_scheduler`/`redbeat_redis_url`
settings but pointed at a REAL Redis instead, is what actually lets these
tests exercise the real locking behavior instead of just asserting a
config dict.

`test_celery_app_is_configured_with_redbeat` is the one test in this file
that DOES import the real `celery_app` singleton -- it only reads
`.conf` values (no network I/O), so it works regardless of whether Redis
is reachable, and exists specifically to catch someone accidentally
removing the RedBeat config from the real app in the future.

WHY A REAL REDIS SERVER IN CI (LIKE test_migrations.py's REAL POSTGRES)
------------------------------------------------------------------------
RedBeat's lock is `redis-py`'s own `Redis.lock(...)` -- a Lua-scripted
SET-NX-with-expiry plus a compare-and-delete on release -- and there's no
in-memory fake substitute for it in this project's dependencies. Faking it
well enough to trust the result would mean re-implementing (and therefore
re-trusting) the exact locking primitive under test, which defeats the
point. This uses the same TEST_REDIS_HOST/TEST_REDIS_PORT +
skip-if-unreachable pattern test_migrations.py established for its own
real-Postgres dependency -- see that file's module docstring for the full
reasoning, which applies here unchanged.
"""

import os
import time

import pytest
import redis as redis_lib
from celery import Celery

REDIS_HOST = os.environ.get("TEST_REDIS_HOST", "localhost")
REDIS_PORT = os.environ.get("TEST_REDIS_PORT", "6379")
# A dedicated logical DB (not 0, the default anything else might use) so
# this file's flushdb() calls can never touch another process's data --
# CI's Redis service container is scoped to this job anyway, but a
# developer running this locally against their own Redis shouldn't have
# to worry about it either.
REDIS_DB = 15
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"


def _connect_or_skip():
    try:
        client = redis_lib.Redis(
            host=REDIS_HOST,
            port=int(REDIS_PORT),
            db=REDIS_DB,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        return client
    except redis_lib.exceptions.RedisError as exc:
        pytest.skip(
            f"No Redis server reachable at {REDIS_HOST}:{REDIS_PORT} ({exc}). "
            "This file needs a real Redis instance -- see its module docstring "
            "for why a fake substitute won't do. Start one locally (e.g. "
            "`docker run -p 6379:6379 redis:7-alpine`) or just skip this file; "
            "CI always has one available (see .github/workflows/ci.yml)."
        )


@pytest.fixture()
def clean_redis():
    """A real Redis client, scoped to a dedicated logical DB this file
    flushes before AND after every test -- so a lock left over from a
    previous (e.g. crashed mid-test) run can never leak into the next
    one, and this file never has to worry about cleaning up after itself
    beyond that."""
    client = _connect_or_skip()
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def _make_replica_app(name):
    """Builds one throwaway Celery app configured exactly like the real
    backend.celery_app's RedBeat setup (see that module's
    `celery_app.conf.update(...)` block) -- minus the parts irrelevant
    here (task result storage, connection retry tuning). Each call
    simulates one independent replica process: same Redis, same (default)
    lock key, but otherwise fully independent Python objects sharing no
    state of their own -- exactly like two separate backend Container App
    replicas, or two separate Render instances, would be."""
    app = Celery(name, broker=REDIS_URL, backend=REDIS_URL)
    app.conf.update(
        redbeat_redis_url=REDIS_URL,
        beat_scheduler="redbeat.RedBeatScheduler",
        # Short on purpose so test_lock_expires_and_fails_over_if_the_active_replica_dies
        # doesn't have to sleep through celery_app.py's real 90s production value.
        redbeat_lock_timeout=2,
    )
    return app


def test_celery_app_is_configured_with_redbeat():
    """Regression guard, no Redis required: fails loudly if someone edits
    celery_app.py and accidentally drops the RedBeat config -- which would
    silently reintroduce the "duplicate email per replica" bug this whole
    file exists to catch, without needing a Redis server to tell you so."""
    from celery_app import celery_app as real_celery_app  # local import: see module docstring

    conf = real_celery_app.conf
    assert conf.beat_scheduler == "redbeat.RedBeatScheduler", (
        "celery_app.py must configure RedBeat as the Beat scheduler -- without it, embedding "
        "`-B` in every replica (render-start.sh / backend/start.sh) would fire scheduled tasks "
        "once per replica instead of once, total"
    )
    assert conf.redbeat_redis_url, "redbeat_redis_url must be set for the scheduler above to have anywhere to store its lock"


def test_only_one_replica_can_hold_the_beat_lock_at_once(clean_redis):
    """The core mechanism: two independent "replicas" (two separate Celery
    app objects, same Redis) both try to become the active Beat scheduler
    for the same tick. Mirrors redbeat.schedulers.acquire_distributed_beat_lock
    exactly (same `redis_client.lock(...)` call this library makes on Beat
    startup), just with `blocking=False` so a failed second attempt
    returns False immediately instead of hanging this test forever waiting
    on a lock nothing is ever going to release."""
    from redbeat.schedulers import RedBeatScheduler, get_redis

    replica_a = RedBeatScheduler(app=_make_replica_app("replica-a"), lazy=True)
    replica_b = RedBeatScheduler(app=_make_replica_app("replica-b"), lazy=True)

    lock_a = get_redis(replica_a.app).lock(replica_a.lock_key, timeout=replica_a.lock_timeout, sleep=0.1)
    lock_b = get_redis(replica_b.app).lock(replica_b.lock_key, timeout=replica_b.lock_timeout, sleep=0.1)

    assert lock_a.acquire(blocking=False) is True, "the first replica to start up should become the active scheduler"
    assert lock_b.acquire(blocking=False) is False, (
        "a second replica must NOT also become active while the first still holds the lock -- if this "
        "were True, both replicas would independently fire the same scheduled task"
    )

    lock_a.release()
    assert lock_b.acquire(blocking=False) is True, (
        "once the active replica releases the lock (e.g. a clean shutdown), a standby replica must be "
        "able to take over as the new active scheduler"
    )
    lock_b.release()


def test_lock_expires_and_fails_over_if_the_active_replica_dies(clean_redis):
    """Same idea as above, but simulating a crash rather than a clean
    shutdown: replica A acquires the lock and then simply disappears --
    never calls .release(). Because the lock carries a TTL
    (redbeat_lock_timeout, wired through as this scheduler's
    lock_timeout), replica B must still be able to take over once that
    TTL elapses -- proving the notification digest resumes on its own
    rather than staying stalled forever just because whichever replica
    held the lock got OOM-killed, rescheduled, or redeployed."""
    from redbeat.schedulers import RedBeatScheduler, get_redis

    replica_a = RedBeatScheduler(app=_make_replica_app("replica-a-crash"), lazy=True)
    replica_b = RedBeatScheduler(app=_make_replica_app("replica-b-takeover"), lazy=True)

    lock_a = get_redis(replica_a.app).lock(replica_a.lock_key, timeout=replica_a.lock_timeout, sleep=0.1)
    lock_b = get_redis(replica_b.app).lock(replica_b.lock_key, timeout=replica_b.lock_timeout, sleep=0.1)

    assert lock_a.acquire(blocking=False) is True
    # Deliberately no lock_a.release() here -- this stands in for replica A
    # crashing without ever getting a chance to shut down cleanly.
    assert lock_b.acquire(blocking=False) is False, "replica B shouldn't take over before the lock actually expires"

    time.sleep(replica_a.lock_timeout + 0.5)  # wait out the crashed replica's TTL

    assert lock_b.acquire(blocking=False) is True, (
        "once the crashed replica's lock TTL elapses, a standby replica must be able to take over as the "
        "new active scheduler -- otherwise a single crashed leader would permanently stall the "
        "notification digest with no automatic recovery"
    )
    lock_b.release()


def test_two_replicas_racing_only_one_actually_dispatches_the_scheduled_task(clean_redis):
    """Ties the lock mechanism above directly to actual task execution
    counts -- closest to answering "does this actually stop duplicate
    emails from going out": two replicas simultaneously "wake up" for the
    same tick and each independently decides whether to dispatch a
    (here, lightweight/fake) scheduled notification task, gated by
    whether it is holding the beat lock -- exactly what
    RedBeatScheduler.maybe_due() does internally before calling
    apply_async(). Regardless of both replicas racing for the same tick,
    the task must execute exactly once, never twice."""
    from redbeat.schedulers import RedBeatScheduler, get_redis

    dispatch_count = {"n": 0}

    app_a = _make_replica_app("replica-a-dispatch")
    app_b = _make_replica_app("replica-b-dispatch")

    # task_always_eager: .apply_async() below runs the task body
    # synchronously, in-process, instead of needing a real separate worker
    # to consume it. All that matters for this test is HOW MANY TIMES the
    # task body ran -- out-of-band delivery through a real worker is
    # already covered by every other test in this suite that exercises
    # `.delay(...)` against the real celery_app (see api/audit.py's
    # callers in test_quotation_workflow.py and friends).
    for app in (app_a, app_b):
        app.conf.task_always_eager = True

    @app_a.task(name="send_notification_digest")
    def send_notification_digest_a():
        dispatch_count["n"] += 1

    @app_b.task(name="send_notification_digest")
    def send_notification_digest_b():
        dispatch_count["n"] += 1

    replica_a = RedBeatScheduler(app=app_a, lazy=True)
    replica_b = RedBeatScheduler(app=app_b, lazy=True)

    def _try_dispatch(replica, task):
        """The same pattern RedBeatScheduler.maybe_due() follows
        internally: only actually send the task if this replica is (or,
        having just acquired the lock, becomes) the active scheduler."""
        lock = get_redis(replica.app).lock(replica.lock_key, timeout=replica.lock_timeout, sleep=0.1)
        if lock.acquire(blocking=False):
            task.apply_async()
            return True
        return False

    # Both "replicas" race for the same tick at effectively the same time.
    a_dispatched = _try_dispatch(replica_a, send_notification_digest_a)
    b_dispatched = _try_dispatch(replica_b, send_notification_digest_b)

    assert a_dispatched != b_dispatched, "exactly one replica should have won the race for this tick"
    assert dispatch_count["n"] == 1, (
        f"the scheduled task ran {dispatch_count['n']} times for a single tick -- it must run exactly "
        "once no matter how many replicas are racing to dispatch it, or Render/Azure users would get "
        "duplicate overdue/due-soon notification emails"
    )
