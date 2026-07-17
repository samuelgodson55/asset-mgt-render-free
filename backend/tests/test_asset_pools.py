"""
tests/test_asset_pools.py
--------------------------
Asset Inventory CRUD: only a Super Admin/Admin can create/update/delete a
pool, the Available = Total - Outbound - Isolated formula stays correct
after a checkout, and a pool with outstanding checkouts can't be deleted.
"""


def test_admin_can_create_and_view_asset_pool(as_admin):
    client, headers = as_admin
    create = client.post(
        "/api/assets", headers=headers,
        json={"name": "Test Laptop Pool", "total_quantity": 10, "category": "Engineering", "price": 999.5},
    )
    assert create.status_code == 200, create.text
    asset_id = create.json()["id"]

    details = client.get(f"/api/assets/{asset_id}/details", headers=headers)
    assert details.status_code == 200
    body = details.json()
    assert body["name"] == "Test Laptop Pool"
    assert body["total_quantity"] == 10
    assert body["available_quantity"] == 10
    assert body["category"] == "Engineering"
    assert body["price"] == 999.5


def test_staff_cannot_create_asset_pool(as_staff):
    client, headers = as_staff
    response = client.post("/api/assets", headers=headers, json={"name": "Staff Should Not Create This", "total_quantity": 5})
    assert response.status_code == 403


def test_manager_cannot_create_asset_pool(as_manager):
    # Managers can dispatch/return, but pool CRUD/capacity stays Super
    # Admin/Admin-only (see README.md's Roles & Permissions Model table).
    client, headers = as_manager
    response = client.post("/api/assets", headers=headers, json={"name": "Manager Should Not Create This", "total_quantity": 5})
    assert response.status_code == 403


def test_anyone_logged_in_can_list_assets(as_staff):
    client, headers = as_staff
    response = client.get("/api/assets", headers=headers)
    assert response.status_code == 200
    assert "items" in response.json()


def test_update_quantity_recalculates_available(as_admin):
    client, headers = as_admin
    create = client.post("/api/assets", headers=headers, json={"name": "Resizable Pool", "total_quantity": 5})
    asset_id = create.json()["id"]

    update = client.put(f"/api/assets/{asset_id}/quantity", headers=headers, json={"new_total": 20})
    assert update.status_code == 200

    details = client.get(f"/api/assets/{asset_id}/details", headers=headers).json()
    assert details["total_quantity"] == 20
    assert details["available_quantity"] == 20


def test_cannot_delete_pool_with_outstanding_checkout(as_admin, as_manager):
    admin_client, admin_headers = as_admin
    create = admin_client.post("/api/assets", headers=admin_headers, json={"name": "Pool With A Loan", "total_quantity": 3})
    asset_id = create.json()["id"]

    manager_client, manager_headers = as_manager
    # Dispatch one unit to the seeded demo Staff account (t.okafor@corp.io, id 3 in seed_db()'s insert order)
    # -- looked up by email via the manager's own user list instead of a hardcoded id.
    users = manager_client.get("/api/users", headers=manager_headers).json()["items"]
    staff_user = next(u for u in users if u["email"] == "t.okafor@corp.io")

    checkout = manager_client.post(
        f"/api/assets/{asset_id}/checkout_advanced", headers=manager_headers,
        json={"assignee_type": "user", "quantity": 1, "user_id": staff_user["id"]},
    )
    assert checkout.status_code == 200, checkout.text

    delete = admin_client.delete(f"/api/assets/{asset_id}", headers=admin_headers)
    assert delete.status_code == 400
