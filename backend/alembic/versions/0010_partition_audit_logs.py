"""partition audit_logs by year (native Postgres RANGE partitioning)

Revision ID: 0010_partition_audit_logs
Revises: 0009_recovery_codes
Create Date: 2026-07-22

WHY THIS MIGRATION EXISTS
--------------------------
`audit_logs` is the one table in this app that's guaranteed to grow
forever -- it's an append-only ledger, nothing is ever deleted from it
(see services/audit_service.py's module docstring). Left as a single
ordinary table, that means:
  - Every `ORDER BY timestamp DESC LIMIT/OFFSET` page of the Audit Trail
    (get_audit_logs()) and every date-ranged CSV/PDF export
    (_filtered_audit_logs_query()) has to sort/scan a table that only
    ever gets bigger, forever -- there was not even an index on
    `timestamp` before this migration (see the PK change below).
  - The ONLY way to ever reclaim disk from old entries is a bulk
    `DELETE FROM audit_logs WHERE timestamp < ...`, which (a) has to visit
    and rewrite every matching row's page, (b) leaves dead tuples behind
    that need a `VACUUM` (potentially a slow, I/O-heavy one on a
    multi-year table) to actually free the disk back to the OS, and
    (c) fights for locks/IO with the exact same table every live
    checkout/checkin/export is hitting.

Native Postgres RANGE partitioning (by `timestamp`, one partition per
calendar year) fixes both:
  - Queries that filter/sort by `timestamp` (which is all of them --
    see audit_service.py) only ever touch the partition(s) that can
    possibly match ("partition pruning"), not the whole ledger's history.
  - Retiring an old year is a `DROP TABLE audit_logs_yYYYY;` -- physically
    a separate file on disk, dropped instantly, no VACUUM required, no
    scanning/locking of any row anyone still cares about. See
    SRE_STRATEGY.md's "Audit log partitioning & annual archive" section
    for the actual runbook a human follows to do that safely, once a
    year (or whenever disk space actually requires it) -- this migration
    only builds the mechanism, it never drops data itself.

WHY THE PRIMARY KEY CHANGES FROM (id) TO (timestamp, id)
----------------------------------------------------------
Postgres requires every unique constraint (including the primary key) on
a partitioned table to include the partitioning column -- a plain `id`-only
PK is rejected outright ("unique constraint on partitioned table must
include all partitioning columns"). Ordering the composite key as
`(timestamp, id)` -- rather than `(id, timestamp)` -- is deliberate: the
index Postgres builds to enforce that PK then has `timestamp` as its
LEADING column, which is exactly what `ORDER BY timestamp DESC` /
date-range filters want. Net effect: this migration doesn't just avoid
breaking anything, it gives every existing audit-ledger query a
supporting index it never had before. Nothing in the app looks up an
AuditLog row by bare `id` (checked: no FK references `audit_logs.id`, no
`db.query(models.AuditLog).get(...)` / `.filter(models.AuditLog.id ==
...)` anywhere), so widening the PK has no functional impact anywhere
else.

WHY A DEFAULT PARTITION (`audit_logs_default`) EXISTS
-------------------------------------------------------
A row whose `timestamp` doesn't fall inside ANY explicitly-created
partition's range is normally rejected outright at INSERT time ("no
partition of relation \"audit_logs\" found for row") -- e.g. if the
scheduled job that pre-creates next year's partition
(services/audit_partition_service.py's ensure_future_partitions(), run
daily by tasks/audit_partition_tasks.py -- see celery_app.py's
beat_schedule) never got to run for whatever reason before the calendar
rolled over. A DEFAULT partition is Postgres's built-in catch-all for
exactly that gap: nothing ever fails to insert. If a row does land there,
Postgres AUTOMATICALLY migrates it into the correct partition the moment
that partition is created (see ensure_future_partitions()'s docstring for
why this makes the default partition self-healing, not just a safety net
that silently accumulates rows forever).

WHAT THIS MIGRATION ACTUALLY DOES, IN ORDER
----------------------------------------------
1. Renames the existing plain `audit_logs` table out of the way.
2. Creates the new partitioned `audit_logs` parent, reusing the SAME
   `audit_logs_id_seq` sequence the original table already had (so `id`
   values keep incrementing from wherever they left off -- no reset, no
   collision).
3. Creates one partition per calendar year from the OLDEST existing row's
   year (or this year, if the table's empty) through
   `AUDIT_PARTITION_YEARS_AHEAD` years past whichever is later of "this
   year" or the NEWEST existing row's year -- plus the DEFAULT partition
   above as a catch-all.
4. Copies every existing row across (Postgres routes each one into the
   correct new partition automatically, by its own `timestamp`).
5. Re-points the sequence's ownership at the new table's `id` column, and
   drops the old, now-empty table.

AUDIT_PARTITION_YEARS_AHEAD (optional env var, default 2): how many years
of FUTURE partitions to pre-create right now, on top of whatever the
existing data already needed. Same env-var-read-directly pattern as
0002_bootstrap_root_admin.py's module docstring explains (a migration
should only need DATABASE_URL, plus a couple of plain values it reads
straight from `os.environ` -- never `from config import settings`, which
runs production-secret validation this file has no business triggering).
"""
import datetime
import os

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_partition_audit_logs"
down_revision = "0009_recovery_codes"
branch_labels = None
depends_on = None

_DEFAULT_PARTITION_NAME = "audit_logs_default"


def _partition_name(year: int) -> str:
    return f"audit_logs_y{year}"


def _years_ahead() -> int:
    raw = os.environ.get("AUDIT_PARTITION_YEARS_AHEAD", "2").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 2
    return max(value, 0)


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. Move the existing table out of the way ------------------------
    op.execute("ALTER TABLE audit_logs RENAME TO audit_logs_legacy")

    # --- Figure out which years need a partition ---------------------------
    min_ts, max_ts = bind.execute(
        sa.text('SELECT min("timestamp"), max("timestamp") FROM audit_logs_legacy')
    ).one()

    this_year = datetime.datetime.now(datetime.timezone.utc).year
    start_year = min_ts.year if min_ts is not None else this_year
    end_year = max(this_year, max_ts.year if max_ts is not None else this_year) + _years_ahead()

    # --- 2. Create the new partitioned parent, reusing the existing id
    #        sequence so ids keep incrementing from where they left off --
    #        see the module docstring's PK section for why (timestamp, id).
    op.execute(
        """
        CREATE TABLE audit_logs (
            id INTEGER NOT NULL DEFAULT nextval('audit_logs_id_seq'::regclass),
            operator VARCHAR NOT NULL,
            action VARCHAR NOT NULL,
            target_type VARCHAR NOT NULL,
            target_id INTEGER NOT NULL,
            details VARCHAR NOT NULL,
            "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY ("timestamp", id)
        ) PARTITION BY RANGE ("timestamp")
        """
    )

    # --- 3. One partition per calendar year, plus the DEFAULT catch-all ---
    op.execute(f'CREATE TABLE {_DEFAULT_PARTITION_NAME} PARTITION OF audit_logs DEFAULT')
    for year in range(start_year, end_year + 1):
        op.execute(
            f'CREATE TABLE {_partition_name(year)} PARTITION OF audit_logs '
            f"FOR VALUES FROM ('{year}-01-01T00:00:00+00:00') "
            f"TO ('{year + 1}-01-01T00:00:00+00:00')"
        )

    # --- 4. Copy the data across -- Postgres routes each row into the
    #        correct partition by its own `timestamp` automatically.
    op.execute(
        "INSERT INTO audit_logs (id, operator, action, target_type, target_id, details, \"timestamp\") "
        "SELECT id, operator, action, target_type, target_id, details, \"timestamp\" "
        "FROM audit_logs_legacy"
    )

    # --- 5. Re-point the sequence at the new table, drop the old one ------
    op.execute("ALTER SEQUENCE audit_logs_id_seq OWNED BY audit_logs.id")
    op.execute("DROP TABLE audit_logs_legacy")


def downgrade() -> None:
    bind = op.get_bind()

    # Rebuild a plain, non-partitioned table with the ORIGINAL single-column
    # `id` primary key -- exactly the shape 0001_baseline_schema.py's own
    # downgrade() expects to find and drop by the name "audit_logs".
    op.create_table(
        "audit_logs_plain",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False,
                  server_default=sa.text("nextval('audit_logs_id_seq'::regclass)")),
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("details", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )

    # `SELECT * FROM audit_logs` (the partitioned parent) transparently
    # reads across every partition, including the default one -- no need to
    # enumerate partitions here.
    op.execute(
        "INSERT INTO audit_logs_plain (id, operator, action, target_type, target_id, details, \"timestamp\") "
        "SELECT id, operator, action, target_type, target_id, details, \"timestamp\" FROM audit_logs"
    )

    # Re-point the sequence at the plain table BEFORE dropping the
    # partitioned one, same reasoning as upgrade()'s step 5 -- otherwise
    # `DROP TABLE audit_logs` (which still owns the sequence at this point)
    # would take the sequence down with it.
    op.execute("ALTER SEQUENCE audit_logs_id_seq OWNED BY audit_logs_plain.id")

    # Dropping a partitioned table drops every one of its partitions
    # (including the default one) along with it -- no CASCADE needed.
    op.execute("DROP TABLE audit_logs")
    op.execute("ALTER TABLE audit_logs_plain RENAME TO audit_logs")

    # Keep the sequence itself consistent with whatever's actually in the
    # table (belt-and-braces -- it should already be correct, since it was
    # never reset, only re-owned).
    bind.execute(sa.text(
        "SELECT setval('audit_logs_id_seq', COALESCE((SELECT max(id) FROM audit_logs), 1), "
        "(SELECT max(id) FROM audit_logs) IS NOT NULL)"
    ))
