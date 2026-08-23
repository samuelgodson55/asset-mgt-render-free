"""
services/quotation/serialize.py
----------------------------------
The single shared shaping function for a Quotation -> API response dict
(`_serialize_quotation`), used by every read/write endpoint across
self-service, admin, and fulfillment. Also `_get_or_create_draft`, which
every self-service cart mutation starts from.
"""

from __future__ import annotations

from decimal import Decimal

import models
from config import settings
from sqlalchemy.orm import Session, joinedload

from .catalog import get_vat_percent
from .helpers import _money, _outsider_brief, _rental_days, _user_brief


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
            "department": asset.department if asset else None,
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
        "locked": quotation.status == "paid",
        "reference_number": quotation.reference_number,
        "submitted_at": quotation.submitted_at.isoformat() if quotation.submitted_at else None,
        "approved_at": quotation.approved_at.isoformat() if quotation.approved_at else None,
        "fulfilled_at": quotation.fulfilled_at.isoformat() if quotation.fulfilled_at else None,
        "paid_at": quotation.paid_at.isoformat() if quotation.paid_at else None,
        # Payment method/reference are internal reconciliation details. The
        # requester still sees the terminal Paid status and timestamp, while
        # only privileged callers receive the reconciliation fields below.
        "payment_method": None,
        "payment_reference": None,
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
        result["paid_by"] = _user_brief(quotation.paid_by)
        result["payment_method"] = quotation.payment_method
        result["payment_reference"] = quotation.payment_reference
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
