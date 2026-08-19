"""
api/backup.py
--------------
Backup/restore endpoints for the Admin dashboard's "System Backups" panel.
Every route here is gated on `require_true_super_admin` -- the root Super
Admin account only. Unlike most of the rest of this app, the regular
`admin` role is deliberately NOT treated as Super-Admin-equivalent here:
a backup contains literally everything (including every other user's
password hash, and every `admin` account's own row), and a restore
instantly and irreversibly replaces the whole database with it -- both
ends of that are kept to the one account nothing else in the app can
affect. See deps.require_true_super_admin's docstring for the full
reasoning.

See services/backup_service.py for the actual pg_dump/psql/Google Drive
implementation -- this router is intentionally thin.
"""

from telemetry import trace_operation

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from deps import require_true_super_admin
import services.backup_service as backup_service
from services.backup_service import RestoreInProgressError, BackupInProgressError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("/status")
def backup_status(user: dict = Depends(require_true_super_admin)):
    """Scheduler config, Google Drive on/off, and the most recent backup's metadata -- powers the status card at the top of the Backups panel."""
    try:
        return backup_service.get_status()
    except Exception as exc:
        # Was a bare `return backup_service.get_status()` -- any failure
        # here (e.g. an unwritable/missing BACKUP_DIR, a corrupt
        # index.json, a bad DISPLAY_TIMEZONE/BACKUP_HOURS_UTC value) fell
        # straight through to main.py's UnhandledExceptionMiddleware,
        # which logs the real traceback but hands the caller back only a
        # generic "An unexpected error occurred" 500 -- so a failure here
        # was invisible from the response itself (e.g. in a CI test log)
        # even though the app-side log had the answer the whole time.
        # Logging *and* surfacing a specific detail here (like every other
        # route in this file already does) makes that failure
        # self-diagnosing instead of a bare `assert 500 == 200`.
        logger.exception("backup: failed to load backup status")
        raise HTTPException(status_code=500, detail=f"Failed to load backup status: {exc}")


@router.get("/list")
def list_backups(user: dict = Depends(require_true_super_admin)):
    """Newest-first list of local backup files (each entry also reports its Google Drive upload state, if enabled)."""
    try:
        return backup_service.list_backups()
    except Exception as exc:
        # See backup_status()'s comment above for why this is now wrapped.
        logger.exception("backup: failed to list backups")
        raise HTTPException(status_code=500, detail=f"Failed to list backups: {exc}")


@router.post("/create")
@trace_operation("backup.create")
def create_backup_now(user: dict = Depends(require_true_super_admin)):
    """
    "Backup Now" button. Runs pg_dump synchronously -- a full dump of this
    app's data is small/fast enough (no BLOBs live in Postgres here; asset
    photos etc. aren't part of this schema) that doing this inline, rather
    than as a background job, is simpler and gives the admin an immediate
    pass/fail instead of a polling UI.
    """
    try:
        entry = backup_service.create_backup(triggered_by="manual")
    except BackupInProgressError as exc:
        # Distinct 409 (not 500) -- same reasoning as restore's own 409
        # below: this isn't a failure of THIS request, it's a correct
        # refusal because another backup (or a restore) is already
        # running. See services.backup_service._acquire_backup_lock's
        # docstring for why letting this one proceed anyway would risk a
        # silently torn/inconsistent backup file instead.
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(exc, None, 500, component="backup_api")
        logger.exception("backup: manual backup failed")
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")
    return entry


@router.get("/download/{filename}")
def download_backup(filename: str, user: dict = Depends(require_true_super_admin)):
    """Streams a previously-created backup file back to the browser for download (e.g. to keep an off-Drive copy, or if Google Drive isn't configured)."""
    try:
        filepath = backup_service.get_backup_filepath(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(filepath, media_type="application/gzip", filename=filename)


@router.delete("/{filename}")
@trace_operation("backup.delete")
def delete_backup(filename: str, user: dict = Depends(require_true_super_admin)):
    """Removes a local backup file (and its index entry). Does NOT touch any copy already uploaded to Google Drive."""
    backup_service.delete_backup(filename)
    return {"deleted": filename}


@router.get("/restore-status")
def restore_status(user: dict = Depends(require_true_super_admin)):
    """
    Poll target for the MOST RECENT restore's real outcome ("running" /
    "succeeded" / "failed" / "none") -- exists specifically because a
    restore keeps running to completion server-side even if the HTTP
    response from POST /restore/{filename} never reaches the caller (a
    closed browser tab, a dropped proxy connection, a CI/CD pipeline's
    own timeout). See services.backup_service.restore_backup()'s
    docstring for the full reasoning. Safe to poll repeatedly -- this
    only reads a small local JSON file, no database or subprocess
    involved.
    """
    try:
        return backup_service.get_restore_status()
    except Exception as exc:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(exc, None, 500, component="backup_api")
        logger.exception("backup: failed to load restore status")
        raise HTTPException(status_code=500, detail=f"Failed to load restore status: {exc}")


@router.post("/restore/{filename}")
@trace_operation("backup.restore")
def restore_from_local(filename: str, user: dict = Depends(require_true_super_admin)):
    """
    Restores from a backup already sitting on local disk. DESTRUCTIVE: the
    current database is replaced with the chosen backup's contents (a
    "pre-restore safety" backup of what's about to be overwritten is taken
    automatically first -- see services/backup_service.restore_backup()).

    Root Super Admin only -- see deps.require_true_super_admin.
    """
    try:
        filepath = backup_service.get_backup_filepath(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        result = backup_service.restore_backup(filepath)
    except RestoreInProgressError as exc:
        # Distinct 409 (not 500) -- this isn't a failure of THIS request,
        # it's a correct refusal because another restore is already
        # running. The caller should poll GET /restore-status rather than
        # retry, which is exactly what this status code + message says.
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(exc, None, 500, component="backup_api")
        logger.exception("backup: restore from local backup failed")
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
    return result


@router.post("/restore-upload")
@trace_operation("backup.restore.upload")
async def restore_from_upload(
    file: UploadFile = File(...),
    user: dict = Depends(require_true_super_admin),
):
    """
    Recovery path for when local disk has been wiped (e.g. a Render
    redeploy/spin-down happened since the last backup): the Super Admin
    downloads the last good backup from Google Drive and uploads it here.
    Accepts either a .sql.gz (as produced by this app) or a plain .sql
    file. DESTRUCTIVE -- see restore_from_local() above.

    Root Super Admin only -- see deps.require_true_super_admin.
    """
    name_lower = (file.filename or "").lower()
    if not (name_lower.endswith(".sql.gz") or name_lower.endswith(".gz") or name_lower.endswith(".sql")):
        raise HTTPException(status_code=400, detail="File must be a .sql or .sql.gz backup file.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        result = backup_service.restore_from_upload(contents, file.filename)
    except RestoreInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(exc, None, 500, component="backup_api")
        logger.exception("backup: restore from uploaded file failed")
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
    return result
