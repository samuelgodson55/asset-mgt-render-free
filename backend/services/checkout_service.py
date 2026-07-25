"""
services/checkout_service.py
------------------------------
Processing a return against an existing AssetCheckout row, and listing
overdue / soon-to-be-due checkouts for dashboard alerts. Used by
api/checkouts.py.
"""

import datetime
import logging
import math

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from models import utc_now
from config import settings
from schemas.checkouts_schema import ReturnRequest
from services.stock import recalculate_asset_stock

logger = logging.getLogger(__name__)

# Same pagination reasoning as every other listing endpoint in this project
# (see services/user_service.py's DEFAULT_LIMIT/MAX_LIMIT comment) -- caps
# how many overdue rows a single request can return.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def return_checkout(db: Session, checkout_id: int, req: ReturnRequest, user: dict) -> dict:
    """
    Processes a QUANTIFIED (partial or full) return of ONE active checkout
    record (used by the Custody Ledger and the Active Field Deployments
    Ledger 'Process Return' buttons, for both regular Users and Ad-Hoc
    Outsiders alike). The caller specifies exactly how many units are
    coming back right now, instead of all-or-nothing.

    Validation: 0 < req.quantity <= outstanding (quantity - quantity_returned).
    Only Super Admins and Managers can process returns -- staff/customers get
    a read-only view of their own custody on their self-service dashboards.
    """
    checkout = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.id == checkout_id, models.AssetCheckout.status == "active"
    ).first()
    if not checkout:
        raise HTTPException(status_code=404, detail="Active checkout record not found.")

    outstanding = checkout.quantity - checkout.quantity_returned
    if req.quantity > outstanding:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot return {req.quantity} unit(s) -- only {outstanding} unit(s) are currently outstanding on this checkout.",
        )

    asset = checkout.asset
    checkout.quantity_returned += req.quantity

    # Fully settled once every outstanding unit has come back.
    if checkout.quantity_returned >= checkout.quantity:
        checkout.status = "returned"
        checkout.returned_at = utc_now()

    # OUTSOURCED checkouts have no real AssetType behind them (asset_id is
    # NULL -- see models.py's AssetCheckout comment), so there's no
    # inventory stock to recalculate.
    if asset is not None:
        recalculate_asset_stock(db, asset)  # Outbound drops, Available rises by req.quantity

    # AUDIT TRAIL: identify WHO the equipment is being returned FROM (the
    # custodian), not just who (the Super Admin/Manager) clicked "Process
    # Return" -- `operator` above already covers that. A checkout is always
    # against EITHER a linked system User OR an unlinked ad-hoc Outsider
    # (never both, never neither -- see models.AssetCheckout), so both
    # branches are covered here.
    if checkout.user:
        holder_label = f"{checkout.user.name} ({checkout.user.email})"
    elif checkout.outsider:
        holder_label = f"{checkout.outsider.name} (Ad-Hoc/Unlinked" + (f" · {checkout.outsider.company}" if checkout.outsider.company else "") + ")"
    else:
        holder_label = "Unknown holder"  # should be unreachable given the DB constraints, but never crash an audit write over it

    db.add(models.AuditLog(
        operator=user["email"], action="CHECKIN_RETURN", target_type="AssetCheckout", target_id=checkout.id,
        details=(
            f"Processed return of {req.quantity} unit(s) of '{models.checkout_display_name(checkout)}' from {holder_label} "
            f"({checkout.quantity - checkout.quantity_returned} still outstanding on this checkout)."
        ),
    ))
    db.commit()
    logger.info(
        "Checkout return processed",
        extra={"user": user["email"], "checkout_id": checkout.id, "quantity_returned": req.quantity, "checkout_status": checkout.status},
    )
    return {
        "message": f"Successfully returned {req.quantity} unit(s).",
        "outstanding": checkout.quantity - checkout.quantity_returned,
        "checkout_status": checkout.status,
    }


def list_overdue_checkouts(db: Session, user: dict, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """
    Powers GET /checkouts/overdue (Operations & Observability requirement
    #5) -- the data behind a "Dashboard Alerts" banner on admin.html /
    manager.html that flags active checkouts whose `due_date` has already
    passed, so Super Admins/Managers notice overdue equipment without
    having to hunt for it inside every asset pool's Properties Hub one by
    one.

    "Overdue" here means: status == "active" (not yet returned) AND
    due_date is not null AND due_date is strictly in the past compared to
    right now (`models.utc_now()`). A checkout with no due_date at all is
    intentionally never considered overdue -- it was checked out
    open-ended on purpose (see AdvancedCheckoutRequest.due_date being
    Optional).

    SCOPING: both Super Admins and Managers see every overdue checkout
    system-wide -- Managers no longer have department-scoping anywhere in
    this app.

    Sorted with the MOST overdue item first (oldest due_date first) since
    that's usually the most urgent thing to chase down.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    now = utc_now()

    query = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.status == "active",
        models.AssetCheckout.due_date.isnot(None),
        models.AssetCheckout.due_date < now,
    )

    total = query.count()
    overdue = query.order_by(models.AssetCheckout.due_date.asc()).offset(offset).limit(limit).all()

    items = []
    for c in overdue:
        if c.user:
            assignee_name, assignee_type = c.user.name, c.user.role.capitalize()
            entity_id, entity_type = c.user.id, "user"
        elif c.outsider:
            assignee_name, assignee_type = f"{c.outsider.name} ({c.outsider.company or 'No Company'})", "External Outsider"
            entity_id, entity_type = c.outsider.id, "outsider"
        else:
            assignee_name, assignee_type = "Unknown", "Unknown"
            entity_id, entity_type = None, None

        days_overdue = (now - c.due_date).days
        items.append({
            "checkout_id": c.id,
            "asset_id": c.asset_id,
            "asset_name": models.checkout_display_name(c),
            "is_outsourced": c.is_outsourced,
            "assignee_name": assignee_name,
            "assignee_type": assignee_type,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "quantity": c.quantity,
            "outstanding": c.quantity - c.quantity_returned,
            "due_date": c.due_date.strftime("%Y-%m-%d"),
            "days_overdue": max(days_overdue, 0),
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def list_due_soon_checkouts(db: Session, user: dict, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """
    Powers GET /checkouts/due-soon -- the proactive counterpart to
    list_overdue_checkouts() above: a "Due Soon" dashboard banner on
    admin.html/manager.html that flags active checkouts approaching their
    due date BEFORE they actually go overdue, so a Super Admin/Manager can
    chase down a return (or grant an extension) ahead of time instead of
    only ever finding out after the fact.

    "Due soon" here means: status == "active" (not yet returned) AND
    due_date is not null AND due_date is still in the future AND due_date
    falls within `settings.DUE_SOON_REMINDER_DAYS` days from now. An
    already-overdue checkout is deliberately EXCLUDED (that's what the
    "Overdue" banner is for) -- the two lists are mutually exclusive by
    construction, on either side of `now`, so nothing ever double-counts
    across both banners. A checkout with no due_date at all is, same as
    for "overdue", never considered due soon either -- it was checked out
    open-ended on purpose.

    SCOPING: both Super Admins and Managers see every due-soon checkout
    system-wide -- Managers no longer have department-scoping anywhere in
    this app (same as list_overdue_checkouts() above).

    Sorted with the SOONEST-due item first, since that's the most urgent
    one to act on.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    now = utc_now()
    horizon = now + datetime.timedelta(days=settings.DUE_SOON_REMINDER_DAYS)

    query = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.status == "active",
        models.AssetCheckout.due_date.isnot(None),
        models.AssetCheckout.due_date >= now,
        models.AssetCheckout.due_date <= horizon,
    )

    total = query.count()
    due_soon = query.order_by(models.AssetCheckout.due_date.asc()).offset(offset).limit(limit).all()

    items = []
    for c in due_soon:
        if c.user:
            assignee_name, assignee_type = c.user.name, c.user.role.capitalize()
            entity_id, entity_type = c.user.id, "user"
        elif c.outsider:
            assignee_name, assignee_type = f"{c.outsider.name} ({c.outsider.company or 'No Company'})", "External Outsider"
            entity_id, entity_type = c.outsider.id, "outsider"
        else:
            assignee_name, assignee_type = "Unknown", "Unknown"
            entity_id, entity_type = None, None

        # Ceiling division on the remaining time, not floor: something due
        # in 6 hours should read "due in 1 day", not "due in 0 days"
        # (which would misleadingly read as if it were already overdue).
        remaining = c.due_date - now
        days_until_due = max(1, math.ceil(remaining.total_seconds() / 86400))
        items.append({
            "checkout_id": c.id,
            "asset_id": c.asset_id,
            "asset_name": models.checkout_display_name(c),
            "is_outsourced": c.is_outsourced,
            "assignee_name": assignee_name,
            "assignee_type": assignee_type,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "quantity": c.quantity,
            "outstanding": c.quantity - c.quantity_returned,
            "due_date": c.due_date.strftime("%Y-%m-%d"),
            "days_until_due": days_until_due,
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}
