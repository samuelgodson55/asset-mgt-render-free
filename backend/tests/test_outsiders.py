"""
tests/test_outsiders.py
--------------------------
Ad-Hoc (Unlinked) Individuals: soft delete (services/outsider_service.py
-> delete_outsider()) plus the "dispatch/quote-assign to an EXISTING
ad-hoc profile" routes added to the Issue/Dispatch drawer and Quote
creation/assignment (services/asset_service.py's checkout_advanced(),
services/quotation_service.py's admin_create_quotation()/
assign_quotation()).
"""

import datetime

import models

TODAY = datetime.date.today()
DUE_DATE = (TODAY + datetime.timedelta(days=14)).isoformat()


def _create_pool(client, headers, name="Outsider Test Pool", total_quantity=5):
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
# DELETE (soft delete)
# ---------------------------------------------------------------------------

def test_manager_can_delete_outsider_with_no_outstanding_items(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Bola Ojo")
    outsider_id = _get_outsider_id_by_name(db_session, "Bola Ojo")

    # Return the item first -- delete_outsider() blocks while anything is
    # still checked out (see the next test).
    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    assert ledger.status_code == 200, ledger.text
    checkout_id = ledger.json()["assigned_items"][0]["checkout_id"]
    ret = admin_client.post(f"/api/checkouts/{checkout_id}/return", headers=admin_headers, json={"quantity": 1})
    assert ret.status_code == 200, ret.text

    db_session.expire_all()
    response = manager_client.delete(f"/api/outsiders/{outsider_id}", headers=manager_headers)
    assert response.status_code == 200, response.text

    # Deleted profile no longer appears in the directory...
    directory = admin_client.get("/api/outsiders", headers=admin_headers)
    assert outsider_id not in [o["id"] for o in directory.json()["items"]]

    # ...but the row itself (and its now-returned checkout history) is
    # still intact, not hard-deleted.
    db_session.expire_all()
    row = db_session.query(models.Outsider).filter(models.Outsider.id == outsider_id).first()
    assert row is not None
    assert row.is_deleted is True
    assert row.deleted_at is not None


def test_delete_blocked_while_items_in_active_custody(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Ngozi Umeh")
    outsider_id = _get_outsider_id_by_name(db_session, "Ngozi Umeh")

    response = manager_client.delete(f"/api/outsiders/{outsider_id}", headers=manager_headers)
    assert response.status_code == 400, response.text
    assert "active custody" in response.json()["detail"]

    # Still present in the directory -- the delete never went through.
    directory = admin_client.get("/api/outsiders", headers=admin_headers)
    assert outsider_id in [o["id"] for o in directory.json()["items"]]


def test_staff_cannot_delete_outsiders(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Chika Eze", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Chika Eze")

    response = staff_client.delete(f"/api/outsiders/{outsider_id}", headers=staff_headers)
    assert response.status_code == 403, response.text


def test_deleting_already_deleted_outsider_404s(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Tunde Bakare", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Tunde Bakare")

    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    checkout_id = ledger.json()["assigned_items"][0]["checkout_id"]
    admin_client.post(f"/api/checkouts/{checkout_id}/return", headers=admin_headers, json={"quantity": 1})

    first = manager_client.delete(f"/api/outsiders/{outsider_id}", headers=manager_headers)
    assert first.status_code == 200, first.text

    second = manager_client.delete(f"/api/outsiders/{outsider_id}", headers=manager_headers)
    assert second.status_code == 404, second.text


# ---------------------------------------------------------------------------
# DISPATCH TO AN EXISTING AD-HOC PROFILE (Issue/Dispatch drawer)
# ---------------------------------------------------------------------------

def test_dispatch_to_existing_outsider_reuses_the_same_profile(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers, name="Pool A", total_quantity=5)
    second_asset_id = _create_pool(admin_client, admin_headers, name="Pool B", total_quantity=5)

    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Ada Nwosu", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Ada Nwosu")

    response = manager_client.post(
        f"/api/assets/{second_asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "outsider", "quantity": 1, "due_date": DUE_DATE, "outsider_id": outsider_id},
    )
    assert response.status_code == 200, response.text

    # Still exactly one Outsider row named "Ada Nwosu" -- the second
    # dispatch reused it instead of creating a duplicate.
    db_session.expire_all()
    matches = db_session.query(models.Outsider).filter(models.Outsider.name == "Ada Nwosu").all()
    assert len(matches) == 1

    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    assert ledger.status_code == 200
    assert len(ledger.json()["assigned_items"]) == 2


def test_dispatch_to_nonexistent_outsider_id_404s(as_admin, as_manager):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)

    response = manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "outsider", "quantity": 1, "due_date": DUE_DATE, "outsider_id": 999999},
    )
    assert response.status_code == 404, response.text


def test_dispatch_to_deleted_outsider_id_404s(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Yemi Alade", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Yemi Alade")

    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    checkout_id = ledger.json()["assigned_items"][0]["checkout_id"]
    admin_client.post(f"/api/checkouts/{checkout_id}/return", headers=admin_headers, json={"quantity": 1})
    manager_client.delete(f"/api/outsiders/{outsider_id}", headers=manager_headers)

    response = manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "outsider", "quantity": 1, "due_date": DUE_DATE, "outsider_id": outsider_id},
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# QUOTE CREATION / ASSIGNMENT TO AN EXISTING AD-HOC PROFILE
# ---------------------------------------------------------------------------

def test_create_quote_assigned_to_existing_outsider(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Kunle Afolayan", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Kunle Afolayan")

    response = manager_client.post(
        "/api/quotations", headers=manager_headers,
        json={"assignee_type": "outsider", "outsider_id": outsider_id},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["assigned_outsider"]["name"] == "Kunle Afolayan"

    db_session.expire_all()
    matches = db_session.query(models.Outsider).filter(models.Outsider.name == "Kunle Afolayan").all()
    assert len(matches) == 1


def test_assign_quote_to_existing_outsider(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Ijeoma Chukwu", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Ijeoma Chukwu")

    create = manager_client.post("/api/quotations", headers=manager_headers, json={})
    assert create.status_code == 200, create.text
    quote_id = create.json()["id"]

    response = manager_client.post(
        f"/api/quotations/{quote_id}/assign", headers=manager_headers,
        json={"assignee_type": "outsider", "outsider_id": outsider_id},
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    matches = db_session.query(models.Outsider).filter(models.Outsider.name == "Ijeoma Chukwu").all()
    assert len(matches) == 1


def test_assign_quote_to_deleted_outsider_id_404s(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_new_outsider(manager_client, manager_headers, asset_id, name="Segun Arinze", quantity=1)
    outsider_id = _get_outsider_id_by_name(db_session, "Segun Arinze")

    ledger = admin_client.get(f"/api/outsiders/{outsider_id}/items", headers=admin_headers)
    checkout_id = ledger.json()["assigned_items"][0]["checkout_id"]
    admin_client.post(f"/api/checkouts/{checkout_id}/return", headers=admin_headers, json={"quantity": 1})
    manager_client.delete(f"/api/outsiders/{outsider_id}", headers=manager_headers)

    create = manager_client.post("/api/quotations", headers=manager_headers, json={})
    quote_id = create.json()["id"]

    response = manager_client.post(
        f"/api/quotations/{quote_id}/assign", headers=manager_headers,
        json={"assignee_type": "outsider", "outsider_id": outsider_id},
    )
    assert response.status_code == 404, response.text
