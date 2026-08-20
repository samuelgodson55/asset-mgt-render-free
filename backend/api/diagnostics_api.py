"""
diagnostics_api.py
-------------------
Read-only, admin-only operational diagnostics. Currently a single
endpoint: the combined DB/PgBouncer/Postgres connection-pool snapshot
from db_pool_metrics.snapshot_all(), added so PGBOUNCER_SAFETY_MARGIN_PERCENT
and DB_BACKGROUND_CONNECTION_RESERVE (backend/config.py) can be tuned from
real numbers instead of guessed defaults -- see db_pool_metrics.py's own
docstring for exactly what each field means, why it's safe to sample on
demand (short-lived, unpooled probe connections only), and how it relates
to the same numbers exported as OpenTelemetry gauges when OTEL_ENABLED.

Gated on require_true_super_admin (the same gate api/backup_api.py uses)
rather than a lower privileged-role check: pool/PgBouncer/Postgres
internals aren't something a regular Manager/Admin account needs, and
this endpoint's PgBouncer probe opens a short-lived connection to the
PgBouncer admin console on every call -- worth keeping to the smallest
audience that actually tunes infra.
"""

from fastapi import APIRouter, Depends

from deps import require_true_super_admin
import db_pool_metrics

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/db-pool")
def db_pool_diagnostics(user: dict = Depends(require_true_super_admin)):
    return db_pool_metrics.snapshot_all()
