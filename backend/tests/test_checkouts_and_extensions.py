"""
tests/test_checkouts_and_extensions.py
-----------------------------------------
Dispatch -> partial/full return, and the due-date extension request
lifecycle (self-service request -> Manager decision, plus the Manager
"direct extend" shortcut) -- see services/checkout_service.py and
services/extension_service.py.
"""

import datetime

import models


def _create_pool(admin_client, admin_headers, name="Checkout Test Pool", total_quantity=5):
    response = admin_client.post("/api/assets", headers=admin_headers, json={"name": name, "total_quantity": total_quantity})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=2, due_in_days=14):
    users = manager_client.get("/api/users", headers=manager_headers).json()["items"]
    staff_user = next(u for u in users if u["email"] == "t.okafor@corp.io")
    due_date = (datetime.date.today() + datetime.timedelta(days=due_in_days)).isoformat()
    response = manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "user", "quantity": quantity, "user_id": staff_user["id"], "due_date": due_date},
    )
    assert response.status_code == 200, response.text
    return staff_user


def test_dispatch_reduces_available_and_return_restores_it(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, total_quantity=5)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=2)

    details = admin_client.get(f"/api/assets/{asset_id}/details", headers=admin_headers).json()
    assert details["available_quantity"] == 3
    assert details["outbound_quantity"] == 2

    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    # Partial return: 1 of the 2 outstanding units.
    partial = manager_client.post(f"/api/checkouts/{checkout_row.id}/return", headers=manager_headers, json={"quantity": 1})
    assert partial.status_code == 200

    details_after_partial = admin_client.get(f"/api/assets/{asset_id}/details", headers=admin_headers).json()
    assert details_after_partial["available_quantity"] == 4
    assert details_after_partial["outbound_quantity"] == 1

    # Final return: the last outstanding unit -> checkout fully settled.
    final = manager_client.post(f"/api/checkouts/{checkout_row.id}/return", headers=manager_headers, json={"quantity": 1})
    assert final.status_code == 200

    details_after_full = admin_client.get(f"/api/assets/{asset_id}/details", headers=admin_headers).json()
    assert details_after_full["available_quantity"] == 5
    assert details_after_full["outbound_quantity"] == 0


def test_cannot_checkout_more_than_available(as_admin, as_manager):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    asset_id = _create_pool(admin_client, admin_headers, total_quantity=1)

    users = manager_client.get("/api/users", headers=manager_headers).json()["items"]
    staff_user = next(u for u in users if u["email"] == "t.okafor@corp.io")

    response = manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "user", "quantity": 5, "user_id": staff_user["id"]},
    )
    assert response.status_code == 400


def test_staff_can_request_extension_on_own_checkout_only(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff

    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=7)
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    new_due_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    request = staff_client.post(
        f"/api/checkouts/{checkout_row.id}/extension-requests", headers=staff_headers,
        json={"new_due_date": new_due_date, "reason": "Still need it for the project."},
    )
    assert request.status_code == 200, request.text
    assert request.json()["status"] == "pending"


def test_staff_cannot_request_extension_on_someone_elses_checkout(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff  # t.okafor@corp.io

    asset_id = _create_pool(admin_client, admin_headers)
    # Dispatch to the OTHER seeded staff account (a.bello@corp.io), not the
    # one logged in as `as_staff` above.
    users = manager_client.get("/api/users", headers=manager_headers).json()["items"]
    other_staff = next(u for u in users if u["email"] == "a.bello@corp.io")
    manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "user", "quantity": 1, "user_id": other_staff["id"]},
    )
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    new_due_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    response = staff_client.post(
        f"/api/checkouts/{checkout_row.id}/extension-requests", headers=staff_headers,
        json={"new_due_date": new_due_date},
    )
    assert response.status_code == 403


def test_manager_can_approve_extension_request_and_due_date_updates(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff

    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=7)
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    new_due_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    request = staff_client.post(
        f"/api/checkouts/{checkout_row.id}/extension-requests", headers=staff_headers,
        json={"new_due_date": new_due_date},
    ).json()

    decision = manager_client.post(
        f"/api/checkouts/extension-requests/{request['id']}/decision", headers=manager_headers,
        json={"approve": True, "note": "Approved for one more sprint."},
    )
    assert decision.status_code == 200

    db_session.expire_all()
    refreshed = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == checkout_row.id).one()
    assert refreshed.due_date.date().isoformat() == new_due_date


def test_manager_can_deny_extension_request_and_due_date_unchanged(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff

    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=7)
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()
    original_due_date = checkout_row.due_date

    new_due_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    request = staff_client.post(
        f"/api/checkouts/{checkout_row.id}/extension-requests", headers=staff_headers,
        json={"new_due_date": new_due_date},
    ).json()

    decision = manager_client.post(
        f"/api/checkouts/extension-requests/{request['id']}/decision", headers=manager_headers,
        json={"approve": False, "note": "Pool needed back sooner."},
    )
    assert decision.status_code == 200

    db_session.expire_all()
    refreshed = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == checkout_row.id).one()
    assert refreshed.due_date == original_due_date


def test_manager_can_extend_checkout_directly_without_a_request(as_admin, as_manager, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=7)
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    new_due_date = (datetime.date.today() + datetime.timedelta(days=45)).isoformat()
    response = manager_client.post(
        f"/api/checkouts/{checkout_row.id}/extend", headers=manager_headers,
        json={"new_due_date": new_due_date, "reason": "Extending on a phone call."},
    )
    assert response.status_code == 200

    db_session.expire_all()
    refreshed = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == checkout_row.id).one()
    assert refreshed.due_date.date().isoformat() == new_due_date


def test_staff_cannot_decide_extension_requests(as_admin, as_manager, as_staff, db_session):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff

    asset_id = _create_pool(admin_client, admin_headers)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=7)
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.asset_id == asset_id).one()

    new_due_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    request = staff_client.post(
        f"/api/checkouts/{checkout_row.id}/extension-requests", headers=staff_headers,
        json={"new_due_date": new_due_date},
    ).json()

    response = staff_client.post(
        f"/api/checkouts/extension-requests/{request['id']}/decision", headers=staff_headers,
        json={"approve": True},
    )
    assert response.status_code == 403
