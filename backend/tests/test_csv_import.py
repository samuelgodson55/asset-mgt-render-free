"""
tests/test_csv_import.py
--------------------------
POST /api/assets/import -- the CSV batch import endpoint and its
Post-Import Summary contract (services/asset_service.py's
import_assets_from_csv()): a mixed file of valid/invalid rows must save
every valid row, report every rejected row with a row number + reason
(never silently drop one), and never let one bad row take the whole
batch down.

DUPLICATE-ROW COVERAGE: this file also pins down the "same asset name
listed twice in one file" case (e.g. "Duplicate Item" entered twice).
That is treated as a data-entry mistake, not two genuinely separate
deliveries: the FIRST occurrence of the name imports normally, and every
LATER occurrence of that same name in the same file is rejected as an
error row -- `Duplicate item "<name>" already exists in this import file
(first seen on row <N>).` -- instead of silently adding the quantities
together. See import_assets_from_csv()'s `seen_names_this_file` comment
for the full explanation.

EXISTING-POOL COVERAGE: a name that matches a pool that already existed
in the database BEFORE this import started (from a previous import or
the UI) gets the same treatment -- flagged as an error (`Item "<name>"
already exists in the system (Pool ID <id>). ...`) instead of silently
added to that pool's total_quantity. Restocking an existing pool has its
own explicit "Update Quantity" action; a CSV import is for registering
NEW pools only. See test_reimporting_existing_pool_is_rejected_not_merged
below.
"""

import io
import os

import models

SAMPLE_CSV_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_import_mixed_valid_invalid.csv")


def _upload(client, headers, csv_text: str, filename: str = "import.csv"):
    files = {"file": (filename, io.BytesIO(csv_text.encode("utf-8")), "text/csv")}
    return client.post("/api/assets/import", headers=headers, files=files)


def test_only_super_admin_can_import(as_manager):
    """Import is gated to Super Admin (see require_super_admin on the
    route) -- a Manager, who can approve/checkout Quotations, still can't
    bulk-mutate the asset catalog this way."""
    manager_client, manager_headers = as_manager
    response = _upload(manager_client, manager_headers, "name,total_quantity\nWidget,5\n")
    assert response.status_code == 403


def test_mixed_valid_and_invalid_rows_partial_success(as_super_admin):
    """The exact file from the bug report: 13 data rows, a mix of valid
    rows, every documented validation failure (missing name, missing/
    negative/non-numeric quantity, negative/non-numeric/too-large price),
    and an in-file duplicate of a brand-new name. Must come back 200 with
    every valid row saved, every invalid row reported, and the in-file
    duplicate rejected as its own error -- not a 500."""
    client, headers = as_super_admin
    with open(SAMPLE_CSV_PATH, "rb") as f:
        csv_bytes = f.read()
    files = {"file": ("sample_import_mixed_valid_invalid.csv", io.BytesIO(csv_bytes), "text/csv")}
    response = client.post("/api/assets/import", headers=headers, files=files)

    assert response.status_code == 200, response.text
    body = response.json()

    # 13 data rows total: 5 valid (HP ProBook, Office Chair, SSD 1TB, Desk,
    # and the FIRST "Duplicate Item" row), 8 rejected (the 7 validation
    # failures plus the SECOND "Duplicate Item" row, which is now rejected
    # as an in-file duplicate instead of merged).
    assert body["imported_count"] == 5
    assert body["error_count"] == 8
    assert len(body["errors"]) == 8

    # Every rejected row is reported with its 1-based row number (header =
    # row 1) and a human-readable reason -- never silently dropped.
    errors_by_row = {e["row"]: e for e in body["errors"]}
    assert errors_by_row[4]["reason"] == "Missing asset name."
    assert "not a whole number" in errors_by_row[5]["reason"]  # Cisco Switch, blank qty
    assert "cannot be negative" in errors_by_row[6]["reason"]  # Canon Printer, qty -2
    assert "not a whole number" in errors_by_row[7]["reason"]  # APC UPS, qty "ten"
    assert "cannot be negative" in errors_by_row[9]["reason"]  # Keyboard, price -5000
    assert "valid number" in errors_by_row[10]["reason"]  # Mouse, price "abc"
    assert "exceeds the supported maximum" in errors_by_row[12]["reason"]  # Laptop, price too large
    # Second "Duplicate Item" row (row 14) rejected, pointing back at the
    # first occurrence (row 13).
    assert errors_by_row[14]["reason"] == (
        'Duplicate item "Duplicate Item" already exists in this import file (first seen on row 13).'
    )

    # Rejected rows never touched the database.
    for missing_name in ("Cisco Switch", "Canon Printer", "APC UPS", "Keyboard", "Mouse", "Laptop"):
        assert client.get(
            "/api/assets", headers=headers, params={"search": missing_name},
        ).json()["items"] == [] or all(
            i["name"] != missing_name
            for i in client.get("/api/assets", headers=headers, params={"search": missing_name}).json()["items"]
        )


def test_duplicate_new_name_in_same_file_is_rejected_not_merged(as_super_admin, db_session):
    """TWO rows introducing the SAME brand-new asset name in one file:
    the first row imports normally, and the second is rejected as a
    duplicate row (not merged into the first, and must NOT 500 the whole
    import)."""
    client, headers = as_super_admin
    csv_text = (
        "name,total_quantity,category,price\n"
        "Brand New Widget,4,Misc,1000\n"
        "Brand New Widget,7,Misc,2000\n"
    )
    response = _upload(client, headers, csv_text)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_count"] == 1
    assert body["error_count"] == 1
    assert body["errors"][0]["row"] == 3  # header=1, second "Brand New Widget" row is row 3
    assert body["errors"][0]["reason"] == (
        'Duplicate item "Brand New Widget" already exists in this import file (first seen on row 2).'
    )

    rows = db_session.query(models.AssetType).filter(models.AssetType.name == "Brand New Widget").all()
    assert len(rows) == 1, "only the first occurrence should have been created"
    assert rows[0].total_quantity == 4, "the second (rejected) row's quantity must not be added in"
    assert rows[0].available_quantity == 4


def test_valid_rows_are_saved_even_when_other_rows_fail(as_super_admin, db_session):
    """A single bad row must never take down the good rows around it --
    this is the "1,242 of 1,250 saved" partial-success contract."""
    client, headers = as_super_admin
    csv_text = (
        "name,total_quantity,price\n"
        "Good Row One,5,10.00\n"
        "Bad Row,notanumber,5.00\n"
        "Good Row Two,3,20.00\n"
    )
    response = _upload(client, headers, csv_text)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_count"] == 2
    assert body["error_count"] == 1
    assert body["errors"][0]["row"] == 3  # header=1, "Bad Row" is the 2nd data row

    saved_names = {a.name for a in db_session.query(models.AssetType).all()}
    assert "Good Row One" in saved_names
    assert "Good Row Two" in saved_names
    assert "Bad Row" not in saved_names


def test_reimporting_existing_pool_is_rejected_not_merged(as_super_admin, db_session):
    """Re-importing a name that already exists in the system is now
    rejected as an error row -- not merged into the existing pool's
    total_quantity -- with a clean message pointing at the existing pool
    so a Super Admin can go update its quantity directly instead."""
    client, headers = as_super_admin
    create = client.post(
        "/api/assets", headers=headers,
        json={"name": "Restock Pool", "total_quantity": 10, "category": "Engineering", "price": 500.0},
    )
    assert create.status_code == 200, create.text
    existing_id = create.json()["id"]

    csv_text = "name,total_quantity,category,price\nRestock Pool,5,,\n"
    response = _upload(client, headers, csv_text)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_count"] == 0
    assert body["error_count"] == 1
    assert body["errors"][0]["row"] == 2
    assert body["errors"][0]["reason"] == (
        f'Item "Restock Pool" already exists in the system (Pool ID {existing_id}). '
        f'Update its quantity directly from the Asset Inventory table instead of re-importing it.'
    )

    # The existing pool's quantity/category/price are all untouched by the
    # rejected row.
    db_session.expire_all()
    pool = db_session.query(models.AssetType).filter(models.AssetType.name == "Restock Pool").one()
    assert pool.total_quantity == 10
    assert pool.available_quantity == 10
    assert pool.category == "Engineering"
    assert float(pool.price) == 500.0


def test_missing_required_columns_rejected_before_any_row_processing(as_super_admin):
    client, headers = as_super_admin
    response = _upload(client, headers, "name,quantity\nWidget,5\n")  # wrong header name
    assert response.status_code == 400
    assert "name" in response.json()["detail"] and "total_quantity" in response.json()["detail"]


def test_exported_inventory_csv_can_update_existing_pool(as_super_admin, db_session):
    """The Asset Inventory export is intentionally round-trippable: Pool ID
    identifies the exact pool, editable columns update it, and Available/
    Status are ignored because stock is derived from live records."""
    client, headers = as_super_admin
    create = client.post(
        "/api/assets", headers=headers,
        json={
            "name": "Production Camera Pool",
            "total_quantity": 10,
            "category": "Cameras",
            "department": None,
            "price": 1200.0,
        },
    )
    assert create.status_code == 200, create.text
    pool_id = create.json()["id"]

    csv_text = (
        "Pool ID,Asset Name,Category,Department,Price,Available,Total,Status\n"
        f'{pool_id},Production Camera Pool,Cameras,Production,"₦1,350.00",999,10,In Stock\n'
    )
    response = _upload(client, headers, csv_text)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_count"] == 1
    assert body["created_count"] == 0
    assert body["error_count"] == 0

    db_session.expire_all()
    pool = db_session.query(models.AssetType).filter(models.AssetType.id == pool_id).one()
    assert pool.department == "Production"
    assert pool.category == "Cameras"
    assert float(pool.price) == 1350.0
    assert pool.total_quantity == 10
    assert pool.available_quantity == 10


def test_exported_inventory_blank_optional_fields_preserve_values_and_clear_is_explicit(as_super_admin, db_session):
    client, headers = as_super_admin
    create = client.post(
        "/api/assets", headers=headers,
        json={
            "name": "Lighting Pool",
            "total_quantity": 5,
            "category": "Lighting",
            "department": "Studio",
            "price": 500.0,
        },
    )
    pool_id = create.json()["id"]

    preserve_csv = (
        "Pool ID,Asset Name,Category,Department,Price,Available,Total,Status\n"
        f"{pool_id},Lighting Pool,—,—,—,5,5,In Stock\n"
    )
    response = _upload(client, headers, preserve_csv)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    pool = db_session.query(models.AssetType).filter(models.AssetType.id == pool_id).one()
    assert pool.category == "Lighting"
    assert pool.department == "Studio"
    assert float(pool.price) == 500.0

    clear_csv = (
        "Pool ID,Asset Name,Category,Department,Price,Available,Total,Status\n"
        f"{pool_id},Lighting Pool,__CLEAR__,__CLEAR__,__CLEAR__,5,5,In Stock\n"
    )
    response = _upload(client, headers, clear_csv)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    pool = db_session.query(models.AssetType).filter(models.AssetType.id == pool_id).one()
    assert pool.category is None
    assert pool.department is None
    assert pool.price is None


def test_exported_inventory_quantity_update_respects_allocated_stock(as_super_admin, db_session):
    client, headers = as_super_admin
    create = client.post(
        "/api/assets", headers=headers,
        json={"name": "Allocated Pool", "total_quantity": 5},
    )
    pool_id = create.json()["id"]

    # Simulate an allocated unit directly so this test focuses on the import
    # guard rather than the checkout workflow.
    checkout = models.AssetCheckout(
        asset_id=pool_id,
        quantity=2,
        quantity_returned=0,
        status="active",
        checkout_date=models.utc_now(),
    )
    db_session.add(checkout)
    db_session.commit()

    csv_text = (
        "Pool ID,Asset Name,Category,Department,Price,Available,Total,Status\n"
        f"{pool_id},Allocated Pool,—,—,—,3,1,Low\n"
    )
    response = _upload(client, headers, csv_text)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_count"] == 0
    assert body["error_count"] == 1
    assert "Cannot reduce total below 2" in body["errors"][0]["reason"]

    db_session.expire_all()
    pool = db_session.query(models.AssetType).filter(models.AssetType.id == pool_id).one()
    assert pool.total_quantity == 5
