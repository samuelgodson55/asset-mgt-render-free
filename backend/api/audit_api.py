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
"""

import datetime
import os
from typing import Optional

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from celery_app import celery_app
from database import get_db
from deps import require_privileged_role
from tasks.export_tasks import generate_audit_export
import services.audit_service as audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("")
def get_audit_logs(
    limit: int = Query(audit_service.DEFAULT_LIMIT, ge=1, le=audit_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through the ledger)"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return audit_service.get_audit_logs(db, user, limit, offset)


@router.post("/export")
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

    task = generate_audit_export.delay(
        requested_by_email=user["email"],
        requested_by_role=user["role"],
        fmt=fmt,
        start_date_iso=start_date.isoformat() if start_date else None,
        end_date_iso=end_date.isoformat() if end_date else None,
    )
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
    result = AsyncResult(task_id, app=celery_app)
    if result.state == "PENDING":
        # Celery reports state as PENDING both for "still queued/running"
        # AND for "no such task_id has ever existed" -- it never persists
        # a distinct "unknown" state. That ambiguity is fine here: either
        # way, the correct response to the frontend is "not ready yet".
        return {"state": "PENDING", "ready": False}
    if result.state == "FAILURE":
        return {"state": "FAILURE", "ready": True, "error": str(result.result)}
    if result.state == "SUCCESS":
        return {"state": "SUCCESS", "ready": True}
    # STARTED / RETRY / any other in-progress Celery state.
    return {"state": result.state, "ready": False}


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
    result = AsyncResult(task_id, app=celery_app)
    if result.state == "FAILURE":
        raise HTTPException(status_code=500, detail=f"Export failed: {result.result}")
    if result.state != "SUCCESS":
        raise HTTPException(status_code=409, detail="Export is not ready yet.")

    payload = result.result
    if not payload:
        raise HTTPException(status_code=404, detail="Export result not found -- it may have expired. Please start a new export.")

    disk_path = payload.get("disk_path")
    if not disk_path or not os.path.isfile(disk_path):
        raise HTTPException(status_code=404, detail="Export file not found -- it may have expired. Please start a new export.")

    return FileResponse(
        path=disk_path,
        media_type=payload["content_type"],
        filename=payload["filename"],
    )
