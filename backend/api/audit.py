"""
api/audit.py
------------
GET /audit-logs, plus the async export job trio:
  POST /audit-logs/export                   -- submit a CSV/PDF export job
  GET  /audit-logs/export/{job_id}/status   -- poll a job's progress
  GET  /audit-logs/export/{job_id}/download -- fetch the finished file

WHY ASYNC?
----------
The audit ledger is the one dataset in this app guaranteed to grow without
bound (see services/audit_service.py's module docstring) -- a Super Admin
exporting a wide date range as a PDF could take long enough to generate
that it isn't safe to build inline in a single request/response cycle
(it would tie up the request, and risks the browser/proxy timing out
before the file is ready). Generation happens on a background thread (see
jobs.py and tasks/export_tasks.py) -- this router's job is just to submit
that work and let the frontend poll for it, never to build the file
itself.

This used to enqueue a job onto a separate Celery `worker` container via
Redis; now it submits to jobs.py's in-process thread pool instead -- see
that module's docstring for why (short version: a separate worker/broker
doesn't fit Render's, or most platforms', free tier).
"""

import base64
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

import jobs
from database import get_db
from deps import require_privileged_role
from tasks.export_tasks import generate_audit_export
from config import settings
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
    Submits a `generate_audit_export` job to the in-process thread pool
    (see jobs.py) and immediately returns its job_id -- this endpoint
    never blocks waiting for the file itself. `user["role"]`/`user["email"]`
    come off the already-validated JWT (see deps.require_privileged_role),
    NOT from anything the caller can directly control, so the background
    job re-derives the same (now unscoped, Manager-and-Admin-alike)
    visibility `audit_service.get_audit_logs` enforces on the synchronous
    listing endpoint above.
    """
    fmt = format.lower()
    if fmt not in ("csv", "pdf"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'pdf'.")

    job_id = jobs.submit(
        generate_audit_export,
        requested_by_email=user["email"],
        requested_by_role=user["role"],
        fmt=fmt,
        start_date_iso=start_date.isoformat() if start_date else None,
        end_date_iso=end_date.isoformat() if end_date else None,
        ttl_seconds=settings.EXPORT_JOB_TTL_SECONDS,
    )
    return {"task_id": job_id, "status": "queued"}


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
    status = jobs.get_status(task_id)
    state = status["state"]
    if state == "PENDING":
        # Reported both for "still queued" AND for "no such task_id has
        # ever existed" -- jobs.py never persists a distinct "unknown"
        # state, matching Celery's own AsyncResult behavior here. That
        # ambiguity is fine: either way, the correct response to the
        # frontend is "not ready yet".
        return {"state": "PENDING", "ready": False}
    if state == "FAILURE":
        return {"state": "FAILURE", "ready": True, "error": status["error"]}
    if state == "SUCCESS":
        return {"state": "SUCCESS", "ready": True}
    # STARTED / any other in-progress state.
    return {"state": state, "ready": False}


@router.get("/export/{task_id}/download")
def download_audit_export(task_id: str, user: dict = Depends(require_privileged_role)):
    """
    Returns the finished file for a completed export job. 409s if the job
    hasn't finished yet (the frontend is expected to have already polled
    .../status to SUCCESS before calling this) and 404s if the task_id is
    unknown or its result already expired out of memory (see
    settings.EXPORT_JOB_TTL_SECONDS, and jobs.py's note on jobs being lost
    if the process restarts/redeploys/spins down).
    """
    status = jobs.get_status(task_id)
    if status["state"] == "FAILURE":
        raise HTTPException(status_code=500, detail=f"Export failed: {status['error']}")
    if status["state"] != "SUCCESS":
        raise HTTPException(status_code=409, detail="Export is not ready yet.")

    payload = jobs.get_result(task_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Export result not found -- it may have expired. Please start a new export.")

    file_bytes = base64.b64decode(payload["content_b64"])
    headers = {"Content-Disposition": f"attachment; filename={payload['filename']}"}
    return Response(content=file_bytes, media_type=payload["content_type"], headers=headers)
