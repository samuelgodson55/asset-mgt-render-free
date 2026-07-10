"""
services/checkout_service.py
------------------------------
Processing a return against an existing AssetCheckout row, and listing
overdue checkouts for dashboard alerts. Used by api/checkouts.py.
"""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from models import utc_now
from schemas.checkouts import ReturnRequest
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
            f"Processed return of {req.quantity} unit(s) of '{asset.name}' from {holder_label} "
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
        elif c.outsider:
            assignee_name, assignee_type = f"{c.outsider.name} ({c.outsider.company or 'No Company'})", "External Outsider"
        else:
            assignee_name, assignee_type = "Unknown", "Unknown"

        days_overdue = (now - c.due_date).days
        items.append({
            "checkout_id": c.id,
            "asset_id": c.asset_id,
            "asset_name": c.asset.name if c.asset else "Unknown Asset",
            "assignee_name": assignee_name,
            "assignee_type": assignee_type,
            "quantity": c.quantity,
            "outstanding": c.quantity - c.quantity_returned,
            "due_date": c.due_date.strftime("%Y-%m-%d"),
            "days_overdue": max(days_overdue, 0),
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}
