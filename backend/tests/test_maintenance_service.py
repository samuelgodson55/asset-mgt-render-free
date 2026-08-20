# Covers services/maintenance_service.py: reading the default (off) status
# on a fresh DB, and that toggling it both persists to the AppSetting
# key/value store AND writes an audit log entry (so "who turned on
# maintenance mode and when" is always answerable later).
import models
from services import maintenance_service

def test_maintenance_defaults_off(db_session):
    # No AppSetting row exists yet on a fresh DB -- get_status() must fall
    # back to "disabled" with a non-empty default message rather than
    # erroring or returning enabled=True.
    status = maintenance_service.get_status(db_session)
    assert status["enabled"] is False
    assert status["message"]

def test_maintenance_update_persists_and_audits(db_session):
    user={"email":"root@example.com"}
    status=maintenance_service.update_status(db_session, True, "Deploying an update", user)
    assert status["enabled"] is True
    assert status["message"] == "Deploying an update"
    # Persisted as the string "true" (not a native bool) since AppSetting
    # is a generic string key/value table shared by other settings too.
    assert db_session.query(models.AppSetting).filter_by(key=maintenance_service.MAINTENANCE_MODE_KEY).first().value == "true"
    # Exactly one audit row for this toggle -- catches both "no audit
    # entry written" and "duplicate entries written" regressions.
    assert db_session.query(models.AuditLog).filter_by(action="MAINTENANCE_MODE_ENABLED").count() == 1
