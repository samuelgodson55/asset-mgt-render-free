"""
services/quotation/cart.py
-----------------------------
Self-service "My Order" cart: reading the caller's own draft/submitted
quotations, and every item mutation available to the requester themself
(before/after Submit -- see submit_my_quotation() and
_get_own_editable_quotation() for exactly where that self-service edit
window closes).
"""

from __future__ import annotations

from fastapi import HTTPException

import models
from sqlalchemy import or_
from sqlalchemy.orm import Session

from schemas.quotations_schema import QuotationItemCreate, QuotationItemQuantityUpdate

from .constants import MY_HISTORY_MAX_ROWS
from .helpers import _reference_number
from .serialize import _get_or_create_draft, _serialize_quotation


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
    label rows the caller didn't build themselves ("Requested by ...").

    No limit/offset query params -- this is a self-service "my history"
    panel, not a paged/searched directory -- but it's still capped at
    MY_HISTORY_MAX_ROWS server-side (newest-first, so the cap only ever
    drops the oldest rows) as a defensive ceiling: for code stability, no
    listing endpoint should be able to hand back an unbounded result set,
    even one scoped to a single account, since a long-lived power-user
    account accumulating years of submitted/assigned quotations could
    otherwise turn this into an ever-growing, unbounded query."""
    uid = int(user["sub"])
    quotations = (
        db.query(models.Quotation)
        .filter(
            or_(models.Quotation.user_id == uid, models.Quotation.assigned_to_id == uid),
            models.Quotation.status != "draft",
        )
        .order_by(models.Quotation.submitted_at.desc())
        .limit(MY_HISTORY_MAX_ROWS)
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
    #
    # SKIPPED when the quote already has an assignee -- an Admin/Manager
    # is allowed to assign their own still-building cart to a user before
    # ever hitting Submit (assign_quotation()'s `is_own_draft` branch,
    # driven by the Quotations page's "Assign Quote" action). Previously
    # this line unconditionally overwrote that pre-submission assignment
    # back to the submitter, silently discarding it: the assignee got a
    # "was assigned to you" notification, but the quote itself came back
    # assigned to whoever submitted it, so it never actually showed up on
    # the assignee's "My Quotes" tab (and re-opening it from that
    # notification 404'd, since the assignee wasn't the requester either)
    # until an Admin/Manager noticed and reassigned it a second time from
    # the Quotes tab. Only fall back to self-assignment when nobody
    # (linked user OR Ad-Hoc outsider) is already assigned.
    if not quotation.assigned_to_id and not quotation.assigned_outsider_id:
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
