"""
services/audit_partition_service.py
-------------------------------------
Everything about keeping `audit_logs`'s native Postgres RANGE partitions
(see alembic/versions/0010_partition_audit_logs.py's module docstring)
healthy on an ongoing basis, split into two independent jobs:

  - `ensure_future_partitions()` -- the ONE thing this app automates:
    pre-creating however many years of FUTURE partitions
    `settings.AUDIT_PARTITION_YEARS_AHEAD` calls for, so a write never
    fails once the calendar rolls over. Run daily by
    `tasks/audit_partition_tasks.py` (see celery_app.py's beat_schedule).
    Idempotent -- safe to run as often as you like; it only ever CREATEs
    a partition that doesn't already exist yet.

  - `get_partition_status()` -- a read-only report of every partition
    that exists today (year, row count, on-disk size, whether it's the
    DEFAULT catch-all), for a human to look at once a year when deciding
    whether it's time to retire the oldest one. Used by
    `scripts/audit_partition_status.py` (run by hand, not scheduled).

DELIBERATELY MISSING: ANYTHING THAT DROPS A PARTITION
--------------------------------------------------------
Retiring an old year (`DROP TABLE audit_logs_yYYYY;`) is NEVER automated
here, on purpose -- see SRE_STRATEGY.md's "Audit log partitioning &
annual archive" section for the actual runbook a human follows (confirm
a Google-Drive backup covering that year is genuinely restorable BEFORE
dropping anything). This module only ever grows the set of partitions,
never shrinks it.

WHY EVERYTHING HERE IS A NO-OP AGAINST ANYTHING BUT POSTGRES
----------------------------------------------------------------
Local/dev/test databases get `audit_logs` as a PLAIN, non-partitioned
table from `Base.metadata.create_all()` (see models.py's AuditLog
docstring for why) -- there's no partition to create or report on there.
Every function below checks `db.bind.dialect.name` first and returns
immediately (an empty report, or "did nothing") against anything other
than `"postgresql"`, so this is safe to call unconditionally from a
Celery task or a throwaway SQLite test session alike.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings

logger = logging.getLogger(__name__)

_PARENT_TABLE = "audit_logs"
_DEFAULT_PARTITION_NAME = "audit_logs_default"


def _partition_name(year: int) -> str:
    return f"audit_logs_y{year}"


def _is_postgres(db: Session) -> bool:
    try:
        return db.bind.dialect.name == "postgresql"
    except Exception:  # pragma: no cover - defensive, should never happen
        return False


def _existing_partition_years(db: Session) -> set[int]:
    """Every audit_logs_yYYYY partition that currently exists, as a set of ints."""
    rows = db.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = to_regclass(:parent)"
        ),
        {"parent": _PARENT_TABLE},
    ).all()
    years = set()
    for (relname,) in rows:
        if relname.startswith("audit_logs_y"):
            suffix = relname[len("audit_logs_y"):]
            if suffix.isdigit():
                years.add(int(suffix))
    return years


def ensure_future_partitions(db: Session, years_ahead: Optional[int] = None) -> list[int]:
    """
    Creates whichever of [this year .. this year + years_ahead] don't
    already have a partition yet. Returns the list of years actually
    created (empty list = everything already existed, the common case).

    `years_ahead` defaults to settings.AUDIT_PARTITION_YEARS_AHEAD --
    only overridden directly by tests/scripts that want a specific value.
    """
    if not _is_postgres(db):
        return []

    if years_ahead is None:
        years_ahead = settings.AUDIT_PARTITION_YEARS_AHEAD

    this_year = datetime.datetime.now(datetime.timezone.utc).year
    existing = _existing_partition_years(db)

    created: list[int] = []
    for year in range(this_year, this_year + years_ahead + 1):
        if year in existing:
            continue
        # IF NOT EXISTS as a second safety net against a race with another
        # replica also running this same check concurrently -- CREATE
        # TABLE ... PARTITION OF doesn't support IF NOT EXISTS directly in
        # all Postgres versions we support, so guard with a plain
        # try/except instead; a duplicate-relation error here just means
        # another replica won the race, which is fine.
        try:
            db.execute(
                text(
                    f'CREATE TABLE {_partition_name(year)} PARTITION OF {_PARENT_TABLE} '
                    f"FOR VALUES FROM ('{year}-01-01T00:00:00+00:00') "
                    f"TO ('{year + 1}-01-01T00:00:00+00:00')"
                )
            )
            db.commit()
            created.append(year)
            logger.info("Created audit_logs partition for year %s", year)
        except Exception:
            db.rollback()
            # Re-check rather than assuming "someone else created it" --
            # surfaces a genuine problem (e.g. a permissions issue) instead
            # of silently swallowing it.
            if year not in _existing_partition_years(db):
                logger.exception("Failed to create audit_logs partition for year %s", year)
                raise
    return created


def get_partition_status(db: Session) -> list[dict]:
    """
    One row per existing partition (including the DEFAULT catch-all),
    newest first, with enough detail for a human to decide "is it safe to
    retire this year yet": row count, on-disk size (table + its own
    indexes), and the actual min/max timestamp stored in it. Returns []
    against a non-Postgres database (see module docstring).
    """
    if not _is_postgres(db):
        return []

    rows = db.execute(
        text(
            """
            SELECT
                c.relname AS partition_name,
                pg_get_expr(c.relpartbound, c.oid) AS bounds,
                pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
                pg_total_relation_size(c.oid) AS total_size_bytes
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = to_regclass(:parent)
            ORDER BY c.relname DESC
            """
        ),
        {"parent": _PARENT_TABLE},
    ).mappings().all()

    status = []
    for row in rows:
        partition_name = row["partition_name"]
        count_row = db.execute(text(f"SELECT count(*), min(\"timestamp\"), max(\"timestamp\") FROM {partition_name}")).one()
        row_count, min_ts, max_ts = count_row
        status.append({
            "partition_name": partition_name,
            "is_default": partition_name == _DEFAULT_PARTITION_NAME,
            "bounds": row["bounds"],
            "row_count": row_count,
            "oldest_entry": min_ts,
            "newest_entry": max_ts,
            "total_size_pretty": row["total_size"],
            "total_size_bytes": row["total_size_bytes"],
        })
    return status
