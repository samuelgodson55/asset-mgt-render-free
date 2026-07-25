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
    include=["tasks.export_tasks", "tasks.notification_tasks"],
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
)

# ---------------------------------------------------------------------------
# CELERY BEAT SCHEDULE -- Email + Dashboard Notifications requirement
# ---------------------------------------------------------------------------
# Fires two independent, recurring jobs (both in
# backend/tasks/notification_tasks.py):
#   - `tasks.send_overdue_notifications`, every
#     `settings.OVERDUE_NOTIFICATION_INTERVAL_HOURS` (24 by default) --
#     checkouts that have ALREADY gone overdue.
#   - `tasks.send_due_soon_reminders`, every
#     `settings.DUE_SOON_NOTIFICATION_INTERVAL_HOURS` (24 by default) --
#     "a reminder before something goes overdue": checkouts still on time
#     but due within `settings.DUE_SOON_REMINDER_DAYS`.
# Using a plain `timedelta` here (rather than a fixed `crontab` clock time)
# is deliberate for both: it means the very FIRST run happens that many
# hours after whichever moment the worker container boots, rather than
# "wait until the next 2am" -- much easier to verify locally (e.g.
# temporarily set one of these to a couple of minutes and watch your
# terminal/mail-catcher for the first send) without waiting a full day.
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
        "schedule": datetime.timedelta(hours=settings.OVERDUE_NOTIFICATION_INTERVAL_HOURS),
    },
    # "A reminder before something goes overdue" -- the proactive
    # counterpart just above. Same timedelta-since-boot reasoning, its own
    # independent interval (settings.DUE_SOON_NOTIFICATION_INTERVAL_HOURS)
    # so it can be tuned (e.g. lowered for local testing) without also
    # changing how often the overdue digest fires.
    "send-due-soon-checkout-reminders": {
        "task": "tasks.send_due_soon_reminders",
        "schedule": datetime.timedelta(hours=settings.DUE_SOON_NOTIFICATION_INTERVAL_HOURS),
    },
}
