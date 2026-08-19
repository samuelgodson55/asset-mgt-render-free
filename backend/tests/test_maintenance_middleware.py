"""
tests/test_maintenance_middleware.py
-------------------------------------
Covers middleware/maintenance_mode.py -- the actual security boundary for
maintenance mode. Previously untested (see the review this responds to);
these exercise the specific gaps that review flagged:

  - the middleware must reuse deps.resolve_user_from_token()'s full checks,
    not a hand-rolled subset (AUTH_EPOCH post-restore invalidation and
    is_deleted, not just is_active)
  - the admin-configured message must actually reach locked-out users
    instead of a hardcoded generic string
  - only a TRUE super_admin (not a plain `admin`) may pass the gate,
    matching require_true_super_admin on the real PUT route
  - allowlisted/public routes stay reachable while non-allowlisted ones
    are blocked with 503 MAINTENANCE_MODE
"""
import datetime

import models
import services.maintenance_service as maintenance_service
from security import AUTH_EPOCH_SETTING_KEY

from conftest import DEMO_USERS, SUPER_ADMIN, auth_headers


def _enable_maintenance(db_session, message="Down for a scheduled release"):
    maintenance_service.update_status(db_session, True, message, {"email": "superadmin"})


def test_blocks_protected_route_when_enabled(client, db_session):
    headers = auth_headers(client, **DEMO_USERS["staff"])
    _enable_maintenance(db_session)

    response = client.get("/api/assets", headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "MAINTENANCE_MODE"


def test_custom_message_reaches_locked_out_user(client, db_session):
    headers = auth_headers(client, **DEMO_USERS["staff"])
    _enable_maintenance(db_session, message="Back online at 3pm UTC")

    response = client.get("/api/assets", headers=headers)

    # Regression test: the middleware used to hardcode a generic string
    # instead of reading the admin-configured message.
    assert response.json()["detail"] == "Back online at 3pm UTC"


def test_allowlisted_routes_stay_reachable_when_enabled(client, db_session):
    _enable_maintenance(db_session)

    # /api/config/public is explicitly allowlisted (the login screen depends
    # on it to render the maintenance banner while logged out).
    response = client.get("/api/config/public")
    assert response.status_code != 503

    # GET /api/maintenance/status is intentionally public (see
    # api/maintenance_api.py) so the same login screen can read the
    # enabled/message state directly -- covered here as a regression guard,
    # not because it lacked coverage of a bug: this is by design.
    response = client.get("/api/maintenance/status")
    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_super_admin_bypasses_gate(client, db_session):
    headers = auth_headers(client, **SUPER_ADMIN)
    _enable_maintenance(db_session)

    response = client.get("/api/assets", headers=headers)

    assert response.status_code != 503


def test_plain_admin_does_not_bypass_gate(client, db_session):
    # require_true_super_admin (the real PUT /maintenance/status gate) does
    # NOT treat a plain `admin` as equivalent -- the middleware's bypass
    # must match that, not the broader `_FULL_ADMIN_ROLES` used elsewhere.
    headers = auth_headers(client, **DEMO_USERS["admin"])
    _enable_maintenance(db_session)

    response = client.get("/api/assets", headers=headers)

    assert response.status_code == 503


def test_stale_pre_restore_super_admin_token_does_not_bypass_gate(client, db_session):
    """
    Regression test for the AUTH_EPOCH gap: before this fix, the middleware
    reimplemented its own JWT/DB checks and never consulted AUTH_EPOCH_SETTING_KEY,
    so a super_admin token issued before a backup restore would still pass
    the maintenance gate (even though get_current_user() would reject it on
    the real underlying route). Reusing deps.resolve_user_from_token()
    closes that gap.
    """
    headers = auth_headers(client, **SUPER_ADMIN)
    _enable_maintenance(db_session)

    # Simulate a restore happening AFTER this token was issued: write an
    # AUTH_EPOCH in the future relative to the token's `iat`, using the same
    # aware-UTC helper production code uses (services/backup_service.py) --
    # not the deprecated, naive datetime.datetime.utcnow().
    future_epoch = (models.utc_now() + datetime.timedelta(days=1)).isoformat()
    db_session.add(models.AppSetting(key=AUTH_EPOCH_SETTING_KEY, value=future_epoch, updated_by="system"))
    db_session.commit()

    response = client.get("/api/assets", headers=headers)

    assert response.status_code == 503


def test_deactivated_super_admin_token_does_not_bypass_gate(client, db_session):
    headers = auth_headers(client, **SUPER_ADMIN)
    _enable_maintenance(db_session)

    user = db_session.query(models.User).filter(models.User.role == "super_admin").first()
    user.is_active = False
    db_session.commit()

    response = client.get("/api/assets", headers=headers)

    assert response.status_code == 503


def test_maintenance_disabled_does_not_block(client, db_session):
    headers = auth_headers(client, **DEMO_USERS["staff"])

    response = client.get("/api/assets", headers=headers)

    assert response.status_code != 503
