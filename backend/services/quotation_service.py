"""
services/quotation_service.py
------------------------------
Business logic for the self-service "Equipment Quotation" feature: the
read-only Asset Catalog a staff/customer account browses, that account's
draft Quotation ("saved order"), SUBMITTING a draft into a permanent,
ID-tagged Quotation an Admin/Manager can pull up/adjust/assign (the
"Quotes" tab), the admin-only global VAT setting, and PDF export. Used
by api/quotations.py.

PRICING MODEL
-------------
A line's total is always `quantity * rental_days * asset.price`, where
`rental_days = max(1, (due_date - start_date).days)` -- a same-day
request still bills a full day rather than zero. Nothing here ever
snapshots a price or VAT rate onto a Quotation/QuotationItem row: every
read/export recomputes against the LIVE AssetType.price and the LIVE
`vat_percent` AppSetting, so an Admin's global price or VAT edit is
reflected immediately in every person's saved order -- exactly the "price
changes globally through the Admin's edit" behavior requested.

SUBMISSION WORKFLOW
--------------------
See models.py's Quotation docstring for the full lifecycle. In short:
each account has at most one `status="draft"` row at a time (their "My
Order" cart) -- `submit_my_quotation()` stamps it with a generated
`reference_number` ("QT-000001"), flips it to `status="submitted"`, and
from then on it's read-only to that account but fully manageable by any
Admin/Manager via `list_quotations()` / `get_quotation_detail()` /
`admin_add_item()` / `admin_update_item_quantity()` / `admin_remove_item()`
/ `assign_quotation()` / `update_quotation_meta()` below.

QUOTE-TO-CHECKOUT WORKFLOW (approve -> fulfill)
------------------------------------------------
`approve_quotation()` flips a `status="submitted"` row to `status=
"approved"` -- the gray "Draft"/"Pending" badge turning green ("Approved /
Ready for Pickup") on the requester's "My Quotes" panel. From that point
on `_get_own_editable_quotation()` blocks the REQUESTER/assignee's own
self-service item edits (it requires `status == "submitted"`), but an
Admin/Manager can keep adjusting items/notes/assignment on an approved
quote via `_ensure_admin_editable()` right up until
`bulk_checkout_quotation()`, the Fulfillment Drawer's "physical bulk
checkout" action, which turns every line item into a real AssetCheckout
row in one atomic, stock-locked transaction and flips the row to
`status="fulfilled"` -- only THEN is the quote closed for good, locked
against edits by anyone, Admin/Manager included. Inventory stock is NEVER
touched at "draft"/"submitted"/"approved" -- only at that final
fulfillment step (mirrors services/asset_service.py's checkout_advanced()
row-locking pattern, just looped over every line of the quote instead of
one asset).
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

import models
from config import settings
from schemas.quotations import (
    QuotationItemCreate, QuotationItemQuantityUpdate, VatUpdateRequest,
    QuotationAssignRequest, QuotationMetaUpdate, QuotationCreateRequest,
    QuotationOutsourcedItemCreate, QuotationOutsourceShortfallItem,
    QuotationDiscountUpdateRequest,
)
import services.export_service as export_service
from services.stock import recalculate_asset_stock

VAT_SETTING_KEY = "vat_percent"
_DEFAULT_VAT_PERCENT = Decimal("0")

TWO_PLACES = Decimal("0.01")

DEFAULT_LIST_LIMIT = 10
MAX_LIST_LIMIT = 100


def _money(value: Decimal) -> float:
    return float(value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def _rental_days(start_date: datetime.date, due_date: datetime.date) -> int:
    return max(1, (due_date - start_date).days)


def _inventory_status_label(available_quantity: int) -> str:
    """Mirrors asset_service._inventory_status_label()'s thresholds -- kept
    as its own tiny copy here rather than importing across service modules
    for what's a one-line, unlikely-to-drift rule."""
    return "Critical Low Stock" if available_quantity <= 3 else "In Stock"


def _reference_number(quotation_id: int) -> str:
    """The human-shareable Quotation ID -- derived straight from the
    (already-unique) primary key rather than a separate counter, so it's
    guaranteed unique for free and never needs its own sequence table."""
    return f"QT-{quotation_id:06d}"


def _user_brief(user: Optional["models.User"]) -> Optional[dict]:
    if user is None:
        return None
    return {"id": user.id, "name": user.name, "email": user.email}


def _outsider_brief(outsider: Optional["models.Outsider"]) -> Optional[dict]:
    if outsider is None:
        return None
    return {"id": outsider.id, "name": outsider.name, "company": outsider.company, "contact_details": outsider.contact_details, "is_outsider": True}


# ---------------------------------------------------------------------------
# Public, non-secret runtime config -- what the frontend needs to know
# before it can render the catalog/cart correctly (currency, whether to
# show stock columns at all). Safe for any authenticated user to read.
# ---------------------------------------------------------------------------
def get_public_config() -> dict:
    return {
        "currency_code": settings.CURRENCY_CODE,
        "show_stock_to_staff_customer": settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER,
        "site_name": settings.SITE_NAME,
    }


# ---------------------------------------------------------------------------
# VAT setting (admin-editable, applies globally to every Quotation)
# ---------------------------------------------------------------------------
def get_vat_percent(db: Session) -> Decimal:
    row = db.query(models.AppSetting).filter(models.AppSetting.key == VAT_SETTING_KEY).first()
    if row is None:
        return _DEFAULT_VAT_PERCENT
    try:
        return Decimal(row.value)
    except Exception:
        return _DEFAULT_VAT_PERCENT


def set_vat_percent(db: Session, payload: VatUpdateRequest, user: dict) -> dict:
    row = db.query(models.AppSetting).filter(models.AppSetting.key == VAT_SETTING_KEY).first()
    previous = row.value if row else "0"
    if row is None:
        row = models.AppSetting(key=VAT_SETTING_KEY, value=str(payload.vat_percent), updated_by=user["email"])
        db.add(row)
    else:
        row.value = str(payload.vat_percent)
        row.updated_by = user["email"]
    db.add(models.AuditLog(
        operator=user["email"], action="VAT_UPDATED", target_type="AppSetting", target_id=0,
        details=f"Changed global VAT from {previous}% to {payload.vat_percent}% -- applies to every saved Quotation immediately.",
    ))
    db.commit()
    return {"vat_percent": payload.vat_percent}


# ---------------------------------------------------------------------------
# Asset Catalog (read-only, for building a Quotation)
# ---------------------------------------------------------------------------
def list_catalog(db: Session, user: dict) -> dict:
    """
    Every active (non-soft-deleted) asset pool, shaped for the self-
    service Quotation Catalog. Name/category/price are always
    included; available_quantity/status are included only when
    settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is True OR the caller
    is a Manager/Admin/Super Admin (whose own Asset Inventory view
    already shows full stock detail today, so hiding it here would be a
    downgrade for them, not a privacy boundary).
    """
    full_admin_roles = ("super_admin", "admin", "manager")
    show_stock = user["role"] in full_admin_roles or settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER

    pools = (
        db.query(models.AssetType)
        .filter(~models.AssetType.is_deleted)
        .order_by(models.AssetType.name)
        .all()
    )

    items = []
    for p in pools:
        entry = {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "price": float(p.price) if p.price is not None else None,
        }
        if show_stock:
            entry["available_quantity"] = p.available_quantity
            entry["status"] = _inventory_status_label(p.available_quantity)
        items.append(entry)

    return {"items": items, "show_stock": show_stock, "currency_code": settings.CURRENCY_CODE}


# ---------------------------------------------------------------------------
# The caller's own draft Quotation ("saved order")
# ---------------------------------------------------------------------------
def _get_or_create_draft(db: Session, user: dict) -> models.Quotation:
    """The caller's current OPEN cart -- `status="draft"`. Once a draft is
    submitted (see submit_my_quotation()) it's no longer "draft", so the
    next add_item() call here creates a fresh row rather than reusing the
    submitted one -- exactly like starting a new shopping cart."""
    quotation = (
        db.query(models.Quotation)
        .filter(models.Quotation.user_id == int(user["sub"]), models.Quotation.status == "draft")
        .first()
    )
    if quotation is None:
        quotation = models.Quotation(user_id=int(user["sub"]), status="draft")
        db.add(quotation)
        db.commit()
        db.refresh(quotation)
    return quotation


def _serialize_quotation(db: Session, quotation: models.Quotation, include_admin_fields: bool = False, reveal_sourcing: bool = False) -> dict:
    vat_percent = get_vat_percent(db)
    subtotal = Decimal("0")
    line_items = []

    items = (
        db.query(models.QuotationItem)
        .options(joinedload(models.QuotationItem.asset))
        .filter(models.QuotationItem.quotation_id == quotation.id)
        .order_by(models.QuotationItem.id)
        .all()
    )

    for item in items:
        asset = item.asset
        unit_price = Decimal(str(asset.price)) if (asset and asset.price is not None) else Decimal("0")
        days = _rental_days(item.start_date, item.due_date)
        line_total = unit_price * item.quantity * days
        subtotal += line_total
        line_items.append({
            "item_id": item.id,
            "outsourced_item_id": None,
            "is_outsourced": False,
            "asset_id": item.asset_id,
            "asset_name": asset.name if asset else "(deleted asset)",
            "category": asset.category if asset else None,
            "description": None,
            "unit_price": _money(unit_price),
            "quantity": item.quantity,
            "start_date": item.start_date.isoformat(),
            "due_date": item.due_date.isoformat(),
            "days": days,
            "line_total": _money(line_total),
        })

    # --- Manager/Admin-only "not currently in inventory" lines --
    # merged into the SAME items array as the catalog-backed lines above,
    # flagged `is_outsourced=True`, so every reader of this payload (the
    # requester's own "My Order"/"My Quotes" view included) sees them
    # listed right alongside everything else on the quote -- see
    # models.py's QuotationOutsourcedItem docstring for why the requester
    # can see but never edit/remove one of these. Priced off THIS row's
    # own `unit_price` (never a live AssetType.price -- there's no
    # catalog row backing it) but otherwise totalled with the exact same
    # `quantity * rental_days * unit_price` formula as a normal line.
    outsourced_items = (
        db.query(models.QuotationOutsourcedItem)
        .filter(models.QuotationOutsourcedItem.quotation_id == quotation.id)
        .order_by(models.QuotationOutsourcedItem.id)
        .all()
    )
    for oitem in outsourced_items:
        unit_price = Decimal(str(oitem.unit_price)) if oitem.unit_price is not None else Decimal("0")
        days = _rental_days(oitem.start_date, oitem.due_date)
        line_total = unit_price * oitem.quantity * days
        subtotal += line_total
        line_items.append({
            "item_id": None,
            "outsourced_item_id": oitem.id,
            "is_outsourced": True,
            "asset_id": None,
            "asset_name": oitem.name,
            "category": None,
            "description": oitem.description,
            "unit_price": _money(unit_price),
            "quantity": oitem.quantity,
            "start_date": oitem.start_date.isoformat(),
            "due_date": oitem.due_date.isoformat(),
            "days": days,
            "line_total": _money(line_total),
        })
        # Vendor/supplier this line is sourced from -- an internal
        # Manager/Admin tracking note (see models.py's
        # QuotationOutsourcedItem.sourced_from), never surfaced to the
        # requester's own "My Order"/"My Quotes" view -- only added onto
        # the dict when a privileged caller asked for it (see this
        # function's `reveal_sourcing` param and its call sites).
        if reveal_sourcing:
            line_items[-1]["sourced_from"] = oitem.sourced_from

    # --- Discount -> VAT -> Grand Total order --------------------------
    # discount_percent (0-100, defaults to 0/"no discount") is applied to
    # the raw subtotal FIRST -- the discounted amount is what VAT is then
    # calculated on, matching standard quote/invoice convention (VAT is
    # owed on what the customer actually pays, not on the pre-discount
    # list price). See models.py's Quotation.discount_percent docstring.
    discount_percent = Decimal(str(quotation.discount_percent or 0))
    discount_amount = (subtotal * discount_percent / Decimal("100"))
    discounted_subtotal = subtotal - discount_amount
    vat_amount = (discounted_subtotal * vat_percent / Decimal("100"))
    total = discounted_subtotal + vat_amount

    result = {
        "id": quotation.id,
        "status": quotation.status,
        # True once a quote is "fulfilled" -- the frontend uses this
        # single flag to disable every ADMIN/MANAGER edit control (item
        # qty/remove/add, notes, assignment) once the quote is closed for
        # good. Deliberately NOT true for "approved": an Admin/Manager can
        # keep editing an approved quote right up until the Fulfillment
        # Drawer checks it out (see _ensure_admin_editable() below) --
        # only the REQUESTER/assignee's own self-service editing is cut
        # off at "approved" (see _get_own_editable_quotation() above,
        # which separately requires status == "submitted").
        "locked": quotation.status == "fulfilled",
        "reference_number": quotation.reference_number,
        "submitted_at": quotation.submitted_at.isoformat() if quotation.submitted_at else None,
        "approved_at": quotation.approved_at.isoformat() if quotation.approved_at else None,
        "fulfilled_at": quotation.fulfilled_at.isoformat() if quotation.fulfilled_at else None,
        "items": line_items,
        "subtotal": _money(subtotal),
        "discount_percent": float(discount_percent),
        "discount_amount": _money(discount_amount),
        "vat_percent": float(vat_percent),
        "vat_amount": _money(vat_amount),
        "total": _money(total),
        "currency_code": settings.CURRENCY_CODE,
        "updated_at": quotation.updated_at.isoformat() if quotation.updated_at else None,
    }

    if include_admin_fields:
        result["notes"] = quotation.notes
        result["requester"] = _user_brief(quotation.user)
        result["assigned_to"] = _user_brief(quotation.assigned_to)
        result["assigned_outsider"] = _outsider_brief(quotation.assigned_outsider)
        result["approved_by"] = _user_brief(quotation.approved_by)
        result["fulfilled_by"] = _user_brief(quotation.fulfilled_by)
        # True when the requester (quotation.user) is a plain staff/customer
        # account submitting their own personal request, as opposed to an
        # Admin/Manager who built this quote on someone else's behalf (see
        # admin_create_quotation()). Drives the Admin/Manager Quote Detail
        # UI's "can't reassign a personal request" gating (see
        # js/components/quotation.js's renderQuoteDetail()) -- kept as one
        # server-computed flag, rather than the role list living twice, so
        # the frontend gate can never drift out of sync with
        # assign_quotation()'s own enforcement below.
        result["is_personal_request"] = bool(quotation.user and quotation.user.role in ("staff", "customer"))

    return result


def get_my_quotation(db: Session, user: dict) -> dict:
    quotation = _get_or_create_draft(db, user)
    return _serialize_quotation(db, quotation)


def list_my_submitted_quotations(db: Session, user: dict) -> dict:
    """The caller's own "My Requests / Quotes" panel: every Quotation
    they've formally submitted, in ANY post-draft status (submitted /
    approved / fulfilled), newest first -- PLUS any Quotation an
    Admin/Manager built and assigned TO them (models.Quotation.assigned_to_id),
    even though someone else is the `user`/requester on that row (e.g. a
    Manager phones in an order on a customer's behalf, then assigns it to
    that customer -- see services/quotation_service.py's
    admin_create_quotation()/assign_quotation()). Without this OR clause,
    an assigned-but-not-self-submitted quote would only ever be visible to
    Admin/Manager via the Quotes tab, never to the person it's actually
    for. Read-only from the requester's side the instant it leaves
    "draft" -- see models.py's Quotation docstring -- the frontend renders
    each row's `status` as a badge (gray "Draft"-style while pending
    review, green "Approved / Ready for Pickup" once an Admin/Manager
    approves it, and a final "Fulfilled" once physically checked out).
    `include_admin_fields=True` below is what puts `requester` on each row
    -- js/components/quotation.js's renderMyQuotationHistory() uses it to
    label rows the caller didn't build themselves ("Requested by ...")."""
    uid = int(user["sub"])
    quotations = (
        db.query(models.Quotation)
        .filter(
            or_(models.Quotation.user_id == uid, models.Quotation.assigned_to_id == uid),
            models.Quotation.status != "draft",
        )
        .order_by(models.Quotation.submitted_at.desc())
        .all()
    )
    return {"items": [_serialize_quotation(db, q, include_admin_fields=True) for q in quotations]}


def _get_own_or_assigned_quotation_or_404(db: Session, user: dict, quotation_id: int) -> models.Quotation:
    """Same visibility rule as list_my_submitted_quotations() above, for
    opening a single quote's detail (the "My Quotes" history row click) --
    the caller can look, whether they're the original requester OR the
    person it's assigned to. This is also the lookup EDITING uses (see
    _get_own_editable_quotation() below) -- a staff/customer account an
    Admin/Manager has assigned a Quotation to can adjust quantities/remove
    lines on it exactly like the original requester can, right up until
    it's approved/fulfilled (see _get_own_editable_quotation()'s status
    gate). Being the assignee never lets someone reassign it elsewhere,
    add/remove outsourced lines, edit notes, approve, or checkout -- those
    stay Admin/Manager-only via the Quotes tab regardless of assignment."""
    uid = int(user["sub"])
    quotation = db.query(models.Quotation).filter(
        models.Quotation.id == quotation_id,
        or_(models.Quotation.user_id == uid, models.Quotation.assigned_to_id == uid),
        models.Quotation.status != "draft",
    ).first()
    if not quotation:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    return quotation


def get_my_submitted_quotation_detail(db: Session, user: dict, quotation_id: int) -> dict:
    """Full detail for one of the caller's OWN submitted Quotations, OR one
    assigned to them by an Admin/Manager -- used to reopen it (read-only
    once Approved/Fulfilled; still editable below while "submitted",
    whether the caller is the original requester or the assignee)."""
    quotation = _get_own_or_assigned_quotation_or_404(db, user, quotation_id)
    return _serialize_quotation(db, quotation, include_admin_fields=True)


def _get_own_editable_quotation(db: Session, user: dict, quotation_id: int) -> models.Quotation:
    """The caller's own previously-submitted Quotation, OR one an
    Admin/Manager has assigned to them, only while it's still "submitted"
    (i.e. not yet approved/fulfilled by an Admin/Manager) -- lets either
    the original requester OR the assignee keep adjusting quantities or
    removing lines right up until it's approved, instead of it going
    instantly read-only the moment it's submitted/assigned. Mirrors
    _ensure_admin_editable() below, just from the requester/assignee's own
    side. See _get_own_or_assigned_quotation_or_404() above for exactly
    what being "the assignee" does and doesn't grant."""
    quotation = _get_own_or_assigned_quotation_or_404(db, user, quotation_id)
    if quotation.status != "submitted":
        raise HTTPException(
            status_code=400,
            detail=f"Quotation {quotation.reference_number} is no longer editable -- "
                   "it's already been approved/fulfilled by an admin or manager.",
        )
    return quotation


def update_my_submitted_item_quantity(db: Session, user: dict, quotation_id: int, item_id: int, payload: QuotationItemQuantityUpdate) -> dict:
    quotation = _get_own_editable_quotation(db, user, quotation_id)
    item = db.query(models.QuotationItem).filter(
        models.QuotationItem.id == item_id, models.QuotationItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That line item was not found on this quotation.")
    item.quantity = payload.quantity
    db.commit()
    return _serialize_quotation(db, quotation, include_admin_fields=True)


def remove_my_submitted_item(db: Session, user: dict, quotation_id: int, item_id: int) -> dict:
    quotation = _get_own_editable_quotation(db, user, quotation_id)
    item = db.query(models.QuotationItem).filter(
        models.QuotationItem.id == item_id, models.QuotationItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That line item was not found on this quotation.")
    db.delete(item)
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True)


def add_my_submitted_item(db: Session, user: dict, quotation_id: int, payload: QuotationItemCreate) -> dict:
    """Lets the REQUESTER/assignee add another catalog asset to their own
    already-submitted Quotation, right up until it's approved -- the
    self-service equivalent of admin_add_item() below, just gated by
    _get_own_editable_quotation() instead of _ensure_admin_editable().
    Mirrors add_item()'s "merge into the existing line for this asset if
    one's already on the quote" behavior so re-adding the same asset just
    updates quantity/dates instead of creating a duplicate row."""
    quotation = _get_own_editable_quotation(db, user, quotation_id)
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == payload.asset_id, ~models.AssetType.is_deleted,
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found.")

    existing = db.query(models.QuotationItem).filter(
        models.QuotationItem.quotation_id == quotation.id,
        models.QuotationItem.asset_id == payload.asset_id,
    ).first()
    if existing:
        existing.quantity = payload.quantity
        existing.start_date = payload.start_date
        existing.due_date = payload.due_date
    else:
        db.add(models.QuotationItem(
            quotation_id=quotation.id, asset_id=payload.asset_id, quantity=payload.quantity,
            start_date=payload.start_date, due_date=payload.due_date,
        ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True)


def add_item(db: Session, user: dict, payload: QuotationItemCreate) -> dict:
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == payload.asset_id, ~models.AssetType.is_deleted,
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found.")

    quotation = _get_or_create_draft(db, user)

    # If this asset is already on the order, just update the existing line
    # (new quantity/dates) instead of creating a duplicate row for the
    # same pool -- keeps "My Order" one row per asset, matching how a
    # normal shopping cart behaves.
    existing = db.query(models.QuotationItem).filter(
        models.QuotationItem.quotation_id == quotation.id,
        models.QuotationItem.asset_id == payload.asset_id,
    ).first()
    if existing:
        existing.quantity = payload.quantity
        existing.start_date = payload.start_date
        existing.due_date = payload.due_date
    else:
        db.add(models.QuotationItem(
            quotation_id=quotation.id, asset_id=payload.asset_id, quantity=payload.quantity,
            start_date=payload.start_date, due_date=payload.due_date,
        ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation)


def update_item_quantity(db: Session, user: dict, item_id: int, payload: QuotationItemQuantityUpdate) -> dict:
    quotation = _get_or_create_draft(db, user)
    item = db.query(models.QuotationItem).filter(
        models.QuotationItem.id == item_id, models.QuotationItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That order line was not found in your saved order.")
    item.quantity = payload.quantity
    db.commit()
    return _serialize_quotation(db, quotation)


def remove_item(db: Session, user: dict, item_id: int) -> dict:
    quotation = _get_or_create_draft(db, user)
    item = db.query(models.QuotationItem).filter(
        models.QuotationItem.id == item_id, models.QuotationItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That order line was not found in your saved order.")
    db.delete(item)
    db.commit()
    return _serialize_quotation(db, quotation)


# ---------------------------------------------------------------------------
# SUBMIT -- turns the caller's current draft into a permanent, ID-tagged
# Quotation an Admin/Manager can look up, adjust, and assign to a user.
# ---------------------------------------------------------------------------
def submit_my_quotation(db: Session, user: dict) -> dict:
    quotation = _get_or_create_draft(db, user)
    has_items = db.query(models.QuotationItem).filter(models.QuotationItem.quotation_id == quotation.id).first()
    if not has_items:
        raise HTTPException(status_code=400, detail="Your saved order is empty -- add at least one asset before submitting.")

    quotation.status = "submitted"
    quotation.reference_number = _reference_number(quotation.id)
    quotation.submitted_at = models.utc_now()
    # Auto-assigned to the requester themselves the moment they submit --
    # covers the common self-pickup case without making an Admin/Manager do
    # it by hand every time. Because the requester is a staff/customer
    # account, assign_quotation() below permanently refuses to move this
    # assignment to anyone else -- a personal request always stays
    # assigned to the person who made it (see that function's own
    # docstring for the full reasoning).
    quotation.assigned_to_id = int(user["sub"])
    db.add(models.AuditLog(
        operator=user["email"], action="QUOTATION_SUBMITTED", target_type="Quotation", target_id=quotation.id,
        details=f"Submitted Quotation {quotation.reference_number} for review/assignment.",
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True)


# ---------------------------------------------------------------------------
# ADMIN/MANAGER: the "Quotes" tab -- look up any submitted Quotation by
# its reference number/requester, adjust its line items, and assign it to
# a user. Gated by deps.require_privileged_role at the API layer.
# ---------------------------------------------------------------------------
def _get_quotation_or_404(db: Session, quotation_id: int) -> models.Quotation:
    quotation = db.query(models.Quotation).filter(models.Quotation.id == quotation_id).first()
    if not quotation:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    return quotation


def _ensure_admin_editable(quotation: models.Quotation) -> None:
    """Blocks item/notes/assignment edits once a Quotation has moved past
    "approved" -- i.e. once it's "fulfilled" (already checked out, now
    just history/closed for good). An Admin/Manager can still keep
    adjusting a "submitted" OR "approved" quote right up until the
    Fulfillment Drawer actually checks it out -- approval only locks the
    quote against the REQUESTER/assignee's own self-service edits (see
    _get_own_editable_quotation() above, which requires status ==
    "submitted"); it does not freeze it for an Admin/Manager. Called at
    the top of every Admin/Manager mutation below EXCEPT
    approve_quotation() and bulk_checkout_quotation() themselves, which
    are the only two actions still allowed to move a submitted/approved
    quote forward. See models.py's Quotation docstring for the full
    lifecycle."""
    if quotation.status == "fulfilled":
        raise HTTPException(status_code=400, detail=f"Quotation {quotation.reference_number} has already been fulfilled and can no longer be edited.")
    if quotation.status == "draft":
        # Shouldn't normally be reachable (drafts have no reference_number
        # and aren't surfaced in the Quotes tab), but guard anyway rather
        # than letting an admin edit somebody's still-in-progress cart.
        raise HTTPException(status_code=400, detail="This Quotation hasn't been submitted yet.")


def list_quotations(
    db: Session, search: Optional[str] = None, status: Optional[str] = None,
    limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
) -> dict:
    """The Admin/Manager "Quotes" master queue -- every Quotation that has
    left "draft" (submitted / approved / fulfilled), newest submission
    first. `search` matches the reference number or the requester's
    name/email; `status` optionally narrows to exactly one of those three
    real statuses (e.g. the Quotes tab's own status filter, or the
    Fulfillment Drawer reusing this same function with status="approved"
    -- see get_fulfillment_queue() below)."""
    query = (
        db.query(models.Quotation)
        .options(joinedload(models.Quotation.user), joinedload(models.Quotation.assigned_to), joinedload(models.Quotation.assigned_outsider))
        .filter(models.Quotation.status != "draft")
    )

    if status:
        query = query.filter(models.Quotation.status == status)

    if search:
        like = f"%{search.strip()}%"
        query = query.join(models.User, models.Quotation.user_id == models.User.id).filter(
            (models.Quotation.reference_number.ilike(like))
            | (models.User.name.ilike(like))
            | (models.User.email.ilike(like))
        )

    total = query.count()
    quotations = (
        query.order_by(models.Quotation.submitted_at.desc())
        .offset(offset).limit(limit).all()
    )

    items = []
    for q in quotations:
        item_count = db.query(models.QuotationItem).filter(models.QuotationItem.quotation_id == q.id).count()
        item_count += db.query(models.QuotationOutsourcedItem).filter(models.QuotationOutsourcedItem.quotation_id == q.id).count()
        serialized = _serialize_quotation(db, q, include_admin_fields=True, reveal_sourcing=True)
        items.append({
            "id": q.id,
            "reference_number": q.reference_number,
            "status": q.status,
            "locked": serialized["locked"],
            "submitted_at": serialized["submitted_at"],
            "approved_at": serialized["approved_at"],
            "requester": serialized["requester"],
            "assigned_to": serialized["assigned_to"],
            "assigned_outsider": serialized["assigned_outsider"],
            "item_count": item_count,
            "total": serialized["total"],
            "currency_code": serialized["currency_code"],
        })

    return {"items": items, "total": total}


def get_quotation_detail(db: Session, quotation_id: int) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def admin_add_item(db: Session, actor: dict, quotation_id: int, payload: QuotationItemCreate) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    asset = db.query(models.AssetType).filter(
        models.AssetType.id == payload.asset_id, ~models.AssetType.is_deleted,
    ).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found.")

    existing = db.query(models.QuotationItem).filter(
        models.QuotationItem.quotation_id == quotation.id,
        models.QuotationItem.asset_id == payload.asset_id,
    ).first()
    if existing:
        existing.quantity = payload.quantity
        existing.start_date = payload.start_date
        existing.due_date = payload.due_date
    else:
        db.add(models.QuotationItem(
            quotation_id=quotation.id, asset_id=payload.asset_id, quantity=payload.quantity,
            start_date=payload.start_date, due_date=payload.due_date,
        ))
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_ITEM_ADDED", target_type="Quotation", target_id=quotation.id,
        details=f"Added/updated {asset.name} (qty {payload.quantity}) on Quotation {quotation.reference_number}.",
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def admin_update_item_quantity(db: Session, actor: dict, quotation_id: int, item_id: int, payload: QuotationItemQuantityUpdate) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    item = db.query(models.QuotationItem).filter(
        models.QuotationItem.id == item_id, models.QuotationItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That line was not found on this Quotation.")
    item.quantity = payload.quantity
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_ITEM_UPDATED", target_type="Quotation", target_id=quotation.id,
        details=f"Set quantity to {payload.quantity} on Quotation {quotation.reference_number}, line {item_id}.",
    ))
    db.commit()
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def admin_remove_item(db: Session, actor: dict, quotation_id: int, item_id: int) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    item = db.query(models.QuotationItem).filter(
        models.QuotationItem.id == item_id, models.QuotationItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That line was not found on this Quotation.")
    db.delete(item)
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_ITEM_REMOVED", target_type="Quotation", target_id=quotation.id,
        details=f"Removed line {item_id} from Quotation {quotation.reference_number}.",
    ))
    db.commit()
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


# ---------------------------------------------------------------------------
# Manager/Admin-only: "not currently in inventory" lines -- see models.py's
# QuotationOutsourcedItem docstring for the full rationale. Gated by the
# SAME _ensure_admin_editable() lock as every other item mutation above
# (frozen the instant a quote is approved), and by
# deps.require_privileged_role at the API layer -- there is no self-service
# equivalent of either function below.
# ---------------------------------------------------------------------------
def admin_add_outsourced_item(db: Session, actor: dict, quotation_id: int, payload: QuotationOutsourcedItemCreate) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)

    db.add(models.QuotationOutsourcedItem(
        quotation_id=quotation.id, name=payload.name, description=payload.description,
        unit_price=payload.unit_price, quantity=payload.quantity, sourced_from=payload.sourced_from,
        start_date=payload.start_date, due_date=payload.due_date,
        # Same hardcoded-Super-Admin FK caveat as approve_quotation() below.
        added_by_id=int(actor["sub"]) if actor["role"] != "super_admin" else None,
    ))
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_OUTSOURCED_ITEM_ADDED", target_type="Quotation", target_id=quotation.id,
        details=f"Added outsourced (not-in-inventory) item '{payload.name}' (qty {payload.quantity}, "
                f"{_money(Decimal(str(payload.unit_price)))}/day"
                f"{f', sourced from {payload.sourced_from}' if payload.sourced_from else ''}) to Quotation {quotation.reference_number}.",
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def admin_remove_outsourced_item(db: Session, actor: dict, quotation_id: int, item_id: int) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    item = db.query(models.QuotationOutsourcedItem).filter(
        models.QuotationOutsourcedItem.id == item_id, models.QuotationOutsourcedItem.quotation_id == quotation.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="That outsourced item was not found on this Quotation.")
    db.delete(item)
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_OUTSOURCED_ITEM_REMOVED", target_type="Quotation", target_id=quotation.id,
        details=f"Removed outsourced item '{item.name}' from Quotation {quotation.reference_number}.",
    ))
    db.commit()
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def update_quotation_meta(db: Session, actor: dict, quotation_id: int, payload: QuotationMetaUpdate) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    quotation.notes = payload.notes
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_NOTES_UPDATED", target_type="Quotation", target_id=quotation.id,
        details=f"Updated notes on Quotation {quotation.reference_number}.",
    ))
    db.commit()
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def update_quotation_discount(db: Session, actor: dict, quotation_id: int, payload: QuotationDiscountUpdateRequest) -> dict:
    """Admin/Manager-only: sets the discount percentage on THIS Quotation.
    Gated by the exact same _ensure_admin_editable() lock as every other
    item/note/assignment edit -- editable right up until the quote is
    fulfilled, same as "the other quote items" per the feature request."""
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    previous = float(quotation.discount_percent or 0)
    quotation.discount_percent = payload.discount_percent
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_DISCOUNT_UPDATED", target_type="Quotation", target_id=quotation.id,
        details=f"Changed discount from {previous}% to {payload.discount_percent}% on Quotation {quotation.reference_number}.",
    ))
    db.commit()
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def admin_create_quotation(db: Session, actor: dict, payload: QuotationCreateRequest) -> dict:
    """Admin/Manager starts a brand new Quotation directly from the Quotes
    tab -- e.g. building one on a user's behalf over the phone, rather
    than waiting for that person to build/submit their own cart. Created
    already `status="submitted"` (there's no self-service cart owner here
    to "submit" it) with zero line items, attributed to the actor as
    `user`/requester, and optionally assigned right away to either a
    linked Staff/Customer account or a brand new Ad-Hoc/unlinked
    individual (mirroring the Issue/Dispatch drawer's own three-way
    Staff/Customer/Ad-Hoc split). Items are then added the same way as
    any other submitted Quotation, via admin_add_item()."""
    assigned_to_id = None
    assigned_outsider_id = None
    assigned_label = "Unassigned"

    if payload.assignee_type == "user":
        target = db.query(models.User).filter(
            models.User.id == payload.assigned_user_id, ~models.User.is_deleted,
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="That user was not found.")
        assigned_to_id = target.id
        assigned_label = f"{target.name} <{target.email}>"
    elif payload.assignee_type == "outsider":
        outsider = models.Outsider(name=payload.outsider_name, contact_details=payload.outsider_contact, company=payload.outsider_company)
        db.add(outsider)
        db.flush()
        assigned_outsider_id = outsider.id
        assigned_label = f"Ad-Hoc: {outsider.name} ({payload.outsider_company or 'No Company'})"

    quotation = models.Quotation(
        user_id=int(actor["sub"]), status="submitted", submitted_at=models.utc_now(),
        assigned_to_id=assigned_to_id, assigned_outsider_id=assigned_outsider_id,
    )
    db.add(quotation)
    db.commit()
    db.refresh(quotation)
    quotation.reference_number = _reference_number(quotation.id)

    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_CREATED", target_type="Quotation", target_id=quotation.id,
        details=f"Created Quotation {quotation.reference_number} directly from the Quotes tab (assigned to {assigned_label}).",
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def assign_quotation(db: Session, actor: dict, quotation_id: int, payload: QuotationAssignRequest) -> dict:
    quotation = _get_quotation_or_404(db, quotation_id)
    _ensure_admin_editable(quotation)
    # A customer/staff account's OWN self-submitted request (built via
    # their own "My Order" cart, see models.py's Quotation docstring) is
    # never reassignable by an Admin/Manager -- who the equipment goes to
    # on a personal request stays with the person who asked for it
    # (bulk_checkout_quotation() already falls back to `user_id` for
    # exactly these quotes when assigned_to_id is unset). This only
    # applies to the requester's own personal request; a quote an
    # Admin/Manager built THEMSELVES on someone's behalf (see
    # admin_create_quotation() -- `quotation.user` there is the
    # Admin/Manager, not a staff/customer) is unaffected and stays fully
    # assignable/reassignable, including changing who it's assigned to
    # after the fact.
    if quotation.user and quotation.user.role in ("staff", "customer"):
        raise HTTPException(
            status_code=403,
            detail=f"Quotation {quotation.reference_number} was submitted personally by "
                   f"{quotation.user.name} and can't be reassigned to someone else.",
        )

    if payload.assignee_type == "user":
        target = db.query(models.User).filter(
            models.User.id == payload.user_id, ~models.User.is_deleted,
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="That user was not found.")
        quotation.assigned_to_id = target.id
        quotation.assigned_outsider_id = None
        target_name = f"{target.name} <{target.email}>"
    elif payload.assignee_type == "outsider":
        outsider = models.Outsider(name=payload.outsider_name, contact_details=payload.outsider_contact, company=payload.outsider_company)
        db.add(outsider)
        db.flush()
        quotation.assigned_to_id = None
        quotation.assigned_outsider_id = outsider.id
        target_name = f"Ad-Hoc: {outsider.name} ({payload.outsider_company or 'No Company'})"
    else:
        quotation.assigned_to_id = None
        quotation.assigned_outsider_id = None
        target_name = "Unassigned"

    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_ASSIGNED", target_type="Quotation", target_id=quotation.id,
        details=f"Assigned Quotation {quotation.reference_number} to {target_name}.",
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


# ---------------------------------------------------------------------------
# QUOTE-TO-CHECKOUT: approve a submitted Quotation (locks it for editing --
# see _ensure_admin_editable() above), and the Fulfillment Drawer's bulk
# physical checkout that turns an approved Quotation's line items into real
# AssetCheckout rows, evaluating/deducting stock ONLY at this final moment
# (never at draft/submit/approve time). Both gated by
# deps.require_privileged_role at the API layer.
# ---------------------------------------------------------------------------
def approve_quotation(db: Session, actor: dict, quotation_id: int) -> dict:
    """Flips a `status="submitted"` Quotation to `status="approved"` --
    the gray "Draft"/pending badge turning into the sharp green
    "Approved / Ready for Pickup" badge on the requester's "My Quotes"
    panel (see components/quotation.js's quotationStatusBadge()). From
    here on _get_own_editable_quotation() blocks the requester/assignee's
    own self-service edits, but an Admin/Manager can keep adjusting items/
    notes/assignment (still gated only by _ensure_admin_editable()) right
    up until bulk_checkout_quotation() below closes it out for good."""
    quotation = _get_quotation_or_404(db, quotation_id)
    if quotation.status != "submitted":
        raise HTTPException(
            status_code=400,
            detail=f"Only a submitted Quotation awaiting review can be approved (this one is currently \"{quotation.status}\").",
        )
    has_items = db.query(models.QuotationItem).filter(models.QuotationItem.quotation_id == quotation.id).first()
    has_outsourced_items = db.query(models.QuotationOutsourcedItem).filter(models.QuotationOutsourcedItem.quotation_id == quotation.id).first()
    if not has_items and not has_outsourced_items:
        raise HTTPException(status_code=400, detail="This Quotation has no items -- add at least one before approving it.")

    quotation.status = "approved"
    quotation.approved_at = models.utc_now()
    # The hardcoded Super Admin isn't a real `users` table row (see
    # deps.py's get_current_user() comment) -- FK'ing to its `sub` (-1)
    # would violate the FK constraint, so leave the id NULL for that one
    # case. The AuditLog row below always carries the operator's email
    # either way, so attribution is never actually lost.
    quotation.approved_by_id = int(actor["sub"]) if actor["role"] != "super_admin" else None
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_APPROVED", target_type="Quotation", target_id=quotation.id,
        details=f"Approved Quotation {quotation.reference_number} -- Ready for Pickup. Locked for further "
                "edits by the requester/assignee; still adjustable by an Admin/Manager until it's checked "
                "out via the Fulfillment Drawer.",
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)


def get_fulfillment_queue(db: Session) -> dict:
    """Every `status="approved"` Quotation, oldest-approved-first (FIFO --
    whoever's been waiting longest for pickup surfaces first) -- the data
    behind the Manager/Admin Fulfillment Drawer. Each line item also
    carries the asset's LIVE `available_quantity` so the drawer can warn
    about a potential shortfall before the person commits to checking a
    quote out -- purely advisory; the actual authoritative check happens
    transactionally, with a row lock, inside bulk_checkout_quotation()."""
    quotations = (
        db.query(models.Quotation)
        .options(joinedload(models.Quotation.user), joinedload(models.Quotation.assigned_to), joinedload(models.Quotation.assigned_outsider))
        .filter(models.Quotation.status == "approved")
        .order_by(models.Quotation.approved_at.asc())
        .all()
    )

    items = []
    for q in quotations:
        serialized = _serialize_quotation(db, q, include_admin_fields=True, reveal_sourcing=True)
        checkout_target = serialized["assigned_outsider"] or serialized["assigned_to"] or serialized["requester"]
        for line in serialized["items"]:
            if line["is_outsourced"]:
                # No AssetType backs an outsourced line -- there's no
                # inventory stock to check a shortfall against.
                line["available_quantity"] = None
                line["stock_shortfall"] = False
                line["shortfall_quantity"] = 0
                continue
            asset = db.query(models.AssetType).filter(models.AssetType.id == line["asset_id"]).first()
            line["available_quantity"] = asset.available_quantity if asset else 0
            line["stock_shortfall"] = asset is not None and asset.available_quantity < line["quantity"]
            line["shortfall_quantity"] = max(0, line["quantity"] - line["available_quantity"])
        items.append({
            "id": q.id,
            "reference_number": q.reference_number,
            "approved_at": serialized["approved_at"],
            "approved_by": serialized["approved_by"],
            "checkout_to": checkout_target,
            "items": serialized["items"],
            "item_count": len(serialized["items"]),
            "total": serialized["total"],
            "currency_code": serialized["currency_code"],
            "has_shortfall": any(li["stock_shortfall"] for li in serialized["items"]),
        })

    return {"items": items, "total": len(items)}


def bulk_checkout_quotation(
    db: Session, actor: dict, quotation_id: int,
    outsource_shortfall_items: Optional[list[QuotationOutsourceShortfallItem]] = None,
) -> dict:
    """
    The Fulfillment Drawer's "physical bulk checkout" action: turns EVERY
    line item on an `status="approved"` Quotation into a real
    AssetCheckout row, dispatched to `assigned_to_id` (falling back to
    `user_id`, the original requester, if never explicitly assigned), then
    flips the quote to `status="fulfilled"`.

    STOCK LOGIC: this is the ONLY point in the whole Quote-to-Checkout
    workflow that ever reserves or deducts inventory -- never at draft,
    submit, or approve time (see models.py's Quotation docstring). Mirrors
    services/asset_service.py's checkout_advanced() row-locking pattern,
    just looped over every distinct asset on the quote instead of one:
    every asset row involved is locked with `with_for_update()` (sorted by
    asset_id first, so two concurrent bulk checkouts touching overlapping
    pools always acquire their locks in the same order and can't
    deadlock), THEN stock is re-checked fresh for every line. If ANY line
    can't be fully covered, the whole checkout is rejected and rolled
    back -- all-or-nothing, exactly like a normal single-asset checkout,
    just extended across every line of the quote in one transaction --
    UNLESS the caller pre-authorized that specific line to be sourced
    externally instead (see below).

    OUTSOURCING A DEPLETED LINE: `outsource_shortfall_items` (from
    QuotationCheckoutRequest.outsource_shortfall_items, the Fulfillment
    Drawer's per-line "source the shortfall externally" controls -- see
    components/quotation.js's renderFulfillmentQueue()/
    processFulfillmentSelected()) is a list of {quotation_item_id,
    allocations: [{quantity, sourced_from, unit_price}, ...]} decisions,
    keyed here by quotation_item_id. For each regular (inventory-backed)
    line, the fresh stock check below runs FIRST, exactly as before; only
    if THAT check comes up short does this function even look at whether a
    matching decision was supplied.

    Unlike before, a shortfall no longer sends the line's ENTIRE quantity
    outsourced -- whatever stock genuinely IS on hand still gets checked
    out of inventory normally (a real AssetCheckout against the asset,
    stock deducted as usual), and only the remaining shortfall (requested
    minus available) is covered externally. That shortfall itself can be
    split across more than one outsourcing company -- each allocation
    becomes its OWN AssetCheckout with `asset_id=NULL` and
    `is_outsourced=True`, snapshotting the depleted AssetType's own name
    (so the recipient still sees "Projector" on their Custody Ledger, not
    a generic placeholder) and either that allocation's typed-in price
    override or the AssetType's own live `price` as of this moment. The
    allocations on a decision must add up to EXACTLY the live shortfall
    quantity -- see QuotationOutsourceShortfallItem's docstring for why.
    A shortfall line with NO matching decision still raises and rolls back
    the ENTIRE checkout exactly like before this feature existed --
    outsourcing a depleted line is always an explicit, per-line opt-in,
    never an automatic fallback.
    """
    outsource_decisions = {d.quotation_item_id: d for d in (outsource_shortfall_items or [])}
    quotation = (
        db.query(models.Quotation)
        .options(joinedload(models.Quotation.items), joinedload(models.Quotation.outsourced_items))
        .filter(models.Quotation.id == quotation_id)
        .first()
    )
    if not quotation:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    if quotation.status != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"Only an Approved / Ready for Pickup Quotation can be checked out (this one is currently \"{quotation.status}\").",
        )
    if not quotation.items and not quotation.outsourced_items:
        raise HTTPException(status_code=400, detail="This Quotation has no items to check out.")

    target_user = None
    target_outsider = None
    if quotation.assigned_outsider_id is not None:
        target_outsider = db.query(models.Outsider).filter(models.Outsider.id == quotation.assigned_outsider_id).first()
        if not target_outsider:
            raise HTTPException(status_code=400, detail="The Ad-Hoc individual this Quotation is assigned to could not be found -- reassign it before checking out.")
    else:
        target_user_id = quotation.assigned_to_id or quotation.user_id
        target_user = db.query(models.User).filter(
            models.User.id == target_user_id, ~models.User.is_deleted,
        ).first()
        if not target_user:
            raise HTTPException(status_code=400, detail="The user this Quotation is for could not be found -- reassign it before checking out.")

    try:
        # Lock every distinct asset row this quote touches, in a fixed
        # (ascending asset_id) order, before evaluating stock on any of
        # them -- see the STOCK LOGIC note above for why.
        asset_ids = sorted({item.asset_id for item in quotation.items})
        assets_by_id = {
            a.id: a for a in (
                db.query(models.AssetType)
                .filter(models.AssetType.id.in_(asset_ids), ~models.AssetType.is_deleted)
                .with_for_update()
                .order_by(models.AssetType.id)
                .all()
            )
        }

        created_checkouts = []
        line_summaries = []
        for item in quotation.items:
            asset = assets_by_id.get(item.asset_id)
            if not asset:
                raise HTTPException(status_code=404, detail=f"An asset on this Quotation (id {item.asset_id}) no longer exists.")

            stock = recalculate_asset_stock(db, asset)
            if stock["available"] < item.quantity:
                decision = outsource_decisions.get(item.id)
                if decision is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot fulfill Quotation {quotation.reference_number}: '{asset.name}' needs {item.quantity} unit(s) but only {stock['available']} are available.",
                    )

                # PARTIAL-SHORTFALL OUTSOURCING: whatever stock genuinely IS
                # on hand for this line still gets checked out of inventory
                # normally below; only the shortfall itself (requested minus
                # available) is covered externally, optionally split across
                # more than one outsourcing company -- see the OUTSOURCING A
                # DEPLETED LINE section of this function's docstring above.
                shortfall_qty = item.quantity - stock["available"]
                allocated_qty = sum(a.quantity for a in decision.allocations)
                if allocated_qty != shortfall_qty:
                    raise HTTPException(
                        status_code=400,
                        detail=f"'{asset.name}' has a shortfall of {shortfall_qty} unit(s) ({item.quantity} needed, "
                               f"{stock['available']} available) -- the outsourcing allocation(s) for this line "
                               f"must add up to exactly {shortfall_qty}, not {allocated_qty}.",
                    )

                final_due_datetime = datetime.datetime.combine(
                    item.due_date, datetime.time.max
                ).replace(tzinfo=datetime.timezone.utc)

                # 1) Whatever's actually on hand -- a normal, inventory-backed
                # checkout for the available quantity (skipped entirely if
                # available is 0, i.e. the whole line is a shortfall).
                if stock["available"] > 0:
                    checkout = models.AssetCheckout(
                        asset_id=asset.id,
                        user_id=target_user.id if target_user else None,
                        outsider_id=target_outsider.id if target_outsider else None,
                        quantity=stock["available"], quantity_returned=0,
                        due_date=final_due_datetime, status="active", quotation_id=quotation.id,
                    )
                    db.add(checkout)
                    db.flush()
                    created_checkouts.append(checkout.id)
                    line_summaries.append(f"{stock['available']}x {asset.name}")
                    recalculate_asset_stock(db, asset)  # Available immediately drops to 0 for this asset

                # 2) The remaining shortfall -- one outsourced checkout per
                # allocation, so it can be split across different outsourcing
                # companies (or left as a single one covering the whole
                # shortfall, which is the default the Fulfillment Drawer
                # pre-fills).
                for alloc in decision.allocations:
                    outsourced_unit_price = (
                        alloc.unit_price if alloc.unit_price is not None
                        else (float(asset.price) if asset.price is not None else 0.0)
                    )
                    outsourced_checkout = models.AssetCheckout(
                        asset_id=None,
                        user_id=target_user.id if target_user else None,
                        outsider_id=target_outsider.id if target_outsider else None,
                        quantity=alloc.quantity, quantity_returned=0,
                        due_date=final_due_datetime, status="active", quotation_id=quotation.id,
                        is_outsourced=True, outsourced_item_name=asset.name,
                        outsourced_unit_price=outsourced_unit_price, outsourced_source=alloc.sourced_from,
                    )
                    db.add(outsourced_checkout)
                    db.flush()
                    created_checkouts.append(outsourced_checkout.id)
                    source_note = f" from {alloc.sourced_from}" if alloc.sourced_from else ""
                    line_summaries.append(f"{alloc.quantity}x {asset.name} (Outsourced due to stock shortage{source_note})")
                continue

            final_due_datetime = datetime.datetime.combine(
                item.due_date, datetime.time.max
            ).replace(tzinfo=datetime.timezone.utc)

            checkout = models.AssetCheckout(
                asset_id=asset.id,
                user_id=target_user.id if target_user else None,
                outsider_id=target_outsider.id if target_outsider else None,
                quantity=item.quantity, quantity_returned=0,
                due_date=final_due_datetime, status="active", quotation_id=quotation.id,
            )
            db.add(checkout)
            db.flush()
            created_checkouts.append(checkout.id)
            line_summaries.append(f"{item.quantity}x {asset.name}")

            recalculate_asset_stock(db, asset)  # Available immediately drops by item.quantity

        # --- Outsourced (not-in-inventory) lines -- see models.py's
        # QuotationOutsourcedItem docstring. No AssetType involved at all,
        # so no row-lock/stock-check/recalculate_asset_stock() step here
        # -- each becomes a real AssetCheckout with asset_id=NULL and
        # is_outsourced=True, carrying its own snapshotted name/price
        # forward since there's no catalog row left to join back to.
        for oitem in quotation.outsourced_items:
            final_due_datetime = datetime.datetime.combine(
                oitem.due_date, datetime.time.max
            ).replace(tzinfo=datetime.timezone.utc)

            checkout = models.AssetCheckout(
                asset_id=None,
                user_id=target_user.id if target_user else None,
                outsider_id=target_outsider.id if target_outsider else None,
                quantity=oitem.quantity, quantity_returned=0,
                due_date=final_due_datetime, status="active", quotation_id=quotation.id,
                is_outsourced=True, outsourced_item_name=oitem.name, outsourced_unit_price=oitem.unit_price,
                outsourced_source=oitem.sourced_from,
            )
            db.add(checkout)
            db.flush()
            created_checkouts.append(checkout.id)
            source_note = f" from {oitem.sourced_from}" if oitem.sourced_from else ""
            line_summaries.append(f"{oitem.quantity}x {oitem.name} (Outsourced{source_note})")

        quotation.status = "fulfilled"
        quotation.fulfilled_at = models.utc_now()
        # Same Super Admin FK caveat as approve_quotation() above.
        quotation.fulfilled_by_id = int(actor["sub"]) if actor["role"] != "super_admin" else None

        target_label = f"{target_user.name} ({target_user.email})" if target_user else f"{target_outsider.name} (Ad-Hoc)"
        db.add(models.AuditLog(
            operator=actor["email"], action="QUOTATION_FULFILLED", target_type="Quotation", target_id=quotation.id,
            details=f"Bulk-checked-out Quotation {quotation.reference_number} to {target_label}: "
                    f"{'; '.join(line_summaries)}.",
        ))
        db.commit()
        db.refresh(quotation)
        result = _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)
        result["checkout_ids"] = created_checkouts
        return result

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Bulk checkout failed due to an unexpected server error. No changes were made.")


# ---------------------------------------------------------------------------
# PDF export -- a printable snapshot of a Quotation (draft = "share with a
# manager offline"; submitted = the official record for a given Quote ID),
# laid out to match the business's own paper Rental Equipment Quotation
# template: a letterhead, a bordered Client Details panel, the line-item
# table, a totals box, notes, and a Terms & Conditions / Authorisation
# footer -- see services/export_service.py's build_quotation_document_pdf().
# No status change on export.
# ---------------------------------------------------------------------------

# Mirrors frontend/js/components/quotation.js's quotationStatusBadge() labels
# EXACTLY, so the PDF's "Status:" line always reads the same word a person
# just saw on-screen ("Pending Review", "Approved · Ready for Pickup", etc.)
# rather than a second, independently-drifting label vocabulary.
STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Pending Review",
    "approved": "Approved \u00b7 Ready for Pickup",
    "fulfilled": "Fulfilled",
}

_DEFAULT_TERMS = [
    "This document is a system-generated record of the Quotation/Checkout described above.",
    "Equipment must be returned in the same condition it was issued.",
    "Damage or loss will be charged at replacement cost.",
    "Rental rates exclude delivery and collection fees unless stated otherwise in Special Notes.",
]


def _quotation_rental_duration(items: list[dict]) -> str:
    """
    A single "N day(s)" summary spanning every line -- from the earliest
    start_date to the latest due_date across the whole Quotation -- mirroring
    the paper template's one "Rental Duration" field even though, unlike the
    template, each of our line items technically carries its own
    start/due date pair. Falls back to "—" for an empty (brand-new draft)
    Quotation with no lines yet.
    """
    if not items:
        return "—"
    starts = [datetime.date.fromisoformat(li["start_date"]) for li in items]
    dues = [datetime.date.fromisoformat(li["due_date"]) for li in items]
    days = _rental_days(min(starts), max(dues))
    return f"{days} day{'s' if days != 1 else ''}"


def _build_quotation_pdf(data: dict, requester_name: str):
    if not data["items"]:
        raise HTTPException(status_code=400, detail="This Quotation has no items to export.")

    currency = data["currency_code"]

    def fmt(value):
        return export_service.format_money(value, currency)

    # --- Client Details panel -------------------------------------------------
    # "Customer Name" = the User/Outsider the equipment is being checked
    # out TO: an assigned Ad-Hoc Outsider (who carries company/phone --
    # see models.py's Outsider) takes priority, then an assigned linked
    # User, then -- for a Quotation nobody has assigned a recipient to yet
    # (still Draft/Pending Review) -- the requester themself, as the only
    # person associated with the quote so far.
    #
    # "Manager" / "Fulfiller" are two SEPARATE fields (rather than one
    # "Handled By"): "Manager" is the Manager/Admin who APPROVED the
    # request (Quotation.approved_by); "Fulfiller" is the Manager/Admin
    # who physically ran the Fulfillment Drawer checkout (Quotation.
    # fulfilled_by) -- frequently a different person from the approver.
    # Each prints "—" until that step of the Quote-to-Checkout workflow
    # has actually happened (see models.py's Quotation docstring for the
    # draft -> submitted -> approved -> fulfilled lifecycle) -- a Draft/
    # Pending Review export correctly shows "—" for both, since nobody has
    # approved or fulfilled it yet.
    assigned_outsider = data.get("assigned_outsider")
    assigned_to = data.get("assigned_to")
    if assigned_outsider:
        customer_name = assigned_outsider["name"]
        company_name = assigned_outsider.get("company") or "—"
        phone_number = assigned_outsider.get("contact_details") or "—"
    elif assigned_to:
        customer_name = assigned_to["name"]
        company_name = "—"
        phone_number = "—"
    else:
        customer_name = requester_name
        company_name = "—"
        phone_number = "—"

    approved_by = data.get("approved_by")
    fulfilled_by = data.get("fulfilled_by")
    manager_name = approved_by["name"] if approved_by else "—"
    fulfiller_name = fulfilled_by["name"] if fulfilled_by else "—"

    client_fields = [
        ("Customer Name", customer_name),
        ("Company Name", company_name),
        ("Phone Number", phone_number),
        ("Manager", manager_name),
        ("Fulfiller", fulfiller_name),
        ("Rental Duration", _quotation_rental_duration(data["items"])),
        ("Delivery Address", "—"),
    ]

    # --- Line items -------------------------------------------------------
    rows = [
        [li["asset_name"], li["category"] or "—", li["quantity"], fmt(li["unit_price"]), li["days"], fmt(li["line_total"])]
        for li in data["items"]
    ]

    # --- Summary box (Subtotal / VAT / Discount / Grand Total) ------------
    discount_amount = data.get("discount_amount") or 0
    discount_label = "Discount"
    if data.get("discount_percent"):
        discount_label = f"Discount ({data['discount_percent']:.2f}%)"
    discount_display = f"-{fmt(discount_amount)}" if discount_amount else fmt(0)
    summary_rows = [
        ("Subtotal", fmt(data["subtotal"]), False),
        (f"VAT ({data['vat_percent']:.2f}%)", fmt(data["vat_amount"]), False),
        (discount_label, discount_display, False),
        ("GRAND TOTAL", fmt(data["total"]), True),
    ]

    reference_number = data.get("reference_number")
    status_label = STATUS_LABELS.get(data["status"], data["status"].title())
    today = export_service.display_now().strftime("%Y-%m-%d")

    pdf_bytes = export_service.build_quotation_document_pdf(
        site_name=settings.SITE_NAME,
        reference_number=reference_number,
        date_str=today,
        status_label=status_label,
        client_fields=client_fields,
        items=rows,
        summary_rows=summary_rows,
        notes=data.get("notes"),
        terms=_DEFAULT_TERMS,
    )
    filename_ref = reference_number or today
    doc_kind = "checkout_receipt" if data["status"] == "fulfilled" else "equipment_quotation"
    return pdf_bytes, "application/pdf", f"{doc_kind}_{filename_ref}.pdf"


def export_quotation_pdf(db: Session, user: dict):
    """Self-service export of the CALLER'S own current draft."""
    quotation = _get_or_create_draft(db, user)
    data = _serialize_quotation(db, quotation, include_admin_fields=True)
    return _build_quotation_pdf(data, user["name"])


def export_quotation_pdf_by_id(db: Session, quotation_id: int):
    """Admin/Manager export of ANY quotation (draft or submitted) by ID."""
    quotation = _get_quotation_or_404(db, quotation_id)
    data = _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)
    requester = data.get("requester")
    requester_name = requester["name"] if requester else "—"
    return _build_quotation_pdf(data, requester_name)


def export_my_quotation_pdf_by_id(db: Session, user: dict, quotation_id: int):
    """Self-service export of ONE of the caller's own submitted Quotations
    (or one an Admin/Manager assigned to them) -- the "My Quotes" history
    equivalent of export_quotation_pdf_by_id() above. Same visibility rule
    as get_my_submitted_quotation_detail() (_get_own_or_assigned_quotation_or_404()
    -- requester OR assignee, any non-draft status), and deliberately does
    NOT pass reveal_sourcing=True: a staff/customer account should never
    see WHERE an outsourced line was sourced from, same as the JSON detail
    payload this PDF mirrors."""
    quotation = _get_own_or_assigned_quotation_or_404(db, user, quotation_id)
    data = _serialize_quotation(db, quotation, include_admin_fields=True)
    # If an Admin/Manager assigned this quote to someone else, the PDF's
    # "Client" name should still read as the ORIGINAL requester (same as
    # export_quotation_pdf_by_id() above), not the assignee viewing it --
    # `data["requester"]` is always the original requester regardless of
    # who's asking (see _serialize_quotation()).
    requester = data.get("requester")
    requester_name = requester["name"] if requester else user["name"]
    return _build_quotation_pdf(data, requester_name)
