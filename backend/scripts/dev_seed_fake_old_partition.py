"""
backend/scripts/dev_seed_fake_old_partition.py
-------------------------------------------------
Creates ONE disposable, clearly-fake `audit_logs_yYYYY` partition (year
2020 by default) with a handful of throwaway rows in it, purely so a human
can exercise the "drop an old partition" half of SRE_STRATEGY.md's "Audit
log partitioning & annual archive" runbook against something that isn't
real production data -- see that file's "The annual retirement runbook"
section for the actual drop steps this is meant to be tested against.

WHY THIS EXISTS
`services/audit_partition_service.py` deliberately has NOTHING that drops
a partition (see that module's docstring) -- retiring a year is a manual,
once-a-year, backup-verified-first human decision, never automated code.
That's the right call for real data, but it also means there was
previously no safe way to rehearse the DROP TABLE step itself without
either waiting for a real year to actually need retiring, or improvising
ad-hoc SQL through `az containerapp exec`/`docker compose exec` shell
quoting by hand (fragile and easy to fat-finger against a partition you
didn't mean to touch). This script is that rehearsal fixture: it only
ever touches ONE specific, obviously-fake partition (year 2020 by
default, or whatever `--year` you pass), tagged with 'TEST_EVENT' /
'FAKE_SEED_DATA_FOR_PARTITION_DROP_TESTING' so it's unmistakable in any
listing, and it refuses to run at all outside a non-production
environment (see the guard below).

REFUSES TO RUN IN PRODUCTION -- ON PURPOSE
Same reasoning as AUTO_SEED_DEMO_DATA in config.py: inserting throwaway
rows into `audit_logs` -- an append-only, audit-relevant ledger -- would
be actively wrong to ever do against a real production database, even by
accident. This checks `settings.is_production` before touching anything
and hard-exits if it's true. There is deliberately no `--force`/override
flag to bypass that check.

USAGE (from inside the `backend` container -- same pattern as
scripts/audit_partition_status.py):

    python scripts/dev_seed_fake_old_partition.py
    python scripts/dev_seed_fake_old_partition.py --year 2019 --rows 3

Then verify it landed with the existing read-only report:

    python scripts/audit_partition_status.py

...and, once you're done rehearsing, drop it the exact same way the real
runbook does (this script does NOT drop anything itself -- see
SRE_STRATEGY.md's "Step 2" for the actual DROP TABLE, run by hand against
`$DATABASE_URL` via psql, same as it would be for a real year):

    psql "$DATABASE_URL" -c "DROP TABLE audit_logs_y2020;"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import text  # noqa: E402

from config import settings  # noqa: E402
from database import SessionLocal  # noqa: E402

_PARENT_TABLE = "audit_logs"
_FAKE_ACTION = "TEST_EVENT"
_FAKE_DETAILS_TAG = "FAKE_SEED_DATA_FOR_PARTITION_DROP_TESTING"


def _partition_name(year: int) -> str:
    return f"audit_logs_y{year}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Seeds ONE disposable fake-old-year audit_logs partition with "
            "throwaway rows, for rehearsing the partition-drop runbook in "
            "SRE_STRATEGY.md against non-production data. Refuses to run "
            "if ENVIRONMENT=production."
        )
    )
    parser.add_argument(
        "--year", type=int, default=2020,
        help="Fake year to create a partition for (default: 2020 -- pick "
             "something obviously outside any real partition range so it "
             "can never be confused with a genuine year).",
    )
    parser.add_argument(
        "--rows", type=int, default=1,
        help="How many disposable rows to insert into it (default: 1).",
    )
    args = parser.parse_args()

    if settings.is_production:
        print(
            "Refusing to run: ENVIRONMENT=production. This script inserts "
            "throwaway rows into audit_logs and is for rehearsing the "
            "partition-drop runbook against non-production data ONLY -- "
            "see this script's own module docstring.",
            file=sys.stderr,
        )
        return 1

    if args.rows < 1:
        print("--rows must be at least 1.", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        if db.bind.dialect.name != "postgresql":
            print(
                "No-op: this database isn't PostgreSQL, so there's no "
                "partitioned audit_logs table to seed against (see "
                "services/audit_partition_service.py's module docstring "
                "for why partitioning is Postgres-only here).",
            )
            return 0

        partition_name = _partition_name(args.year)

        # IF NOT EXISTS -- safe to re-run; a second call with the same
        # --year just adds more rows to the partition that already exists
        # rather than erroring out.
        db.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {partition_name} "
                f"PARTITION OF {_PARENT_TABLE} "
                f"FOR VALUES FROM ('{args.year}-01-01T00:00:00+00:00') "
                f"TO ('{args.year + 1}-01-01T00:00:00+00:00')"
            )
        )

        for i in range(args.rows):
            db.execute(
                text(
                    f"INSERT INTO {_PARENT_TABLE} "
                    "(operator, action, target_type, target_id, details, \"timestamp\") "
                    "VALUES (:operator, :action, :target_type, :target_id, :details, "
                    f"'{args.year}-06-15T00:00:00+00:00')"
                ),
                {
                    "operator": "dev-seed-script@localhost",
                    "action": _FAKE_ACTION,
                    "target_type": "FakeSeed",
                    "target_id": i,
                    "details": f"{_FAKE_DETAILS_TAG} (row {i + 1} of {args.rows}).",
                },
            )

        db.commit()
        print(
            f"Seeded {args.rows} disposable row(s) into '{partition_name}' "
            f"(year {args.year}). Run scripts/audit_partition_status.py to "
            "confirm, then follow SRE_STRATEGY.md's runbook to drop it: "
            f'psql "$DATABASE_URL" -c "DROP TABLE {partition_name};"'
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
