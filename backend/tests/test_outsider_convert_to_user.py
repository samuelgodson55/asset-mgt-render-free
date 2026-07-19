"""
tests/test_outsider_convert_to_user.py
----------------------------------------
"The outsider finally decides he wants a login": POST
/outsiders/{id}/convert-to-user (services/outsider_service.py ->
convert_outsider_to_user()). Covers the safe-migration guarantees: the
new account can log in, every existing checkout (active AND returned)
and quotation assignment follows them over, the old ad-hoc profile is
retired (soft-deleted + `converted_to_user_id` set) rather than erased,
and the same role/email-uniqueness rules a brand-new account creation
enforces still apply here.
"""

import datetime

import models

TODAY = datetime.date.today()
DUE_DATE = (TODAY + datetime.timedelta(days=14)).isoformat()


def _create_pool(client, headers, name="Convert Test Pool", total_quantity=5):
    response = client.post("/api/assets", headers=headers, json={"name": name, "total_quantity": total_quantity})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _dispatch_new_outsider(client, headers, asset_id, name="Femi Adeyemi", quantity=1, due_date=DUE_DATE):
    response = client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=headers,
        json={
            "assignee_type": "outsider", "quantity": quantity, "due_date": due_date,
            "outsider_name": name, "outsider_contact": "femi@example.com", "outsider_company": "Lagos Fintech Ltd.",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _get_outsider_id_by_name(db_session, name):
    row = db_session.query(models.Outsider).filter(models.Outsider.name == name).first()
    assert row is not None, f"No outsider row named {name!r} was created."
    return row.id


# ---------------------------------------------------------------------------
# HAPPY PATH
# ---------------------------------------------------------------------------

def test_convert_migrates_active_and_returned_checkouts_and_allows_login(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers, name="Pool A", total_quantity=5)

    # One checkout that will stay active, one that will be returned before
    # conversion -- both should follow the person into their new account.
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Femi Adeyemi", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Femi Adeyemi")

    second_pool_id = _create_pool(admin_client, admin_headers, name="Pool B", total_quantity=3)
    response = admin_client.post(
        f"/api/assets/{second_pool_id}/checkout_advanced", headers=admin_headers,
        json={"assignee_type": "outsider", "quantity": 1, "due_date": DUE_DATE, "outsider_id": outsider_id},
    )
    assert response.status_code == 200, response.text

    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    assert ledger.status_code == 200, ledger.text
    checkout_ids = [item["checkout_id"] for item in ledger.json()["assigned_items"]]
    assert len(checkout_ids) == 2
    returned_checkout_id, still_active_checkout_id = checkout_ids[0], checkout_ids[1]
    ret = admin_client.post(f"/api/checkouts/{returned_checkout_id}/return", headers=admin_headers, json={"quantity": 1})
    assert ret.status_code == 200, ret.text

    convert = admin_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=admin_headers,
        json={"email": "femi.adeyemi@corp.io", "password": "Convert123!", "role": "staff", "department": "Engineering"},
    )
    assert convert.status_code == 200, convert.text
    body = convert.json()
    assert body["checkouts_migrated"] == 2
    assert body["quotations_migrated"] == 0
    new_user_id = body["user_id"]

    # The new account can actually log in with the password just set.
    login = admin_client.post("/api/auth/login", json={"identifier": "femi.adeyemi@corp.io", "password": "Convert123!"})
    assert login.status_code == 200, login.text

    # Both checkouts (the returned one AND the still-active one) now
    # belong to the new user, not the old outsider.
    db_session.expire_all()
    still_active = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == still_active_checkout_id).first()
    returned = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == returned_checkout_id).first()
    assert still_active.user_id == new_user_id and still_active.outsider_id is None
    assert returned.user_id == new_user_id and returned.outsider_id is None

    # The new user's own Custody Ledger shows the still-outstanding item.
    items = admin_client.get(f"/api/users/{new_user_id}/items", headers=admin_headers)
    assert items.status_code == 200, items.text
    assert any(i["checkout_id"] == still_active_checkout_id for i in items.json()["assigned_items"])

    # The old ad-hoc profile is retired: gone from the live directory, but
    # its row (and the permanent link to who it became) is intact.
    directory = admin_client.get("/api/outsiders", headers=admin_headers)
    assert outsider_id not in [o["id"] for o in directory.json()["items"]]
    outsider_row = db_session.query(models.Outsider).filter(models.Outsider.id == outsider_id).first()
    assert outsider_row.is_deleted is True
    assert outsider_row.deleted_at is not None
    assert outsider_row.converted_to_user_id == new_user_id


def test_convert_migrates_quotation_assignment(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Ngozi Umeh", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Ngozi Umeh")
    # Return the dispatched item so it doesn't interfere with the assertions below.
    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    checkout_id = ledger.json()["assigned_items"][0]["checkout_id"]
    admin_client.post(f"/api/checkouts/{checkout_id}/return", headers=admin_headers, json={"quantity": 1})

    quote = admin_client.post(
        "/api/quotations", headers=admin_headers,
        json={"assignee_type": "outsider", "outsider_id": outsider_id},
    )
    assert quote.status_code == 200, quote.text
    quotation_id = quote.json()["id"]

    convert = admin_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=admin_headers,
        json={"email": "ngozi.umeh@corp.io", "password": "Convert123!", "role": "customer"},
    )
    assert convert.status_code == 200, convert.text
    assert convert.json()["quotations_migrated"] == 1
    new_user_id = convert.json()["user_id"]

    db_session.expire_all()
    quotation_row = db_session.query(models.Quotation).filter(models.Quotation.id == quotation_id).first()
    assert quotation_row.assigned_to_id == new_user_id
    assert quotation_row.assigned_outsider_id is None


# ---------------------------------------------------------------------------
# PERMISSION / VALIDATION GUARDS
# ---------------------------------------------------------------------------

def test_manager_cannot_convert_outsider_into_an_admin_account(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Bola Ojo", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Bola Ojo")

    response = manager_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=manager_headers,
        json={"email": "bola.ojo@corp.io", "password": "Convert123!", "role": "admin"},
    )
    assert response.status_code == 403, response.text

    # Nothing was mutated -- the outsider profile is untouched and still
    # holds its checkout.
    db_session.expire_all()
    outsider_row = db_session.query(models.Outsider).filter(models.Outsider.id == outsider_id).first()
    assert outsider_row.is_deleted is False
    assert outsider_row.converted_to_user_id is None


def test_staff_cannot_convert_outsiders(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Chika Eze", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Chika Eze")

    response = staff_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=staff_headers,
        json={"email": "chika.eze@corp.io", "password": "Convert123!", "role": "staff"},
    )
    assert response.status_code == 403, response.text


def test_convert_blocked_when_email_already_taken(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Tunde Bakare", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Tunde Bakare")

    # r.adeyemi@corp.io is the seeded demo admin's email (see conftest.py's DEMO_USERS).
    response = admin_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=admin_headers,
        json={"email": "r.adeyemi@corp.io", "password": "Convert123!", "role": "staff"},
    )
    assert response.status_code == 400, response.text
    assert "already exists" in response.json()["detail"]

    db_session.expire_all()
    outsider_row = db_session.query(models.Outsider).filter(models.Outsider.id == outsider_id).first()
    assert outsider_row.is_deleted is False


def test_converting_already_converted_outsider_404s(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Kemi Sowande", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Kemi Sowande")

    first = admin_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=admin_headers,
        json={"email": "kemi.sowande@corp.io", "password": "Convert123!", "role": "staff"},
    )
    assert first.status_code == 200, first.text

    second = admin_client.post(
        f"/api/outsiders/{outsider_id}/convert-to-user", headers=admin_headers,
        json={"email": "kemi.sowande2@corp.io", "password": "Convert123!", "role": "staff"},
    )
    assert second.status_code == 404, second.text
