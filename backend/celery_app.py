"""
celery_app.py
-------------
The single Celery application instance shared by two different processes:

  1. The FastAPI `backend` container -- as a *producer* only. It imports
     `celery_app` + the task functions from `tasks/` and calls
     `.delay(...)` on them to enqueue a job, then immediately returns a
     task_id to the browser instead of blocking a request/response cycle
     on export generation (see api/audit.py).
  2. The `worker` container (docker-compose.yml) -- as the *consumer*. It
     runs `celery -A celery_app worker` and does the actual CSV/PDF
     generation work, completely out-of-band from any HTTP request.

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
# THIS APP DOES NOT RUN A SEPARATE `celery beat` CONTAINER: docker-compose.yml
# runs `celery -A celery_app worker -B`, which embeds Beat directly inside the
# single worker process (the `-B` flag) instead of adding a fourth container.
# That's the right tradeoff for a small, single-worker "lite" deployment like
# this one -- it does NOT scale to running multiple worker replicas (each
# replica would embed its OWN Beat scheduler and every one of them would fire
# these tasks independently, sending duplicate emails). If you ever scale the
# `worker` service to more than one replica, split Beat back out into its own
# single-replica `celery -A celery_app beat` service/container instead.
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
