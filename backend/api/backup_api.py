"""
api/backup.py
--------------
Backup/restore endpoints for the Admin dashboard's "System Backups" panel.
Everything here is gated on `require_super_admin` (Super Admin / Admin
only) -- a database backup contains literally everything, including every
other user's password hash, and a restore is instantly and irreversibly
destructive to whatever's in the database right now.

See services/backup_service.py for the actual pg_dump/psql/Google Drive
implementation -- this router is intentionally thin.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from deps import require_super_admin
import services.backup_service as backup_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("/status")
def backup_status(user: dict = Depends(require_super_admin)):
    """Scheduler config, Google Drive on/off, and the most recent backup's metadata -- powers the status card at the top of the Backups panel."""
    return backup_service.get_status()


@router.get("/list")
def list_backups(user: dict = Depends(require_super_admin)):
    """Newest-first list of local backup files (each entry also reports its Google Drive upload state, if enabled)."""
    return backup_service.list_backups()


@router.post("/create")
def create_backup_now(user: dict = Depends(require_super_admin)):
    """
    "Backup Now" button. Runs pg_dump synchronously -- a full dump of this
    app's data is small/fast enough (no BLOBs live in Postgres here; asset
    photos etc. aren't part of this schema) that doing this inline, rather
    than as a background job, is simpler and gives the admin an immediate
    pass/fail instead of a polling UI.
    """
    try:
        entry = backup_service.create_backup(triggered_by="manual")
    except Exception as exc:
        logger.exception("backup: manual backup failed")
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")
    return entry


@router.get("/download/{filename}")
def download_backup(filename: str, user: dict = Depends(require_super_admin)):
    """Streams a previously-created backup file back to the browser for download (e.g. to keep an off-Drive copy, or if Google Drive isn't configured)."""
    try:
        filepath = backup_service.get_backup_filepath(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(filepath, media_type="application/gzip", filename=filename)


@router.delete("/{filename}")
def delete_backup(filename: str, user: dict = Depends(require_super_admin)):
    """Removes a local backup file (and its index entry). Does NOT touch any copy already uploaded to Google Drive."""
    backup_service.delete_backup(filename)
    return {"deleted": filename}


@router.post("/restore/{filename}")
def restore_from_local(filename: str, user: dict = Depends(require_super_admin)):
    """
    Restores from a backup already sitting on local disk. DESTRUCTIVE: the
    current database is replaced with the chosen backup's contents (a
    "pre-restore safety" backup of what's about to be overwritten is taken
    automatically first -- see services/backup_service.restore_backup()).
    """
    try:
        filepath = backup_service.get_backup_filepath(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        result = backup_service.restore_backup(filepath)
    except Exception as exc:
        logger.exception("backup: restore from local backup failed")
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
    return result


@router.post("/restore-upload")
async def restore_from_upload(
    file: UploadFile = File(...),
    user: dict = Depends(require_super_admin),
):
    """
    Recovery path for when local disk has been wiped (e.g. a Render
    redeploy/spin-down happened since the last backup): the admin
    downloads the last good backup from Google Drive and uploads it here.
    Accepts either a .sql.gz (as produced by this app) or a plain .sql
    file. DESTRUCTIVE -- see restore_from_local() above.
    """
    name_lower = (file.filename or "").lower()
    if not (name_lower.endswith(".sql.gz") or name_lower.endswith(".gz") or name_lower.endswith(".sql")):
        raise HTTPException(status_code=400, detail="File must be a .sql or .sql.gz backup file.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        result = backup_service.restore_from_upload(contents, file.filename)
    except Exception as exc:
        logger.exception("backup: restore from uploaded file failed")
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
    return result
