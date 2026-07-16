"""
services/asset_service.py
--------------------------
All business logic for AssetType pools: create/list/detail/quantity/delete,
maintenance exceptions (isolate/recall), reconciliation check-ins, the
advanced checkout flow, and CSV batch import. Used by api/assets.py.
"""

import csv
import io
import datetime
from typing import Optional
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func

import models
from services.search_utils import apply_search_filter
from models import utc_now
from schemas.assets import AssetTypeCreate, ExceptionCreate, AdvancedCheckoutRequest, QuantityUpdateRequest, NameUpdateRequest, CategoryUpdateRequest, PriceUpdateRequest
from services.stock import recalculate_asset_stock
import services.export_service as export_service

# Same reasoning as user_service.DEFAULT_LIMIT/MAX_LIMIT -- bounds how many
# asset pools a single request can return (Data Quality & Usability
# requirement #4).
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000

# SECURITY: caps how large a CSV import upload can be, in bytes (5 MiB).
# Without a cap, `POST /assets/import` would read an attacker-supplied file
# of ANY size fully into memory (see `file.file.read()` below) before doing
# any validation at all -- a trivial denial-of-service vector (upload a
# multi-gigabyte file repeatedly to exhaust server memory). 5 MiB is
# generous for a plain-text CSV of asset names/quantities (tens of
# thousands of rows) while still bounding worst-case memory use per request.
MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024


def create_asset_type(db: Session, asset: AssetTypeCreate, user: dict) -> dict:
    # Only Super Admins may create brand new stock pools.
    existing = db.query(models.AssetType).filter(models.AssetType.name == asset.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Asset type name already exists")

    new_asset_type = models.AssetType(
        name=asset.name,
        total_quantity=asset.total_quantity,
        available_quantity=asset.total_quantity,  # no checkouts/isolations yet, so Available == Total
        custom_fields=asset.custom_fields,
        category=asset.category,
        price=asset.price,
    )
    db.add(new_asset_type)
    db.commit()
    db.refresh(new_asset_type)

    dept_log_text = f" (Category: {asset.category})" if asset.category else ""
    price_log_text = f" (Price: {export_service.format_money(asset.price)})" if asset.price is not None else ""
    db.add(models.AuditLog(
        operator=user["email"], action="POOL_CREATED", target_type="AssetType", target_id=new_asset_type.id,
        details=f"Created asset category '{asset.name}' with initial quantity of {asset.total_quantity}{dept_log_text}{price_log_text}",
    ))
    db.commit()
    return {"message": "Asset type created successfully", "id": new_asset_type.id}


def list_assets(db: Session, limit: int = DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None) -> dict:
    """
    Any authenticated user (admin, manager, or staff) can view the pool
    list. Soft-deleted pools are excluded -- they're gone from active
    inventory even though the row is kept for historical checkouts.

    PAGINATION + SEARCH (Data Quality & Usability requirement #4, extended
    to true server-side search): `limit`/`offset` cap how many pools a
    single request can return; `total` tells the caller the true size of
    the (optionally search-narrowed) inventory regardless of page size.
    `search` -- when present -- narrows the result to pools whose name
    contains it (case-insensitive), matching the single field the Asset
    Inventory table's search box has always searched by (see
    js/components/assets.js). Applied and counted BEFORE the offset/limit
    slice, so `total`/pagination always reflect the filtered set, not the
    whole table.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    query = db.query(models.AssetType).filter(models.AssetType.is_deleted == False)
    query = apply_search_filter(query, search, [models.AssetType.name])
    query = query.order_by(models.AssetType.id)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_asset_details(db: Session, asset_id: int) -> dict:
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset category not found")

    # Only currently-isolated (not-yet-recalled) exceptions count toward the
    # "Isolated" bucket of the Available formula and show up here with a
    # "Recall" action. Recalled units are historical and no longer isolated.
    repairs = db.query(models.AssetException).filter(
        models.AssetException.asset_type_id == asset_id,
        models.AssetException.status_label.ilike("%repair%"),
        models.AssetException.isolation_status == "isolated",
    ).all()
    stolen = db.query(models.AssetException).filter(
        models.AssetException.asset_type_id == asset_id,
        models.AssetException.status_label.ilike("%stolen%"),
        models.AssetException.isolation_status == "isolated",
    ).all()
    active_checkouts = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.asset_id == asset_id, models.AssetCheckout.status == "active"
    ).all()

    checkout_list = []
    for c in active_checkouts:
        assignee_name, assignee_type = "Unknown", "Outsider"
        if c.user:
            assignee_name, assignee_type = c.user.name, c.user.role.capitalize()
        elif c.outsider:
            assignee_name, assignee_type = f"{c.outsider.name} ({c.outsider.company or 'No Company'})", "External Outsider"

        outstanding = c.quantity - c.quantity_returned
        checkout_list.append({
            "checkout_id": c.id, "assignee_name": assignee_name, "assignee_type": assignee_type,
            "quantity": c.quantity, "quantity_returned": c.quantity_returned, "outstanding": outstanding,
            # TIMEZONE FIX -- see services/user_service.py's
            # get_my_assigned_items() for the full explanation of why this
            # is `.isoformat()` and not a pre-formatted `.strftime(...)`
            # string.
            "checkout_date": c.checkout_date.isoformat() if c.checkout_date else None,
            "due_date": c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
        })

    # Recompute + persist Available = Total - Outbound - Isolated so the
    # numbers shown here are always live, never stale.
    stock = recalculate_asset_stock(db, asset)
    db.commit()

    return {
        "asset_id": asset.id, "name": asset.name, "category": asset.category,
        # `float(...)` -- asset.price comes back from the DB as a
        # `decimal.Decimal` (Numeric column); cast it to a plain float here
        # so it serializes as an ordinary JSON number, same treatment as
        # every other numeric field in this response.
        "price": float(asset.price) if asset.price is not None else None,
        "total_quantity": stock["total"],
        "available_quantity": stock["available"], "outbound_quantity": stock["outbound"],
        "isolated_quantity": stock["isolated"],
        "under_repair_count": len(repairs),
        "under_repair_items": [{"exception_id": r.id, "serial": r.serial_number, "notes": r.notes} for r in repairs],
        "stolen_count": len(stolen),
        "stolen_items": [{"exception_id": s.id, "serial": s.serial_number, "notes": s.notes} for s in stolen],
        "active_assignments": checkout_list,
    }


def update_asset_quantity(db: Session, asset_id: int, payload: QuantityUpdateRequest, user: dict) -> dict:
    # Adjusting total pool capacity is a Super Admin-only action.
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    # "Allocated" here means units that are NOT sitting free in Available --
    # i.e. Outbound (checked out) + Isolated (in repair/stolen/missing).
    # We must never let total_quantity drop below that, or Available would
    # go negative.
    stock = recalculate_asset_stock(db, asset)
    allocated_items = stock["outbound"] + stock["isolated"]
    if payload.new_total < allocated_items:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reduce total below {allocated_items} (currently {stock['outbound']} outbound + {stock['isolated']} isolated).",
        )

    old_total = asset.total_quantity
    asset.total_quantity = payload.new_total
    recalculate_asset_stock(db, asset)  # re-derive Available from the new Total

    db.add(models.AuditLog(
        operator=user["email"], action="CAPACITY_ADJUSTED", target_type="AssetType", target_id=asset_id,
        details=f"Adjusted '{asset.name}' capacity from {old_total} to {payload.new_total}.",
    ))
    db.commit()
    return {"message": "Successfully updated total capacity."}


def update_asset_name(db: Session, asset_id: int, payload: NameUpdateRequest, user: dict) -> dict:
    # Renaming a stock pool is a Super Admin-only action -- same privilege
    # tier as adjusting its capacity above.
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    # AssetType.name carries a DB-level unique constraint, but we still
    # pre-check here (same pattern create_asset_type uses on creation) so
    # a collision surfaces as a friendly 400 instead of a raw
    # IntegrityError/500 from the database.
    duplicate = db.query(models.AssetType).filter(
        models.AssetType.name == payload.name,
        models.AssetType.id != asset_id,
        models.AssetType.is_deleted == False,
    ).first()
    if duplicate:
        raise HTTPException(status_code=400, detail="Asset type name already exists")

    old_name = asset.name
    asset.name = payload.name

    db.add(models.AuditLog(
        operator=user["email"], action="POOL_RENAMED", target_type="AssetType", target_id=asset_id,
        details=f"Renamed asset pool from '{old_name}' to '{payload.name}'.",
    ))
    db.commit()
    return {"message": "Successfully updated asset name."}


def update_asset_category(db: Session, asset_id: int, payload: CategoryUpdateRequest, user: dict) -> dict:
    # Same privilege tier as renaming/adjusting capacity above -- lets a
    # Super Admin backfill a category onto pools that were created
    # without one (or correct/clear one that's already set).
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    old_category = asset.category
    new_category = payload.category
    if old_category == new_category:
        return {"message": "Category unchanged."}

    asset.category = new_category

    if new_category:
        change_desc = (
            f"changed from '{old_category}' to '{new_category}'"
            if old_category else f"set to '{new_category}'"
        )
    else:
        change_desc = f"cleared (was '{old_category}')"

    db.add(models.AuditLog(
        operator=user["email"], action="POOL_CATEGORY_UPDATED", target_type="AssetType", target_id=asset_id,
        details=f"Category for asset pool '{asset.name}' {change_desc}.",
    ))
    db.commit()
    return {"message": "Successfully updated category."}


def update_asset_price(db: Session, asset_id: int, payload: PriceUpdateRequest, user: dict) -> dict:
    # Same privilege tier as renaming/adjusting capacity/category above --
    # lets a Super Admin backfill a price onto pools that were created
    # without one (or correct/clear one that's already set).
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    old_price = float(asset.price) if asset.price is not None else None
    new_price = payload.price
    if old_price == new_price:
        return {"message": "Price unchanged."}

    asset.price = new_price

    if new_price is not None:
        change_desc = (
            f"changed from {export_service.format_money(old_price)} to {export_service.format_money(new_price)}"
            if old_price is not None else f"set to {export_service.format_money(new_price)}"
        )
    else:
        change_desc = f"cleared (was {export_service.format_money(old_price)})"

    db.add(models.AuditLog(
        operator=user["email"], action="POOL_PRICE_UPDATED", target_type="AssetType", target_id=asset_id,
        details=f"Price for asset pool '{asset.name}' {change_desc}.",
    ))
    db.commit()
    return {"message": "Successfully updated price."}


def delete_asset_type(db: Session, asset_id: int, user: dict) -> dict:
    """
    Deleting an asset pool is a Super Admin-only action.

    SOFT DELETE ONLY -- we never `db.delete()` the row. A hard delete would
    either violate the foreign keys from AssetCheckout.asset_id /
    AssetException.asset_type_id (if RESTRICT), or silently wipe every
    historical checkout/exception tied to this pool out of the audit trail
    (if CASCADE/SET NULL). Instead we flip is_deleted/deleted_at so the row
    -- and everything that references it -- stays intact forever, while the
    pool disappears from active inventory.

    Same shape as delete_user: a pool still holding outstanding checkouts or
    isolated (under-repair/stolen) serials can't be deleted until those are
    resolved, so inventory can't silently "disappear" out from under an
    active custody or maintenance record.
    """
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    outstanding_items = db.query(func.coalesce(func.sum(
        models.AssetCheckout.quantity - models.AssetCheckout.quantity_returned
    ), 0)).filter(
        models.AssetCheckout.asset_id == asset_id, models.AssetCheckout.status == "active",
    ).scalar() or 0
    if outstanding_items > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: {outstanding_items} unit(s) of this pool are still checked out. Process returns first.",
        )

    isolated_items = db.query(func.count(models.AssetException.id)).filter(
        models.AssetException.asset_type_id == asset_id,
        models.AssetException.isolation_status == "isolated",
    ).scalar() or 0
    if isolated_items > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: {isolated_items} serial(s) are still isolated (under repair/stolen/missing). Recall them first.",
        )

    asset_name = asset.name
    asset.is_deleted = True
    asset.deleted_at = utc_now()

    db.add(models.AuditLog(
        operator=user["email"], action="DELETE_ASSET", target_type="AssetType", target_id=asset_id,
        details=f"Soft-deleted category '{asset_name}' (removed from active inventory, history preserved).",
    ))
    db.commit()
    return {"message": f"Asset category '{asset_name}' removed."}


def flag_asset_exception(db: Session, asset_id: int, exc: ExceptionCreate, user: dict) -> dict:
    """
    Isolating a serial for repair/loss is a Super Admin-only action.

    Isolating a unit must NOT shrink `total_quantity` -- Total Capacity is a
    fixed number representing how many units the org owns. Isolating a unit
    only pulls it out of the Available pool: Available = Total - Outbound -
    Isolated. We simply create the exception record and let
    recalculate_asset_stock() derive the new Available count from it.
    """
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    if asset.total_quantity <= 0:
        raise HTTPException(status_code=400, detail="No stock inventory exists to isolate")

    dup = db.query(models.AssetException).filter(
        models.AssetException.serial_number == exc.serial_number,
        models.AssetException.isolation_status == "isolated",
    ).first()
    if dup:
        raise HTTPException(status_code=400, detail="This serial number is already flagged")

    # Guard against isolating more units than are actually free right now
    # (you can't pull a unit out of the pool that's already checked out).
    stock = recalculate_asset_stock(db, asset)
    if stock["available"] <= 0:
        raise HTTPException(status_code=400, detail="No available units left to isolate -- all units are outbound or already isolated.")

    new_exception = models.AssetException(
        asset_type_id=asset_id, serial_number=exc.serial_number, status_label=exc.status_label,
        notes=exc.notes, isolation_status="isolated",
    )
    db.add(new_exception)
    db.flush()

    recalculate_asset_stock(db, asset)  # Available immediately drops by 1

    db.add(models.AuditLog(
        operator=user["email"], action="MAINTENANCE_ISOLATE", target_type="AssetException", target_id=asset_id,
        details=f"Flagged serial {exc.serial_number} as {exc.status_label}.",
    ))
    db.commit()
    return {"message": "Serial number exception logged."}


def recall_asset_exception(db: Session, asset_id: int, exception_id: int, user: dict) -> dict:
    """
    "Recall and Update" workflow: recovers a stolen item or returns a
    repaired item back into active service. Marks the exception as recalled
    (keeping it for history) and lets recalculate_asset_stock() increase
    Available by exactly 1 -- the isolated count drops, Available goes up by
    that same amount, Total Capacity never changes.
    """
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    exception = db.query(models.AssetException).filter(
        models.AssetException.id == exception_id,
        models.AssetException.asset_type_id == asset_id,
        models.AssetException.isolation_status == "isolated",
    ).first()
    if not exception:
        raise HTTPException(status_code=404, detail="Active isolation record not found for this asset.")

    exception.isolation_status = "recalled"
    exception.recalled_at = utc_now()

    recalculate_asset_stock(db, asset)  # Isolated count drops, Available rises by 1

    db.add(models.AuditLog(
        operator=user["email"], action="MAINTENANCE_RECALL", target_type="AssetException", target_id=exception_id,
        details=f"Recalled serial {exception.serial_number} ('{exception.status_label}') back into service for '{asset.name}'.",
    ))
    db.commit()
    return {"message": f"Serial {exception.serial_number} recalled and returned to the Available pool."}


def checkin_asset(db: Session, asset_id: int, quantity: int, user: dict) -> dict:
    """
    Bulk reconciliation check-in (e.g. 'Reconcile & Check-in Stock' from the
    maintenance history table) -- used when NEW physical units are found and
    added to the pool, as opposed to returning a specific outstanding
    checkout (see checkout_service.return_checkout) or recalling an isolated
    unit (see recall_asset_exception above).

    Since Available is now derived (Available = Total - Outbound -
    Isolated), "checking in" newly-found stock means growing Total Capacity
    by `quantity`; Available then rises automatically by the same amount.
    """
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    asset.total_quantity += quantity
    stock = recalculate_asset_stock(db, asset)

    db.add(models.AuditLog(
        operator=user["email"], action="STOCK_RECONCILE", target_type="AssetType", target_id=asset_id,
        details=f"Checked in {quantity} newly-found unit(s) of '{asset.name}'.",
    ))
    db.commit()
    return {"message": f"Successfully checked in {quantity} unit(s).", "remaining_available": stock["available"]}


def checkout_advanced(db: Session, asset_id: int, req: AdvancedCheckoutRequest, user: dict) -> dict:
    # Only Super Admins and Managers can dispatch/issue items to people.
    # NOTE: Managers are allowed to dispatch through all three channels the
    # dispatch drawer offers -- Staff, Linked Customer Accounts, and Ad-Hoc
    # (Unlinked) Individuals -- exactly like a Super Admin. `require_privileged_role`
    # already grants both roles equal access here; there is no extra
    # `assignee_type` restriction for Managers. Combined with user_service's
    # department-scoping fix, this means a Manager can find and dispatch to
    # a real Linked Customer account, not just their own department's staff.
    #
    # ROW-LEVEL LOCK (stability): `with_for_update()` takes a PostgreSQL row
    # lock on this asset_types row for the rest of the transaction. Without
    # it, two concurrent checkout requests for the same pool could both read
    # the same "available" count, both pass the `stock["available"] <
    # req.quantity` check below, and both commit -- overselling the pool
    # (Available going negative). With the lock, the second request's
    # SELECT blocks until the first request commits (or rolls back) and
    # releases it, so the second request re-reads the already-updated stock
    # and is correctly rejected if there's no longer enough available.
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, models.AssetType.is_deleted == False
    ).with_for_update().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    # STABILITY: everything from here on either fully succeeds and commits
    # together, or fails and rolls back together -- never a half-applied
    # checkout (e.g. an Outsider row created, or available_quantity
    # decremented, without the matching AssetCheckout / AuditLog rows also
    # landing).
    try:
        stock = recalculate_asset_stock(db, asset)
        if stock["available"] < req.quantity:
            raise HTTPException(status_code=400, detail=f"Requested {req.quantity} units, but only {stock['available']} are available.")

        target_user_id = None
        target_outsider_id = None
        assignee_label = ""

        final_due_datetime = None
        if req.due_date:
            # Combine the plain `date` the person picked with the END of
            # that day (23:59:59.999999), then attach UTC tzinfo explicitly
            # -- `datetime.combine()` on its own always produces a NAIVE
            # datetime even when given `datetime.time.max`, so without this
            # `.replace(tzinfo=...)` step it would silently violate the
            # timezone-aware `TIMESTAMPTZ` column it's about to be saved into.
            final_due_datetime = datetime.datetime.combine(
                req.due_date, datetime.time.max
            ).replace(tzinfo=datetime.timezone.utc)

        if req.assignee_type == "user":
            if not req.user_id:
                raise HTTPException(status_code=400, detail="User ID is required.")
            target_user = db.query(models.User).filter(
                models.User.id == req.user_id, models.User.is_deleted == False
            ).first()
            if not target_user:
                raise HTTPException(status_code=404, detail="System user not found.")

            target_user_id = target_user.id
            assignee_label = f"{target_user.role.capitalize()}: {target_user.name}"

        elif req.assignee_type == "outsider":
            if not req.outsider_name or not req.outsider_contact:
                raise HTTPException(status_code=400, detail="Name and contact are required for outsiders.")
            if not req.due_date:
                raise HTTPException(status_code=400, detail="Due date is mandatory for external unauthenticated allocations.")

            outsider = models.Outsider(name=req.outsider_name, contact_details=req.outsider_contact, company=req.outsider_company)
            db.add(outsider)
            db.flush()
            target_outsider_id = outsider.id
            assignee_label = f"Outsider: {outsider.name} ({req.outsider_company or 'No Company'})"
        else:
            raise HTTPException(status_code=400, detail="Invalid assignee type specified.")

        new_checkout = models.AssetCheckout(
            asset_id=asset.id, user_id=target_user_id, outsider_id=target_outsider_id,
            quantity=req.quantity, quantity_returned=0, due_date=final_due_datetime, status="active",
        )
        db.add(new_checkout)
        db.flush()

        recalculate_asset_stock(db, asset)  # Available immediately drops by req.quantity

        due_log_text = f" Due back: {req.due_date}." if req.due_date else " No fixed due date."
        db.add(models.AuditLog(
            operator=user["email"], action="CHECKOUT_DISPATCH", target_type="AssetType", target_id=asset_id,
            details=f"Assigned {req.quantity} unit(s) of '{asset.name}' to {assignee_label}.{due_log_text}",
        ))
        db.commit()
        return {"message": f"Successfully checked out {req.quantity} asset(s) to {assignee_label}."}

    except HTTPException:
        # Expected validation failure (bad input, insufficient stock, etc).
        # Roll back so the lock is released and nothing half-applied sticks
        # around, then re-raise the original, informative error unchanged.
        db.rollback()
        raise
    except Exception:
        # Unexpected failure (DB error, etc). Roll back to release the row
        # lock and discard any partial writes, then surface a clean 500
        # instead of leaking a stack trace or leaving the transaction open.
        db.rollback()
        raise HTTPException(status_code=500, detail="Checkout failed due to an unexpected server error. No changes were made.")


def import_assets_from_csv(db: Session, file: UploadFile, user: dict) -> dict:
    """
    Bulk CSV import is a Super Admin-only action.

    ERROR DIAGNOSTIC REPORT (Data Quality & Usability requirement #5): a row
    that fails validation is no longer silently dropped with `continue` and
    never mentioned again. Every rejected row is instead recorded in the
    `errors` list below with its 1-based row number (counting the header as
    row 1, exactly like opening the file in a spreadsheet app -- so the
    first DATA row is "row 2"), the value that was rejected, and a
    human-readable reason. The full report is returned in the response body
    (see js/components/assets.js -> submitCsvImportForm() for how the
    frontend surfaces it), so a Super Admin can immediately see and fix the
    specific rows that didn't import instead of just noticing the "imported
    count" looks lower than expected and having no idea why.
    """
    # SECURITY: read at most MAX_CSV_UPLOAD_BYTES + 1 bytes -- reading
    # "one byte past the limit" is a cheap trick to detect an oversized
    # file without ever having to hold the WHOLE (potentially huge) upload
    # in memory just to measure it.
    raw_bytes = file.file.read(MAX_CSV_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_CSV_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"CSV file is too large. Maximum allowed size is {MAX_CSV_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        contents = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Could not read this file as UTF-8 text. Please export/save it as a plain-text CSV and try again.",
        )

    try:
        csv_file = io.StringIO(contents)
        reader = csv.DictReader(csv_file)

        if not reader.fieldnames or "name" not in reader.fieldnames or "total_quantity" not in reader.fieldnames:
            raise HTTPException(
                status_code=400,
                detail="Invalid CSV format: the file must have a header row containing 'name' and 'total_quantity' columns.",
            )

        imported_count = 0
        errors = []  # one entry per row that failed validation -- never silently skipped

        # `enumerate(reader, start=2)`: row 1 is the header line the reader
        # already consumed, so the first actual data row is "row 2" from
        # the point of view of someone looking at the file in a text
        # editor or spreadsheet program.
        for line_number, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            raw_qty = (row.get("total_quantity") or "").strip()

            # OPTIONAL "category" column -- which internal category this
            # row's equipment belongs to (e.g. "Engineering"). Entirely
            # optional: a missing column, or a blank cell, never rejects the
            # row -- it just leaves/keeps `category` unset for that pool
            # (same "optional, not required" rule as the Create Stock Pool
            # form's category field -- see schemas/assets.py's
            # AssetTypeCreate.category).
            raw_category = (row.get("category") or "").strip()
            category = raw_category or None

            # OPTIONAL "price" column -- the per-unit purchase/replacement
            # price for this row's equipment (e.g. "1899.00"). Entirely
            # optional, same "missing column or blank cell never rejects
            # the row" rule as `category` above -- it just leaves/keeps
            # `price` unset for that pool. A cell that IS present but isn't
            # a valid non-negative number is a real error though (same
            # treatment as an invalid total_quantity cell below), since a
            # typo'd price silently becoming "no price set" would be a
            # worse surprise than rejecting the row outright.
            raw_price = (row.get("price") or "").strip()
            price = None
            if raw_price:
                try:
                    price = round(float(raw_price), 2)
                except ValueError:
                    errors.append({
                        "row": line_number, "name": name,
                        "reason": f"'{raw_price}' is not a valid number for price.",
                    })
                    continue
                if price < 0:
                    errors.append({"row": line_number, "name": name, "reason": "price cannot be negative."})
                    continue

            if not name:
                errors.append({"row": line_number, "name": row.get("name"), "reason": "Missing asset name."})
                continue

            try:
                qty = int(raw_qty)
            except ValueError:
                errors.append({
                    "row": line_number, "name": name,
                    "reason": f"'{raw_qty}' is not a whole number for total_quantity.",
                })
                continue

            if qty < 0:
                errors.append({"row": line_number, "name": name, "reason": "total_quantity cannot be negative."})
                continue

            existing = db.query(models.AssetType).filter(models.AssetType.name == name).first()
            if existing:
                existing.total_quantity += qty
                # A category provided on a re-import of an existing pool
                # updates/fills in that pool's category; a blank cell
                # never clears a category that was already set, so a
                # category-less follow-up import (e.g. a routine
                # quantity-only restock file) can never accidentally wipe
                # out a value someone set earlier.
                if category:
                    existing.category = category
                # Same "blank cell never clears a value already on file"
                # rule as category above -- a routine quantity-only
                # restock file (no price column filled in) must never wipe
                # out a price someone set earlier.
                if price is not None:
                    existing.price = price
                recalculate_asset_stock(db, existing)
            else:
                db.add(models.AssetType(name=name, total_quantity=qty, available_quantity=qty, category=category, price=price))
            imported_count += 1

        summary = f"Spreadsheet processed. Registered {imported_count} update(s), {len(errors)} row(s) rejected."
        db.add(models.AuditLog(
            operator=user["email"], action="BATCH_IMPORT", target_type="AssetType", target_id=0,
            details=summary,
        ))
        db.commit()
        return {
            "message": summary,
            "imported_count": imported_count,
            "error_count": len(errors),
            # Full diagnostic report -- one object per rejected row, so the
            # caller can pinpoint and fix exactly what went wrong instead of
            # guessing why the imported count came in lower than expected.
            "errors": errors,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# ASSET INVENTORY EXPORT (CSV / PDF) -- with per-category download options
# ---------------------------------------------------------------------------
# Unlike the "properties assigned" exports in user_service.py/
# outsider_service.py (which export CHECKOUTS -- who currently holds what),
# this exports the INVENTORY ITSELF -- one row per pool, exactly what the
# Asset Inventory table shows. `list_asset_categories()` powers the
# Export button's dropdown of "download by category" options (plus a
# "Download All" choice); `export_assets_inventory()` does the actual
# CSV/PDF generation, optionally narrowed to a single category.
_INVENTORY_EXPORT_HEADERS = ["Pool ID", "Asset Name", "Category", "Price", "Available", "Total", "Status"]


def list_asset_categories(db: Session) -> dict:
    """
    Every distinct, non-blank category currently set on an active
    (non-soft-deleted) pool, alphabetically sorted -- used to populate the
    Asset Inventory export button's per-category download list. Pools
    with no category set are NOT represented as their own category here;
    they're still included in "Download All" (see export_assets_inventory
    below), just not offered as a specific category filter to pick.
    """
    rows = db.query(models.AssetType.category).filter(
        models.AssetType.is_deleted == False,
        models.AssetType.category.isnot(None),
        models.AssetType.category != "",
    ).distinct().all()
    categories = sorted({r[0] for r in rows if r[0]}, key=str.lower)
    return {"categories": categories}


def _inventory_status_label(available_quantity: int) -> str:
    """Mirrors js/ui.js's statusBadge() thresholds exactly, so a pool that
    reads 'Critical Low Stock' on screen exports with that same label."""
    return "Critical Low Stock" if available_quantity <= 3 else "In Stock"


def export_assets_inventory(db: Session, user: dict, category: Optional[str], fmt: str):
    """
    Exports the Asset Inventory table itself (one row per pool), optionally
    narrowed to a single category. `category=None` (or blank/"all")
    means "Download All" -- every active pool regardless of category.
    Soft-deleted pools are excluded, same as the live Asset Inventory table.
    """
    query = db.query(models.AssetType).filter(models.AssetType.is_deleted == False)

    cat_filter = (category or "").strip()
    if cat_filter and cat_filter.lower() != "all":
        # Case-insensitive exact match against the category string --
        # the dropdown that drives this is itself populated from the exact
        # distinct values on file (see list_asset_categories above), so
        # this only ever needs to match one of those, not do substring
        # search.
        query = query.filter(func.lower(models.AssetType.category) == cat_filter.lower())

    pools = query.order_by(models.AssetType.id).all()

    rows = [
        [p.id, p.name, p.category or "—", export_service.format_money(p.price) if p.price is not None else "—",
         p.available_quantity, p.total_quantity, _inventory_status_label(p.available_quantity)]
        for p in pools
    ]

    today = utc_now().strftime("%Y-%m-%d")
    scope_label = cat_filter if (cat_filter and cat_filter.lower() != "all") else "All Categories"
    filename_stub = f"asset_inventory_{cat_filter.replace(' ', '_')}" if (cat_filter and cat_filter.lower() != "all") else "asset_inventory_all"
    title = f"Asset Inventory — {scope_label}"
    subtitle = f"Exported by {user['email']} · {len(rows)} pool(s) · {today}"

    if fmt == "pdf":
        pdf_bytes = export_service.build_pdf_bytes(title, subtitle, _INVENTORY_EXPORT_HEADERS, rows)
        return pdf_bytes, "application/pdf", f"{filename_stub}_{today}.pdf"
    csv_bytes = export_service.build_csv_bytes(_INVENTORY_EXPORT_HEADERS, rows)
    return csv_bytes, "text/csv", f"{filename_stub}_{today}.csv"
