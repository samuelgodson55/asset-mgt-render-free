"""
services/backup/create.py
---------------------------
Creating a new backup (pg_dump -> gzip -> local file + index.json entry +
optional Google Drive upload + retention), and reading that index back:
listing, resolving a filename to a filepath, deleting, and the overall
status blob the dashboard polls.
"""

from __future__ import annotations

import datetime
import gzip
import logging
import os
import subprocess
import uuid
from typing import Optional

from config import settings
import services.export_service as export_service

from . import (
    _acquire_backup_lock,
    _release_backup_lock,
)
from .gdrive import upload_to_gdrive
from .storage import (
    _index_lock,
    _ensure_backup_dir,
    _load_index,
    _save_index,
    _db_connection_kwargs,
    _require_binary,
)

logger = logging.getLogger(__name__)


def create_backup(triggered_by: str = "manual", _held_lock_token: Optional[str] = None) -> dict:
    """
    Runs `pg_dump` against the direct PostgreSQL URL, gzip-compresses the
    output, writes it to settings.BACKUP_DIR, records it in index.json,
    uploads it to Google Drive if enabled, then enforces local retention.
    Returns the new backup's index entry (including any upload error, so
    the caller can surface a partial-success state instead of a bare 500).

    `_held_lock_token` is internal-only, used exclusively by
    _restore_backup_impl()'s pre-restore safety backup: when set, this
    call trusts that its caller already holds BACKUP_LOCK_KEY under that
    exact token and skips acquiring/releasing it itself here, rather than
    briefly taking the lock out from under (and then handing it right back
    to) the restore that's about to hold it for its own destructive window
    anyway. Every other caller (the "Backup Now" button, the daily
    scheduler) leaves this as None and gets the normal, fully independent
    lock lifecycle below.
    """
    _require_binary("pg_dump")
    conn = _db_connection_kwargs()
    timestamp = datetime.datetime.now(datetime.timezone.utc)
    # TIMEZONE FIX (matches the audit/properties-assigned export fix in
    # services/export_service.py): `created_at` below stays a real UTC
    # instant (correct, unambiguous, and what every comparison/sort in
    # this module uses) -- but the FILENAME is what a person actually
    # reads on screen next to it (js/components/backups.js renders
    # `entry.filename` as plain text), and that column's `created_at` is
    # separately displayed via `new Date(...).toLocaleString()`
    # (browser-local). A raw UTC timestamp baked into the filename with
    # no zone marker used to silently disagree with that adjacent
    # browser-local column by an hour (or more) for anyone outside UTC --
    # same bug, same fix: render the filename's timestamp in
    # DISPLAY_TIMEZONE (see config.py) and label it with the zone's real
    # abbreviation instead of leaving it unlabeled.
    display_timestamp = timestamp.astimezone(export_service.DISPLAY_TZ)
    # Defensive: most IANA zone abbreviations (WAT, EST, JST, ...) are
    # already filename-safe, but sanitize anyway in case DISPLAY_TIMEZONE
    # is ever set to an unusual zone whose abbreviation contains a
    # character (space, '+', ':') that isn't safe in a filename.
    tz_label = "".join(ch for ch in display_timestamp.tzname() if ch.isalnum()) or "TZ"
    filename = f"snipeit_backup_{display_timestamp.strftime('%Y%m%d_%H%M%S')}_{tz_label}.sql.gz"
    backup_dir = _ensure_backup_dir()

    cmd = [
        "pg_dump",
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["dbname"],
        "--no-owner",
        "--no-privileges",
        "--format", "plain",
    ]

    backup_token = _held_lock_token if _held_lock_token is not None else uuid.uuid4().hex
    if _held_lock_token is None:
        _acquire_backup_lock(backup_token)  # raises BackupInProgressError -- nothing written yet if so

    # BUG FIX -- caught by an end-to-end test of the credential-
    # reconciliation feature above, not a report, but a real and serious
    # one: this filename only has SECOND resolution. Two backups landing
    # in the same second (trivially easy: a manual "Backup Now" click
    # immediately followed by a restore, whose own pre-restore safety
    # backup can land in that same second) used to collide on this exact
    # `filepath` and silently overwrite whichever one wrote second --
    # including, in the worst case, overwriting the very file a restore
    # already has open/queued to read FROM, so `psql` would end up
    # loading the wrong dump entirely with no error or warning anywhere.
    # Guarantee uniqueness up front instead of hoping the clock never
    # repeats: if this exact name is already taken, disambiguate with a
    # short counter suffix until it isn't. Deliberately done AFTER
    # acquiring the lock above (not before) -- picking a filename is
    # itself a check-then-act race between two concurrent create_backup()
    # calls, and the lock is exactly what makes "only one caller is ever
    # choosing a filename at a time" true.
    filepath = os.path.join(backup_dir, filename)
    if os.path.exists(filepath):
        suffix = 2
        while True:
            candidate = os.path.join(backup_dir, f"snipeit_backup_{display_timestamp.strftime('%Y%m%d_%H%M%S')}_{tz_label}-{suffix}.sql.gz")
            if not os.path.exists(candidate):
                filepath = candidate
                filename = os.path.basename(candidate)
                break
            suffix += 1

    logger.info("backup_service: starting pg_dump -> %s", filename)
    try:
        with gzip.open(filepath, "wb") as gz_out:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=conn["env"],
                timeout=600,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {result.stderr.decode(errors='replace')[:2000]}")
            gz_out.write(result.stdout)
    except Exception:
        # Don't leave a half-written .sql.gz file sitting in the backup
        # directory / index if pg_dump failed partway through.
        if os.path.exists(filepath):
            os.remove(filepath)
        raise
    finally:
        if _held_lock_token is None:
            _release_backup_lock(backup_token)

    size_bytes = os.path.getsize(filepath)
    entry = {
        "filename": filename,
        "created_at": timestamp.isoformat(),
        "size_bytes": size_bytes,
        "triggered_by": triggered_by,  # "manual" | "scheduled" | "pre_restore_safety"
        "gdrive_uploaded": False,
        "gdrive_file_id": None,
        "gdrive_error": None,
    }

    if settings.BACKUP_GDRIVE_ENABLED:
        try:
            file_id = upload_to_gdrive(filepath, filename)
            entry["gdrive_uploaded"] = True
            entry["gdrive_file_id"] = file_id
        except Exception as exc:
            logger.exception("backup_service: Google Drive upload failed for %s", filename)
            entry["gdrive_error"] = str(exc)

    with _index_lock:
        entries = _load_index()
        entries.append(entry)
        _save_index(entries)

    _enforce_retention()
    logger.info("backup_service: backup complete -> %s (%d bytes)", filename, size_bytes)
    return entry


# ---------------------------------------------------------------------------
# Listing / downloading / deleting
# ---------------------------------------------------------------------------

def list_backups() -> list[dict]:
    """Newest first. Silently drops any index entry whose file no longer exists on disk."""
    entries = _load_index()
    backup_dir = _ensure_backup_dir()
    live = [e for e in entries if os.path.exists(os.path.join(backup_dir, e["filename"]))]
    if len(live) != len(entries):
        with _index_lock:
            _save_index(live)
    return sorted(live, key=lambda e: e["created_at"], reverse=True)

def get_backup_filepath(filename: str) -> str:
    """Validates `filename` against the index (prevents path traversal via a crafted name) and returns its full path."""
    safe_name = os.path.basename(filename)
    entries = _load_index()
    if not any(e["filename"] == safe_name for e in entries):
        raise FileNotFoundError(f"No known backup named '{safe_name}'.")
    filepath = os.path.join(_ensure_backup_dir(), safe_name)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Backup '{safe_name}' is recorded but its file is missing from disk.")
    return filepath

def delete_backup(filename: str) -> None:
    safe_name = os.path.basename(filename)
    filepath = os.path.join(_ensure_backup_dir(), safe_name)
    if os.path.exists(filepath):
        os.remove(filepath)
    with _index_lock:
        entries = [e for e in _load_index() if e["filename"] != safe_name]
        _save_index(entries)

def _enforce_retention() -> None:
    keep = max(1, settings.BACKUP_RETENTION_COUNT)
    entries = sorted(_load_index(), key=lambda e: e["created_at"], reverse=True)
    to_delete = entries[keep:]
    if not to_delete:
        return
    backup_dir = _ensure_backup_dir()
    for entry in to_delete:
        path = os.path.join(backup_dir, entry["filename"])
        if os.path.exists(path):
            os.remove(path)
    with _index_lock:
        _save_index(entries[:keep])
    logger.info("backup_service: retention removed %d old local backup(s) (Google Drive copies, if any, are untouched).", len(to_delete))

def _display_schedule_hours(hours_utc: list[int]) -> list[str]:
    """
    Converts BACKUP_HOURS_UTC's schedule hours into DISPLAY_TIMEZONE
    "HH:MM" strings for the System Backups panel -- same "don't show a
    person a raw UTC number with no zone label" fix as everywhere else in
    this app (see export_service.py's module docstring). A schedule hour
    has no specific calendar date of its own, so this projects each one
    onto TODAY (UTC) purely so zoneinfo can do a correct, DST-aware
    conversion, then keeps only the resulting local HH:MM -- the date
    component is thrown away since only the daily wall-clock time matters
    for a recurring schedule. Sorted/deduped afterwards because converting
    can reorder hours (e.g. 23:00 UTC becomes 00:00 the next day in WAT)
    or, rarely, land two different UTC hours on the same local minute.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    local_hhmm = set()
    for hour in hours_utc:
        utc_dt = datetime.datetime.combine(today, datetime.time(hour=hour), tzinfo=datetime.timezone.utc)
        local_dt = utc_dt.astimezone(export_service.DISPLAY_TZ)
        local_hhmm.add(local_dt.strftime("%H:%M"))
    return sorted(local_hhmm)

def get_status() -> dict:
    entries = list_backups()
    latest = entries[0] if entries else None
    hours_utc = settings.backup_hours_utc_list
    return {
        "auto_backup_enabled": settings.ENABLE_AUTO_BACKUP,
        # Raw UTC hours -- kept for anything that still wants the
        # unconverted scheduling value (e.g. a future API consumer that
        # isn't a person reading a dashboard).
        "backup_hours_utc": hours_utc,
        # What the System Backups panel actually renders now -- see
        # js/components/backups.js's loadBackupStatus(). Pre-converted to
        # DISPLAY_TIMEZONE with the real zone abbreviation attached
        # separately (display_timezone_label) so the frontend never needs
        # its own copy of the UTC->local conversion logic.
        "backup_hours_display": _display_schedule_hours(hours_utc),
        "display_timezone_label": datetime.datetime.now(export_service.DISPLAY_TZ).tzname(),
        "gdrive_enabled": settings.BACKUP_GDRIVE_ENABLED,
        "retention_count": settings.BACKUP_RETENTION_COUNT,
        "backup_count": len(entries),
        "latest_backup": latest,
    }
