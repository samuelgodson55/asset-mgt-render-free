"""
services/backup/
-----------------
Everything backup/restore-related: creating a gzip-compressed `pg_dump` of
the whole database, listing/downloading/deleting local backup files,
restoring from one (local or freshly uploaded), uploading to Google Drive,
and a lightweight daemon-thread scheduler that runs a backup once a day
without depending on Celery/Redis.

This top-level module is deliberately kept to just the distributed-lock /
Redis-client plumbing shared by every other submodule here, plus the public
API re-exports below. The actual logic lives in:

  storage.py    -- index.json + pg_dump/psql connection kwargs
  create.py     -- create_backup / list_backups / delete_backup / get_status
  restore.py    -- restore_backup / restore_from_upload (+ _restore_backup_impl)
  schema.py     -- _detect_schema_revision (pre-restore Alembic detection)
  reconcile.py  -- post-restore reconciliation (credentials/outsiders/audit)
  restore_status.py -- restore_status.json (polled by GET /restore-status)
  gdrive.py     -- Google Drive upload
  scheduler.py  -- the daily backup daemon thread

WHY NOT A DATABASE TABLE FOR BACKUP HISTORY?
Deliberately NOT stored as a `models.py` table + Alembic migration. Two
reasons:
  1. Bootstrapping problem -- if you're restoring BECAUSE the database is
     broken/missing, you don't want the very listing of "what backups
     exist" to depend on that same database being queryable.
  2. A restore replaces the whole `public` schema wholesale (see
     restore.restore_backup()) -- if backup history lived in a table, a
     restore-of-an-older-backup would appear to "lose" every backup taken
     after it, which is confusing.
Instead, metadata lives in a plain JSON index file (`index.json`) sitting
right next to the backup files themselves, inside settings.BACKUP_DIR (see
storage.py).

WHY BOTH LOCAL FILES *AND* GOOGLE DRIVE?
Render's Free plan gives this app's web service an EPHEMERAL disk -- it's
wiped clean on every restart, spin-down/wake-up cycle, and redeploy. Local
files are only ever a convenience cache for "restore to a few minutes ago
without leaving the dashboard". Google Drive is what makes a backup
actually durable across those events -- see restore.restore_from_upload()
for the recovery path once local disk is gone: download the last good file
from Drive, upload it back through the dashboard's Restore modal.
"""

from __future__ import annotations

import logging
from typing import Optional

import redis

from config import settings

logger = logging.getLogger(__name__)

# Lazily-created shared Redis client, used only for the distributed locks
# below -- kept separate from Celery's own Redis usage so this package has
# no hard dependency on celery_app.py importing cleanly first.
#
# NOTE: this stays a module global on THIS file (services/backup/__init__.py)
# rather than living in a submodule, on purpose -- tests reset it between
# runs via `monkeypatch.setattr(backup_service, "_redis_client", None)`,
# which only rebinds the name they can see: `services.backup`'s own
# namespace.
_redis_client: Optional["redis.Redis"] = None


def _get_redis_client() -> "redis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


# ---------------------------------------------------------------------------
# Locking -- backup side
# ---------------------------------------------------------------------------


BACKUP_LOCK_KEY = "backup:create-lock"
# Covers pg_dump's own subprocess timeout (600s) plus a healthy margin, same
# reasoning as RESTORE_LOCK_TTL_SECONDS below: if a replica died mid-backup
# with no chance to run its own `finally: _release_backup_lock`, this still
# self-clears instead of wedging every future backup (or restore -- see
# _acquire_restore_lock's cross-check below) behind a lock nobody will ever
# release.
BACKUP_LOCK_TTL_SECONDS = 2700


class BackupInProgressError(RuntimeError):
    """Raised when a backup is requested while another backup -- or a restore -- is already running."""


def _acquire_backup_lock(token: str) -> None:
    """
    ENTERPRISE HARDENING -- "the backup continues cleanly in the background
    independently, so a frontend/backend hiccup doesn't corrupt it."

    Two distinct backup-time hazards this closes:

      1. Two pg_dumps running at once (a double-click on "Backup Now", or a
         scheduled backup firing at the same moment someone clicks it
         manually) -- wasteful, and on Render's/small-VM's modest DB specs,
         two full dumps competing for I/O can slow each other down enough
         to trip the other's own 600s subprocess timeout, turning "backup
         is a little slow" into "backup fails outright".

      2. Far more serious: a backup starting WHILE a restore is running.
         `_restore_backup_impl` runs `DROP SCHEMA public CASCADE` then
         replays the dump table-by-table -- a `pg_dump` that happens to be
         mid-read during that window doesn't get a stable failure, it gets
         a TORN snapshot: some tables reflect the old (pre-restore) data,
         others are already gone or half-reloaded. That dump would still
         look like a perfectly normal, valid, restorable backup file --
         nothing about it signals "this is corrupted" until someone
         actually restores FROM it and hits inexplicable inconsistencies.
         That is exactly the silent, hard-to-diagnose corruption this
         function exists to prevent, by making the two operations mutually
         exclusive with the SAME lock restore already uses (see
         _acquire_restore_lock's own cross-check below).

    Deliberately FAILS OPEN (logs a warning, proceeds anyway) if Redis
    itself is unreachable -- same philosophy as the pre-existing
    _acquire_scheduled_backup_lock: this lock's worst failure mode on its
    own is a harmless duplicate/slow backup. It does NOT weaken the
    restore-corruption protection above, because _acquire_restore_lock is
    fail-CLOSED on the exact same Redis outage -- if Redis is down, no
    restore could have started in the first place, so there is nothing for
    an unlocked backup to race against.
    """
    try:
        acquired = _get_redis_client().set(BACKUP_LOCK_KEY, token, nx=True, ex=BACKUP_LOCK_TTL_SECONDS)
    except redis.RedisError:
        logger.warning(
            "backup_service: could not acquire backup lock (Redis unreachable) -- "
            "proceeding without it. A concurrent restore can't be in flight either "
            "(it requires the same Redis connection to have started), so this is "
            "safe; a concurrent duplicate manual/scheduled backup is the only risk.",
            exc_info=True,
        )
        return
    if not acquired:
        raise BackupInProgressError(
            "A backup is already in progress (either another manual request, the "
            "daily schedule, or the automatic pre-restore safety backup). Wait for "
            "it to finish and try again."
        )


def _release_backup_lock(token: str) -> None:
    """Token-checked release -- see _release_restore_lock's identical reasoning below."""
    lua = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
    try:
        _get_redis_client().eval(lua, 1, BACKUP_LOCK_KEY, token)
    except redis.RedisError:
        logger.warning(
            "backup_service: failed to release backup lock cleanly -- it will "
            "self-expire via TTL in at most %ds.", BACKUP_LOCK_TTL_SECONDS, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Locking -- restore side
# ---------------------------------------------------------------------------


RESTORE_LOCK_KEY = "backup:restore-lock"
# Generous ceiling above the real worst case (600s pre-restore safety
# pg_dump + 120s schema-reset timeout + 900s restore timeout + however
# long alembic upgrade/stamp takes) so that even a replica that crashed
# outright mid-restore (no chance to run the `finally` release below)
# still self-clears eventually, rather than wedging every future restore
# behind a lock nobody will ever release.
RESTORE_LOCK_TTL_SECONDS = 2700


class RestoreInProgressError(RuntimeError):
    """Raised when a restore is requested while another is already running (see _acquire_restore_lock)."""


def _acquire_restore_lock(token: str) -> None:
    """
    Distributed lock so that a SECOND restore request can never start
    while one is already running -- across replicas (via Redis, same as
    _acquire_scheduled_backup_lock below) AND within a single replica.

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

    # ENTERPRISE HARDENING -- the other half of _acquire_backup_lock's own
    # cross-check above: a `pg_dump` that's mid-read the instant `DROP
    # SCHEMA public CASCADE` runs below doesn't fail loudly, it captures a
    # silently torn snapshot -- a "valid-looking" backup file that's
    # actually corrupted. Refusing to start THIS restore while a backup
    # (BACKUP_LOCK_KEY) is already running closes that off from the
    # restore side too, symmetrically. Release the restore lock we just
    # took above before raising, so this doesn't leave a phantom "restore
    # in progress" for the real backup that's actually running.
    try:
        backup_in_progress = _get_redis_client().get(BACKUP_LOCK_KEY) is not None
    except redis.RedisError:
        backup_in_progress = False  # see _acquire_backup_lock's fail-open reasoning -- symmetric here
    if backup_in_progress:
        _release_restore_lock(token)
        raise RestoreInProgressError(
            "A backup is currently in progress. Wait for it to finish before "
            "starting a restore -- running both at once risks the backup "
            "capturing a torn, inconsistent snapshot mid-restore."
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


# ---------------------------------------------------------------------------
# Locking -- scheduler leader election
# ---------------------------------------------------------------------------


def _acquire_scheduled_backup_lock(run_label: str) -> bool:
    """
    Distributed leader lock so that, when the `backend` service is scaled
    to multiple replicas (see DEPLOYMENT.md's load balancing section),
    only ONE of them actually runs a given scheduled backup -- every
    replica runs its own copy of `scheduler._scheduler_loop` (it's a plain
    daemon thread per process, deliberately -- see
    scheduler.start_backup_scheduler's docstring), and without this lock
    every one of them would wake up at the same target hour and each fire
    its own full `pg_dump`.

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


# ---------------------------------------------------------------------------
# Public API -- re-exported from submodules so `services.backup_service` (or
# `services.backup`) keeps working exactly like the old single-file module.
# ---------------------------------------------------------------------------

from .create import (  # noqa: E402
    create_backup,
    list_backups,
    get_backup_filepath,
    delete_backup,
    get_status,
)
from .restore_status import get_restore_status  # noqa: E402
from .schema import _detect_schema_revision  # noqa: E402
from .reconcile_credentials import _reconcile_post_restore_credentials  # noqa: E402
from .reconcile_activity import (  # noqa: E402
    _reconcile_post_restore_outsiders,
    _reconcile_post_restore_asset_activity,
)
from .reconcile_audit import (  # noqa: E402
    _create_restore_snapshot_engine,
    _snapshot_audit_logs,
    _reconcile_post_restore_audit_logs,
)
from .restore_impl import _restore_backup_impl  # noqa: E402
from .restore import restore_backup, restore_from_upload  # noqa: E402
from .gdrive import upload_to_gdrive, _build_gdrive_credentials  # noqa: E402
from .scheduler import start_backup_scheduler  # noqa: E402

__all__ = [
    "BackupInProgressError",
    "RestoreInProgressError",
    "create_backup",
    "list_backups",
    "get_backup_filepath",
    "delete_backup",
    "get_status",
    "get_restore_status",
    "restore_backup",
    "restore_from_upload",
    "upload_to_gdrive",
    "start_backup_scheduler",
    # Not part of the public API, but re-exported so
    # `services.backup_service._name(...)` keeps working exactly like the
    # old single-file module -- used directly by tests/test_backup_restore.py.
    "_detect_schema_revision",
    "_reconcile_post_restore_credentials",
    "_reconcile_post_restore_outsiders",
    "_reconcile_post_restore_asset_activity",
    "_create_restore_snapshot_engine",
    "_snapshot_audit_logs",
    "_reconcile_post_restore_audit_logs",
    "_restore_backup_impl",
    "_build_gdrive_credentials",
]
