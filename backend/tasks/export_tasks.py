"""
tasks/export_tasks.py
----------------------
Builds one audit-ledger export file (CSV or PDF). Used to run as a Celery
task on a separate `worker` container; now runs as a plain function
submitted to the in-process thread pool in `jobs.py` (see that module's
docstring for why -- short version: Celery+Redis/a separate worker doesn't
fit Render's, or most platforms', free tier).

Only plain, simple arguments go in (a role/email string pair instead of
the full request-scoped `user` dict FastAPI builds, plain ISO date
strings instead of `datetime.date` objects) -- kept that way even though
nothing here needs to serialize over a broker anymore, since it's a
clean, easy-to-reason-about boundary regardless of how the function is
invoked.
"""

import base64
import datetime
import logging

import services.audit_service as audit_service
from database import SessionLocal

logger = logging.getLogger(__name__)


def generate_audit_export(
    requested_by_email: str,
    requested_by_role: str,
    fmt: str,
    start_date_iso: str | None,
    end_date_iso: str | None,
) -> dict:
    """
    Builds one audit-ledger export file (CSV or PDF) and returns a small
    dict -- `content_b64` is the finished file's bytes, base64-encoded.
    `jobs.submit()` stores this return value in memory for
    `settings.EXPORT_JOB_TTL_SECONDS`, which is what
    `GET /audit-logs/export/{job_id}/download` reads back out.

    Runs its own standalone DB session (`SessionLocal()`) rather than
    reusing anything FastAPI's `get_db()` dependency would give it --
    this function executes on a background thread-pool thread, not the
    thread handling the original HTTP request, so there is no
    request-scoped session to inherit.

    Role-scoping (Managers only ever see entries THEY personally
    generated -- see audit_service.get_audit_logs's docstring) is
    re-derived here from `requested_by_role`/`requested_by_email` rather
    than trusted blindly, exactly the same way `deps.require_privileged_role`
    would gate a synchronous endpoint -- the difference is just that the
    original JWT was already validated once, in api/audit.py, before this
    job was ever submitted (see that file for where `user["role"]`/
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
            # on a background thread with no HTTP response to stream into,
            # we just drain it into one buffer like the PDF path.
            csv_chunks = audit_service.export_audit_logs_csv(db, user, start_date, end_date)
            file_bytes = "".join(csv_chunks).encode("utf-8")
            content_type = "text/csv"
    finally:
        db.close()

    today = datetime.date.today().isoformat()
    logger.info(
        "audit_export_generated",
        extra={"format": fmt, "requested_by": requested_by_email, "bytes": len(file_bytes)},
    )
    return {
        "filename": f"audit_export_{today}.{fmt}",
        "content_type": content_type,
        "content_b64": base64.b64encode(file_bytes).decode("ascii"),
        "requested_by": requested_by_email,
    }
