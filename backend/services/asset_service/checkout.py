"""
services/asset/checkout.py
------------------------------
Bulk reconciliation check-in (new physical units found and added to a
pool) and the advanced dispatch/issue flow (checkout_advanced) that lets
a Manager/Super Admin check equipment out to Staff, a linked Customer
account, or an Ad-Hoc/Unlinked outsider.
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from models import utc_now
from schemas.assets_schema import AdvancedCheckoutRequest
from services.stock import recalculate_asset_stock


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
