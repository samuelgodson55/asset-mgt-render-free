"""
services/asset/crud.py
--------------------------
Core AssetType pool CRUD: create, list (active + soft-deleted), detail,
the five focused field-update endpoints, soft-delete/restore, hard purge,
and the distinct-categories helper for the filter dropdown.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from models import utc_now
from schemas.assets_schema import (
    AssetTypeCreate, CategoryUpdateRequest, DepartmentUpdateRequest,
    NameUpdateRequest, PriceUpdateRequest, QuantityUpdateRequest,
)
from services.search_utils import apply_search_filter
from services.stock import recalculate_asset_stock
import services.export_service as export_service

from .constants import DEFAULT_LIMIT, MAX_LIMIT
from .helpers import _can_see_custody, _can_see_stock, _coerce_asset_price, _serialize_asset_type


def create_asset_type(db: Session, asset: AssetTypeCreate, user: dict) -> dict:
    # Only Super Admins may create brand new stock pools.
    existing = db.query(models.AssetType).filter(models.AssetType.name == asset.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Asset type name already exists")

    price_value, price_error = _coerce_asset_price(asset.price)
    if price_error:
        raise HTTPException(status_code=400, detail=price_error)

    new_asset_type = models.AssetType(
        name=asset.name,
        total_quantity=asset.total_quantity,
        available_quantity=asset.total_quantity,  # no checkouts/isolations yet, so Available == Total
        custom_fields=asset.custom_fields,
        category=asset.category,
        department=asset.department,
        price=price_value,
    )
    db.add(new_asset_type)
    db.commit()
    db.refresh(new_asset_type)

    category_log_text = f" (Category: {asset.category})" if asset.category else ""
    department_log_text = f" (Department: {asset.department})" if asset.department else ""
    price_log_text = f" (Price: {export_service.format_money(price_value)})" if price_value is not None else ""
    db.add(models.AuditLog(
        operator=user["email"], action="POOL_CREATED", target_type="AssetType", target_id=new_asset_type.id,
        details=f"Created asset pool '{asset.name}' with initial quantity of {asset.total_quantity}{category_log_text}{department_log_text}{price_log_text}",
    ))
    db.commit()
    return {"message": "Asset type created successfully", "id": new_asset_type.id}

def list_assets(db: Session, user: dict, limit: int = DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None, category: Optional[str] = None, status: Optional[str] = None) -> dict:
    """
    Any authenticated user (admin, manager, staff, or customer) can view
    the pool list. Soft-deleted pools are excluded -- they're gone from
    active inventory even though the row is kept for historical checkouts.

    PAGINATION + SEARCH + CATEGORY (Data Quality & Usability requirement
    #4, extended to true server-side search/filtering): `limit`/`offset`
    cap how many pools a single request can return; `total` tells the
    caller the true size of the (optionally search/category-narrowed)
    inventory regardless of page size. `search` -- when present -- narrows
    the result to pools whose name contains it (case-insensitive),
    matching the single field the Asset Inventory table's search box has
    always searched by (see js/components/assets.js). `category` -- when
    present and not "all" -- narrows to pools with an exact (case-
    insensitive) category match; "Uncategorized" maps to `category IS
    NULL`, mirroring the frontend's own fallback label for a pool with no
    category set (see frontend-app/src/pages/Dashboard.tsx's
    myCategoryCounts and frontend-app/src/pages/Assets.tsx's category
    pills, both of which already display "Uncategorized" for that case).
    Both filters are applied and counted BEFORE the offset/limit slice, so
    `total`/pagination always reflect the filtered set, not the whole
    table -- this is what lets a category tile on the Dashboard deep-link
    into a specific, correctly-paginated slice of the inventory instead of
    a client-side narrowing of whatever single page happened to be loaded.

    STOCK VISIBILITY (see _can_see_stock above): total_quantity/
    available_quantity are only included in each item when the caller is
    a Manager/Admin/Super Admin, or CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER
    is on -- the same rule the Quotation Catalog already enforces (see
    services/quotation_service.py's list_catalog()), now enforced here
    too instead of only being hidden by the frontend.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    query = db.query(models.AssetType).filter(~models.AssetType.is_deleted)
    query = apply_search_filter(query, search, [models.AssetType.name, models.AssetType.department])

    cat_filter = (category or "").strip()
    if cat_filter and cat_filter.lower() != "all":
        if cat_filter.lower() == "uncategorized":
            query = query.filter(models.AssetType.category.is_(None))
        else:
            # Same case-insensitive exact match as export_assets_inventory()
            # below -- the pills that drive this are themselves populated
            # from the exact distinct values on file (see
            # list_asset_categories below), so this only ever needs to
            # match one of those, not do substring search.
            query = query.filter(func.lower(models.AssetType.category) == cat_filter.lower())

    if status:
        if not _can_see_stock(user):
            raise HTTPException(status_code=403, detail="Stock status filtering is not available for this account.")
        normalized_status = status.strip().lower()
        if normalized_status == "out":
            query = query.filter(models.AssetType.available_quantity <= 0)
        elif normalized_status == "low":
            # Match frontend assetStatus(): positive stock at or below 25% of total.
            query = query.filter(
                models.AssetType.available_quantity > 0,
                models.AssetType.total_quantity > 0,
                models.AssetType.available_quantity * 4 <= models.AssetType.total_quantity,
            )
        elif normalized_status == "available":
            query = query.filter(models.AssetType.available_quantity > 0)
        else:
            raise HTTPException(status_code=400, detail="status must be 'available', 'low', or 'out'.")

    query = query.order_by(models.AssetType.id)
    total = query.count()
    rows = query.offset(offset).limit(limit).all()

    show_stock = _can_see_stock(user)
    items = [_serialize_asset_type(row, show_stock) for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset, "show_stock": show_stock}

def get_asset_details(db: Session, asset_id: int, user: dict) -> dict:
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset category not found")

    show_stock = _can_see_stock(user)
    show_custody = _can_see_custody(user)

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
    # numbers shown here are always live, never stale. This still runs
    # (and is still persisted) regardless of `show_stock` -- the recompute
    # keeps the STORED numbers correct for every caller; `show_stock` only
    # controls what's put in THIS response.
    stock = recalculate_asset_stock(db, asset)
    db.commit()

    details = {
        "asset_id": asset.id, "name": asset.name, "category": asset.category, "department": asset.department,
        # `float(...)` -- asset.price comes back from the DB as a
        # `decimal.Decimal` (Numeric column); cast it to a plain float here
        # so it serializes as an ordinary JSON number, same treatment as
        # every other numeric field in this response.
        "price": float(asset.price) if asset.price is not None else None,
        "under_repair_count": len(repairs),
        "under_repair_items": [{"exception_id": r.id, "serial": r.serial_number, "notes": r.notes} for r in repairs],
        "stolen_count": len(stolen),
        "stolen_items": [{"exception_id": s.id, "serial": s.serial_number, "notes": s.notes} for s in stolen],
    }

    # STOCK VISIBILITY (see _can_see_stock above): a Staff/Customer only
    # gets these when CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is on -- same
    # rule as list_assets() above and the Quotation Catalog. Previously
    # this endpoint always included them for any authenticated role,
    # regardless of the flag or the frontend's own AssetDrawer gating.
    if show_stock:
        details["total_quantity"] = stock["total"]
        details["available_quantity"] = stock["available"]
        details["outbound_quantity"] = stock["outbound"]
        details["isolated_quantity"] = stock["isolated"]

    # CUSTODY VISIBILITY (see _can_see_custody above): who currently holds
    # each unit, org-wide, is only included for a Manager/Admin/Super
    # Admin -- independent of the stock flag, since this is custody data
    # (AssetDrawer's `canDispatch` gate), not a stock count. Previously
    # this endpoint always included it for any authenticated role.
    if show_custody:
        details["active_assignments"] = checkout_list

    return details

def update_asset_quantity(db: Session, asset_id: int, payload: QuantityUpdateRequest, user: dict) -> dict:
    # Adjusting total pool capacity is a Super Admin-only action.
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
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
        ~models.AssetType.is_deleted,
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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
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

def update_asset_department(db: Session, asset_id: int, payload: DepartmentUpdateRequest, user: dict) -> dict:
    """Update the production/equipment department without changing the existing category."""
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    old_department = asset.department
    new_department = payload.department
    if old_department == new_department:
        return {"message": "Department unchanged."}

    asset.department = new_department
    if new_department:
        change_desc = (
            f"changed from '{old_department}' to '{new_department}'"
            if old_department else f"set to '{new_department}'"
        )
    else:
        change_desc = f"cleared (was '{old_department}')"

    db.add(models.AuditLog(
        operator=user["email"], action="POOL_DEPARTMENT_UPDATED", target_type="AssetType", target_id=asset_id,
        details=f"Department for asset pool '{asset.name}' {change_desc}.",
    ))
    db.commit()
    return {"message": "Successfully updated department."}

def update_asset_price(db: Session, asset_id: int, payload: PriceUpdateRequest, user: dict) -> dict:
    # Same privilege tier as renaming/adjusting capacity/category above --
    # lets a Super Admin backfill a price onto pools that were created
    # without one (or correct/clear one that's already set).
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset type not found")

    old_price = asset.price
    new_price, price_error = _coerce_asset_price(payload.price)
    if price_error:
        raise HTTPException(status_code=400, detail=price_error)

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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
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


# ---------------------------------------------------------------------------
# SOFT-DELETED ASSET RESTORE ("oops, wrong pool" recovery)
# ---------------------------------------------------------------------------
# Same restorable-deletion pattern as services/user_service.py's
# list_deleted_users()/restore_user() -- a Super Admin who soft-deleted the
# wrong asset pool (or deleted one that turned out to still be needed) can
# bring it back instead of having to recreate it from scratch and lose its
# id, category, price, and total_quantity.

def list_deleted_assets(db: Session, limit: int = DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None) -> dict:
    """
    Mirror of list_assets() above, scoped to soft-deleted pools only --
    powers the "Restore Deleted Assets" panel. Super Admin only (see
    require_super_admin gate in api/assets.py): unlike the main Asset
    Inventory table, this is not visible to Managers/Staff.

    Same limit/offset/search shape as list_assets() so the frontend can
    reuse the exact same pagination/search plumbing (see
    js/ui.js's renderServerPaginationBar()) against a second, independent
    table state.

    EXCLUDES PURGED POOLS -- purge_asset_type() below overwrites a
    pool's `name` with an anonymized placeholder and stamps
    `purged_at`, specifically so its original name's uniqueness lock is
    released. There's nothing meaningful left to "restore" under its
    original name at that point, so purged pools are filtered out here.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    query = db.query(models.AssetType).filter(models.AssetType.is_deleted, models.AssetType.purged_at.is_(None))
    query = apply_search_filter(query, search, [models.AssetType.name])

    total = query.count()
    # Most-recently-deleted first -- that's almost always the row someone
    # opening this panel is looking for ("oops, I just deleted the wrong
    # pool").
    items = query.order_by(models.AssetType.deleted_at.desc()).offset(offset).limit(limit).all()

    results = [{
        "id": a.id, "name": a.name, "category": a.category, "department": a.department,
        "total_quantity": a.total_quantity, "price": a.price,
        # TIMEZONE FIX -- see services/user_service.py's
        # get_my_assigned_items() for the full explanation of why this is
        # `.isoformat()` and not a pre-formatted `.strftime(...)` string.
        "deleted_at": a.deleted_at.isoformat() if a.deleted_at else None,
    } for a in items]
    return {"items": results, "total": total, "limit": limit, "offset": offset}

def restore_asset_type(db: Session, asset_id: int, user: dict) -> dict:
    """
    Reverses delete_asset_type() above: flips is_deleted back to False and
    clears deleted_at, so the pool reappears in the active Asset Inventory
    table exactly as it was (same id, category, price, and quantities) --
    Super Admin only, same gate as delete_asset_type() itself.

    No name-collision handling is needed here: create_asset_type()'s
    duplicate-name check queries ALL `asset_types` rows regardless of
    is_deleted (see its comment), so a soft-deleted pool's original name
    was never up for grabs while it sat deleted -- restoring it can't
    collide with anything created in the meantime.

    NOT FOR PURGED POOLS -- list_deleted_assets() above already keeps
    these out of the "Restore Deleted Assets" panel, but this check is
    repeated here in case restore_asset_type() is ever called directly.
    purge_asset_type() below has already overwritten this row's `name`
    with an anonymized placeholder, so "restoring" it wouldn't bring the
    pool's original name back at all -- it would just reappear in active
    inventory under the placeholder name.
    """
    target = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id,
        models.AssetType.is_deleted,
        models.AssetType.purged_at.is_(None),
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Deleted asset not found.")

    target.is_deleted = False
    target.deleted_at = None

    db.add(models.AuditLog(
        operator=user["email"], action="ASSET_RESTORED", target_type="AssetType", target_id=asset_id,
        details=f"Restored asset category '{target.name}' (returned to active inventory, deletion reversed).",
    ))
    db.commit()
    return {"message": f"Asset category '{target.name}' has been restored."}


# ---------------------------------------------------------------------------
# PURGE ("I'm done with this deleted pool, free up its name")
# ---------------------------------------------------------------------------

def purge_asset_type(db: Session, asset_id: int, user: dict) -> dict:
    """
    Permanently releases a soft-deleted pool's `name` so a brand-new pool
    can reuse it -- called from the "Purge" button that sits next to
    Restore on the Restore Deleted Assets panel. Super Admin only, same
    gate as delete_asset_type()/restore_asset_type().

    WHY THIS EXISTS: `asset_types.name` carries a DB-level `unique=True`
    constraint (see models.py). delete_asset_type() only ever flips
    is_deleted/deleted_at -- it never touches `name` -- so a deleted
    pool's original name stays permanently "reserved" and
    create_asset_type() will keep rejecting it for a new pool (see its
    existing-name check, which deliberately queries ALL rows regardless
    of is_deleted). Previously the only way around that was to restore
    the old pool first (bringing it back into active inventory) purely
    so it could be renamed -- this button skips that detour.

    WHAT "PURGE" DOES *NOT* DO: it is NOT a hard delete. We never
    `db.delete()` this row, for the exact same reason delete_asset_type()
    doesn't -- historical AssetCheckout.asset_id / AssetException rows
    still point at it, and hard-deleting would either violate that
    foreign key or silently erase this pool out of the historical
    custody/audit trail. Instead:
      1. `name` is overwritten with a placeholder that embeds this
         row's own (permanent, unique) id -- "Purged Asset Pool #{id}"
         -- so it can never collide with any other pool, purged or not.
      2. `purged_at` is stamped, which both list_deleted_assets() and
         restore_asset_type() above check -- once purged, the pool
         drops out of the "Restore Deleted Assets" panel entirely and
         can no longer be restored (there'd be nothing meaningful to
         bring back under its original name).
      3. The pool's original name is recorded in the audit log entry
         below (before it's overwritten) so it's still discoverable
         later via the Audit Trail even though the `asset_types` row
         itself no longer carries it.

    Irreversible in the same sense delete_asset_type()'s soft delete is
    reversible and this is not: there's no "unpurge". A caller who
    isn't sure yet should use Restore, not Purge.

    Only ever reachable for rows list_deleted_assets() would surface
    (already soft-deleted, not already purged) -- the same filter is
    repeated here so a raw API call can't purge a live pool or one
    that's already been purged.
    """
    target = db.query(models.AssetType).filter(
        models.AssetType.id == asset_id,
        models.AssetType.is_deleted,
        models.AssetType.purged_at.is_(None),
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Deleted asset not found.")

    original_name = target.name

    target.name = f"Purged Asset Pool #{target.id}"
    target.purged_at = utc_now()

    db.add(models.AuditLog(
        operator=user["email"], action="ASSET_PURGED", target_type="AssetType", target_id=asset_id,
        details=(
            f"Permanently purged deleted asset category '{original_name}'. Its name is now "
            f"free to be reused by a new pool; historical checkout/exception records remain "
            f"intact under this now-renamed row."
        ),
    ))
    db.commit()
    return {"message": f"Asset category '{original_name}' has been purged. Its name is now free to be reused."}

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
        ~models.AssetType.is_deleted,
        models.AssetType.category.isnot(None),
        models.AssetType.category != "",
    ).distinct().all()
    categories = sorted({r[0] for r in rows if r[0]}, key=str.lower)
    return {"categories": categories}
