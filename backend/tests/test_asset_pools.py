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


def test_staff_does_not_see_stock_counts_by_default(as_admin, as_staff):
    """
    CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER defaults to False -- a Staff
    session should get name/category/price for each pool but NOT
    total_quantity/available_quantity, on both the list endpoint and the
    per-pool details endpoint, since a Staff/Customer calling the API
    directly (not through the gated frontend) must not be able to see
    live stock levels either.
    """
    admin_client, admin_headers = as_admin
    create = admin_client.post("/api/assets", headers=admin_headers, json={"name": "Staff Hidden Stock Pool", "total_quantity": 7})
    asset_id = create.json()["id"]

    staff_client, staff_headers = as_staff
    listing = staff_client.get("/api/assets", headers=staff_headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["show_stock"] is False
    item = next(i for i in body["items"] if i["id"] == asset_id)
    assert "total_quantity" not in item
    assert "available_quantity" not in item
    assert item["name"] == "Staff Hidden Stock Pool"

    details = staff_client.get(f"/api/assets/{asset_id}/details", headers=staff_headers)
    assert details.status_code == 200
    dbody = details.json()
    assert "total_quantity" not in dbody
    assert "available_quantity" not in dbody
    assert "outbound_quantity" not in dbody
    assert "isolated_quantity" not in dbody
    # Custody data is a separate gate from stock -- also hidden from Staff.
    assert "active_assignments" not in dbody


def test_admin_still_sees_stock_and_custody(as_admin):
    """Full-admin roles are unaffected by the stock/custody gates above."""
    client, headers = as_admin
    create = client.post("/api/assets", headers=headers, json={"name": "Admin Visible Stock Pool", "total_quantity": 4})
    asset_id = create.json()["id"]

    listing = client.get("/api/assets", headers=headers)
    body = listing.json()
    assert body["show_stock"] is True
    item = next(i for i in body["items"] if i["id"] == asset_id)
    assert item["total_quantity"] == 4
    assert item["available_quantity"] == 4

    details = client.get(f"/api/assets/{asset_id}/details", headers=headers).json()
    assert details["total_quantity"] == 4
    assert details["available_quantity"] == 4
    assert "active_assignments" in details


def test_manager_sees_custody_but_staff_export_hides_stock_columns(as_manager, as_staff, as_admin):
    admin_client, admin_headers = as_admin
    create = admin_client.post("/api/assets", headers=admin_headers, json={"name": "Export Gate Pool", "total_quantity": 6})
    asset_id = create.json()["id"]

    manager_client, manager_headers = as_manager
    mdetails = manager_client.get(f"/api/assets/{asset_id}/details", headers=manager_headers).json()
    assert "active_assignments" in mdetails
    assert "total_quantity" in mdetails

    staff_client, staff_headers = as_staff
    export = staff_client.get("/api/assets/export?format=csv", headers=staff_headers)
    assert export.status_code == 200
    csv_text = export.content.decode()
    header_line = csv_text.splitlines()[0]
    assert "Available" not in header_line
    assert "Total" not in header_line
    assert "Status" not in header_line
    assert "Pool ID" in header_line


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


def test_deleted_pool_is_restorable(as_admin):
    """Soft-deleting a pool (DELETE /assets/{id}) removes it from the
    active Asset Inventory and from GET /assets, but it's not gone for
    good: it shows up in GET /assets/deleted and POST
    /assets/{id}/restore brings it back exactly as it was (same id,
    quantities, category, price) -- same "oops, wrong one" recovery
    contract as users' delete/restore."""
    client, headers = as_admin
    create = client.post(
        "/api/assets", headers=headers,
        json={"name": "Restorable Pool", "total_quantity": 8, "category": "Facilities", "price": 250.0},
    )
    assert create.status_code == 200, create.text
    asset_id = create.json()["id"]

    delete = client.delete(f"/api/assets/{asset_id}", headers=headers)
    assert delete.status_code == 200, delete.text

    # Gone from the active list and from its own details lookup.
    active = client.get("/api/assets", headers=headers).json()["items"]
    assert all(a["id"] != asset_id for a in active)
    assert client.get(f"/api/assets/{asset_id}/details", headers=headers).status_code == 404

    # Shows up in the deleted list.
    deleted = client.get("/api/assets/deleted", headers=headers).json()["items"]
    deleted_entry = next((a for a in deleted if a["id"] == asset_id), None)
    assert deleted_entry is not None
    assert deleted_entry["name"] == "Restorable Pool"
    assert deleted_entry["deleted_at"] is not None

    restore = client.post(f"/api/assets/{asset_id}/restore", headers=headers)
    assert restore.status_code == 200, restore.text

    # Back in the active list with everything intact.
    details = client.get(f"/api/assets/{asset_id}/details", headers=headers)
    assert details.status_code == 200
    body = details.json()
    assert body["name"] == "Restorable Pool"
    assert body["total_quantity"] == 8
    assert body["available_quantity"] == 8
    assert body["category"] == "Facilities"
    assert body["price"] == 250.0

    # No longer in the deleted list.
    deleted_after = client.get("/api/assets/deleted", headers=headers).json()["items"]
    assert all(a["id"] != asset_id for a in deleted_after)


def test_restore_requires_super_admin(as_admin, as_manager):
    admin_client, admin_headers = as_admin
    create = admin_client.post("/api/assets", headers=admin_headers, json={"name": "Manager Cannot Restore This", "total_quantity": 1})
    asset_id = create.json()["id"]
    admin_client.delete(f"/api/assets/{asset_id}", headers=admin_headers)

    manager_client, manager_headers = as_manager
    response = manager_client.post(f"/api/assets/{asset_id}/restore", headers=manager_headers)
    assert response.status_code == 403


def test_restore_nonexistent_deleted_asset_404s(as_admin):
    client, headers = as_admin
    response = client.post("/api/assets/999999/restore", headers=headers)
    assert response.status_code == 404
