"""
services/quotation/admin.py
-------------------------------
Admin/Manager-side quotation management: the Quotes tab's list/detail
views, every item/meta/discount mutation available once a quote has been
Submitted, assignment, approval, and marking paid. Bulk physical
fulfillment (the "Fulfillment Drawer" checkout step) lives in its own
fulfillment.py, since it's a large, separate, stock-locked transaction.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

import models
from sqlalchemy.orm import Session, joinedload

from schemas.quotations_schema import (
    QuotationAssignRequest, QuotationCreateRequest, QuotationDiscountUpdateRequest,
    QuotationItemCreate, QuotationItemQuantityUpdate, QuotationMetaUpdate,
    QuotationOutsourcedItemCreate, QuotationPaidRequest,
)

from .helpers import _money

from .constants import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from .helpers import _display_reference, _reference_number
from .notifications import _notify_quotation_recipient
from .serialize import _serialize_quotation


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
    if quotation.status == "paid":
        raise HTTPException(status_code=400, detail=f"Quotation {quotation.reference_number} has been paid and can no longer be edited.")
    if quotation.status == "draft":
        # Shouldn't normally be reachable (drafts have no reference_number
        # and aren't surfaced in the Quotes tab), but guard anyway rather
        # than letting an admin edit somebody's still-in-progress cart.
        raise HTTPException(status_code=400, detail="This Quotation hasn't been submitted yet.")

def delete_quotation(db: Session, actor: dict, quotation_id: int) -> dict:
    """Admin/Manager-only: permanently deletes a submitted or approved
    Quotation (and, via the model's cascade="all, delete-orphan", its line
    items and outsourced items with it). Gated by the SAME
    _ensure_admin_editable() lock as every other Admin/Manager mutation
    above -- a "fulfilled" quote can never be deleted, since by then it has
    real AssetCheckout rows pointing back at it via
    AssetCheckout.quotation_id (no ON DELETE cascade there, deliberately --
    see that column's docstring in models.py -- so a hard delete would
    either orphan or corrupt genuine checkout history). A "draft" is
    likewise refused, same as every other admin mutation, since it hasn't
    been submitted and isn't reachable from the Quotes tab. Deletion itself
    is a strictly stronger gate than editing (deps.require_super_admin,
    NOT deps.require_privileged_role) -- a Manager can adjust a quote but
    never delete one, only a Super Admin or Admin account can."""
    quotation = _get_quotation_or_404(db, quotation_id)
    if quotation.status not in ("submitted", "approved"):
        if quotation.status == "fulfilled":
            raise HTTPException(status_code=400, detail="A fulfilled quotation cannot be deleted because it has checkout history.")
        if quotation.status == "paid":
            raise HTTPException(status_code=400, detail="A paid quotation is a permanent financial record and cannot be deleted.")
        raise HTTPException(status_code=400, detail="Only submitted or approved quotations can be deleted.")
    reference_number = quotation.reference_number
    # Remove recipient-facing notifications first because their FK deliberately
    # does not cascade: deleting the quote must not leave orphaned notification
    # rows or fail at the database constraint. AuditLog remains as the durable
    # operator history of the deletion.
    db.query(models.QuotationNotification).filter(
        models.QuotationNotification.quotation_id == quotation.id
    ).delete(synchronize_session=False)
    db.delete(quotation)
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_DELETED", target_type="Quotation", target_id=quotation_id,
        details=f"Deleted Quotation {reference_number}.",
    ))
    db.commit()
    return {"detail": f"Quotation {reference_number} deleted."}

def list_quotations(
    db: Session, search: Optional[str] = None, status: Optional[str] = None,
    limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
) -> dict:
    """The Admin/Manager "Quotes" master queue -- every Quotation that has
    left "draft" (submitted / approved / fulfilled / paid), newest submission
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
            "fulfilled_at": serialized["fulfilled_at"],
            "paid_at": serialized["paid_at"],
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
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"{actor['email']} added/updated {asset.name} (qty {payload.quantity}) on Quotation {quotation.reference_number}.",
    )
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
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"{actor['email']} changed a line's quantity to {payload.quantity} on Quotation {quotation.reference_number}.",
    )
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
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"{actor['email']} removed a line from Quotation {quotation.reference_number}.",
    )
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
    # Notify the recipient too, same as every other line-item mutation
    # above -- but deliberately WITHOUT the word "outsourced"/"sourced
    # from" that the AuditLog entry above carries. Where this item
    # actually came from is Admin/Manager-internal bookkeeping (see
    # models.py's QuotationOutsourcedItem docstring); from the customer's
    # side it should read exactly like any other line was added, just the
    # asset name and quantity.
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"{actor['email']} added {payload.name} (qty {payload.quantity}) to Quotation {quotation.reference_number}.",
    )
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
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"{actor['email']} updated the notes on Quotation {quotation.reference_number}.",
    )
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
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"{actor['email']} changed the discount on Quotation {quotation.reference_number} to {payload.discount_percent}%.",
    )
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
        if payload.outsider_id:
            # Assign to an ad-hoc profile ALREADY on file instead of
            # creating a new one -- same "existing vs. brand new" split as
            # the Issue/Dispatch drawer's own Ad-Hoc route (see
            # services/asset_service.py's checkout_advanced()).
            outsider = db.query(models.Outsider).filter(
                models.Outsider.id == payload.outsider_id, ~models.Outsider.is_deleted,
            ).first()
            if not outsider:
                raise HTTPException(status_code=404, detail="That ad-hoc individual was not found.")
        else:
            outsider = models.Outsider(name=payload.outsider_name, email=payload.outsider_email, phone_number=payload.outsider_phone, company=payload.outsider_company)
            db.add(outsider)
            db.flush()
        assigned_outsider_id = outsider.id
        assigned_label = f"Ad-Hoc: {outsider.name} ({outsider.company or 'No Company'})"

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
    # A Manager/Admin may assign their OWN still-draft cart -- the
    # collated "My Order" quote they're actively building via the
    # Quotations page's "Add to quote" action (including from the
    # Inventory page's Asset Drawer) -- to a user right away, without
    # waiting for Submit first. This is their own in-progress request,
    # not a stranger's, so the "don't let an admin edit someone else's
    # still-in-progress cart" concern _ensure_admin_editable() guards
    # against below doesn't apply to it. Any other draft (nobody else's
    # cart should ever reach this endpoint, but guard anyway) still goes
    # through the normal admin-editable gate, same as every submitted/
    # approved/fulfilled quote.
    is_own_draft = quotation.status == "draft" and quotation.user_id == int(actor["sub"])
    if not is_own_draft:
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
        if payload.outsider_id:
            outsider = db.query(models.Outsider).filter(
                models.Outsider.id == payload.outsider_id, ~models.Outsider.is_deleted,
            ).first()
            if not outsider:
                raise HTTPException(status_code=404, detail="That ad-hoc individual was not found.")
        else:
            outsider = models.Outsider(name=payload.outsider_name, email=payload.outsider_email, phone_number=payload.outsider_phone, company=payload.outsider_company)
            db.add(outsider)
            db.flush()
        quotation.assigned_to_id = None
        quotation.assigned_outsider_id = outsider.id
        target_name = f"Ad-Hoc: {outsider.name} ({outsider.company or 'No Company'})"
    else:
        quotation.assigned_to_id = None
        quotation.assigned_outsider_id = None
        target_name = "Unassigned"

    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_ASSIGNED", target_type="Quotation", target_id=quotation.id,
        details=f"Assigned Quotation {_display_reference(quotation)} to {target_name}.",
    ))
    # Assignment is the clearest "you now have a reason to look at this"
    # moment in the whole lifecycle -- notify whoever it just landed on
    # (if that's a real linked user other than the actor themselves; see
    # _notify_quotation_recipient()'s own docstring for the exact rules).
    # `_display_reference()` (not the raw column) because this can fire
    # on a still-draft cart (`is_own_draft` above), whose `reference_number`
    # isn't set until Submit.
    _notify_quotation_recipient(
        db, quotation, actor, kind="assigned",
        message=f"Quotation {_display_reference(quotation)} was assigned to you by {actor['email']}.",
    )
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
    _notify_quotation_recipient(
        db, quotation, actor, kind="updated",
        message=f"Quotation {quotation.reference_number} was approved and is ready for pickup.",
    )
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)

def mark_quotation_paid(db: Session, actor: dict, quotation_id: int, payload: QuotationPaidRequest) -> dict:
    """Record payment only after physical fulfillment. Payment is terminal and
    deliberately cannot be reversed through the normal quotation workflow.
    """
    quotation = _get_quotation_or_404(db, quotation_id)
    if quotation.status == "paid":
        raise HTTPException(status_code=400, detail=f"Quotation {quotation.reference_number} is already marked as paid.")
    if quotation.status != "fulfilled":
        raise HTTPException(status_code=400, detail="Only fulfilled quotations can be marked as paid.")
    if payload.paid_at is not None and quotation.fulfilled_at is not None:
        paid_at = payload.paid_at
        fulfilled_at = quotation.fulfilled_at
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=datetime.timezone.utc)
        if fulfilled_at.tzinfo is None:
            fulfilled_at = fulfilled_at.replace(tzinfo=datetime.timezone.utc)
        if paid_at < fulfilled_at:
            raise HTTPException(status_code=400, detail="Payment date cannot be earlier than the fulfillment date.")

    quotation.status = "paid"
    quotation.paid_at = payload.paid_at or models.utc_now()
    quotation.paid_by_id = int(actor["sub"]) if actor.get("role") != "super_admin" else None
    quotation.payment_method = payload.payment_method
    quotation.payment_reference = payload.payment_reference
    db.add(models.AuditLog(
        operator=actor["email"], action="QUOTATION_PAID", target_type="Quotation", target_id=quotation.id,
        details=f"Marked Quotation {quotation.reference_number} as paid via {payload.payment_method}."
        + (f" Payment reference: {payload.payment_reference}." if payload.payment_reference else ""),
    ))
    db.commit()
    db.refresh(quotation)
    return _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)
