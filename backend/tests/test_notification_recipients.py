"""
tests/test_notification_recipients.py
----------------------------------------
Regression coverage for services/extension_service.py's
_notification_recipients(): who gets emailed about a NEW extension
request must be the admin-configured "Digest Recipients" list (PUT
/settings/digest-recipients) + ADMIN_NOTIFICATION_EMAILS -- the exact same
audience tasks/notification_tasks.py's daily digests use -- and NOT every
Admin/Manager account queried by role. See git history / README.md's
"Email notifications" section for the full rationale.
"""

import models
from services.extension_service import _notification_recipients
from services.notification_service import set_digest_recipient_emails
from schemas.notifications_schema import DigestRecipientsUpdateRequest


def test_recipients_come_from_digest_list_not_admin_manager_roles(db_session):
    # seed_db() (run automatically via the `client`/`db_engine` fixtures'
    # startup event) already created an Admin (r.adeyemi@corp.io) and a
    # Manager (s.chen@corp.io) -- neither should show up here just by
    # virtue of their role.
    admin_emails = {u.email for u in db_session.query(models.User).filter(models.User.role.in_(("admin", "manager")))}
    assert admin_emails, "seed_db() should have created at least one admin/manager account"

    recipients = _notification_recipients(db_session)
    assert recipients == [], "with no Digest Recipients configured, nobody should be notified"
    assert not admin_emails & set(recipients)


def test_recipients_match_configured_digest_list(db_session):
    fake_admin_user = {"email": "test-runner@corp.io"}
    set_digest_recipient_emails(
        db_session,
        DigestRecipientsUpdateRequest(emails=["ops@example.com", "oncall@example.com"]),
        fake_admin_user,
    )

    recipients = _notification_recipients(db_session)
    assert recipients == ["ops@example.com", "oncall@example.com"]

    # The seeded Admin/Manager accounts' own addresses must NOT be present
    # unless someone explicitly added them to the Digest Recipients list.
    admin_emails = {u.email for u in db_session.query(models.User).filter(models.User.role.in_(("admin", "manager")))}
    assert not admin_emails & set(recipients)


def test_extension_request_notification_uses_digest_list_end_to_end(as_admin, as_manager, as_staff, db_session):
    """
    Full HTTP-level version of the two unit tests above: configure the
    Digest Recipients list via the real PUT endpoint, create a brand-new
    extension request over HTTP, and confirm the recipient computation the
    request handler relies on reflects that configured list -- not the
    Admin/Manager accounts that are also logged in during this test.
    """
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff

    put_response = admin_client.put(
        "/api/settings/digest-recipients", headers=admin_headers,
        json={"emails": ["asset-ops@example.com"]},
    )
    assert put_response.status_code == 200

    pool = admin_client.post("/api/assets", headers=admin_headers, json={"name": "Notification Test Pool", "total_quantity": 2})
    asset_id = pool.json()["id"]

    users = manager_client.get("/api/users", headers=manager_headers).json()["items"]
    staff_user = next(u for u in users if u["email"] == "t.okafor@corp.io")
    manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "user", "quantity": 1, "user_id": staff_user["id"]},
    )
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    import datetime
    new_due_date = (datetime.date.today() + datetime.timedelta(days=21)).isoformat()
    request = staff_client.post(
        f"/api/checkouts/{checkout_row.id}/extension-requests", headers=staff_headers,
        json={"new_due_date": new_due_date},
    )
    assert request.status_code == 200

    # The audience the request handler would have notified -- same
    # function the endpoint itself calls.
    recipients = _notification_recipients(db_session)
    assert recipients == ["asset-ops@example.com"]
