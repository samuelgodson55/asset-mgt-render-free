"""
tasks/audit_partition_tasks.py
---------------------------------
Celery Beat runs `ensure_audit_log_partitions` on a schedule (see
celery_app.py's `beat_schedule`, `settings.AUDIT_PARTITION_CHECK_INTERVAL_HOURS`)
to keep `audit_logs`'s future yearly partitions pre-created -- see
services/audit_partition_service.py's module docstring for the full
"why" and alembic/versions/0010_partition_audit_logs.py for how the
partitioning itself was set up.

Like tasks/notification_tasks.py and tasks/export_tasks.py, this opens
its own standalone DB session rather than reusing any request-scoped one
(there is no request -- this runs in the `worker`/embedded-worker
process on a timer, not in response to an API call).

This task is a pure no-op against a non-Postgres database (see
audit_partition_service.py's `_is_postgres` guard), so it's safe to leave
enabled even in a local/dev/test deployment shape that never touches
Postgres directly.
"""

import logging

from celery_app import celery_app
from database import SessionLocal
import services.audit_partition_service as audit_partition_service

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.ensure_audit_log_partitions")
def ensure_audit_log_partitions():
    db = SessionLocal()
    try:
        created = audit_partition_service.ensure_future_partitions(db)
        if created:
            logger.info("audit_logs: created partitions for year(s): %s", created)
        else:
            logger.info("audit_logs: all required future partitions already exist")
        return {"created_years": created}
    finally:
        db.close()
