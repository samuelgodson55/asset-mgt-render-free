"""
services/backup_service.py
---------------------------
Everything backup/restore-related lives here: creating a gzip-compressed
`pg_dump` of the whole database, listing/downloading/deleting local backup
files, restoring from one (local or freshly uploaded), uploading to Google
Drive, and a lightweight daemon-thread scheduler that runs a backup once a
day without depending on Celery/Redis.

WHY NOT A DATABASE TABLE FOR BACKUP HISTORY?
Deliberately NOT stored as a `models.py` table + Alembic migration. Two
reasons:
  1. Bootstrapping problem -- if you're restoring BECAUSE the database is
     broken/missing, you don't want the very listing of "what backups
     exist" to depend on that same database being queryable.
  2. A restore replaces the whole `public` schema wholesale (see
     restore_backup() below) -- if backup history lived in a table, a
     restore-of-an-older-backup would appear to "lose" every backup taken
     after it, which is confusing.
Instead, metadata lives in a plain JSON index file (`index.json`) sitting
right next to the backup files themselves, inside settings.BACKUP_DIR.

WHY BOTH LOCAL FILES *AND* GOOGLE DRIVE?
Render's Free plan gives this app's web service an EPHEMERAL disk -- it's
wiped clean on every restart, spin-down/wake-up cycle, and redeploy. Local
files are only ever a convenience cache for "restore to a few minutes ago
without leaving the dashboard". Google Drive is what makes a backup
actually durable across those events -- see restore_from_upload() for the
recovery path once local disk is gone: download the last good file from
Drive, upload it back through the dashboard's Restore modal.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import datetime
from typing import Optional
from urllib.parse import urlparse

from config import settings
import services.export_service as export_service

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"
_index_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _ensure_backup_dir() -> str:
    os.makedirs(settings.BACKUP_DIR, exist_ok=True)
    return settings.BACKUP_DIR


def _index_path() -> str:
    return os.path.join(_ensure_backup_dir(), INDEX_FILENAME)


def _load_index() -> list[dict]:
    path = _index_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("backup_service: index.json is missing/corrupt -- starting a fresh index.")
        return []


def _save_index(entries: list[dict]) -> None:
    path = _index_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(entries, f, indent=2, default=str)
    os.replace(tmp_path, path)  # atomic on POSIX -- never leaves index.json half-written


def _db_connection_kwargs() -> dict:
    """
    Parses settings.DATABASE_URL (a standard postgresql:// URL) into the
    discrete pieces pg_dump/psql want on the command line, and returns them
    alongside a PGPASSWORD-carrying env dict -- passing the password via
    argv (e.g. `-p<password>`) would leak it into `ps aux` output on the
    same box, whereas an env var passed only to this one subprocess does
    not.
    """
    parsed = urlparse(settings.DATABASE_URL)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "dbname": (parsed.path or "/").lstrip("/") or "postgres",
        "env": env,
    }


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(
            f"'{name}' is not installed in this container. Add the "
            "`postgresql-client` OS package to the Dockerfile (see "
            "Dockerfile.render / backend/Dockerfile) -- pg_dump/psql do NOT "
            "ship with the psycopg2-binary Python package."
        )


# ---------------------------------------------------------------------------
# Creating a backup
# ---------------------------------------------------------------------------


def create_backup(triggered_by: str = "manual") -> dict:
    """
    Runs `pg_dump` against settings.DATABASE_URL, gzip-compresses the
    output, writes it to settings.BACKUP_DIR, records it in index.json,
    uploads it to Google Drive if enabled, then enforces local retention.
    Returns the new backup's index entry (including any upload error, so
    the caller can surface a partial-success state instead of a bare 500).
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
    filepath = os.path.join(backup_dir, filename)

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


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------


def restore_backup(filepath: str, take_safety_backup: bool = True) -> dict:
    """
    Destructive: drops and recreates the `public` schema, then replays the
    given gzip-compressed SQL dump into it via `psql`. Works identically
    whether `filepath` points at an existing local backup or a
    freshly-uploaded temp file (see restore_from_upload()).

    A "pre-restore safety" backup of the CURRENT database is taken first
    (unless take_safety_backup=False), so restoring the wrong file is
    itself undoable via the same Restore flow.
    """
    _require_binary("psql")
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    safety_entry = None
    if take_safety_backup:
        try:
            safety_entry = create_backup(triggered_by="pre_restore_safety")
        except Exception:
            # Log and continue -- refusing to let an admin restore a known-
            # good backup just because the safety snapshot itself failed
            # (e.g. the current DB is already in a broken state) would be
            # worse than proceeding without one.
            logger.exception("backup_service: pre-restore safety backup failed -- proceeding with restore anyway.")

    conn = _db_connection_kwargs()

    # Reset the schema instead of dropping/recreating the whole database --
    # DROP DATABASE can't run inside the same connection pool this app is
    # actively using, while `DROP SCHEMA ... CASCADE` can run as a normal
    # statement over a plain psql connection to that same database.
    reset_sql = "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    reset_cmd = [
        "psql",
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["dbname"],
        "--quiet",
        "--command", reset_sql,
    ]
    reset_result = subprocess.run(reset_cmd, capture_output=True, env=conn["env"], timeout=120)
    if reset_result.returncode != 0:
        raise RuntimeError(f"Failed to reset schema before restore: {reset_result.stderr.decode(errors='replace')[:2000]}")

    restore_cmd = [
        "psql",
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["dbname"],
        "--quiet",
    ]
    with gzip.open(filepath, "rb") as gz_in:
        sql_bytes = gz_in.read()
    result = subprocess.run(
        restore_cmd,
        input=sql_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=conn["env"],
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Restore failed partway through: {result.stderr.decode(errors='replace')[:2000]}")

    logger.warning("backup_service: RESTORE COMPLETE from %s -- database has been replaced.", os.path.basename(filepath))
    return {
        "restored_from": os.path.basename(filepath),
        "safety_backup": safety_entry,
    }


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


# ---------------------------------------------------------------------------
# Google Drive upload
# ---------------------------------------------------------------------------


def upload_to_gdrive(filepath: str, filename: str) -> str:
    """
    Uploads `filepath` into settings.BACKUP_GDRIVE_FOLDER_ID and returns the
    new Drive file's ID. Supports TWO auth modes -- see config.py's
    BACKUP_GDRIVE_* docstring for the full explanation of why there are two:

      1. OAuth as a real Google user (BACKUP_GDRIVE_OAUTH_*) -- required
         for a personal/consumer Google account. Tried FIRST if configured.
      2. A service account (BACKUP_GDRIVE_CREDENTIALS_JSON) -- only works
         if BACKUP_GDRIVE_FOLDER_ID is a Shared Drive folder (Google
         Workspace only). Used as a fallback if mode 1 isn't configured.

    Raises a clear, mode-specific error if neither is configured, or if the
    configured mode is itself missing a required value.
    """
    if not settings.BACKUP_GDRIVE_FOLDER_ID:
        raise RuntimeError("BACKUP_GDRIVE_FOLDER_ID is not set -- add the destination Drive folder's ID.")

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive libraries aren't installed -- add google-api-python-client, "
            "google-auth, and google-auth-httplib2 to backend/requirements.txt."
        ) from exc

    credentials = _build_gdrive_credentials()
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    file_metadata = {"name": filename, "parents": [settings.BACKUP_GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(filepath, mimetype="application/gzip", resumable=False)
    created = drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return created["id"]


def _build_gdrive_credentials():
    """
    Returns a google-auth Credentials object for whichever mode is
    configured, OAuth-as-a-user taking priority over the service account
    (see upload_to_gdrive()'s docstring). Raises a RuntimeError naming
    exactly which setting is missing if the chosen mode is incomplete,
    rather than letting google-auth raise its own less-obvious error.
    """
    oauth_configured = bool(
        settings.BACKUP_GDRIVE_OAUTH_CLIENT_ID
        or settings.BACKUP_GDRIVE_OAUTH_CLIENT_SECRET
        or settings.BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN
    )

    if oauth_configured:
        missing = [
            name for name, value in (
                ("BACKUP_GDRIVE_OAUTH_CLIENT_ID", settings.BACKUP_GDRIVE_OAUTH_CLIENT_ID),
                ("BACKUP_GDRIVE_OAUTH_CLIENT_SECRET", settings.BACKUP_GDRIVE_OAUTH_CLIENT_SECRET),
                ("BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN", settings.BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                f"Google Drive OAuth is only partially configured -- missing {', '.join(missing)}. "
                "Run backend/scripts/gdrive_oauth_setup.py to generate all three values."
            )

        from google.oauth2.credentials import Credentials
        # `token=None` is fine -- google-auth fetches a fresh access token
        # from the refresh_token automatically on first use (and again
        # whenever it expires), no manual refresh() call needed here.
        return Credentials(
            token=None,
            refresh_token=settings.BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN,
            client_id=settings.BACKUP_GDRIVE_OAUTH_CLIENT_ID,
            client_secret=settings.BACKUP_GDRIVE_OAUTH_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )

    if settings.BACKUP_GDRIVE_CREDENTIALS_JSON:
        from google.oauth2 import service_account
        credentials_info = json.loads(settings.BACKUP_GDRIVE_CREDENTIALS_JSON)
        return service_account.Credentials.from_service_account_info(
            credentials_info, scopes=["https://www.googleapis.com/auth/drive.file"]
        )

    raise RuntimeError(
        "No Google Drive credentials configured -- set either BACKUP_GDRIVE_OAUTH_CLIENT_ID/"
        "_CLIENT_SECRET/_REFRESH_TOKEN (personal Google account -- see backend/scripts/"
        "gdrive_oauth_setup.py) or BACKUP_GDRIVE_CREDENTIALS_JSON (Workspace service account)."
    )


# ---------------------------------------------------------------------------
# Daily scheduler (plain daemon thread -- no Celery/Redis dependency)
# ---------------------------------------------------------------------------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _seconds_until_next_run(hours_utc: list[int]) -> float:
    """
    Finds the soonest upcoming run time across ALL configured hours (today
    or tomorrow, whichever comes first) rather than just one -- e.g. for
    hours_utc=[3, 15, 21] at 16:00 UTC, the next run is 21:00 UTC today, not
    tomorrow's 3:00.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for hour in hours_utc:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        candidates.append(target)
    return (min(candidates) - now).total_seconds()


def _scheduler_loop() -> None:
    hours = settings.backup_hours_utc_list
    hours_label = ", ".join(f"{h:02d}:00" for h in hours)
    logger.info("backup_service: scheduler thread started -- backup at %s UTC daily.", hours_label)
    while True:
        sleep_seconds = _seconds_until_next_run(settings.backup_hours_utc_list)
        time.sleep(sleep_seconds)
        try:
            create_backup(triggered_by="scheduled")
        except Exception:
            logger.exception("backup_service: scheduled backup failed.")
        # Small buffer so a slightly-early wakeup (clock drift) can't fire twice.
        time.sleep(5)


def start_backup_scheduler() -> None:
    """
    Called once from main.py's FastAPI startup event. Safe to call more
    than once (e.g. under a test runner that imports main.py repeatedly) --
    only the first call actually starts the thread.

    Deliberately a plain `threading.Thread(daemon=True)` loop rather than
    Celery Beat: this way the daily backup runs whether or not
    RUN_EMBEDDED_WORKER/Redis are configured, since it lives directly
    inside the same uvicorn process Render always keeps running.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        if not settings.ENABLE_AUTO_BACKUP:
            logger.info("backup_service: ENABLE_AUTO_BACKUP is false -- daily scheduled backup disabled.")
            return
        thread = threading.Thread(target=_scheduler_loop, name="backup-scheduler", daemon=True)
        thread.start()
        _scheduler_started = True
