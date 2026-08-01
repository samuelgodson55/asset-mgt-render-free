"""
tests/test_mfa.py
------------------
Two-factor authentication (TOTP), currently required only for
role == super_admin -- see services/auth_service.py's login()/
mfa_setup_confirm()/mfa_verify(). Covers: first-login enrollment, a
second login using an already-enrolled code, wrong/expired codes being
rejected, non-super_admin roles never being asked for 2FA at all, and
that no session cookie/data leaks out before the second factor succeeds.
"""

import pyotp

from conftest import DEMO_USERS, SUPER_ADMIN


def test_regular_roles_never_require_2fa(client):
    """Only super_admin goes through the MFA branch -- everyone else's
    login response looks exactly like it did before this feature existed."""
    for role, creds in DEMO_USERS.items():
        response = client.post("/api/auth/login", json=creds)
        assert response.status_code == 200, f"{role} login failed: {response.text}"
        body = response.json()
        assert "mfa_required" not in body
        assert "mfa_setup_required" not in body
        assert body["role"] in {"admin", "manager", "staff", "customer"}


def test_super_admin_first_login_requires_2fa_setup(client):
    """A fresh (never-enrolled) super_admin gets a setup challenge instead
    of a session -- no cookie should be usable yet."""
    response = client.post("/api/auth/login", json=SUPER_ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_setup_required"] is True
    assert body["mfa_setup_token"]
    assert body["totp_secret"]
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    # No real session was granted -- a protected route must still 401.
    me = client.get("/api/auth/me")
    assert me.status_code == 401


def test_super_admin_setup_confirm_wrong_code_rejected(client):
    login_resp = client.post("/api/auth/login", json=SUPER_ADMIN).json()
    bad = client.post(
        "/api/auth/mfa/setup/confirm",
        json={"mfa_setup_token": login_resp["mfa_setup_token"], "code": "000000"},
    )
    assert bad.status_code == 400
    # Still no session.
    assert client.get("/api/auth/me").status_code == 401


def test_super_admin_setup_confirm_correct_code_grants_session(client):
    login_resp = client.post("/api/auth/login", json=SUPER_ADMIN).json()
    code = pyotp.TOTP(login_resp["totp_secret"]).now()
    confirm = client.post(
        "/api/auth/mfa/setup/confirm",
        json={"mfa_setup_token": login_resp["mfa_setup_token"], "code": code},
    )
    assert confirm.status_code == 200
    assert confirm.json()["role"] == "super_admin"
    assert "token" not in confirm.json()  # never leaked into the JSON body

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "super_admin"


def test_super_admin_second_login_uses_verify_not_setup(client):
    """Once enrolled, subsequent logins ask for a code against the
    EXISTING secret (mfa_required) rather than generating a new one."""
    first = client.post("/api/auth/login", json=SUPER_ADMIN).json()
    secret = first["totp_secret"]
    client.post(
        "/api/auth/mfa/setup/confirm",
        json={"mfa_setup_token": first["mfa_setup_token"], "code": pyotp.TOTP(secret).now()},
    )
    client.post("/api/auth/logout")

    second = client.post("/api/auth/login", json=SUPER_ADMIN)
    assert second.status_code == 200
    body = second.json()
    assert body["mfa_required"] is True
    assert "mfa_setup_required" not in body
    assert "totp_secret" not in body  # the already-confirmed secret is never re-shown
    assert client.get("/api/auth/me").status_code == 401  # not logged in yet

    verify = client.post(
        "/api/auth/mfa/verify",
        json={"mfa_pending_token": body["mfa_pending_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert verify.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_super_admin_verify_wrong_code_rejected_and_locks_out(client, db_session):
    import models

    first = client.post("/api/auth/login", json=SUPER_ADMIN).json()
    secret = first["totp_secret"]
    client.post(
        "/api/auth/mfa/setup/confirm",
        json={"mfa_setup_token": first["mfa_setup_token"], "code": pyotp.TOTP(secret).now()},
    )
    client.post("/api/auth/logout")

    pending_token = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]

    bad = client.post("/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": "000000"})
    assert bad.status_code == 401
    assert client.get("/api/auth/me").status_code == 401

    # SECURITY: wrong 2FA codes count against the SAME per-account lockout
    # a wrong password does (see auth_service.py's mfa_verify() docstring).
    su = db_session.query(models.User).filter(models.User.role == "super_admin").first()
    assert su.failed_login_attempts == 1


def test_expired_or_garbage_mfa_tokens_rejected(client):
    setup_confirm = client.post(
        "/api/auth/mfa/setup/confirm", json={"mfa_setup_token": "not-a-real-token", "code": "123456"}
    )
    assert setup_confirm.status_code == 401

    verify = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": "not-a-real-token", "code": "123456"}
    )
    assert verify.status_code == 401


def _enroll_super_admin(client):
    """Shared setup for the recovery-code tests below: completes 2FA
    enrollment and returns (secret, recovery_codes)."""
    login_resp = client.post("/api/auth/login", json=SUPER_ADMIN).json()
    secret = login_resp["totp_secret"]
    confirm = client.post(
        "/api/auth/mfa/setup/confirm",
        json={"mfa_setup_token": login_resp["mfa_setup_token"], "code": pyotp.TOTP(secret).now()},
    )
    body = confirm.json()
    return secret, body["recovery_codes"]


def test_enrollment_issues_ten_distinct_recovery_codes(client):
    _secret, codes = _enroll_super_admin(client)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for code in codes:
        assert len(code) == 11 and code[5] == "-"  # XXXXX-XXXXX


def test_recovery_code_triggers_reenrollment_not_immediate_login(client, db_session):
    """A correct recovery code means the original device is gone, so
    login isn't completed under the OLD (now-untrusted) secret -- the
    caller instead gets a fresh mfa_setup_required challenge, exactly
    like a brand-new account's first login, so a NEW device can enroll."""
    import models

    secret, codes = _enroll_super_admin(client)
    client.post("/api/auth/logout")

    pending_token = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    code = codes[0]

    first_use = client.post("/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": code})
    assert first_use.status_code == 200
    body = first_use.json()
    assert body["mfa_setup_required"] is True
    assert body["recovery_code_used"] is True
    assert body["mfa_setup_token"]
    new_secret = body["totp_secret"]
    assert new_secret != secret  # a genuinely fresh secret, not the old (compromised-device) one
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    # No session was granted yet -- only a setup challenge.
    assert client.get("/api/auth/me").status_code == 401

    # The recovery code itself is still single-use, exactly as before --
    # reuse the still-valid pending token from the first attempt (a fresh
    # login no longer hands back a NEW mfa_pending_token at this point,
    # since totp_enabled is now False -- see the DB assertion below).
    reuse = client.post("/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": code})
    assert reuse.status_code == 401

    # And the account is genuinely mid-re-enrollment in the DB: the old
    # secret is gone and totp_enabled is back to False.
    su = db_session.query(models.User).filter(models.User.role == "super_admin").first()
    assert su.totp_enabled is False
    assert su.totp_secret_encrypted is not None  # a NEW (unconfirmed) secret, not the old one


def test_recovery_code_reenrollment_completes_login_and_issues_fresh_codes(client):
    """Finishing the challenge from a recovery-code login (confirming a
    LIVE code from the new secret via the normal mfa/setup/confirm
    endpoint) grants the real session and reissues a whole new batch of
    recovery codes -- invalidating every remaining old one too."""
    secret, codes = _enroll_super_admin(client)
    client.post("/api/auth/logout")

    pending_token = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    challenge = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": codes[0]}
    ).json()
    new_secret = challenge["totp_secret"]

    confirm = client.post(
        "/api/auth/mfa/setup/confirm",
        json={"mfa_setup_token": challenge["mfa_setup_token"], "code": pyotp.TOTP(new_secret).now()},
    )
    assert confirm.status_code == 200
    confirm_body = confirm.json()
    assert confirm_body["role"] == "super_admin"
    new_codes = confirm_body["recovery_codes"]
    assert len(new_codes) == 10
    assert set(new_codes).isdisjoint(set(codes))  # remaining old codes are gone too

    # Real session now exists.
    assert client.get("/api/auth/me").status_code == 200

    # The OLD TOTP secret no longer works for a subsequent login.
    client.post("/api/auth/logout")
    pending_token_2 = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    old_secret_attempt = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token_2, "code": pyotp.TOTP(secret).now()}
    )
    assert old_secret_attempt.status_code == 401

    # ...but the NEW one does.
    pending_token_3 = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    new_secret_attempt = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token_3, "code": pyotp.TOTP(new_secret).now()}
    )
    assert new_secret_attempt.status_code == 200


def test_recovery_code_accepted_lowercase(client):
    """Codes are generated uppercase but a lowercase paste should still
    work -- still triggers the re-enrollment challenge, not a 401."""
    secret, codes = _enroll_super_admin(client)
    client.post("/api/auth/logout")
    pending_token = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    verify = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": codes[0].lower()}
    )
    assert verify.status_code == 200
    assert verify.json()["mfa_setup_required"] is True


def test_garbage_recovery_shaped_code_rejected(client):
    _secret, _codes = _enroll_super_admin(client)
    client.post("/api/auth/logout")
    pending_token = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    bad = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": "ZZZZZ-ZZZZZ"}
    )
    assert bad.status_code == 401


def test_regenerate_recovery_codes_invalidates_old_ones(client):
    secret, codes = _enroll_super_admin(client)

    regen = client.post("/api/auth/mfa/recovery-codes/regenerate", json={"password": SUPER_ADMIN["password"]})
    assert regen.status_code == 200
    new_codes = regen.json()["recovery_codes"]
    assert len(new_codes) == 10
    assert set(new_codes).isdisjoint(set(codes))

    client.post("/api/auth/logout")
    pending_token = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]

    # An OLD code no longer works...
    old_rejected = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token, "code": codes[0]}
    )
    assert old_rejected.status_code == 401

    # ...but a NEW one does (fresh pending token, since the failed attempt
    # above didn't lock the account out on its own). Accepting it starts
    # the re-enrollment challenge (see test_recovery_code_triggers_
    # reenrollment_not_immediate_login) rather than an immediate session.
    pending_token_2 = client.post("/api/auth/login", json=SUPER_ADMIN).json()["mfa_pending_token"]
    new_accepted = client.post(
        "/api/auth/mfa/verify", json={"mfa_pending_token": pending_token_2, "code": new_codes[0]}
    )
    assert new_accepted.status_code == 200
    assert new_accepted.json()["mfa_setup_required"] is True


def test_regenerate_recovery_codes_requires_correct_password(client):
    _enroll_super_admin(client)
    wrong = client.post(
        "/api/auth/mfa/recovery-codes/regenerate", json={"password": "definitely-wrong"}
    )
    assert wrong.status_code == 401


def test_regenerate_recovery_codes_requires_auth(client):
    """Not logged in at all -- must 401, not 403/500."""
    resp = client.post(
        "/api/auth/mfa/recovery-codes/regenerate", json={"password": SUPER_ADMIN["password"]}
    )
    assert resp.status_code == 401


def test_non_super_admin_cannot_regenerate_recovery_codes(client):
    from conftest import auth_headers

    headers = auth_headers(client, **DEMO_USERS["admin"])
    resp = client.post(
        "/api/auth/mfa/recovery-codes/regenerate", json={"password": DEMO_USERS["admin"]["password"]}, headers=headers,
    )
    assert resp.status_code == 403


def test_mfa_setup_token_cannot_be_reused_as_verify_token(client):
    """A setup-purpose token must be rejected by the verify endpoint (and
    vice versa) -- see security.py's decode_mfa_token() purpose check."""
    login_resp = client.post("/api/auth/login", json=SUPER_ADMIN).json()
    misuse = client.post(
        "/api/auth/mfa/verify",
        json={"mfa_pending_token": login_resp["mfa_setup_token"], "code": pyotp.TOTP(login_resp["totp_secret"]).now()},
    )
    assert misuse.status_code == 401
