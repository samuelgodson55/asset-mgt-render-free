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
"""

import base64
import datetime
import logging

import services.audit_service as audit_service
from celery_app import celery_app
from database import SessionLocal

logger = logging.getLogger(__name__)


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
    Builds one audit-ledger export file (CSV or PDF) and returns it as a
    small JSON-safe dict -- `content_b64` is the finished file's bytes,
    base64-encoded so they survive Celery's JSON result serialization.
    Celery stores this return value in Redis (the result backend) for
    `settings.EXPORT_RESULT_TTL_SECONDS`, which is what
    `GET /audit-logs/export/{task_id}/download` reads back out.

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
    user = {"email": requested_by_email, "role": requested_by_role}
    start_date = datetime.date.fromisoformat(start_date_iso) if start_date_iso else None
    end_date = datetime.date.fromisoformat(end_date_iso) if end_date_iso else None

    db = SessionLocal()
    try:
        if fmt == "pdf":
            file_bytes = audit_service.export_audit_logs_pdf(db, user, start_date, end_date)
            content_type = "application/pdf"
        else:
            # The CSV export is a row-by-row generator in audit_service
            # (built that way for the *synchronous* streaming path it used
            # to serve directly to the browser -- see its docstring). Here,
            # inside a background worker with no HTTP response to stream
            # into, we just drain it into one buffer like the PDF path.
            csv_chunks = audit_service.export_audit_logs_csv(db, user, start_date, end_date)
            file_bytes = "".join(csv_chunks).encode("utf-8")
            content_type = "text/csv"
    finally:
        db.close()

    today = datetime.date.today().isoformat()
    logger.info(
        "audit_export_generated",
        extra={"task_id": self.request.id, "format": fmt, "requested_by": requested_by_email, "bytes": len(file_bytes)},
    )
    return {
        "filename": f"audit_export_{today}.{fmt}",
        "content_type": content_type,
        "content_b64": base64.b64encode(file_bytes).decode("ascii"),
        "requested_by": requested_by_email,
    }
