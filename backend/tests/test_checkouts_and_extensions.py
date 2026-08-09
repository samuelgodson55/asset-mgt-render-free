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


def test_activity_scoped_to_own_checkouts_for_staff_and_customer(as_admin, as_manager, as_staff, as_customer):
    """
    GET /assets/activity feeds the Dashboard's "Checkout activity" chart.
    A Manager/Admin sees the whole org's checkouts; a Staff/Customer -- not
    privileged to see who-has-what org-wide (see asset_service._can_see_
    custody()) -- must only see counts for checkouts made against their OWN
    account, the same way "My Items" already scopes to them elsewhere.
    """
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    staff_client, staff_headers = as_staff  # t.okafor@corp.io
    customer_client, customer_headers = as_customer  # d.martins@customer.io

    # The seeded demo DB (see database.py's seed_db()) already ships one
    # checkout to t.okafor@corp.io and one to d.martins@customer.io, both
    # dated "today" -- so both baselines are captured up front rather than
    # assumed to be zero, keeping this test correct regardless of what the
    # seed data happens to contain.
    org_wide_before = sum(day["checkouts"] for day in manager_client.get("/api/assets/activity", headers=manager_headers).json())
    staff_before = sum(day["checkouts"] for day in staff_client.get("/api/assets/activity", headers=staff_headers).json())
    customer_before = sum(day["checkouts"] for day in customer_client.get("/api/assets/activity", headers=customer_headers).json())

    asset_id = _create_pool(admin_client, admin_headers, total_quantity=5)
    # One NEW checkout to the logged-in staff account, one to a DIFFERENT
    # user -- so the org-wide total moves by 2 while the staff account's
    # own total moves by only 1.
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=7)
    users = manager_client.get("/api/users", headers=manager_headers).json()["items"]
    other_staff = next(u for u in users if u["email"] == "a.bello@corp.io")
    manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "user", "quantity": 1, "user_id": other_staff["id"]},
    )

    manager_activity = manager_client.get("/api/assets/activity", headers=manager_headers).json()
    assert sum(day["checkouts"] for day in manager_activity) == org_wide_before + 2

    staff_activity = staff_client.get("/api/assets/activity", headers=staff_headers).json()
    assert sum(day["checkouts"] for day in staff_activity) == staff_before + 1

    # The Customer account got no NEW checkout at all this test -- its
    # count must stay exactly at its own baseline, not shift toward the
    # org-wide total that just grew by 2.
    customer_activity = customer_client.get("/api/assets/activity", headers=customer_headers).json()
    assert sum(day["checkouts"] for day in customer_activity) == customer_before


def test_checkouts_list_includes_items_not_overdue_or_due_soon(as_admin, as_manager):
    """
    GET /checkouts (the Checkouts page's "All" tab) must show every active
    checkout, not just the ones that happen to be overdue or landing
    within the due-soon reminder window (settings.DUE_SOON_REMINDER_DAYS,
    2 days by default). A checkout dispatched with a due date weeks out is
    neither overdue nor due-soon, so relying on those two feeds alone (the
    bug this test guards against) would leave it invisible on the page
    even though it's real, outstanding custody -- exactly what a fresh
    dispatch with a comfortably-far-off due date looks like.
    """
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, total_quantity=5)
    # 30 days out is well past any due-soon window and nowhere near overdue.
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=30)

    overdue = manager_client.get("/api/checkouts/overdue", headers=manager_headers).json()
    due_soon = manager_client.get("/api/checkouts/due-soon", headers=manager_headers).json()
    assert not any(item["asset_id"] == asset_id for item in overdue["items"])
    assert not any(item["asset_id"] == asset_id for item in due_soon["items"])

    all_checkouts = manager_client.get("/api/checkouts", headers=manager_headers).json()
    matching = [item for item in all_checkouts["items"] if item["asset_id"] == asset_id]
    assert len(matching) == 1
    assert matching[0]["is_overdue"] is False
    assert matching[0]["outstanding"] == 1


def test_staff_cannot_reach_full_checkouts_list(as_staff):
    client, headers = as_staff
    response = client.get("/api/checkouts", headers=headers)
    assert response.status_code == 403


def test_checkouts_list_due_soon_column_obeys_env_setting(as_admin, as_manager, monkeypatch):
    """
    GET /checkouts' `is_due_soon` flag (and the `due_soon_reminder_days` it
    echoes back) must be driven by the SAME settings.DUE_SOON_REMINDER_DAYS
    value (.env) the Dashboard's own /checkouts/due-soon alert feed already
    uses -- not a hardcoded number -- so the Checkouts page's "Due soon"
    column always agrees with whatever the deployment has configured.
    """
    import services.checkout_service as checkout_service

    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager

    # Narrow the window to 5 days so a checkout due in 3 days lands inside
    # it and one due in 10 days does not -- proves the flag actually reads
    # the setting live rather than a baked-in constant.
    monkeypatch.setattr(checkout_service.settings, "DUE_SOON_REMINDER_DAYS", 5)

    asset_id = _create_pool(admin_client, admin_headers, total_quantity=5)
    _dispatch_to_staff(manager_client, manager_headers, asset_id, quantity=1, due_in_days=3)

    other_asset_id = _create_pool(admin_client, admin_headers, name="Far-Out Pool", total_quantity=5)
    _dispatch_to_staff(manager_client, manager_headers, other_asset_id, quantity=1, due_in_days=10)

    response = manager_client.get("/api/checkouts", headers=manager_headers).json()
    assert response["due_soon_reminder_days"] == 5

    soon_item = next(item for item in response["items"] if item["asset_id"] == asset_id)
    far_item = next(item for item in response["items"] if item["asset_id"] == other_asset_id)
    assert soon_item["is_due_soon"] is True
    assert soon_item["is_overdue"] is False
    assert far_item["is_due_soon"] is False
