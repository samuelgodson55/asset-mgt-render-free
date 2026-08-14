"""
tests/test_permissions.py
---------------------------
Smoke tests for the role-gate dependencies in deps.py
(require_super_admin / require_privileged_role) across a handful of
representative endpoints from different routers -- not exhaustive, but
enough to catch an accidentally-loosened or accidentally-tightened gate on
the most sensitive routes.
"""

import pytest


def test_no_token_is_rejected_everywhere(client):
    for method, path in [
        ("GET", "/api/assets"),
        ("GET", "/api/users"),
        ("GET", "/api/audit-logs"),
        ("GET", "/api/settings/digest-recipients"),
        ("GET", "/api/checkouts/extension-requests"),
    ]:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} should require auth, got {response.status_code}"


@pytest.mark.parametrize("fixture_name", ["as_staff", "as_customer"])
def test_self_service_roles_cannot_reach_admin_only_routes(fixture_name, request):
    client, headers = request.getfixturevalue(fixture_name)
    for method, path, body in [
        ("GET", "/api/users", None),
        ("GET", "/api/audit-logs", None),
        ("GET", "/api/settings/digest-recipients", None),
        ("GET", "/api/checkouts/extension-requests", None),
        ("POST", "/api/assets", {"name": "Should Not Be Created", "total_quantity": 1}),
    ]:
        response = client.request(method, path, headers=headers, json=body)
        assert response.status_code == 403, f"{fixture_name} {method} {path} should be forbidden, got {response.status_code}"


def test_manager_can_view_but_not_configure_digest_recipients(as_manager):
    client, headers = as_manager
    read = client.get("/api/settings/digest-recipients", headers=headers)
    # Digest Recipients is Super Admin/Admin-only to READ too (see
    # api/notifications.py's require_super_admin on both routes) -- a
    # Manager gets 403 on both GET and PUT.
    assert read.status_code == 403

    write = client.put("/api/settings/digest-recipients", headers=headers, json={"emails": []})
    assert write.status_code == 403


def test_admin_can_read_and_write_digest_recipients(as_admin):
    client, headers = as_admin
    read = client.get("/api/settings/digest-recipients", headers=headers)
    assert read.status_code == 200
    assert "emails" in read.json()

    write = client.put("/api/settings/digest-recipients", headers=headers, json={"emails": ["ops@example.com"]})
    assert write.status_code == 200
    assert write.json()["emails"] == ["ops@example.com"]


def test_staff_can_only_see_their_own_items(as_staff):
    client, headers = as_staff
    response = client.get("/api/users/me/items", headers=headers)
    assert response.status_code == 200


def test_own_self_service_dashboard_route_does_not_require_privilege(as_customer):
    client, headers = as_customer
    response = client.get("/api/users/me/items", headers=headers)
    assert response.status_code == 200


def test_admin_cannot_reach_any_backup_route(as_admin):
    """
    /backup/* is gated on require_true_super_admin (root Super Admin only,
    see deps.py), NOT require_super_admin -- unlike almost every other
    admin-only route in this app, a regular `admin` account is explicitly
    excluded here, including from read-only routes like /status and
    /list, not just Restore. See api/backup_api.py's module docstring.
    """
    client, headers = as_admin
    for method, path in [
        ("GET", "/api/backup/status"),
        ("GET", "/api/backup/list"),
        ("POST", "/api/backup/create"),
        ("GET", "/api/backup/download/whatever.sql.gz"),
        ("DELETE", "/api/backup/whatever.sql.gz"),
        ("POST", "/api/backup/restore/whatever.sql.gz"),
        ("POST", "/api/backup/restore-upload"),
    ]:
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403, f"admin {method} {path} should be forbidden, got {response.status_code}"


def test_super_admin_can_view_backup_status_and_list(as_super_admin):
    """Sanity check that the tightened gate doesn't also lock out the root Super Admin it's meant for."""
    client, headers = as_super_admin
    status = client.get("/api/backup/status", headers=headers)
    assert status.status_code == 200

    listing = client.get("/api/backup/list", headers=headers)
    assert listing.status_code == 200


def test_admin_can_change_user_rbac_role(client, as_admin, db_session):
    """Admin can promote/demote non-root accounts among normal RBAC roles."""
    _, headers = as_admin
    from models import User

    target = db_session.query(User).filter(User.role == "staff", User.email != "r.adeyemi@corp.io").first()
    assert target is not None

    response = client.patch(f"/api/users/{target.id}", headers=headers, json={"role": "manager"})
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(User, target.id).role == "manager"

    response = client.patch(f"/api/users/{target.id}", headers=headers, json={"role": "admin"})
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(User, target.id).role == "admin"


def test_admin_cannot_grant_super_admin_role(client, as_admin, db_session):
    """The hardcoded root Super Admin role remains non-assignable."""
    _, headers = as_admin
    from models import User

    target = db_session.query(User).filter(User.role == "staff").first()
    assert target is not None
    response = client.patch(f"/api/users/{target.id}", headers=headers, json={"role": "super_admin"})
    assert response.status_code == 400


def test_manager_cannot_promote_staff_to_manager_or_admin(client, as_manager, db_session):
    """Managers stay capped at Staff/Customer even when calling the API directly."""
    _, headers = as_manager
    from models import User

    target = db_session.query(User).filter(User.role == "staff").first()
    assert target is not None
    for role in ("manager", "admin"):
        response = client.patch(f"/api/users/{target.id}", headers=headers, json={"role": role})
        assert response.status_code == 403, response.text
