"""
api/audit.py
------------
GET /audit-logs, plus the async export job trio:
  POST /audit-logs/export                    -- enqueue a CSV/PDF export job
  GET  /audit-logs/export/{task_id}/status   -- poll a job's progress
  GET  /audit-logs/export/{task_id}/download -- fetch the finished file

WHY ASYNC?
----------
The audit ledger is the one dataset in this app guaranteed to grow without
bound (see services/audit_service.py's module docstring) -- a Super Admin
exporting a wide date range as a PDF could take long enough to generate
that it isn't safe to build inline in a single request/response cycle
(it would tie up an API worker process, and risks the browser/proxy
timing the request out before the file is ready). Generation now happens
out-of-band on a separate `worker` container running Celery (see
celery_app.py and tasks/export_tasks.py) -- this router's job is just to
enqueue that work and let the frontend poll for it, never to build the
file itself.

RESILIENCE: REDIS IS ON THE CRITICAL PATH FOR ALL THREE ENDPOINTS
-------------------------------------------------------------------
Unlike most of this app's Redis usage (the rate limiter, db_admission),
which is optional and fails OPEN, every endpoint below genuinely needs
Redis -- it's both the Celery broker (POST /export enqueues into it) and
the result backend (the status/download endpoints read a job's
state/result back out of it). All three endpoints therefore fail CLOSED
with a clean 503 (see `_EXPORT_UNAVAILABLE_DETAIL`) instead of a bare 500
when Redis is unreachable, same principle already applied to the email
path (services/extension_service.py's _notify()) just adapted for an
endpoint where there's no safe "log and move on" fallback.

Separately, the status/download endpoints never trust Celery's reported
state as the sole source of truth: if the worker finishes writing the
export file to disk but loses its connection to Redis before it can
store the result, Celery reports FAILURE (or nothing at all) even though
the file exists. Both endpoints fall back to checking
`settings.EXPORT_RESULT_DIR` directly by task_id in that case -- see
tasks/export_tasks.py's `find_export_on_disk()`.
"""

from telemetry import trace_operation

import datetime
import logging
import os
from typing import Optional

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from celery_app import celery_app
from database import get_db
from deps import require_privileged_role
from tasks.export_tasks import generate_audit_export, find_export_on_disk
import services.audit_service as audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit-logs", tags=["audit"])

# Same message/status the export endpoints below all fail-soft to when
# Redis (Celery's broker AND result backend, see celery_app.py's module
# docstring) is unreachable -- one 503 the frontend can key off of
# regardless of which of the three export endpoints it hit.
_EXPORT_UNAVAILABLE_DETAIL = "Export service temporarily unavailable, try again shortly"


def _report_export_dependency_failure(exc: Exception, operation: str) -> None:
    """
    Shared best-effort ErrorBeacon report for all three export endpoints'
    Redis fail-soft paths below -- same "dependency_degraded" shape
    utils/rate_limiter.py already reports fail-OPEN Redis errors with, just
    fail-CLOSED (503) here instead, since -- unlike the rate limiter --
    there's no safe default to fall back to for "start/poll/download an
    export job" if the broker/result backend that job actually lives in
    is unreachable.
    """
    try:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(
            exc,
            None,
            503,
            component="audit_api",
            operation=operation,
            severity="warning",
            category="dependency_degraded",
            context={"failure_mode": "fail_closed_503", "dependency": "redis"},
        )
    except Exception:
        pass
    logger.warning("audit_api.%s: Redis/Celery unavailable.", operation, exc_info=True)


def _file_response_from_disk(fallback: dict) -> FileResponse:
    return FileResponse(
        path=fallback["disk_path"],
        media_type=fallback["content_type"],
        filename=fallback["filename"],
    )


@router.get("")
def get_audit_logs(
    limit: int = Query(audit_service.DEFAULT_LIMIT, ge=1, le=audit_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through the ledger)"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return audit_service.get_audit_logs(db, user, limit, offset)


@router.post("/export")
@trace_operation("audit.export.start")
def start_audit_export(
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    start_date: Optional[datetime.date] = Query(None),
    end_date: Optional[datetime.date] = Query(None),
    user: dict = Depends(require_privileged_role),
):
    """
    Enqueues a `generate_audit_export` job on the Celery worker and
    immediately returns its task_id -- this endpoint never blocks waiting
    for the file itself. `user["role"]`/`user["email"]` come off the
    already-validated JWT (see deps.require_privileged_role), NOT from
    anything the caller can directly control, so the worker task re-derives
    the same (now unscoped, Manager-and-Admin-alike) visibility
    `audit_service.get_audit_logs` enforces on the synchronous listing
    endpoint above.
    """
    fmt = format.lower()
    if fmt not in ("csv", "pdf"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'pdf'.")

    # `.delay()` itself talks to Redis (the broker) to actually enqueue the
    # job -- if Redis is down/unreachable, that raises straight out of this
    # call. Same fail-soft principle already applied to the email path
    # (services/extension_service.py's _notify()): don't let a broker
    # outage surface as an opaque, generic 500. Unlike that fire-and-forget
    # email path, though, there's no "the important part already
    # succeeded, just log and move on" available here -- starting an
    # export IS the request -- so this fails CLOSED with an explicit,
    # actionable 503 instead of failing open.
    try:
        task = generate_audit_export.delay(
            requested_by_email=user["email"],
            requested_by_role=user["role"],
            fmt=fmt,
            start_date_iso=start_date.isoformat() if start_date else None,
            end_date_iso=end_date.isoformat() if end_date else None,
        )
    except Exception as exc:
        _report_export_dependency_failure(exc, "start_export")
        raise HTTPException(status_code=503, detail=_EXPORT_UNAVAILABLE_DETAIL)
    return {"task_id": task.id, "status": "queued"}


@router.get("/export/{task_id}/status")
def get_audit_export_status(task_id: str, user: dict = Depends(require_privileged_role)):
    """
    Cheap poll endpoint the frontend calls every second or two after
    starting an export (see js/components/audit.js). Deliberately does NOT
    require the polling user to be the same one who started the job --
    task_id is an unguessable UUID and every caller here already has to
    hold a valid privileged-role session anyway, same trust boundary as
    the rest of this router.
    """
    # Reading `.state` is what actually round-trips to Redis (the result
    # backend) -- AsyncResult(...) itself is just a local object. Same
    # fail-closed 503 as start_audit_export above if that round-trip
    # can't complete at all.
    try:
        result = AsyncResult(task_id, app=celery_app)
        state = result.state
    except Exception as exc:
        _report_export_dependency_failure(exc, "poll_status")
        # BUG FIX (export "false negative"): even with Redis completely
        # unreachable, the file itself may already be sitting on disk --
        # see tasks/export_tasks.py's find_export_on_disk() docstring for
        # the exact race this covers. Only THEN fall through to the 503.
        fallback = find_export_on_disk(task_id)
        if fallback:
            return {"state": "SUCCESS", "ready": True}
        raise HTTPException(status_code=503, detail=_EXPORT_UNAVAILABLE_DETAIL)

    if state == "PENDING":
        # Celery reports state as PENDING both for "still queued/running"
        # AND for "no such task_id has ever existed" -- it never persists
        # a distinct "unknown" state. That ambiguity is fine here: either
        # way, the correct response to the frontend is "not ready yet".
        return {"state": "PENDING", "ready": False}
    if state == "FAILURE":
        # Same "false negative" case as above, just reached via a clean
        # FAILURE state instead of an exception -- the worker finished
        # writing the file, then lost its connection to Redis before it
        # could store the SUCCESS result. Trust disk over Celery's state
        # here rather than telling the person their export failed when it
        # didn't.
        fallback = find_export_on_disk(task_id)
        if fallback:
            return {"state": "SUCCESS", "ready": True}
        return {"state": "FAILURE", "ready": True, "error": str(result.result)}
    if state == "SUCCESS":
        return {"state": "SUCCESS", "ready": True}
    # STARTED / RETRY / any other in-progress Celery state.
    return {"state": state, "ready": False}


@router.get("/export/{task_id}/download")
def download_audit_export(task_id: str, user: dict = Depends(require_privileged_role)):
    """
    Returns the finished file for a completed export job. 409s if the job
    hasn't finished yet (the frontend is expected to have already polled
    .../status to SUCCESS before calling this) and 404s if the task_id is
    unknown, its result already expired out of Redis (see
    settings.EXPORT_RESULT_TTL_SECONDS), or the underlying file has already
    been swept off disk by tasks/export_tasks.py's
    _sweep_expired_exports().

    SPEED: the file is streamed straight off disk (FileResponse) rather
    than being base64-decoded out of the Celery/Redis result -- see
    tasks/export_tasks.py's module docstring for why generation now writes
    to settings.EXPORT_RESULT_DIR instead of embedding the bytes in the
    job result.
    """
    try:
        result = AsyncResult(task_id, app=celery_app)
        state = result.state
    except Exception as exc:
        _report_export_dependency_failure(exc, "download")
        fallback = find_export_on_disk(task_id)
        if fallback:
            return _file_response_from_disk(fallback)
        raise HTTPException(status_code=503, detail=_EXPORT_UNAVAILABLE_DETAIL)

    if state == "FAILURE":
        # Export "false negative": the file may already be on disk even
        # though Celery reports FAILURE -- see tasks/export_tasks.py's
        # find_export_on_disk() docstring and the /status endpoint above
        # for the exact race this covers.
        fallback = find_export_on_disk(task_id)
        if fallback:
            return _file_response_from_disk(fallback)
        raise HTTPException(status_code=500, detail=f"Export failed: {result.result}")
    if state != "SUCCESS":
        raise HTTPException(status_code=409, detail="Export is not ready yet.")

    try:
        payload = result.result
    except Exception as exc:
        _report_export_dependency_failure(exc, "download")
        fallback = find_export_on_disk(task_id)
        if fallback:
            return _file_response_from_disk(fallback)
        raise HTTPException(status_code=503, detail=_EXPORT_UNAVAILABLE_DETAIL)

    disk_path = payload.get("disk_path") if payload else None
    if not disk_path or not os.path.isfile(disk_path):
        # Same disk fallback as above -- covers a SUCCESS result whose
        # disk_path Redis returned stale/empty, not just an outright
        # FAILURE/unreachable Redis.
        fallback = find_export_on_disk(task_id)
        if fallback:
            return _file_response_from_disk(fallback)
        raise HTTPException(status_code=404, detail="Export file not found -- it may have expired. Please start a new export.")

    return FileResponse(
        path=disk_path,
        media_type=payload["content_type"],
        filename=payload["filename"],
    )
