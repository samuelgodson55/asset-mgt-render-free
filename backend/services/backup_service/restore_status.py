"""
services/backup/restore_status.py
-----------------------------------
The `restore_status.json` file that GET /api/backup/restore-status polls,
so a caller (CI/CD pipeline, or an admin whose browser dropped mid-restore)
can find out how the MOST RECENT restore attempt actually went instead of
depending on the one HTTP response from POST /restore/{filename} arriving.
"""

from __future__ import annotations

import json
import logging
import os

from .storage import _ensure_backup_dir

logger = logging.getLogger(__name__)

RESTORE_STATUS_FILENAME = "restore_status.json"


def _restore_status_path() -> str:
    return os.path.join(_ensure_backup_dir(), RESTORE_STATUS_FILENAME)


def _write_restore_status(status: dict) -> None:
    """Atomic write (tmp + os.replace), same pattern as _save_index -- never leaves restore_status.json half-written for a poller to read mid-write."""
    path = _restore_status_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(status, f, indent=2, default=str)
    os.replace(tmp_path, path)


def get_restore_status() -> dict:
    """
    Powers GET /api/backup/restore-status -- the thing a caller (CI/CD
    pipeline, or an admin whose browser dropped mid-restore) actually
    polls instead of depending on the one HTTP response from POST
    /restore/{filename} ever arriving. Reflects the MOST RECENT restore
    attempt only, whether it's still running, succeeded, or failed.
    """
    path = _restore_status_path()
    if not os.path.exists(path):
        return {"status": "none", "detail": "No restore has been run yet."}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("backup_service: restore_status.json is missing/corrupt.")
        return {"status": "unknown", "detail": "restore_status.json is missing or corrupt."}
