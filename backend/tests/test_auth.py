"""
tests/test_auth.py
-------------------
POST /auth/login, GET /auth/me, POST /auth/update-password -- covers every
demo role logging in successfully, bad credentials being rejected, the
hardcoded Super Admin identity, and that a JWT actually gates protected
routes.
"""

from conftest import DEMO_USERS, SUPER_ADMIN


def test_login_succeeds_for_every_seeded_role(client):
    for role, creds in DEMO_USERS.items():
        response = client.post("/api/auth/login", json=creds)
        assert response.status_code == 200, f"{role} login failed: {response.text}"
        body = response.json()
        assert body["role"] in {"admin", "manager", "staff", "customer"}


def test_login_accepts_username_as_well_as_email(client):
    # seed_db() derives a `username` (local-part of the email) for every
    # demo account -- POST /auth/login must accept either (see
    # schemas/auth.py's LoginRequest docstring).
    response = client.post("/api/auth/login", json={"identifier": "r.adeyemi", "password": "Admin123!"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_login_rejects_wrong_password(client):
    response = client.post(
        "/api/auth/login", json={"identifier": DEMO_USERS["admin"]["identifier"], "password": "definitely-wrong"}
    )
    assert response.status_code == 401


def test_login_rejects_unknown_identifier(client):
    response = client.post("/api/auth/login", json={"identifier": "nobody@nowhere.io", "password": "whatever123!"})
    assert response.status_code == 401


def test_super_admin_is_a_hidden_db_row(as_super_admin, client, db_session):
    su_client, headers = as_super_admin
    me = su_client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "super_admin"

    # The root admin IS a real `users` table row now (see security.py's
    # module docstring) -- exactly one -- but it must never appear in the
    # User Directory (GET /users), even to another privileged account.
    import models
    assert db_session.query(models.User).filter(models.User.role == "super_admin").count() == 1

    admin_headers = headers  # already a Super Admin/Admin-equivalent token
    directory = client.get("/api/users", headers=admin_headers)
    assert directory.status_code == 200
    assert all(u["role"] != "super_admin" for u in directory.json()["items"])


def test_protected_route_requires_authentication(client):
    response = client.get("/api/audit-logs")
    assert response.status_code == 401


def test_me_endpoint_reflects_logged_in_user(as_staff):
    client, headers = as_staff
    response = client.get("/api/auth/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == DEMO_USERS["staff"]["identifier"]
    assert body["role"] == "staff"


def test_update_own_password_requires_current_password(as_staff, db_session):
    client, headers = as_staff
    me = client.get("/api/auth/me", headers=headers).json()

    # Wrong current password -> rejected.
    bad = client.post(
        "/api/auth/update-password",
        headers=headers,
        json={"user_id": me["id"], "new_password": "BrandNewPass123!", "current_password": "not-the-real-one"},
    )
    assert bad.status_code in (400, 401, 403)

    # Correct current password -> succeeds, and the new password can log in.
    good = client.post(
        "/api/auth/update-password",
        headers=headers,
        json={"user_id": me["id"], "new_password": "BrandNewPass123!", "current_password": "Staff123!"},
    )
    assert good.status_code == 200

    relogged = client.post(
        "/api/auth/login", json={"identifier": DEMO_USERS["staff"]["identifier"], "password": "BrandNewPass123!"}
    )
    assert relogged.status_code == 200


def test_update_password_rejects_weak_new_password(as_staff):
    client, headers = as_staff
    me = client.get("/api/auth/me", headers=headers).json()
    response = client.post(
        "/api/auth/update-password",
        headers=headers,
        json={"user_id": me["id"], "new_password": "weak", "current_password": "Staff123!"},
    )
    assert response.status_code == 422


def test_maintenance_mode_blocks_non_super_admin_login(as_super_admin, client):
    su_client, headers = as_super_admin
    enabled = su_client.put(
        "/api/maintenance/status",
        headers=headers,
        json={"enabled": True, "message": "Maintenance test"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    # The login endpoint must remain reachable so the root Super Admin can
    # complete its MFA flow, but ordinary users must never receive a session
    # while maintenance is active.
    for role, creds in DEMO_USERS.items():
        response = client.post("/api/auth/login", json=creds)
        assert response.status_code == 503, f"{role} was allowed to log in during maintenance"
        assert response.json()["detail"] == "The application is currently undergoing maintenance."


def test_maintenance_mode_keeps_super_admin_mfa_entry_available(as_super_admin, client):
    # The root Super Admin is the sole maintenance-mode exception. The
    # password step may therefore return an MFA challenge (rather than 503),
    # but must not issue a session cookie until MFA is completed.
    response = client.post("/api/auth/login", json=SUPER_ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body.get("role") == "super_admin"
    assert body.get("mfa_required") or body.get("mfa_setup_required")
    assert "access_token" not in response.cookies
