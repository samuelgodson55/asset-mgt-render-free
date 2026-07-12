"""
tasks/export_tasks.py
----------------------
The Celery task(s) that actually generate export files, run entirely
inside the `worker` container -- never inline in an API request/response
cycle. See celery_app.py's module docstring for how the two processes
(API producer, worker consumer) share one Celery app.

Only plain, JSON-serializable arguments go in (a role/email string pair
instead of the full request-scoped `user` dict FastAPI builds, plain ISO
date strings instead of `datetime.date` objects) and only a plain dict
comes out -- see celery_app.py's `task_serializer="json"` for why.

SPEED: DISK INSTEAD OF RAM
--------------------------
This used to return the finished file's bytes directly in the Celery
result (base64-encoded so they survive JSON serialization), which Celery
then stashes in Redis -- an in-memory datastore -- for
`settings.EXPORT_RESULT_TTL_SECONDS`. That meant every in-flight or
recently-finished export held its full file size (x1.33 for the base64
overhead) in RAM, for as long as the TTL window, whether or not anyone had
actually downloaded it yet. Fine for one person clicking "export"
occasionally; wasteful and slow once this app is scaled to handle peak
traffic (see DEPLOYMENT.md's load balancing section), where several
exports can easily be in flight and expiring concurrently.

Now the task writes the finished file to plain disk under
`settings.EXPORT_RESULT_DIR` and returns only a small JSON dict (filename,
content type, and the file's path on disk) as the actual Celery/Redis
result -- kilobytes instead of megabytes per job. `api/audit.py`'s
download endpoint streams the file straight off disk. `EXPORT_RESULT_DIR`
must be a volume shared between the `backend` and `worker` containers
(see docker-compose.yml) since `worker` is what writes the file and
`backend` is what serves it back down to the browser.
"""

import datetime
import logging
import os
import time
import uuid

import services.audit_service as audit_service
from celery_app import celery_app
from config import settings
from database import SessionLocal

logger = logging.getLogger(__name__)


def _ensure_export_dir() -> str:
    os.makedirs(settings.EXPORT_RESULT_DIR, exist_ok=True)
    return settings.EXPORT_RESULT_DIR


def _sweep_expired_exports() -> None:
    """
    Deletes any export file older than settings.EXPORT_RESULT_TTL_SECONDS.

    There's no Celery Beat entry for this -- it would need its own
    always-on schedule just to clean up a handful of files. Instead, every
    new export job sweeps the directory for stale files from PAST jobs
    before it does its own work, which is cheap (a directory listing plus
    an mtime check per file) and means disk usage never grows unbounded
    even if a lot of exports are requested and never downloaded.
    """
    export_dir = _ensure_export_dir()
    cutoff = time.time() - settings.EXPORT_RESULT_TTL_SECONDS
    try:
        for name in os.listdir(export_dir):
            path = os.path.join(export_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                continue
    except OSError:
        logger.exception("export_tasks: failed to sweep %s for expired exports.", export_dir)


@celery_app.task(name="tasks.generate_audit_export", bind=True)
def generate_audit_export(
    self,
    requested_by_email: str,
    requested_by_role: str,
    fmt: str,
    start_date_iso: str | None,
    end_date_iso: str | None,
) -> dict:
    """
    Builds one audit-ledger export file (CSV or PDF), writes it to disk
    under settings.EXPORT_RESULT_DIR, and returns a small JSON-safe dict
    describing where to find it. Celery stores this return value in Redis
    (the result backend) for `settings.EXPORT_RESULT_TTL_SECONDS`, which is
    what `GET /audit-logs/export/{task_id}/download` reads back out to
    locate and stream the actual file.

    Runs its own standalone DB session (`SessionLocal()`) rather than
    reusing anything FastAPI's `get_db()` dependency would give it --
    this function executes in a completely separate `worker` container/
    process from the one that received the original HTTP request, so
    there is no request-scoped session to inherit.

    Role-scoping (Managers only ever see entries THEY personally
    generated -- see audit_service.get_audit_logs's docstring) is
    re-derived here from `requested_by_role`/`requested_by_email` rather
    than trusted blindly, exactly the same way `deps.require_privileged_role`
    would gate a synchronous endpoint -- the difference is just that the
    original JWT was already validated once, in api/audit.py, before this
    task was ever enqueued (see that file for where `user["role"]`/
    `user["email"]` are read off the validated token and passed in here).
    """
    _sweep_expired_exports()

    user = {"email": requested_by_email, "role": requested_by_role}
    start_date = datetime.date.fromisoformat(start_date_iso) if start_date_iso else None
    end_date = datetime.date.fromisoformat(end_date_iso) if end_date_iso else None

    db = SessionLocal()
    try:
        if fmt == "pdf":
            file_bytes = audit_service.export_audit_logs_pdf(db, user, start_date, end_date)
            content_type = "application/pdf"
        else:
            csv_chunks = audit_service.export_audit_logs_csv(db, user, start_date, end_date)
            file_bytes = "".join(csv_chunks).encode("utf-8")
            content_type = "text/csv"
    finally:
        db.close()

    today = datetime.date.today().isoformat()
    filename = f"audit_export_{today}.{fmt}"

    export_dir = _ensure_export_dir()
    disk_filename = f"{self.request.id or uuid.uuid4().hex}.{fmt}"
    disk_path = os.path.join(export_dir, disk_filename)
    with open(disk_path, "wb") as f:
        f.write(file_bytes)

    logger.info(
        "audit_export_generated",
        extra={"task_id": self.request.id, "format": fmt, "requested_by": requested_by_email, "bytes": len(file_bytes)},
    )
    return {
        "filename": filename,
        "content_type": content_type,
        "disk_path": disk_path,
        "requested_by": requested_by_email,
    }
