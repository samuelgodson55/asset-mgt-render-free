"""
celery_app.py
-------------
The single Celery application instance shared by two different deployment
shapes:

  1. docker-compose.yml's separate `worker` + `beat` containers (and the
     FastAPI `backend` container as a producer only, via `.delay(...)` --
     see api/audit.py).
  2. The embedded-worker deployment shape (Render's free-tier
     render-start.sh, Azure's cost-optimized backend/start.sh): worker
     AND beat run inside the SAME process as the FastAPI app, since
     neither deployment provisions a separate worker/beat service. See
     RedBeat below for why that's safe even when this process runs as
     more than one replica (Azure's `backendApp` can scale to
     `backendMaxReplicas`).

Both processes point at the SAME Redis instance (`settings.REDIS_URL`),
which Celery uses as both the message broker (where queued jobs live until
a worker picks them up) and the result backend (where a finished job's
return value -- here, the exported file's bytes, base64-encoded -- is
stored until the API reads it back out for the frontend to download).

Task modules are imported explicitly in `include=[...]` below rather than
relying on Celery's autodiscovery, since this is a small, single-package
app with no Django-style "installed apps" list to scan.
"""

import datetime

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_process_init

from config import settings
from logging_config import configure_logging

# The `worker` container runs this module directly (`celery -A celery_app
# worker`) rather than importing main.py, so nothing else calls
# `configure_logging()` for it -- do it here instead. Safe to also run a
# second time when the `backend` API container imports this module (as a
# producer): `configure_logging()` clears/replaces handlers rather than
# stacking them, so it never causes duplicate log lines either way.
configure_logging(settings)

celery_app = Celery(
    "snipeit_lite",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["tasks.export_tasks", "tasks.notification_tasks", "tasks.audit_partition_tasks", "tasks.sla_tasks"],
)

celery_app.conf.update(
    # BUG FIX (Render free-tier cold start): the render-start.sh embedded
    # worker (RUN_EMBEDDED_WORKER=true) and this FastAPI process both
    # start up on the SAME 0.1-shared-CPU free instance. Celery's
    # defaults have no bound on how long a broker connection attempt can
    # take, and it retries on startup indefinitely by default -- against
    # a Redis instance that's ALSO a free Render Key Value service (and
    # therefore ALSO asleep after inactivity), that retry loop was
    # burning the container's one sliver of CPU for a long stretch
    # before Redis finished spinning up, directly at the expense of
    # uvicorn's own boot. `broker_transport_options` bounds each
    # individual connection attempt to a couple of seconds;
    # `broker_connection_retry_on_startup`/`broker_connection_max_retries`
    # bound the total number of attempts so this settles (successfully
    # or not) in seconds rather than competing for CPU for minutes.
    broker_transport_options={
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
    },
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=5,
    # Store each task's return value (or exception) for
    # EXPORT_RESULT_TTL_SECONDS after it finishes, then let Redis expire it
    # automatically -- we don't want finished export files (which can
    # contain a full copy of the audit ledger) sitting in Redis forever if
    # nobody downloads them.
    result_expires=settings.EXPORT_RESULT_TTL_SECONDS,
    # Tasks only ever take a plain dict of JSON-safe args in and return a
    # plain dict out (see tasks/export_tasks.py) -- keeping serialization
    # to JSON (rather than Celery's default pickle) means a compromised/
    # buggy worker or broker message can't deserialize into arbitrary
    # Python objects.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # A finished job's own status/result should update in Redis as soon as
    # the worker finishes it, not batched -- exports are a low-volume,
    # latency-sensitive ("did my export finish yet?") workload, not a
    # high-throughput one.
    task_track_started=True,
    # If a worker process dies mid-export (OOM, container restart, etc.),
    # don't silently redeliver the same job to another worker and risk it
    # running twice -- better to surface the failure and let the person
    # click "export" again.
    task_acks_late=False,
    # --- RedBeat: makes embedding Beat in every replica safe -----------
    # See the "LOAD BALANCING" comment on beat_schedule below for the
    # full story. In short: RedBeat stores the schedule AND a distributed
    # lock in Redis, so no matter how many processes run
    # `celery -A celery_app worker -B`, only the one currently holding
    # the lock actually fires scheduled tasks -- the rest sit idle as
    # standby. If the lock holder dies, the lock's TTL expires and
    # another replica picks it up automatically on its next tick. No
    # manual "only run Beat on one replica" bookkeeping required.
    redbeat_redis_url=settings.REDIS_URL,
    beat_scheduler="redbeat.RedBeatScheduler",
    # How long the leader's lock is held before it must renew -- longer
    # than beat's own tick interval so a healthy leader always renews in
    # time, short enough that a crashed leader's replacement takes over
    # within a reasonable window rather than leaving the schedule stalled.
    redbeat_lock_timeout=90,
    # BUG FIX (LockNotOwnedError crash-loop): RedBeat only renews its lock
    # when Beat wakes up to check the schedule -- and Celery's own default
    # `beat_max_loop_interval` is 300s. With nothing due sooner than that
    # (this app's notification tasks run every 24h -- see beat_schedule
    # below), Beat can legitimately sleep the full 300s, which is >3x
    # longer than the 90s lock above. Redis expires the lock key mid-sleep,
    # so the next wake-up's `lock.extend()` raises LockNotOwnedError,
    # crashing the process (Compose/Container Apps then restart it, which
    # re-acquires the lock cleanly -- confusing but not data-lossy, since
    # only one replica ever holds the lock at a time either way). Forcing
    # Beat to wake well inside the lock's TTL, regardless of how far away
    # the next scheduled task is, is what actually prevents this.
    beat_max_loop_interval=30,
    # BUG FIX (fragmented/inconsistent log lines from worker & beat):
    # Celery's own `worker`/`beat` CLI commands reconfigure the root
    # logger themselves once they start (`worker_hijack_root_logger`
    # defaults to True), which clobbers `logging_config.py`'s
    # `configure_logging()` setup that already ran at import time above.
    # In practice this meant `worker`/`beat` output didn't consistently
    # use our JSON/text formatter, and multi-line output (like a
    # traceback) could come out as several separate, disjointed log
    # lines instead of one coherent record -- confusing to read and
    # awkward to alert on in a log aggregator. Setting this to False
    # leaves OUR logging config in charge for every process that imports
    # this module (`backend`, `worker`, `beat` alike), same as the
    # embedded-worker deployment shapes already get for free.
    worker_hijack_root_logger=False,
)

# ---------------------------------------------------------------------------
# DISTRIBUTED TRACING (OpenTelemetry, Operations & Observability requirement)
# ---------------------------------------------------------------------------
# A no-op when settings.OTEL_ENABLED is false (the default) -- see
# telemetry.py's module docstring for the full "why" and exactly what gets
# instrumented.
#
# WHY THIS ISN'T JUST `setup_tracing(settings)` AT PLAIN MODULE LEVEL
# ------------------------------------------------------------------
# `configure_logging(settings)` above already runs unconditionally at
# import time, and that file's own comment notes it's "safe to also run a
# second time when the `backend` API container imports this module (as a
# producer)". Tracing setup is NOT safe to copy that same pattern, for two
# separate reasons:
#
#   1. IDENTITY: if `setup_tracing()` ran here at plain import time, the
#      `backend` API process would ALSO run it -- api/audit_api.py imports
#      this module just to call `.delay(...)`, and that import happens
#      before main.py gets a chance to call its OWN `setup_tracing()` (see
#      main.py's imports at the top of that file). Whichever call runs
#      first wins (telemetry.py's `_tracing_configured` guard makes the
#      second a no-op) -- so `backend`'s own spans would end up wrongly
#      labeled with THIS file's "-worker" service name instead of
#      settings.OTEL_SERVICE_NAME.
#   2. FORK SAFETY: Celery's default (prefork) worker pool imports this
#      module exactly ONCE in the parent/arbiter process, then calls
#      `os.fork()` to create its actual task-executing child processes.
#      OpenTelemetry's BatchSpanProcessor runs a background thread that
#      batches and periodically flushes spans to the exporter -- a thread
#      started in the parent BEFORE that fork does not reliably survive
#      being forked (the child gets a copy of the thread's stack but not a
#      running copy of the thread itself), silently dropping every span
#      the child ever creates.
#
# `worker_process_init` is Celery's own documented fix for exactly this:
# it fires once inside EACH forked child process, after the fork has
# already happened -- so setting up the TracerProvider there guarantees
# every child gets a live, working background flush thread of its own.
# This is also the exact pattern opentelemetry-instrumentation-celery's
# own documentation recommends. `beat_init` covers the separate case of a
# standalone `celery beat` process (docker-compose.yml's dedicated `beat`
# service) -- that process never forks a worker pool at all, so
# `worker_process_init` would never fire there, but it still enqueues
# tasks (Redis `PUBLISH`/`LPUSH` calls this app's `instrument_redis()`
# below will trace) and deserves its own service name to tell it apart
# from `worker` in a trace waterfall view.
@worker_process_init.connect(weak=False)
def _init_worker_tracing(**_kwargs) -> None:
    from telemetry import instrument_celery, instrument_redis, instrument_sqlalchemy_engine, setup_tracing
    setup_tracing(settings, service_name=f"{settings.OTEL_SERVICE_NAME}-worker")
    instrument_celery(settings)
    instrument_redis(settings)
    # Imported here, not at module level, so this module still imports
    # cleanly even if `database.py` isn't reachable for some reason (e.g.
    # a future consumer of this file that only cares about the Celery app
    # object itself) -- `database.engine` is only ever needed at the exact
    # moment a worker process is actually about to run task code against it.
    from database import engine as db_engine
    instrument_sqlalchemy_engine(db_engine, settings)


@beat_init.connect(weak=False)
def _init_beat_tracing(**_kwargs) -> None:
    from telemetry import instrument_redis, setup_tracing
    setup_tracing(settings, service_name=f"{settings.OTEL_SERVICE_NAME}-beat")
    instrument_redis(settings)


# ---------------------------------------------------------------------------
# CELERY BEAT SCHEDULE -- Email + Dashboard Notifications requirement
# ---------------------------------------------------------------------------
# Fires five independent, recurring jobs -- two in
# backend/tasks/notification_tasks.py, backend/tasks/audit_partition_tasks.py's
# partition-maintenance check (see its own docstring, and
# services/audit_partition_service.py's, for why that one exists and why
# it's safe to run this often), and two in backend/tasks/sla_tasks.py
# (see that module's own docstring for the pending-approval SLA-nudge
# rationale):
#   - `tasks.send_overdue_notifications`, daily at
#     `settings.OVERDUE_DIGEST_HOURS_UTC` (08:00 UTC by default) --
#     checkouts that have ALREADY gone overdue.
#   - `tasks.send_due_soon_reminders`, daily at
#     `settings.DUE_SOON_DIGEST_HOURS_UTC` (08:00 UTC by default) -- "a
#     reminder before something goes overdue": checkouts still on time
#     but due within `settings.DUE_SOON_REMINDER_DAYS`.
# Both use a `crontab` (a fixed UTC clock time), not a plain `timedelta`
# -- deliberately the same scheduling model as
# services/backup_service.py's BACKUP_HOURS_UTC scheduler, so "when does
# my daily digest/backup actually land in my inbox" is one predictable
# mental model across this whole app, and both env vars accept the exact
# same comma-separated-hours syntax (e.g. "8,20" for twice a day).
# `crontab(hour=..., minute=0)` accepts that comma-separated string
# directly (Celery's own crontab syntax already supports it) -- see
# `overdue_digest_hours_utc_list`/`due_soon_digest_hours_utc_list` in
# config.py for the shared parsing/validation those strings go through
# first, so a typo fails fast at startup rather than silently being
# swallowed by Celery's own crontab parser.
#
# LOAD BALANCING: safe to embed in every replica (RedBeat)
# ------------------------------------------------------------------------
# docker-compose.yml still runs Beat as its OWN dedicated `beat` service,
# separate from `worker` -- a fine, simple shape when you have a service
# type to spare. Render's free-tier and Azure's cost-optimized layouts
# don't (see render-start.sh / backend/start.sh's RUN_EMBEDDED_WORKER),
# so they run `celery -A celery_app worker -B` -- worker AND beat -- in
# the SAME process as the API.
#
# Naively, THAT would mean every replica of an embedded worker+beat
# process independently fires these two tasks on its own timer, i.e.
# duplicate notification emails once you're running more than one
# replica (Azure's `backendApp` can scale to `backendMaxReplicas`). The
# `beat_scheduler`/`redbeat_redis_url` config above fixes this at the
# scheduler level instead of requiring operators to keep Beat pinned to
# exactly one replica by hand: RedBeat stores a distributed lock in
# Redis, so only ONE embedded replica is ever the active scheduler at a
# time, no matter how many replicas are running `-B` -- see that config
# block's comment for the full mechanism.
celery_app.conf.beat_schedule = {
    "send-overdue-checkout-notifications": {
        "task": "tasks.send_overdue_notifications",
        "schedule": crontab(hour=",".join(str(h) for h in settings.overdue_digest_hours_utc_list), minute=0),
    },
    # "A reminder before something goes overdue" -- the proactive
    # counterpart just above. Its own independent schedule
    # (settings.DUE_SOON_DIGEST_HOURS_UTC) so it can be tuned (e.g. to a
    # couple of minutes from now for local testing) without also
    # changing when the overdue digest fires.
    "send-due-soon-checkout-reminders": {
        "task": "tasks.send_due_soon_reminders",
        "schedule": crontab(hour=",".join(str(h) for h in settings.due_soon_digest_hours_utc_list), minute=0),
    },
    # Keeps `audit_logs`'s future yearly Postgres partitions pre-created --
    # see tasks/audit_partition_tasks.py and
    # services/audit_partition_service.py's module docstrings. Cheap and
    # idempotent (a no-op almost every run), and a no-op entirely against
    # a non-Postgres database, so this is safe to leave in every
    # deployment shape. Still a plain timedelta-since-boot, not a fixed
    # clock time -- unlike the two digests above, there's no "landed in
    # my inbox at a predictable time" expectation to satisfy here, just
    # "checked recently enough that a partition is never missing when a
    # write needs it".
    "ensure-audit-log-partitions": {
        "task": "tasks.ensure_audit_log_partitions",
        "schedule": datetime.timedelta(hours=settings.AUDIT_PARTITION_CHECK_INTERVAL_HOURS),
    },
    # SLA nudges on pending approvals -- see tasks/sla_tasks.py's module
    # docstring for the full rationale. Like `ensure-audit-log-partitions`
    # above (and unlike the two daily digests), these are plain
    # timedelta-since-last-tick schedules, not a fixed clock time: "how
    # promptly does an escalation land after crossing the SLA threshold"
    # is what matters here, not a specific time of day. Both queues share
    # `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` for how OFTEN the check runs;
    # each has its own independent `*_SLA_HOURS` threshold for what counts
    # as overdue for a decision (see config.py).
    "escalate-pending-extension-requests": {
        "task": "tasks.escalate_pending_extension_requests",
        "schedule": datetime.timedelta(minutes=settings.APPROVAL_SLA_CHECK_INTERVAL_MINUTES),
    },
    "escalate-pending-quotations": {
        "task": "tasks.escalate_pending_quotations",
        "schedule": datetime.timedelta(minutes=settings.APPROVAL_SLA_CHECK_INTERVAL_MINUTES),
    },
}
