"""
tests/test_user_convert_to_outsider.py
----------------------------------------
"Revoke a user's login access, turning them into an ad-hoc individual":
POST /users/{id}/convert-to-outsider (services/user_service.py ->
convert_user_to_outsider()). The exact reverse of
test_outsider_convert_to_user.py -- covers the same safe-migration
guarantees mirrored the other way: every existing checkout (active AND
returned) and quotation assignment follows the person over to their new
ad-hoc profile, the old login is retired (soft-deleted + is_active=False
+ `converted_to_outsider_id` set) rather than erased, the account can no
longer log in afterward, and the same Manager role ceiling account
provisioning gets applies here too.
"""

import datetime

import models

TODAY = datetime.date.today()
DUE_DATE = (TODAY + datetime.timedelta(days=14)).isoformat()


def _create_pool(client, headers, name="Revoke Test Pool", total_quantity=5):
    response = client.post("/api/assets", headers=headers, json={"name": name, "total_quantity": total_quantity})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _get_user_by_email(client, headers, email):
    users = client.get("/api/users", headers=headers).json()["items"]
    return next(u for u in users if u["email"] == email)


def _dispatch_to_user(client, headers, asset_id, user_id, quantity=1, due_date=DUE_DATE):
    response = client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=headers,
        json={"assignee_type": "user", "quantity": quantity, "user_id": user_id, "due_date": due_date},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# HAPPY PATH
# ---------------------------------------------------------------------------

def test_convert_migrates_active_and_returned_checkouts_and_blocks_login(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff = _get_user_by_email(admin_client, admin_headers, "a.bello@corp.io")

    asset_id = _create_pool(admin_client, admin_headers, name="Pool A")
    _dispatch_to_user(manager_client, manager_headers, asset_id, staff["id"], quantity=1)

    second_pool_id = _create_pool(admin_client, admin_headers, name="Pool B", total_quantity=3)
    _dispatch_to_user(admin_client, admin_headers, second_pool_id, staff["id"], quantity=1)

    ledger = admin_client.get(f"/api/users/{staff['id']}/items", headers=admin_headers)
    assert ledger.status_code == 200, ledger.text
    checkout_ids = [item["checkout_id"] for item in ledger.json()["assigned_items"]]
    assert len(checkout_ids) == 2
    returned_checkout_id, still_active_checkout_id = checkout_ids[0], checkout_ids[1]
    ret = admin_client.post(f"/api/checkouts/{returned_checkout_id}/return", headers=admin_headers, json={"quantity": 1})
    assert ret.status_code == 200, ret.text

    convert = admin_client.post(f"/api/users/{staff['id']}/convert-to-outsider", headers=admin_headers, json={})
    assert convert.status_code == 200, convert.text
    body = convert.json()
    assert body["checkouts_migrated"] == 2
    assert body["quotations_migrated"] == 0
    # email defaults to the account's own login email when omitted.
    assert body["email"] == "a.bello@corp.io"
    new_outsider_id = body["outsider_id"]

    # The old login can no longer authenticate.
    login = admin_client.post("/api/auth/login", json={"identifier": "a.bello@corp.io", "password": "Staff123!"})
    assert login.status_code in (401, 403), login.text

    # Both checkouts (the returned one AND the still-active one) now
    # belong to the new outsider profile, not the old user.
    db_session.expire_all()
    still_active = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == still_active_checkout_id).first()
    returned = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == returned_checkout_id).first()
    assert still_active.outsider_id == new_outsider_id and still_active.user_id is None
    assert returned.outsider_id == new_outsider_id and returned.user_id is None

    # The new profile's own Custody Ledger shows the still-outstanding item.
    items = admin_client.get(f"/api/outsiders/{new_outsider_id}/items", headers=admin_headers)
    assert items.status_code == 200, items.text
    assert any(i["checkout_id"] == still_active_checkout_id for i in items.json()["assigned_items"])

    # The old account is retired: gone from the live User Directory, but
    # its row (and the permanent link to what it became) is intact.
    directory = admin_client.get("/api/users", headers=admin_headers)
    assert staff["id"] not in [u["id"] for u in directory.json()["items"]]
    user_row = db_session.query(models.User).filter(models.User.id == staff["id"]).first()
    assert user_row.is_deleted is True
    assert user_row.is_active is False
    assert user_row.converted_to_outsider_id == new_outsider_id


def test_convert_migrates_quotation_assignment(as_admin, db_session):
    admin_client, admin_headers = as_admin
    customer = _get_user_by_email(admin_client, admin_headers, "d.martins@customer.io")

    quote = admin_client.post(
        "/api/quotations", headers=admin_headers,
        json={"assignee_type": "user", "assigned_user_id": customer["id"]},
    )
    assert quote.status_code == 200, quote.text
    quotation_id = quote.json()["id"]

    convert = admin_client.post(
        f"/api/users/{customer['id']}/convert-to-outsider", headers=admin_headers,
        json={"email": "d.martins@personal.example", "company": "Martins & Co"},
    )
    assert convert.status_code == 200, convert.text
    assert convert.json()["quotations_migrated"] == 1
    assert convert.json()["email"] == "d.martins@personal.example"
    assert convert.json()["company"] == "Martins & Co"
    new_outsider_id = convert.json()["outsider_id"]

    db_session.expire_all()
    quotation_row = db_session.query(models.Quotation).filter(models.Quotation.id == quotation_id).first()
    assert quotation_row.assigned_outsider_id == new_outsider_id
    assert quotation_row.assigned_to_id is None


# ---------------------------------------------------------------------------
# PERMISSION / VALIDATION GUARDS
# ---------------------------------------------------------------------------

def test_manager_cannot_revoke_an_admin_or_manager_account(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    admin_target = _get_user_by_email(admin_client, admin_headers, "r.adeyemi@corp.io")

    response = manager_client.post(f"/api/users/{admin_target['id']}/convert-to-outsider", headers=manager_headers, json={})
    assert response.status_code == 403, response.text

    db_session.expire_all()
    user_row = db_session.query(models.User).filter(models.User.id == admin_target["id"]).first()
    assert user_row.is_deleted is False
    assert user_row.converted_to_outsider_id is None


def test_manager_can_revoke_a_staff_account(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff = _get_user_by_email(admin_client, admin_headers, "a.bello@corp.io")

    response = manager_client.post(f"/api/users/{staff['id']}/convert-to-outsider", headers=manager_headers, json={})
    assert response.status_code == 200, response.text


def test_staff_cannot_revoke_other_users(as_admin, as_staff, db_session):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    target = _get_user_by_email(admin_client, admin_headers, "a.bello@corp.io")

    response = staff_client.post(f"/api/users/{target['id']}/convert-to-outsider", headers=staff_headers, json={})
    assert response.status_code == 403, response.text


def test_admin_cannot_revoke_their_own_login(as_admin, db_session):
    admin_client, admin_headers = as_admin
    admin_self = _get_user_by_email(admin_client, admin_headers, "r.adeyemi@corp.io")

    response = admin_client.post(f"/api/users/{admin_self['id']}/convert-to-outsider", headers=admin_headers, json={})
    assert response.status_code == 403, response.text


def test_converting_already_converted_user_404s(as_admin, db_session):
    admin_client, admin_headers = as_admin
    staff = _get_user_by_email(admin_client, admin_headers, "t.okafor@corp.io")

    first = admin_client.post(f"/api/users/{staff['id']}/convert-to-outsider", headers=admin_headers, json={})
    assert first.status_code == 200, first.text

    second = admin_client.post(f"/api/users/{staff['id']}/convert-to-outsider", headers=admin_headers, json={})
    assert second.status_code == 404, second.text
