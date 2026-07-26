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
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import datetime
import uuid
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

import redis

from config import settings
import services.export_service as export_service

logger = logging.getLogger(__name__)

# Lazily-created shared Redis client, used only for the scheduled-backup
# distributed lock below (_acquire_scheduled_backup_lock) -- kept separate
# from Celery's own Redis usage so this module has no hard dependency on
# celery_app.py importing cleanly first.
_redis_client: Optional["redis.Redis"] = None


def _get_redis_client() -> "redis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client

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

    BUG FIX -- "Backup failed: pg_dump failed (exit 1): ... FATAL:
    password authentication failed for user '...' ... FATAL: no
    pg_hba.conf entry for host '...', ..., no encryption" on Azure, on a
    password that was NEVER changed/rotated (ruled that out -- don't
    mis-diagnose this as drift between the GitHub secret and the live
    server, like this comment itself used to). Two independent bugs,
    both in this one function:

    1. THE REAL ONE, and the actual cause of the "password authentication
       failed" line specifically: `urllib.parse.urlparse()` does NOT
       percent-decode the username/password portion of a URL -- e.g.
       parsing `postgresql://snipeit:x%2By%2Fz@host/db` gives you back
       the literal 8-character string "x%2By%2Fz", not the 3-character
       password "x+y/z" it actually encodes. infra/main.bicep's
       `databaseUrl` variable correctly percent-encodes the password with
       `uriComponent(postgresPassword)` before building DATABASE_URL
       (has to -- a raw `+`/`/`/`@`/`:`/`#`/... in a password would
       otherwise be parsed as URL syntax, not password content) -- and
       DEPLOYMENT.md's setup instructions specifically tell you to
       generate that password with `openssl rand -base64 24`, whose
       output routinely CONTAINS `+` and `/` (base64's alphabet). Every
       *other* consumer of DATABASE_URL (SQLAlchemy, used by the actual
       running app for every real query) decodes the URL properly and
       connects fine -- which is exactly why login/migrations/normal use
       all worked while backups alone failed on a password that was
       never touched. This one hand-rolled parse was the only place
       still handing pg_dump/psql the raw, still-percent-encoded string
       as PGPASSWORD, guaranteed to mismatch the real password the
       moment it contains any URL-reserved character. Fixed by decoding
       both username and password with `unquote()` below.

    2. `?sslmode=require` was ALSO being silently dropped entirely --
       `urlparse(...).query` was parsed and then never read anywhere, so
       with no sslmode communicated any other way, libpq fell back to
       its own default (`prefer`), whose plaintext-fallback attempt is
       what Azure's Flexible Server (which enforces SSL -- see
       infra/main.bicep's `postgresServer` comment) rejects with "no
       encryption", the SECOND FATAL line in that error. `PGSSLMODE` (an
       env var libpq itself reads, same as `PGPASSWORD`) is honored
       identically by both `pg_dump` and `psql` without needing to touch
       either's argv, and defaults to `"prefer"` only as a last resort if
       DATABASE_URL genuinely has no `sslmode` at all (e.g. local
       docker-compose, which talks to `db` on the same Docker network
       with no TLS involved) -- every Azure deployment's DATABASE_URL
       always has `sslmode=require` already baked in, so this preserves
       that exact value rather than hardcoding `require` unconditionally
       here.

    Both bugs independently produced one of the two FATAL lines in that
    error message -- neither one alone was the whole story, which is why
    the error looked like two unrelated failures concatenated together.
    """
    parsed = urlparse(settings.DATABASE_URL)
    query_params = parse_qs(parsed.query)
    sslmode = query_params.get("sslmode", ["prefer"])[0]
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    env["PGSSLMODE"] = sslmode
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username) if parsed.username else "postgres",
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


RESTORE_LOCK_KEY = "backup:restore-lock"
# Generous ceiling above the real worst case (600s pre-restore safety
# pg_dump + 120s schema-reset timeout + 900s restore timeout + however
# long alembic upgrade/stamp takes) so that even a replica that crashed
# outright mid-restore (no chance to run the `finally` release below)
# still self-clears eventually, rather than wedging every future restore
# behind a lock nobody will ever release.
RESTORE_LOCK_TTL_SECONDS = 2700
RESTORE_STATUS_FILENAME = "restore_status.json"


class RestoreInProgressError(RuntimeError):
    """Raised when a restore is requested while another is already running (see _acquire_restore_lock)."""


def _acquire_restore_lock(token: str) -> None:
    """
    Distributed lock so that a SECOND restore request can never start
    while one is already running -- across replicas (via Redis, same as
    _acquire_scheduled_backup_lock above) AND within a single replica.

    This matters specifically because a restore keeps running to
    completion in its own worker thread even after the HTTP request that
    started it is gone (a closed browser tab, or nginx's default
    `proxy_ignore_client_abort off` tearing down the upstream connection
    on client disconnect -- neither one can or does kill the underlying
    OS thread/subprocess). Without this lock, an admin (or a CI/CD
    pipeline retrying a request it never got a response for) who
    re-triggers a restore they *think* failed could start a SECOND
    `DROP SCHEMA public CASCADE` / `psql` restore racing the first one
    still in flight against the very same database -- corruption, not a
    harmless duplicate.

    `SET key token NX EX ttl` is atomic: exactly one caller can ever
    acquire this for a given window, no matter how close together two
    requests arrive.

    Deliberately FAILS CLOSED if Redis itself is unreachable -- the
    opposite choice from _acquire_scheduled_backup_lock's fail-open,
    because that lock's worst failure mode is a harmless duplicate
    backup, while this one's is a corrupting concurrent destructive
    restore. Refusing to restore blind is the safer of the two bad
    options here.
    """
    try:
        acquired = _get_redis_client().set(RESTORE_LOCK_KEY, token, nx=True, ex=RESTORE_LOCK_TTL_SECONDS)
    except redis.RedisError as exc:
        raise RuntimeError(
            "Could not verify that no other restore is already running (Redis is "
            "unreachable) -- refusing to start a second destructive restore blind. "
            "Retry once Redis is reachable again."
        ) from exc
    if not acquired:
        raise RestoreInProgressError(
            "A restore is already in progress. Check GET /api/backup/restore-status "
            "for its outcome instead of retrying -- starting a second restore while "
            "one is still running risks corrupting the database."
        )


def _release_restore_lock(token: str) -> None:
    """
    Only deletes the lock if it still holds THIS restore's own token --
    guards against the edge case where this restore ran long enough for
    RESTORE_LOCK_TTL_SECONDS to expire and a DIFFERENT restore has since
    legitimately acquired the key; without the token check, this call
    would delete that other restore's still-valid lock out from under it.
    Failure here just means the lock self-expires via its TTL instead of
    clearing immediately -- logged, not raised, since we're already in a
    `finally` and the restore's real result has already been decided.
    """
    lua = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
    try:
        _get_redis_client().eval(lua, 1, RESTORE_LOCK_KEY, token)
    except redis.RedisError:
        logger.warning(
            "backup_service: failed to release restore lock in Redis -- it will "
            "self-expire via TTL in at most %ds.", RESTORE_LOCK_TTL_SECONDS, exc_info=True,
        )


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



def _restore_backup_impl(filepath: str, take_safety_backup: bool = True) -> dict:
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

    # BUG FIX -- "Restore failed: ... 'DROP SCHEMA public CASCADE; CREATE
    # SCHEMA public;'] timed out after 120 seconds", and the app appearing
    # to hang / "can't connect" for the whole 120s while this ran. Root
    # cause: `DROP SCHEMA ... CASCADE` needs an ACCESS EXCLUSIVE lock on
    # every object in the schema, which has to wait for every OTHER
    # session with an open transaction touching any of those objects to
    # finish -- and THIS VERY REQUEST already has one. This route depends
    # on `require_true_super_admin` -> `get_current_user`
    # (see deps.py), which runs `db.query(models.User)...first()` on a
    # `Session` from `Depends(get_db)` -- SQLAlchemy 2.0 auto-begins a
    # transaction on that first query and does NOT end it until
    # `get_db()`'s `finally: db.close()` runs, which only happens after
    # the ENTIRE request (including this synchronous call into
    # restore_backup()) finishes. So the DROP SCHEMA below wound up
    # waiting on a lock that could only be released once THIS SAME
    # request finished -- which it never would, since it was busy waiting
    # on the DROP SCHEMA. Not a Postgres-detected deadlock (nothing on the
    # DB side was itself waiting on anything), just an unbreakable wait
    # from Postgres's point of view -- hence sitting there until the
    # subprocess timeout below finally killed it.
    #
    # Any OTHER concurrent request/tab with its own open transaction on a
    # public-schema table would cause the exact same hang, not just this
    # one -- and a "replace the ENTIRE database" operation (see this
    # route's own confirmation modal in the frontend) makes every other
    # session's in-flight view of the data moot anyway. So the fix here
    # terminates every OTHER backend connection using this same DB role
    # (a role can always terminate its own other sessions, no extra
    # privilege needed) immediately before the reset -- covering this
    # request's own self-held lock AND any other concurrent one in a
    # single, simple statement.
    terminate_others_sql = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid();"
    )

    # Reset the schema instead of dropping/recreating the whole database --
    # DROP DATABASE can't run inside the same connection pool this app is
    # actively using, while `DROP SCHEMA ... CASCADE` can run as a normal
    # statement over a plain psql connection to that same database.
    reset_sql = terminate_others_sql + " DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    reset_cmd = [
        "psql",
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["dbname"],
        "--quiet",
        # BUG FIX -- restore appeared to "succeed" (200 OK, no error shown
        # anywhere) but left the app unusable afterward (e.g. login
        # throwing an unhandled 500 with `relation "users" does not
        # exist`, or similar). Root cause: by default `psql` does NOT stop
        # on a failing statement -- it logs the error to stderr and keeps
        # going, then still exits 0 once it reaches the end of the
        # script/command. `reset_result.returncode != 0` below therefore
        # never caught anything, because a single `DROP SCHEMA ... CREATE
        # SCHEMA` pair essentially can't partially fail in a way that
        # still exits 0 -- but the SAME missing flag on restore_cmd below
        # is what actually let a broken restore look like a clean one:
        # if e.g. an early CREATE TABLE/CREATE TYPE statement in the dump
        # failed (a permissions error, a leftover object from a half-
        # dropped schema, an out-of-order FK reference), psql just moved
        # on to the next statement -- INSERTs for a table that was never
        # created fail too, silently, and psql still exits 0 at the end.
        # `--set ON_ERROR_STOP=1` makes psql abort with a non-zero exit
        # code the moment ANY statement fails, so `returncode != 0` below
        # actually means what the code assumes it means. Added here too
        # (even though a single --command is low-risk) so both psql
        # invocations in this function share the same fail-loud behavior.
        "--set", "ON_ERROR_STOP=1",
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
        # See reset_cmd's comment above -- ON_ERROR_STOP is the flag that
        # actually matters here, since the restore is many statements, not
        # one.
        #
        # BUG FIX -- "Restore failed: ... ERROR: unrecognized configuration
        # parameter \"transaction_timeout\" / STATEMENT: SET
        # transaction_timeout = 0;", restore aborting immediately every
        # time. This USED to also pass --single-transaction (wrapping the
        # whole restore in one BEGIN/COMMIT so a detected failure rolls
        # back cleanly instead of leaving a half-restored schema) -- good
        # in principle, but psql 17+ clients automatically prepend
        # `SET transaction_timeout = 0;` whenever --single-transaction is
        # used (to stop a SERVER-side transaction_timeout from aborting a
        # long-running restore mid-way). `transaction_timeout` is itself a
        # Postgres 17+ GUC -- if the actual DB SERVER predates 17 (very
        # common; this app doesn't pin a minimum Postgres version anywhere
        # -- see docker-compose.yml/.env.*.example), it doesn't recognize
        # that parameter at all and rejects the SET outright, which
        # (correctly, thanks to ON_ERROR_STOP) aborts the ENTIRE restore
        # before a single real statement from the dump even runs. This is
        # a psql-CLIENT-version-vs-Postgres-SERVER-version mismatch, not a
        # server-configurable/data problem -- there's no flag to suppress
        # just that one auto-SET, so --single-transaction is dropped
        # entirely rather than pinning/detecting exact client/server
        # versions here. ON_ERROR_STOP=1 alone still catches and reports
        # any real mid-restore failure correctly (see reset_cmd's comment
        # above); the only difference is a failure now leaves whatever ran
        # before it committed rather than cleanly rolling back to empty --
        # the pre-restore safety backup taken above remains the recovery
        # path either way.
        "--set", "ON_ERROR_STOP=1",
    ]
    with gzip.open(filepath, "rb") as gz_in:
        sql_bytes = gz_in.read()

    # BUG FIX -- "Restore failed partway through: ERROR: unrecognized
    # configuration parameter \"transaction_timeout\"", even with
    # --single-transaction long since removed above (see that comment).
    # Root cause turned out to be one level further back than the psql
    # invocation itself: backend/Dockerfile used to install the generic,
    # unversioned `postgresql-client` package, which resolved to a 17.x
    # pg_dump -- and pg_dump 17.x writes `SET transaction_timeout = 0;`
    # into the STANDARD PREAMBLE of every dump it produces, regardless of
    # --single-transaction. `transaction_timeout` is itself a PG17+-only
    # GUC, so a 16.x (or older) server -- like docker-compose.yml's
    # `db: postgres:16-alpine` -- rejects that SET outright, and
    # ON_ERROR_STOP correctly aborts the entire restore before a single
    # real statement from the dump runs.
    #
    # The Dockerfile now pins postgresql-client to major version 16 (see
    # its own comment) so this line never gets written into FUTURE
    # backups. But every backup already taken with the old, mismatched
    # image -- on disk or already uploaded to Drive -- already has this
    # line baked into its bytes, and rebuilding the image doesn't rewrite
    # backups that already exist. So this strips any such line here too,
    # unconditionally, on every restore: a no-op for dumps that never had
    # it (new backups, or ones taken by psql <17 to begin with), and the
    # difference between "restorable" and "permanently broken" for the
    # ones that do. Anchored to start-of-line and requires the trailing
    # semicolon so it can only ever match pg_dump's own auto-generated
    # SET statement, not e.g. a value inside a COPY block that happens to
    # contain this text.
    sql_bytes = re.sub(rb"(?im)^SET transaction_timeout = .*?;\r?\n?", b"", sql_bytes)

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

    # BUG FIX -- restore "succeeded" (200 OK) but the app was broken
    # immediately afterward: /admin came back blank, and logging back in
    # (specifically as the root Super Admin) failed with an unhandled 500.
    # Root cause: `pg_dump`/`psql` only ever move DATA -- a backup captures
    # the schema exactly as it existed AT BACKUP TIME, with no awareness of
    # which alembic revision that was. If the restored file predates a
    # migration this running code already depends on (e.g.
    # 0008_super_admin_totp.py's `users.totp_enabled`/
    # `totp_secret_encrypted` -- read unconditionally by
    # services/auth_service.py's login() for the super_admin role;
    # 0009_recovery_codes.py's whole `recovery_codes` table; or
    # 0010_partition_audit_logs.py's partitioned `audit_logs`), the restore
    # would quietly leave the database on an OLDER schema than this code
    # expects -- the DROP SCHEMA/reload above has no concept of "old" vs
    # "new" schema, it just replays whatever SQL the file contains. The
    # very next query that touches a newer column/table then fails at the
    # database level (e.g. `UndefinedColumn: users.totp_enabled does not
    # exist`), surfacing as an opaque unhandled 500 with no indication the
    # ROOT problem was a stale backup, not a broken restore mechanism.
    #
    # Fix: reconcile the schema immediately after loading the dump, the
    # same way a fresh deploy does -- run `alembic upgrade head` against
    # the now-restored database so its schema matches what THIS running
    # code actually expects, while keeping every row the backup brought
    # back. `database.get_schema_status()` (already used by GET /readyz)
    # is reused here before/after so the caller gets a clear, structured
    # answer instead of a bare pass/fail -- and so a restore whose
    # migration step itself fails (e.g. a destructive/irreversible
    # migration between the backup's era and now) raises a specific,
    # actionable error instead of returning a falsely "successful" restore
    # that's still broken.
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from sqlalchemy import inspect as sa_inspect
    import database as database_module

    # BUG (caught before shipping): this file lives at
    # backend/services/backup_service.py, so it needs to go up TWO
    # directories to reach backend/ (where alembic.ini and the alembic/
    # script location actually are) -- database.py's own
    # get_schema_status() only needs ONE `os.path.dirname()` because
    # database.py itself already lives directly in backend/. Copy-pasting
    # that single-dirname call here would silently point alembic_cfg at
    # backend/services/ instead, and AlembicConfig("backend/services/"
    # + "alembic.ini") would fail to find alembic.ini at all.
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    alembic_cfg = AlembicConfig(os.path.join(backend_dir, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))

    schema_status_before = database_module.get_schema_status()
    if not schema_status_before["ready"]:
        logger.warning(
            "backup_service: restored schema is behind this build (%s) -- running "
            "'alembic upgrade head' to bring it current before declaring the restore done.",
            schema_status_before["reason"],
        )
        # BUG FIX -- "Restore loaded the backup's data successfully, but
        # bringing its schema up to date (alembic upgrade head) failed:
        # (psycopg2.errors.DuplicateTable) relation \"asset_types\" already
        # exists". Root cause: schema_status_before["ready"] is False for
        # TWO meaningfully different situations, and this used to treat
        # them identically:
        #   1. the schema is genuinely empty (a brand-new/blank database)
        #      -- current_heads == [] because alembic_version has never
        #      been written, AND no other tables exist either. Replaying
        #      the full migration chain from scratch is exactly right
        #      here.
        #   2. the schema already has every real table (asset_types,
        #      users, ...) -- just restored from the dump's own CREATE
        #      TABLE statements -- but STILL has current_heads == [],
        #      because the database this backup came from was bootstrapped
        #      via AUTO_INIT_DB's init_db()/create_all() (see database.py's
        #      own docstring on init_db()), a deliberately supported
        #      alternative to Alembic that builds every table straight
        #      from models.py and never stamps `alembic_version` at all.
        # `alembic upgrade head` can't tell these apart on its own -- it
        # just sees no recorded revision and replays 0001_baseline_schema's
        # `CREATE TABLE asset_types (...)` from scratch, which collides
        # with the identical table the restore already recreated.
        #
        # Fix: check for actual pre-existing tables ourselves before
        # deciding which of the two situations this is. Real schema
        # content (case 2) means create_all() already built something
        # that matches THIS build's models.py -- i.e. it's already
        # equivalent to head in substance, it just never got the
        # `alembic_version` row saying so -- so `alembic stamp head`
        # (record the revision without running any DDL) is the correct
        # move, not `upgrade`. An actually empty schema (case 1) still
        # goes through the real `upgrade(head)` path below, unchanged.
        #
        # Deliberately NOT applied when current_heads is non-empty (the
        # "revision mismatch" branch of get_schema_status(), e.g. an
        # older-but-Alembic-tracked backup) -- there, alembic_version DOES
        # already correctly describe the existing tables' real revision,
        # and `upgrade(head)` applying only the INCREMENTAL migrations on
        # top of that is exactly right; stamping would wrongly skip them.
        try:
            with database_module.engine.connect() as _conn:
                existing_tables = set(sa_inspect(_conn).get_table_names()) - {"alembic_version"}

            if schema_status_before["current_heads"] or not existing_tables:
                command.upgrade(alembic_cfg, "head")
            else:
                logger.warning(
                    "backup_service: restored schema already has %d table(s) despite "
                    "missing/empty 'alembic_version' -- treating this as an "
                    "AUTO_INIT_DB/create_all()-bootstrapped backup and running 'alembic "
                    "stamp head' instead of replaying migrations against tables that "
                    "already exist.",
                    len(existing_tables),
                )
                command.stamp(alembic_cfg, "head")
        except Exception as exc:
            logger.exception("backup_service: post-restore schema reconciliation failed")
            raise RuntimeError(
                "Restore loaded the backup's data successfully, but bringing its schema "
                f"up to date failed: {exc}. The database is on an older or otherwise "
                "unreconciled schema than this app expects and may not work correctly "
                "until this is resolved -- restore the pre-restore safety backup above "
                "if you need to revert."
            ) from exc

    # Existing pooled connections were opened against the schema as it
    # stood before the DROP SCHEMA/reload/migrate above -- dispose them so
    # every request after this one grabs a fresh connection against the
    # now-current schema instead of a stale one, belt-and-suspenders
    # alongside pool_pre_ping (see database.py's engine setup).
    database_module.engine.dispose()

    schema_status_after = database_module.get_schema_status()
    logger.warning(
        "backup_service: post-restore schema status -- ready=%s (%s)",
        schema_status_after["ready"], schema_status_after["reason"],
    )

    return {
        "restored_from": os.path.basename(filepath),
        "safety_backup": safety_entry,
        "schema_status": schema_status_after,
    }


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


def _acquire_scheduled_backup_lock(run_label: str) -> bool:
    """
    Distributed leader lock so that, when the `backend` service is scaled
    to multiple replicas (see DEPLOYMENT.md's load balancing section),
    only ONE of them actually runs a given scheduled backup -- every
    replica runs its own copy of `_scheduler_loop` (it's a plain daemon
    thread per process, deliberately -- see start_backup_scheduler's
    docstring), and without this lock every one of them would wake up at
    the same target hour and each fire its own full `pg_dump`.

    `run_label` identifies THIS specific scheduled run (e.g.
    "2026-07-12T03:00") rather than being a fixed key, so the lock only
    needs to be held long enough to decide "am I the one running the
    03:00 backup today" -- it does not need to be held for the entire
    duration of the backup itself. `SET key value NX EX ttl` is atomic:
    exactly one replica's call can ever return True for a given
    `run_label`, no matter how close together the competing replicas'
    clocks wake them up.
    """
    key = f"backup:scheduled-lock:{run_label}"
    try:
        acquired = _get_redis_client().set(key, "1", nx=True, ex=300)
        return bool(acquired)
    except redis.RedisError:
        # Redis briefly unreachable -- fail OPEN here (let this replica run
        # the backup) rather than silently skipping a scheduled backup
        # entirely. Worst case on a multi-replica deployment during a
        # Redis blip is a duplicate backup, which is harmless; worst case
        # of failing closed is NO backup running at all.
        logger.warning("backup_service: Redis unavailable for scheduler lock -- proceeding without it.", exc_info=True)
        return True


def _scheduler_loop() -> None:
    hours = settings.backup_hours_utc_list
    hours_label = ", ".join(f"{h:02d}:00" for h in hours)
    logger.info("backup_service: scheduler thread started -- backup at %s UTC daily.", hours_label)
    while True:
        sleep_seconds = _seconds_until_next_run(settings.backup_hours_utc_list)
        time.sleep(sleep_seconds)
        now = datetime.datetime.now(datetime.timezone.utc)
        run_label = now.strftime("%Y-%m-%dT%H")
        if _acquire_scheduled_backup_lock(run_label):
            try:
                create_backup(triggered_by="scheduled")
            except Exception:
                logger.exception("backup_service: scheduled backup failed.")
        else:
            logger.info("backup_service: another replica already claimed the %s scheduled backup -- skipping.", run_label)
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

    Every replica of this service runs its own copy of this thread when
    scaled horizontally (see DEPLOYMENT.md's load balancing section) --
    that's safe because `_scheduler_loop` acquires a short-lived Redis
    leader lock (`_acquire_scheduled_backup_lock`) before actually firing
    each scheduled backup, so only one replica's `pg_dump` ever runs per
    scheduled time even though every replica independently wakes up for it.
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
