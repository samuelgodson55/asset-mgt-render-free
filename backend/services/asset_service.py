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
import re
from decimal import Decimal, InvalidOperation
from typing import Optional
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func

import models
from config import settings
from services.search_utils import apply_search_filter
from models import utc_now
from schemas.assets_schema import AssetTypeCreate, ExceptionCreate, AdvancedCheckoutRequest, QuantityUpdateRequest, NameUpdateRequest, CategoryUpdateRequest, DepartmentUpdateRequest, PriceUpdateRequest
from services.stock import recalculate_asset_stock
import services.export_service as export_service

# --- Stock/custody visibility gates -----------------------------------
# Mirrors services/quotation_service.py's list_catalog() / lib/roles.ts's
# canSeeStock()/isPrivileged(): a Manager/Admin/Super Admin always sees
# live stock counts (available/total quantity, in-stock/low/out status);
# a Staff/Customer sees them only when
# settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is on. This used to be a
# frontend-only distinction -- GET /assets, GET /assets/{id}/details, and
# the CSV/PDF export all returned full stock data to any authenticated
# role regardless of this flag, so a Staff/Customer calling the API
# directly (curl, Postman, a modified frontend) could always see it no
# matter what the setting said or what the UI chose to render. These
# helpers, and the call sites below that use them, are what actually
# enforce the setting server-side.
_STOCK_VISIBLE_ROLES = ("super_admin", "admin", "manager")


def _can_see_stock(user: dict) -> bool:
    return user["role"] in _STOCK_VISIBLE_ROLES or settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER


# Custody data -- who currently holds each unit, org-wide (assignee_name/
# assignee_type/quantity across every active checkout against a pool) --
# is a DIFFERENT category of information than stock counts, and is never
# gated by CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER: it's who-has-what, not
# how-much-is-available. Mirrors deps.py's require_privileged_role /
# lib/roles.ts's isPrivileged() (used as AssetDrawer's `canDispatch`
# gate) -- Super Admin, Admin, or Manager only, regardless of the stock
# flag's value.
_CUSTODY_VISIBLE_ROLES = ("super_admin", "admin", "manager")


def _can_see_custody(user: dict) -> bool:
    return user["role"] in _CUSTODY_VISIBLE_ROLES


def _serialize_asset_type(asset: "models.AssetType", show_stock: bool) -> dict:
    """
    Shapes an AssetType row for the Asset Inventory list, gating the
    stock-derived fields the same way services/quotation_service.py's
    list_catalog() already gates them for the Quotation Catalog. Name/
    category/price are descriptive, not stock, so they're always
    included -- unchanged from before this fix.
    """
    entry = {
        "id": asset.id,
        "name": asset.name,
        "category": asset.category,
        "department": asset.department,
        "price": float(asset.price) if asset.price is not None else None,
        "custom_fields": asset.custom_fields,
    }
    if show_stock:
        entry["total_quantity"] = asset.total_quantity
        entry["available_quantity"] = asset.available_quantity
    return entry

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


def _coerce_asset_price(raw_price) -> tuple[Optional[Decimal], Optional[str]]:
    """Validate and normalize per-unit prices for the Numeric(10, 2) DB column."""
    if raw_price is None:
        return None, None
    if isinstance(raw_price, bool):
        return None, "price must be a valid number."

    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError):
        return None, "price must be a valid number."

    price = price.quantize(Decimal("0.01"))
    if price < 0:
        return None, "price cannot be negative."
    if price > Decimal("99999999.99"):
        return None, "price exceeds the supported maximum of 99,999,999.99."
    return price, None


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


def get_activity(db: Session, user: dict, days: int = 14) -> list[dict]:
    """
    Daily checkout/return counts for the last `days` days -- feeds the
    Dashboard's "Checkout activity" chart (frontend-app/src/pages/
    Dashboard.tsx). This has no legacy equivalent (the vanilla-JS frontend
    never had a real endpoint behind that chart either -- see
    frontend-app/src/lib/api.ts's loadStats(), which previously always
    returned `activity: []`); it's a genuinely new endpoint, not a port.

    "Checkouts" counts every AssetCheckout row whose checkout_date falls on
    a given day, regardless of current status. "Returns" counts every row
    whose returned_at falls on a given day -- returned_at is only stamped
    once a checkout is FULLY settled (see services/checkout_service.py's
    return_checkout()), so a day's "returns" reflects checkouts that
    finished closing out that day, not every partial-return event against
    them; there's no per-partial-return timestamp to aggregate on.

    SCOPE (mirrors _can_see_custody above / deps.require_privileged_role):
    a Super Admin/Admin/Manager sees org-wide activity across every
    checkout, the same "whole ledger" view the rest of the Overview
    dashboard (Total Pooled Units, Overdue Returns, etc.) already gives
    them. A Staff/Customer is not privileged to see who-has-what org-wide
    (see _can_see_custody's docstring), so their chart is narrowed to only
    checkouts made against their own account (`user_id`) -- matching what
    "My Items" already shows them elsewhere in the app. Outsourced-to-an-
    outsider checkouts (`user_id IS NULL`, see AssetCheckout's docstring)
    never belong to any Staff/Customer, so they're naturally excluded from
    the narrowed view along with everyone else's checkouts.

    Aggregated in Python (not a SQL date_trunc/group-by) to sidestep
    timezone-bucketing edge cases across the checkout_date/returned_at
    columns -- the row counts here are small enough (bounded by `days`,
    capped below) that this comfortably avoids a second, more fragile
    piece of SQL to maintain.
    """
    days = max(1, min(days, 90))
    since = utc_now() - datetime.timedelta(days=days - 1)
    since_day = since.date()

    checkout_query = db.query(models.AssetCheckout.checkout_date).filter(
        models.AssetCheckout.checkout_date >= since
    )
    return_query = db.query(models.AssetCheckout.returned_at).filter(
        models.AssetCheckout.returned_at.isnot(None), models.AssetCheckout.returned_at >= since
    )
    if not _can_see_custody(user):
        checkout_query = checkout_query.filter(models.AssetCheckout.user_id == int(user["sub"]))
        return_query = return_query.filter(models.AssetCheckout.user_id == int(user["sub"]))

    checkout_days = checkout_query.all()
    return_days = return_query.all()

    checkouts_by_day: dict[str, int] = {}
    for (dt,) in checkout_days:
        key = dt.date().isoformat()
        checkouts_by_day[key] = checkouts_by_day.get(key, 0) + 1

    returns_by_day: dict[str, int] = {}
    for (dt,) in return_days:
        key = dt.date().isoformat()
        returns_by_day[key] = returns_by_day.get(key, 0) + 1

    return [
        {
            "date": (since_day + datetime.timedelta(days=i)).isoformat(),
            "checkouts": checkouts_by_day.get((since_day + datetime.timedelta(days=i)).isoformat(), 0),
            "returns": returns_by_day.get((since_day + datetime.timedelta(days=i)).isoformat(), 0),
        }
        for i in range(days)
    ]


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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).with_for_update().first()
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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).with_for_update().first()
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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
    ).with_for_update().first()
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
        models.AssetType.id == asset_id, ~models.AssetType.is_deleted
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
                models.User.id == req.user_id, ~models.User.is_deleted
            ).first()
            if not target_user:
                raise HTTPException(status_code=404, detail="System user not found.")

            target_user_id = target_user.id
            assignee_label = f"{target_user.role.capitalize()}: {target_user.name}"

        elif req.assignee_type == "outsider":
            if not req.due_date:
                raise HTTPException(status_code=400, detail="Due date is mandatory for external unauthenticated allocations.")

            if req.outsider_id:
                # Route 1: dispatch to an ad-hoc profile ALREADY on file
                # (picked from the "Existing Ad-Hoc Individual" dropdown --
                # see frontend/js/components/assets.js's submitDispatchForm())
                # instead of creating a new one from scratch.
                outsider = db.query(models.Outsider).filter(
                    models.Outsider.id == req.outsider_id, ~models.Outsider.is_deleted
                ).first()
                if not outsider:
                    raise HTTPException(status_code=404, detail="Ad-hoc individual not found.")
            else:
                # Route 2: create a brand new unlinked profile on the spot
                # (the original, only-ever-existing behavior).
                if not req.outsider_name or (not req.outsider_email and not req.outsider_phone):
                    raise HTTPException(status_code=400, detail="Name and at least one of email/phone are required for outsiders.")
                outsider = models.Outsider(
                    name=req.outsider_name, email=req.outsider_email,
                    phone_number=req.outsider_phone, company=req.outsider_company,
                )
                db.add(outsider)
                db.flush()

            target_outsider_id = outsider.id
            assignee_label = f"Outsider: {outsider.name} ({outsider.company or 'No Company'})"
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
    except Exception as exc:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(exc, None, 500, component="asset_service", operation="checkout")
        # Unexpected failure (DB error, etc). Roll back to release the row
        # lock and discard any partial writes, then surface a clean 500
        # instead of leaking a stack trace or leaving the transaction open.
        db.rollback()
        raise HTTPException(status_code=500, detail="Checkout failed due to an unexpected server error. No changes were made.")


def import_assets_from_csv(db: Session, file: UploadFile, user: dict) -> dict:
    """
    Import new asset pools and round-trip updates from Asset Inventory CSV
    exports.

    Two modes are supported in the same file:
      * New pool: no Pool ID; `name`/`Asset Name` and `total_quantity`/`Total`
        are required.
      * Existing pool update: `Pool ID`/`pool_id` identifies the pool. The
        editable columns (name, total quantity, category, department, price)
        are applied to that exact pool. `Available` and `Status` are exported
        for reference only and are intentionally ignored on import because
        available stock is derived from live checkouts/exceptions.

    Blank/"—" values for optional descriptive fields preserve the current
    value on an update. Use `__CLEAR__` when a Super Admin intentionally
    wants to clear category, department, or price.
    """
    raw_bytes = file.file.read(MAX_CSV_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_CSV_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"CSV file is too large. Maximum allowed size is {MAX_CSV_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        contents = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Could not read this file as UTF-8 text. Please export/save it as a plain-text CSV and try again.",
        )

    try:
        reader = csv.DictReader(io.StringIO(contents))
        if not reader.fieldnames:
            raise HTTPException(status_code=400, detail="Invalid CSV format: the file has no header row.")

        # Accept both the original import-template headers and the exact
        # headers produced by Asset Inventory CSV export. This makes an
        # export -> edit -> import round trip a first-class workflow.
        aliases = {
            "pool_id": ("pool id", "pool_id", "id"),
            "name": ("name", "asset name", "asset_name"),
            "total_quantity": ("total_quantity", "total quantity", "total"),
            "category": ("category",),
            "department": ("department",),
            "price": ("price",),
        }
        normalized_headers = {
            str(h or "").strip().lower().replace("-", " "): h
            for h in reader.fieldnames
        }

        def source_key(field: str):
            for alias in aliases[field]:
                if alias in normalized_headers:
                    return normalized_headers[alias]
            return None

        pool_id_key = source_key("pool_id")
        name_key = source_key("name")
        quantity_key = source_key("total_quantity")
        category_key = source_key("category")
        department_key = source_key("department")
        price_key = source_key("price")

        # A Pool ID column turns the file into an update-capable export. A
        # template/new-pool import still uses name + total_quantity.
        if not pool_id_key and (not name_key or not quantity_key):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid CSV format: use either an Asset Inventory export "
                    "(with 'Pool ID' and 'Asset Name') or a new-pool template "
                    "containing 'name' and 'total_quantity'."
                ),
            )

        imported_count = 0
        updated_count = 0
        errors = []
        seen_pool_ids = set()
        seen_names_this_file = {}

        def cell(row, key):
            return (row.get(key) or "").strip() if key else ""

        def is_preserve(value: str) -> bool:
            return not value or value == "—"

        def parse_pool_id(raw: str):
            if not raw:
                return None, None
            try:
                value = int(raw)
            except ValueError:
                return None, "Pool ID must be a whole number."
            if value <= 0:
                return None, "Pool ID must be greater than zero."
            return value, None

        def parse_quantity(raw: str):
            if not raw:
                return None, "'' is not a whole number for total quantity."
            try:
                value = int(raw)
            except ValueError:
                return None, f"'{raw}' is not a whole number for total quantity."
            if value < 0:
                return None, "Total quantity cannot be negative."
            return value, None

        def parse_price(raw: str):
            if is_preserve(raw):
                return None, None, False
            if raw == "__CLEAR__":
                return None, None, True
            # Exported values may be formatted as "₦1,899.00" (or another
            # configured currency symbol). Strip common presentation chars;
            # the Decimal validator remains the authority on the result.
            # Accept every display currency produced by export_service
            # (₦, $, £, €, GH₵, KSh, R, or a configured ISO-code prefix)
            # without making the importer depend on a particular deployment
            # currency. Keep only the numeric representation.
            cleaned = re.sub(r"[^0-9.\-]", "", raw)
            price, price_error = _coerce_asset_price(cleaned)
            if price_error:
                return None, price_error, False
            return price, None, True

        for line_number, row in enumerate(reader, start=2):
            raw_pool_id = cell(row, pool_id_key)
            pool_id, pool_id_error = parse_pool_id(raw_pool_id)
            if pool_id_error:
                errors.append({"row": line_number, "name": cell(row, name_key), "reason": pool_id_error})
                continue

            if pool_id is not None:
                if pool_id in seen_pool_ids:
                    errors.append({"row": line_number, "name": cell(row, name_key), "reason": f"Pool ID {pool_id} appears more than once in this import file."})
                    continue
                seen_pool_ids.add(pool_id)

                asset = db.query(models.AssetType).filter(
                    models.AssetType.id == pool_id,
                    ~models.AssetType.is_deleted,
                ).first()
                if not asset:
                    errors.append({"row": line_number, "name": cell(row, name_key), "reason": f"Pool ID {pool_id} was not found in active inventory."})
                    continue

                raw_name = cell(row, name_key)
                if raw_name and raw_name != "—":
                    duplicate = db.query(models.AssetType).filter(
                        func.lower(models.AssetType.name) == raw_name.lower(),
                        models.AssetType.id != pool_id,
                        ~models.AssetType.is_deleted,
                    ).first()
                    if duplicate:
                        errors.append({"row": line_number, "name": raw_name, "reason": f'Asset name "{raw_name}" already belongs to Pool ID {duplicate.id}.'})
                        continue

                raw_qty = cell(row, quantity_key)
                qty = None
                if raw_qty and raw_qty != "—":
                    qty, qty_error = parse_quantity(raw_qty)
                    if qty_error:
                        errors.append({"row": line_number, "name": raw_name or asset.name, "reason": qty_error})
                        continue
                    stock = recalculate_asset_stock(db, asset)
                    allocated_items = stock["outbound"] + stock["isolated"]
                    if qty < allocated_items:
                        errors.append({
                            "row": line_number,
                            "name": raw_name or asset.name,
                            "reason": f"Cannot reduce total below {allocated_items} ({stock['outbound']} outbound + {stock['isolated']} isolated).",
                        })
                        continue

                parsed_price = None
                price_provided = False
                if price_key:
                    raw_price = cell(row, price_key)
                    parsed_price, price_error, price_provided = parse_price(raw_price)
                    if price_error:
                        errors.append({"row": line_number, "name": raw_name or asset.name, "reason": price_error})
                        continue

                if qty is not None:
                    asset.total_quantity = qty
                    recalculate_asset_stock(db, asset)

                if raw_name and raw_name != "—" and raw_name != asset.name:
                    asset.name = raw_name

                if category_key:
                    raw_category = cell(row, category_key)
                    if raw_category == "__CLEAR__":
                        asset.category = None
                    elif not is_preserve(raw_category):
                        asset.category = raw_category

                if department_key:
                    raw_department = cell(row, department_key)
                    if raw_department == "__CLEAR__":
                        asset.department = None
                    elif not is_preserve(raw_department):
                        asset.department = raw_department

                if price_provided:
                    asset.price = parsed_price

                updated_count += 1
                continue

            # No Pool ID: this remains the original new-pool import mode.
            name = cell(row, name_key)
            if not name:
                errors.append({"row": line_number, "name": row.get(name_key) if name_key else None, "reason": "Missing asset name."})
                continue
            if name in seen_names_this_file:
                errors.append({"row": line_number, "name": name, "reason": f'Duplicate item "{name}" already exists in this import file (first seen on row {seen_names_this_file[name]}).'})
                continue
            seen_names_this_file[name] = line_number

            qty, qty_error = parse_quantity(cell(row, quantity_key))
            if qty_error:
                errors.append({"row": line_number, "name": name, "reason": qty_error})
                continue

            existing = db.query(models.AssetType).filter(models.AssetType.name == name).first()
            if existing:
                errors.append({
                    "row": line_number,
                    "name": name,
                    "reason": f'Item "{name}" already exists in the system (Pool ID {existing.id}). Update its quantity directly from the Asset Inventory table instead of re-importing it.',
                })
                continue

            category = None
            if category_key and not is_preserve(cell(row, category_key)):
                category = cell(row, category_key) or None
            department = None
            if department_key and not is_preserve(cell(row, department_key)):
                department = cell(row, department_key) or None

            price = None
            if price_key:
                raw_price = cell(row, price_key)
                parsed_price, price_error, provided = parse_price(raw_price)
                if price_error:
                    errors.append({"row": line_number, "name": name, "reason": price_error})
                    continue
                if provided:
                    price = parsed_price

            db.add(models.AssetType(
                name=name,
                total_quantity=qty,
                available_quantity=qty,
                category=category,
                department=department,
                price=price,
            ))
            imported_count += 1

        summary = f"CSV processed. Updated {updated_count} pool(s), registered {imported_count} new pool(s), {len(errors)} row(s) rejected."
        db.add(models.AuditLog(
            operator=user["email"], action="BATCH_IMPORT", target_type="AssetType", target_id=0,
            details=summary,
        ))
        db.commit()
        return {
            "message": summary,
            "imported_count": imported_count + updated_count,
            "updated_count": updated_count,
            "created_count": imported_count,
            "error_count": len(errors),
            "errors": errors,
        }
    except HTTPException:
        raise
    except Exception as e:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(e, None, 500, component="asset_service", operation="csv_import")
        db.rollback()
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
_INVENTORY_EXPORT_HEADERS = ["Pool ID", "Asset Name", "Category", "Department", "Price", "Available", "Total", "Status"]


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

    STOCK VISIBILITY (see _can_see_stock above): the Available/Total/
    Status columns are only included when the caller is a Manager/Admin/
    Super Admin, or CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is on -- same
    rule as list_assets()/get_asset_details() above. Previously this
    export always included them for any authenticated role, so a Staff/
    Customer without on-screen stock visibility could still download it
    via CSV/PDF.
    """
    query = db.query(models.AssetType).filter(~models.AssetType.is_deleted)

    cat_filter = (category or "").strip()
    if cat_filter and cat_filter.lower() != "all":
        # Case-insensitive exact match against the category string --
        # the dropdown that drives this is itself populated from the exact
        # distinct values on file (see list_asset_categories above), so
        # this only ever needs to match one of those, not do substring
        # search.
        query = query.filter(func.lower(models.AssetType.category) == cat_filter.lower())

    pools = query.order_by(models.AssetType.id).all()

    show_stock = _can_see_stock(user)
    headers = _INVENTORY_EXPORT_HEADERS if show_stock else _INVENTORY_EXPORT_HEADERS[:5]

    rows = []
    for p in pools:
        price_for_export = export_service.format_money(p.price) if p.price is not None else "—"
        price_for_csv = f"{float(p.price):.2f}" if p.price is not None else "—"
        row_tail = [p.id, p.name, p.category or "—", p.department or "—"]
        if fmt == "pdf":
            row = row_tail + [price_for_export]
        else:
            row = row_tail + [price_for_csv]
        if show_stock:
            row += [p.available_quantity, p.total_quantity, _inventory_status_label(p.available_quantity)]
        rows.append(row)

    today = utc_now().strftime("%Y-%m-%d")
    scope_label = cat_filter if (cat_filter and cat_filter.lower() != "all") else "All Categories"
    filename_stub = f"asset_inventory_{cat_filter.replace(' ', '_')}" if (cat_filter and cat_filter.lower() != "all") else "asset_inventory_all"
    title = f"Asset Inventory — {scope_label}"
    subtitle = f"Exported by {user['email']} · {len(rows)} pool(s) · {today}"

    if fmt == "pdf":
        pdf_bytes = export_service.build_pdf_bytes(title, subtitle, headers, rows)
        return pdf_bytes, "application/pdf", f"{filename_stub}_{today}.pdf"
    csv_bytes = export_service.build_csv_bytes(headers, rows)
    return csv_bytes, "text/csv", f"{filename_stub}_{today}.csv"
