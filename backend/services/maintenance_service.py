"""Runtime maintenance-mode settings backed by AppSetting.

The request middleware uses a short-lived process-local cache so ordinary API
traffic does not perform a PostgreSQL query for every request. The cache is
invalidated immediately after the maintenance-control API commits a change;
other replicas converge within the TTL.
"""

import threading
import time
from copy import deepcopy

from sqlalchemy.orm import Session

import models
import database

MAINTENANCE_MODE_KEY = "maintenance_mode"
MAINTENANCE_MESSAGE_KEY = "maintenance_message"
DEFAULT_MESSAGE = "We are currently performing scheduled maintenance. Please check back shortly."

# Keep the middleware cheap while bounding cross-replica staleness. A control
# update invalidates this process immediately; other processes refresh within
# this TTL. The value is intentionally short because maintenance mode is a
# safety/recovery switch, not a long-lived application setting.
MAINTENANCE_CACHE_TTL_SECONDS = 1.0
_cache_lock = threading.RLock()
_cached_status: dict | None = None
_cached_at = 0.0


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def get_status(db: Session) -> dict:
    rows = {
        r.key: r
        for r in db.query(models.AppSetting)
        .filter(models.AppSetting.key.in_([MAINTENANCE_MODE_KEY, MAINTENANCE_MESSAGE_KEY]))
        .all()
    }
    mode = rows.get(MAINTENANCE_MODE_KEY)
    msg = rows.get(MAINTENANCE_MESSAGE_KEY)
    return {
        "enabled": _bool(mode.value if mode else None),
        "message": (msg.value if msg and msg.value.strip() else DEFAULT_MESSAGE),
        "updated_at": mode.updated_at.isoformat() if mode and mode.updated_at else None,
        "updated_by": mode.updated_by if mode else None,
    }


def invalidate_status_cache() -> None:
    """Drop the local middleware cache after a committed control change."""
    global _cached_status, _cached_at
    with _cache_lock:
        _cached_status = None
        _cached_at = 0.0


def get_cached_status() -> dict:
    """Return maintenance state with a bounded-TTL process-local cache.

    A lock also provides single-flight behavior on a cold cache: a burst of
    requests causes one DB read, while the other worker threads wait for the
    cached result instead of opening their own connections simultaneously.
    """
    global _cached_status, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_status is not None and now - _cached_at < MAINTENANCE_CACHE_TTL_SECONDS:
            return deepcopy(_cached_status)

        db = database.SessionLocal()
        try:
            status = get_status(db)
        finally:
            db.close()
        _cached_status = deepcopy(status)
        _cached_at = time.monotonic()
        return status


def update_status(db: Session, enabled: bool, message: str, user: dict) -> dict:
    message = message.strip() or DEFAULT_MESSAGE
    before = get_status(db)
    for key, value in ((MAINTENANCE_MODE_KEY, "true" if enabled else "false"), (MAINTENANCE_MESSAGE_KEY, message)):
        row = db.query(models.AppSetting).filter(models.AppSetting.key == key).first()
        if row is None:
            db.add(models.AppSetting(key=key, value=value, updated_by=user["email"]))
        else:
            row.value = value
            row.updated_by = user["email"]
    action = "MAINTENANCE_MODE_ENABLED" if enabled else "MAINTENANCE_MODE_DISABLED"
    db.add(models.AuditLog(operator=user["email"], action=action, target_type="AppSetting", target_id=0, details=f"Maintenance mode changed from {before['enabled']} to {enabled}. Message: {message}"))
    db.commit()
    invalidate_status_cache()
    return get_status(db)
