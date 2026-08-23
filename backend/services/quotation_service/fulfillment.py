"""
services/quotation/fulfillment.py
-------------------------------------
The Fulfillment Drawer: listing every approved quote awaiting physical
pickup (get_fulfillment_queue), and the stock-locked, all-or-nothing bulk
checkout transaction that turns one into real AssetCheckout rows
(bulk_checkout_quotation) -- the only point in the whole Quote-to-Checkout
workflow that ever touches inventory (see models.py's Quotation
docstring).
"""

from __future__ import annotations

import datetime
from typing import Optional

from fastapi import HTTPException

import models
from sqlalchemy.orm import Session, joinedload

from schemas.quotations_schema import QuotationOutsourceShortfallItem
from services.stock import recalculate_asset_stock

from .notifications import _notify_quotation_recipient
from .serialize import _serialize_quotation


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
    # Lock the quotation itself before checking its status. Two managers can
    # otherwise click Fulfill at nearly the same instant: both transactions
    # read `approved`, both create checkout rows, and both eventually commit.
    # Asset row locks below prevent stock overselling, but they do not prevent
    # duplicate fulfillment of the SAME quotation when the first transaction
    # has not committed yet. PostgreSQL serializes this row lock so the second
    # transaction re-reads the now-fulfilled state and is rejected.
    quotation = (
        db.query(models.Quotation)
        .options(joinedload(models.Quotation.items), joinedload(models.Quotation.outsourced_items))
        .filter(models.Quotation.id == quotation_id)
        .with_for_update()
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
        # Fulfillment is the final, most consequential step in the whole
        # lifecycle -- "your equipment is ready" -- so notify the
        # recipient here too, same rules/gating as every other quotation
        # notification (see _notify_quotation_recipient()'s own
        # docstring): only a real linked user, and never the actor
        # notifying themselves.
        _notify_quotation_recipient(
            db, quotation, actor, kind="updated",
            message=f"Quotation {quotation.reference_number} has been fulfilled and checked out to you.",
        )
        db.commit()
        db.refresh(quotation)
        result = _serialize_quotation(db, quotation, include_admin_fields=True, reveal_sourcing=True)
        result["checkout_ids"] = created_checkouts
        return result

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        from integrations.fastapi_errorbeacon import report_exception
        report_exception(exc, None, 500, component="quotation_service", operation="bulk_checkout")
        db.rollback()
        raise HTTPException(status_code=500, detail="Bulk checkout failed due to an unexpected server error. No changes were made.")
