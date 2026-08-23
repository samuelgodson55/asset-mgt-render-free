"""
services/backup/restore.py
-----------------------------
Public restore entry points. Wraps restore_impl._restore_backup_impl()
(the actual pg_terminate_backend/DROP SCHEMA/psql/alembic work) with the
distributed lock + persisted status file it needs specifically because a
restore keeps running to completion in its own thread even after the HTTP
request that triggered it is gone.
"""

from __future__ import annotations

import datetime
import gzip
import os
import shutil
import tempfile
import uuid

from . import _acquire_restore_lock, _release_restore_lock
from .restore_impl import _restore_backup_impl
from .restore_status import _write_restore_status


def restore_backup(filepath: str, take_safety_backup: bool = True) -> dict:
    """
    Public entry point for a restore -- wraps _restore_backup_impl() (the
    actual pg_terminate_backend/DROP SCHEMA/psql/alembic work, unchanged)
    with two things that matter specifically because a restore keeps
    running to completion in its own thread even after the HTTP request
    that triggered it is gone (closed browser tab, or nginx's default
    `proxy_ignore_client_abort off` dropping the upstream connection on
    client disconnect -- see RestoreInProgressError's docstring):

      1. A distributed lock (_acquire_restore_lock) so a second restore
         request -- e.g. a CI/CD pipeline reasonably retrying one it
         never got a response for -- can't start while this one is still
         running and race it against the same database.
      2. A persisted status file (restore_status.json, via
         _write_restore_status) that a caller can poll via GET
         /api/backup/restore-status for the REAL outcome, instead of
         depending on that one HTTP response ever arriving.

    Raises RestoreInProgressError (unchanged, propagates straight
    through) if another restore already holds the lock -- the caller
    (backup_api.py) turns that into a 409, distinct from every other
    failure here which is a 500.
    """
    token = uuid.uuid4().hex
    _acquire_restore_lock(token)  # raises RestoreInProgressError / RuntimeError -- nothing written yet if so
    started_at = datetime.datetime.now(datetime.timezone.utc)
    _write_restore_status({
        "status": "running",
        "restore_from": os.path.basename(filepath),
        "started_at": started_at.isoformat(),
        "finished_at": None,
    })
    try:
        result = _restore_backup_impl(filepath, take_safety_backup=take_safety_backup)
    except Exception as exc:
        _write_restore_status({
            "status": "failed",
            "restore_from": os.path.basename(filepath),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "error": str(exc),
        })
        raise
    else:
        _write_restore_status({
            "status": "succeeded",
            "restore_from": os.path.basename(filepath),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "result": result,
        })
        return result
    finally:
        _release_restore_lock(token)

def restore_from_upload(file_bytes: bytes, original_filename: str) -> dict:
    """
    Writes an uploaded backup file to a temp path and restores from it --
    the recovery path for when local disk was wiped (a Render redeploy/
    spin-down) and the admin has downloaded the last good .sql.gz from
    Google Drive and is uploading it back through the Restore modal.
    """
    suffix = ".sql.gz" if original_filename.endswith(".gz") else ".sql"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        # Uploaded file might be a plain .sql (not gzipped) -- normalize to
        # gzip on disk since restore_backup() always gzip-decompresses.
        if not original_filename.endswith(".gz"):
            gz_path = tmp_path + ".gz"
            with open(tmp_path, "rb") as raw, gzip.open(gz_path, "wb") as gz_out:
                shutil.copyfileobj(raw, gz_out)
            os.remove(tmp_path)
            tmp_path = gz_path
        return restore_backup(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
