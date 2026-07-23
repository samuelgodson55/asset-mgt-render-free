"""
backend/scripts/audit_partition_status.py
--------------------------------------------
Read-only report of every `audit_logs` partition that exists right now --
row count, on-disk size, and the actual oldest/newest entry in it -- for
a human deciding whether it's time to retire an old year. This is the
FIRST step of the annual partition runbook in SRE_STRATEGY.md's "Audit
log partitioning & annual archive" section; it never modifies anything.

USAGE (from inside the `backend` container, where DATABASE_URL is
already set -- same pattern as `alembic upgrade head` in DEPLOYMENT.md):

    docker compose exec backend python scripts/audit_partition_status.py

Prints nothing and exits 0 with a one-line notice if run against a
non-Postgres database (e.g. by accident against a local SQLite test
setup) -- see services/audit_partition_service.py's module docstring for
why partitioning is a Postgres-only concept here.
"""

import sys
from pathlib import Path

BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from database import SessionLocal  # noqa: E402
import services.audit_partition_service as audit_partition_service  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        status = audit_partition_service.get_partition_status(db)
    finally:
        db.close()

    if not status:
        print(
            "No partitions found -- either this isn't a Postgres database, "
            "or `alembic upgrade head` (0010_partition_audit_logs.py) hasn't "
            "run yet."
        )
        return 0

    header = f"{'PARTITION':<22} {'ROWS':>10}  {'SIZE':>10}  {'OLDEST ENTRY':<26} {'NEWEST ENTRY':<26} BOUNDS"
    print(header)
    print("-" * len(header))
    for row in status:
        name = row["partition_name"] + (" (default)" if row["is_default"] else "")
        oldest = row["oldest_entry"].isoformat() if row["oldest_entry"] else "-- empty --"
        newest = row["newest_entry"].isoformat() if row["newest_entry"] else "-- empty --"
        print(
            f"{name:<22} {row['row_count']:>10}  {row['total_size_pretty']:>10}  "
            f"{oldest:<26} {newest:<26} {row['bounds']}"
        )

    print()
    print(
        "Before dropping any partition: confirm a Google Drive backup covering "
        "that year restores cleanly in a scratch/local docker compose setup "
        "FIRST (see SRE_STRATEGY.md's 'Audit log partitioning & annual archive' "
        "runbook) -- this script only reports what exists, it never deletes "
        "anything."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
