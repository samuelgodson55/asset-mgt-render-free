"""
services/backup/scheduler.py
-------------------------------
Daily backup scheduler -- a plain daemon thread (no Celery/Redis
dependency for the scheduling itself; Redis is only used for the
leader-election lock so multiple replicas don't all fire the same
scheduled backup).
"""

from __future__ import annotations

import datetime
import logging
import threading
import time

from config import settings

from . import _acquire_scheduled_backup_lock
from .create import create_backup

logger = logging.getLogger(__name__)

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _seconds_until_next_run(hours_utc: list[int]) -> float:
    """
    Finds the soonest upcoming run time across ALL configured hours (today
    or tomorrow, whichever comes first) rather than just one -- e.g. for
    hours_utc=[3, 15, 21] at 16:00 UTC, the next run is 21:00 UTC today, not
    tomorrow's 3:00.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for hour in hours_utc:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        candidates.append(target)
    return (min(candidates) - now).total_seconds()

def _scheduler_loop() -> None:
    hours = settings.backup_hours_utc_list
    hours_label = ", ".join(f"{h:02d}:00" for h in hours)
    logger.info("backup_service: scheduler thread started -- backup at %s UTC daily.", hours_label)
    while True:
        sleep_seconds = _seconds_until_next_run(settings.backup_hours_utc_list)
        time.sleep(sleep_seconds)
        now = datetime.datetime.now(datetime.timezone.utc)
        run_label = now.strftime("%Y-%m-%dT%H")
        if _acquire_scheduled_backup_lock(run_label):
            try:
                create_backup(triggered_by="scheduled")
            except Exception:
                logger.exception("backup_service: scheduled backup failed.")
        else:
            logger.info("backup_service: another replica already claimed the %s scheduled backup -- skipping.", run_label)
        # Small buffer so a slightly-early wakeup (clock drift) can't fire twice.
        time.sleep(5)

def start_backup_scheduler() -> None:
    """
    Called once from main.py's FastAPI startup event. Safe to call more
    than once (e.g. under a test runner that imports main.py repeatedly) --
    only the first call actually starts the thread.

    Deliberately a plain `threading.Thread(daemon=True)` loop rather than
    Celery Beat: this way the daily backup runs whether or not
    RUN_EMBEDDED_WORKER/Redis are configured, since it lives directly
    inside the same uvicorn process Render always keeps running.

    Every replica of this service runs its own copy of this thread when
    scaled horizontally (see DEPLOYMENT.md's load balancing section) --
    that's safe because `_scheduler_loop` acquires a short-lived Redis
    leader lock (`_acquire_scheduled_backup_lock`) before actually firing
    each scheduled backup, so only one replica's `pg_dump` ever runs per
    scheduled time even though every replica independently wakes up for it.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        if not settings.ENABLE_AUTO_BACKUP:
            logger.info("backup_service: ENABLE_AUTO_BACKUP is false -- daily scheduled backup disabled.")
            return
        thread = threading.Thread(target=_scheduler_loop, name="backup-scheduler", daemon=True)
        thread.start()
        _scheduler_started = True
