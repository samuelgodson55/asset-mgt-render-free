"""Runtime maintenance-mode settings backed by AppSetting."""
from sqlalchemy.orm import Session
import models

MAINTENANCE_MODE_KEY = "maintenance_mode"
MAINTENANCE_MESSAGE_KEY = "maintenance_message"
DEFAULT_MESSAGE = "We are currently performing scheduled maintenance. Please check back shortly."

def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

def get_status(db: Session) -> dict:
    rows = {r.key: r for r in db.query(models.AppSetting).filter(models.AppSetting.key.in_([MAINTENANCE_MODE_KEY, MAINTENANCE_MESSAGE_KEY])).all()}
    mode = rows.get(MAINTENANCE_MODE_KEY)
    msg = rows.get(MAINTENANCE_MESSAGE_KEY)
    return {
        "enabled": _bool(mode.value if mode else None),
        "message": (msg.value if msg and msg.value.strip() else DEFAULT_MESSAGE),
        "updated_at": mode.updated_at.isoformat() if mode and mode.updated_at else None,
        "updated_by": mode.updated_by if mode else None,
    }

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
    return get_status(db)
