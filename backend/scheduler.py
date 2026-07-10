"""
scheduler.py
------------
Runs `tasks.notification_tasks.send_overdue_notifications` on a repeating
interval, entirely inside this same web service process -- no separate
Celery Beat/worker container.

WHY THIS EXISTS
---------------
Celery Beat used to fire this task from a dedicated `worker` container
(see docker-compose.yml's old `worker` service and celery_app.py's
`beat_schedule`). Neither a background worker NOR a cron job is available
on Render's (or most platforms') free tier -- see
https://render.com/docs/free, which lists Web Services, Postgres, and Key
Value as the only Free instance types. A single daemon thread started
from this same process is the simplest thing that still "just works" for
a free, single-instance deployment.

THE HONEST LIMITATION
----------------------
A free web service on Render SPINS DOWN after 15 minutes with no inbound
HTTP traffic (see README.md's "Deploying on Render's Free Plan" section),
which pauses this thread along with the entire process. If nobody's
actively using the app, the daily digest may simply not fire that day --
it resumes the next time something wakes the service back up. Two ways
to work around this, in increasing order of reliability:
  1. Keep the service warm with an external uptime pinger (e.g.
     UptimeRobot, cron-job.org) hitting GET /api/system/health every few
     minutes -- keeps this thread alive as a side effect.
  2. Point an external scheduler directly at
     POST /api/system/notifications/run (see api/system.py) instead of
     relying on this in-process timer at all -- a GitHub Actions
     scheduled workflow (see .github/workflows/notifications.yml, or add
     one) works well and is also free.
Neither is required for the app to work -- extension-request emails
(services/extension_service.py) still send immediately/inline regardless
of whether this scheduler thread is currently running. Only the *daily
overdue digest* depends on it.
"""

import logging
import threading
import time

from config import settings
from tasks.notification_tasks import send_overdue_notifications

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()


def start() -> None:
    """
    Starts the background daemon thread, if notifications are enabled and
    it isn't already running. Safe to call more than once (e.g. from a
    test suite that imports main.py multiple times) -- only the first
    call actually spawns a thread.
    """
    global _started
    with _lock:
        if _started:
            return
        if not settings.NOTIFICATIONS_ENABLED:
            logger.info("scheduler: NOTIFICATIONS_ENABLED is false -- not starting the overdue-digest scheduler.")
            return
        thread = threading.Thread(target=_run_loop, name="overdue-notification-scheduler", daemon=True)
        thread.start()
        _started = True
        logger.info(
            "scheduler: started overdue-digest scheduler",
            extra={"interval_hours": settings.OVERDUE_NOTIFICATION_INTERVAL_HOURS},
        )


def _run_loop() -> None:
    interval_seconds = max(settings.OVERDUE_NOTIFICATION_INTERVAL_HOURS, 0) * 3600
    if interval_seconds <= 0:
        logger.warning("scheduler: OVERDUE_NOTIFICATION_INTERVAL_HOURS is 0 -- scheduler thread exiting.")
        return
    # Sleep first: the very first digest fires one full interval after
    # boot, not immediately on startup. This matches the old Celery Beat
    # `timedelta` schedule's behavior and avoids spamming a digest email
    # every time the free instance spins back up from an idle nap.
    while True:
        time.sleep(interval_seconds)
        try:
            result = send_overdue_notifications()
            logger.info("scheduler: overdue-digest run complete", extra=result)
        except Exception:  # noqa: BLE001 -- never let one bad run kill the loop
            logger.exception("scheduler: overdue-digest run failed")
