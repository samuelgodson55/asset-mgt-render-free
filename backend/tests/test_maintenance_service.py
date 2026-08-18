import models
from services import maintenance_service

def test_maintenance_defaults_off(db_session):
    status = maintenance_service.get_status(db_session)
    assert status["enabled"] is False
    assert status["message"]

def test_maintenance_update_persists_and_audits(db_session):
    user={"email":"root@example.com"}
    status=maintenance_service.update_status(db_session, True, "Deploying an update", user)
    assert status["enabled"] is True
    assert status["message"] == "Deploying an update"
    assert db_session.query(models.AppSetting).filter_by(key=maintenance_service.MAINTENANCE_MODE_KEY).first().value == "true"
    assert db_session.query(models.AuditLog).filter_by(action="MAINTENANCE_MODE_ENABLED").count() == 1
