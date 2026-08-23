"""
services/backup/restore_impl.py
----------------------------------
The actual destructive restore: drop and recreate the `public` schema,
replay a gzip-compressed SQL dump via `psql`, detect/repair the Alembic
revision, and reconcile anything that legitimately changed since the
backup was taken (credentials, outsider contacts, asset activity, audit
logs).

This lives on its own, apart from restore.py's thin public wrapper,
because it is one long serialized sequence of steps that share state
(the safety-backup entry, pre-restore snapshots, the reconciliation
results dict) from start to finish -- splitting it into several
functions would mean threading all of that through parameter lists
for no real gain in readability, and risks subtly reordering a
sequence where order matters (see the comments throughout for why each
step happens where it does).
"""

from __future__ import annotations

import datetime
import gzip
import logging
import os
import re
import subprocess
import uuid

from . import (
    _acquire_backup_lock,
    _release_backup_lock,
)
from .create import create_backup
from .reconcile_credentials import _reconcile_post_restore_credentials
from .reconcile_activity import (
    _reconcile_post_restore_outsiders,
    _reconcile_post_restore_asset_activity,
)
from .reconcile_audit import _create_restore_snapshot_engine, _snapshot_audit_logs, _reconcile_post_restore_audit_logs
from .schema import _detect_schema_revision
from .storage import _db_connection_kwargs, _require_binary

logger = logging.getLogger(__name__)


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

    # Hoisted up here (used to be imported further down, right before the
    # alembic reconciliation step) because the credential snapshot below --
    # which has to run BEFORE the destructive DROP SCHEMA, i.e. earlier
    # than that old import site -- needs them too.
    import database as database_module
    from sqlalchemy import inspect as sa_inspect, text as sa_text
    from security import AUTH_EPOCH_SETTING_KEY

    # Reserved here (not right before the destructive window below) so the
    # pre-restore safety backup immediately below can be told to reuse it
    # instead of taking out its own -- see that call's own comment, and the
    # `try:` block further down where this token's lock is actually held.
    _restore_window_lock_token = uuid.uuid4().hex

    # ENTERPRISE HARDENING -- hold the SAME backup-vs-restore mutual
    # exclusion lock for the safety snapshot AND the entire destructive
    # window. This prevents a manual/scheduled backup from racing the
    # pre-restore safety dump and keeps the safety snapshot + restore as one
    # serialized maintenance operation.
    _acquire_backup_lock(_restore_window_lock_token)
    try:
        safety_entry = None
        if take_safety_backup:
            try:
                # Reuses the lock already held by this restore instead of
                # acquiring/releasing a second lock around the safety dump.
                safety_entry = create_backup(
                    triggered_by="pre_restore_safety",
                    _held_lock_token=_restore_window_lock_token,
                )
            except Exception as exc:
                # FAIL CLOSED. A destructive restore without a fresh safety
                # snapshot is not safe: if the selected backup is wrong,
                # incompatible, or the restore fails halfway through, there is no
                # guaranteed way back to the exact live state that existed before
                # the button was pressed.
                logger.exception("backup_service: pre-restore safety backup failed -- refusing destructive restore")
                raise RuntimeError(
                    "Restore aborted before any database changes because the required "
                    f"pre-restore safety backup could not be created: {exc}"
                ) from exc
        # ENTERPRISE HARDENING -- credential continuity across a restore.
        #
        # All pre-restore snapshots below use a dedicated DIRECT_DATABASE_URL
        # connection. The request's auth dependency may still be holding the
        # only application-pool slot, so using database.engine here would make
        # a pool_size=1 deployment wait on itself and falsely report that the
        # audit ledger could not be snapshotted.
        snapshot_engine = _create_restore_snapshot_engine()
        try:
            snapshot_conn = snapshot_engine.connect()
            try:
                # A restore must preserve the current root account/profile.
                # Snapshot every current user while the live database is still
                # intact.
                if sa_inspect(snapshot_conn).has_table("users"):
                    _pre_restore_users = [
                        {**dict(row), "email_lc": dict(row)["email"].lower()}
                        for row in snapshot_conn.execute(sa_text("SELECT * FROM users")).mappings().all()
                    ]
                else:
                    _pre_restore_users = []

                current_super_admin_count = sum(
                    1 for row in _pre_restore_users if row.get("role") == "super_admin"
                )
                if _pre_restore_users and current_super_admin_count != 1:
                    raise RuntimeError(
                        "Restore aborted: the current database must contain exactly one super_admin "
                        f"account before restore; found {current_super_admin_count}. "
                        "Repair the root account first so the current root can remain authoritative."
                    )

                def _safe_snapshot_table(table_name: str) -> list:
                    if not sa_inspect(snapshot_conn).has_table(table_name):
                        logger.info(
                            "backup_service: no existing '%s' table to snapshot before this restore -- "
                            "nothing to preserve for it.", table_name,
                        )
                        return []
                    try:
                        return [
                            dict(row)
                            for row in snapshot_conn.execute(sa_text(f"SELECT * FROM {table_name}")).mappings().all()
                        ]
                    except Exception as exc:
                        raise RuntimeError(
                            f"Restore aborted because the current '{table_name}' table could not be snapshotted: {exc}"
                        ) from exc

                _pre_restore_outsiders = _safe_snapshot_table("outsiders")
                _pre_restore_checkouts = _safe_snapshot_table("asset_checkouts")
                _pre_restore_quotations = _safe_snapshot_table("quotations")
                _pre_restore_quotation_items = _safe_snapshot_table("quotation_items")
                _pre_restore_quotation_outsourced_items = _safe_snapshot_table("quotation_outsourced_items")

                # AUDIT HISTORY IS IMMUTABLE. Snapshot it before the destructive
                # schema replacement so entries created after the selected backup
                # cannot disappear merely because restore rolled mutable tables back.
                _pre_restore_audit_logs = _snapshot_audit_logs(snapshot_conn)
            finally:
                snapshot_conn.close()
        finally:
            snapshot_engine.dispose()

        #
        # A restore's whole point is to replace the database with an OLDER
        # snapshot, while the live root credentials and immutable audit trail
        # remain authoritative. The preflight snapshots above are complete and
        # were taken outside the request pool, so the destructive window can
        # now begin safely.

        conn = _db_connection_kwargs()

        # PRE-DESTRUCTIVE VALIDATION -- fully decompress and sanitize the dump
        # before dropping anything. A corrupt/truncated gzip must never erase
        # the live schema before we discover the file is unusable.
        try:
            with gzip.open(filepath, "rb") as gz_in:
                sql_bytes = gz_in.read()
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise RuntimeError(
                "Restore aborted before any database changes because the backup "
                f"file could not be decompressed: {exc}"
            ) from exc
        if not sql_bytes.strip():
            raise RuntimeError(
                "Restore aborted before any database changes because the backup "
                "contains no SQL statements."
            )
        # Backups created by older pg_dump 17 clients may contain this
        # PostgreSQL-17-only GUC. Strip only the exact generated SET line so
        # those dumps remain restorable on PostgreSQL 16.
        sql_bytes = re.sub(rb"(?im)^SET transaction_timeout = .*?;\r?\n?", b"", sql_bytes)

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

        # The reset command deliberately terminates every other PostgreSQL
        # connection, including idle connections held by SQLAlchemy's pool.
        # Dispose the pool now so the next readiness/schema query cannot reuse
        # a connection that PostgreSQL already killed. SQLAlchemy will create
        # fresh connections on demand for the restored database.
        database_module.engine.dispose()

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
        # sa_inspect / database_module already imported near the top of
        # this function (needed earlier there, for the credential
        # snapshot) -- reused here as-is.

        # BUG (caught before shipping): this file lives at
        # backend/services/backup_service/restore_impl.py (the backup/restore
        # package -- see backend/services/backup_service/__init__.py's
        # docstring for why this is a package rather than a single file), so
        # it needs to go up THREE directories to reach backend/ (where
        # alembic.ini and the alembic/ script location actually are) --
        # database.py's own get_schema_status() only needs ONE
        # `os.path.dirname()` because database.py itself already lives
        # directly in backend/. Copy-pasting that single-dirname call here
        # would silently point alembic_cfg at backend/services/backup_service/
        # instead, and AlembicConfig("backend/services/backup_service/"
        # + "alembic.ini") would fail to find alembic.ini at all.
        backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
            #
            # BUG FIX -- see _detect_schema_revision()'s own docstring above
            # for the full incident this fixes. In short: "tables already
            # exist despite no alembic_version row" used to be treated as ONE
            # situation (assume it's an AUTO_INIT_DB/create_all() database
            # that already matches THIS build's models.py, so blindly `stamp
            # head`) when it's actually TWO different situations that look
            # identical from table-existence alone -- and only one of them is
            # safe to stamp straight to head. A genuinely old backup (from
            # before this project used Alembic at all, or from a version of
            # models.py missing later columns/tables like totp_enabled /
            # recovery_codes / partitioned audit_logs) hits this exact branch
            # too, and blindly stamping it "head" lies to Alembic about the
            # schema being current -- the missing DDL never actually runs,
            # and the very next request that touches one of those columns
            # fails at the database level. Fixed by never guessing "head":
            # detect the restored schema's REAL revision by inspecting it
            # directly, stamp exactly that, then still run `upgrade(head)` on
            # top of it either way -- a schema that's genuinely already
            # current detects straight through to the actual migration head
            # (0016_quotation_paid_status) and upgrade(head) is then a no-op,
            # same as before; a genuinely
            # older schema detects an earlier revision and upgrade(head)
            # actually applies the DDL it's missing, instead of skipping it.
            try:
                with database_module.engine.connect() as _conn:
                    existing_tables = set(sa_inspect(_conn).get_table_names()) - {"alembic_version"}

                if schema_status_before["current_heads"] or not existing_tables:
                    # A tracked schema can be upgraded normally. An actually
                    # empty schema also needs the complete migration chain.
                    command.upgrade(alembic_cfg, "head")
                else:
                    with database_module.engine.connect() as _conn:
                        detected_revision = _detect_schema_revision(_conn)
                    logger.warning(
                        "backup_service: restored schema has %d table(s) but no "
                        "'alembic_version' row -- inspected its actual shape and it "
                        "corresponds to migration '%s'. Stamping that revision before "
                        "upgrading to the current head.",
                        len(existing_tables), detected_revision,
                    )
                    # A create_all()/legacy dump has tables but no Alembic
                    # marker. Stamp the revision that the restored columns/tables
                    # actually represent; never blindly stamp "head".
                    command.stamp(alembic_cfg, detected_revision)

                # Always execute an idempotent final upgrade, even when the
                # restored dump already reported the current head. This makes
                # restore reconciliation self-healing: if the dump's
                # alembic_version marker was stale, incomplete, or otherwise
                # disagreed with the physical schema, Alembic gets one final
                # opportunity to reconcile it before readiness is evaluated.
                command.upgrade(alembic_cfg, "head")
            except Exception as exc:
                logger.exception("backup_service: post-restore schema reconciliation failed")
                raise RuntimeError(
                    "Restore loaded the backup's data successfully, but bringing its schema "
                    f"up to date failed: {exc}. The database is on an older or otherwise "
                    "unreconciled schema than this app expects and may not work correctly "
                    "until this is resolved -- restore the pre-restore safety backup above "
                    "if you need to revert."
                ) from exc

        # Schema is now guaranteed to be fully at head (users.password_hash,
        # users.totp_enabled, recovery_codes, app_settings all exist for
        # certain past this point, whatever the restored dump's own era
        # was) -- safe to reconcile credentials against it now. See
        # _reconcile_post_restore_credentials()'s own docstring for what
        # this actually does; _pre_restore_users was captured right at the
        # top of this function, before anything destructive ran.
        credential_reconciliation = _reconcile_post_restore_credentials(
            database_module.engine, _pre_restore_users,
        )

        # EXTENDED -- same continuity guarantee, extended past login
        # credentials to the ad-hoc (no-login) profiles and the actual
        # checkouts/quotations those and real accounts own. Outsiders
        # run first (mirroring users running before this point) since
        # _reconcile_post_restore_asset_activity() needs
        # `preserved_outsider_ids` to know which ad-hoc-assigned
        # checkouts/quotations are safe to reconcile. See both
        # functions' own docstrings for exactly what "current wins for
        # preserved accounts, backup-only data is left alone" means here.
        outsider_reconciliation = _reconcile_post_restore_outsiders(
            database_module.engine, _pre_restore_outsiders,
        )
        asset_activity_reconciliation = _reconcile_post_restore_asset_activity(
            database_module.engine,
            _pre_restore_checkouts,
            _pre_restore_quotations,
            _pre_restore_quotation_items,
            _pre_restore_quotation_outsourced_items,
            credential_reconciliation["preserved_user_ids"],
            outsider_reconciliation["preserved_outsider_ids"],
        )

        # AUDIT TRAIL CONTINUITY -- unlike mutable business tables, the
        # append-only ledger must survive a rollback-style database restore.
        # Reinsert only pre-restore entries that are not already present in
        # the restored backup. This also preserves the audit rows describing
        # post-backup checkouts/returns/user changes that the continuity
        # reconciliation keeps alive in their source tables.
        audit_reconciliation = _reconcile_post_restore_audit_logs(
            database_module.engine, _pre_restore_audit_logs,
        )

        # ENTERPRISE HARDENING -- force EVERY existing session (including
        # whoever just triggered this restore) to log back in, so nobody
        # -- anywhere in the app, not just the Super Admin -- keeps
        # working against a view of the data that no longer reflects
        # what's actually in the database. Deliberately global rather
        # than scoped to just the Super Admin: every table just got
        # replaced, so every session's picture of the world is equally
        # stale, not just the one that ran the restore. See
        # security.AUTH_EPOCH_SETTING_KEY's own docstring and
        # deps.py's get_current_user() for the other half of this --
        # the actual rejection happens there, on each session's very
        # next request. Written with Python's own UTC clock (not the
        # database's NOW()) specifically so it compares directly against
        # JWT "iat" values, which are also stamped with this same
        # process's clock (see security.py's create_access_token()).
        with database_module.engine.begin() as _epoch_conn:
            _epoch_conn.execute(
                sa_text(
                    "INSERT INTO app_settings (key, value, updated_by, updated_at) "
                    "VALUES (:key, :value, :updated_by, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET "
                    "value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at"
                ),
                {
                    "key": AUTH_EPOCH_SETTING_KEY,
                    "value": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "updated_by": "system:restore",
                },
            )

        # Existing pooled connections were opened against the schema as it
        # stood before the DROP SCHEMA/reload/migrate above -- dispose them so
        # every request after this one grabs a fresh connection against the
        # now-current schema instead of a stale one, belt-and-suspenders
        # alongside pool_pre_ping (see database.py's engine setup).
        database_module.engine.dispose()

        # The readiness engine is deliberately separate from the request pool,
        # but it can also hold a connection lifecycle tied to the pre-restore
        # schema. Dispose it explicitly after a destructive restore so the
        # final readiness check is guaranteed to open a brand-new connection
        # against the restored database.
        readiness_engine = getattr(database_module, "readiness_engine", None)
        if readiness_engine is not None:
            readiness_engine.dispose()
        if hasattr(database_module, "_readiness_engine_source"):
            database_module._readiness_engine_source = None
        if hasattr(database_module, "_readiness_engine_source_url"):
            database_module._readiness_engine_source_url = None

        schema_status_after = database_module.get_schema_status()
        logger.warning(
            "backup_service: post-restore schema status -- ready=%s (%s)",
            schema_status_after["ready"], schema_status_after["reason"],
        )

        return {
            "restored_from": os.path.basename(filepath),
            "safety_backup": safety_entry,
            "schema_status": schema_status_after,
            "credential_reconciliation": credential_reconciliation,
            "outsider_reconciliation": outsider_reconciliation,
            "asset_activity_reconciliation": asset_activity_reconciliation,
            "audit_reconciliation": audit_reconciliation,
        }
    finally:
        _release_backup_lock(_restore_window_lock_token)
