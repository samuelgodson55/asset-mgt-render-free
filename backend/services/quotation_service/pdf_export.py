"""
services/quotation/pdf_export.py
------------------------------------
PDF export -- a printable snapshot of a Quotation (draft = "share with a
manager offline"; submitted = the official record for a given Quote ID),
laid out to match the business's own paper Rental Equipment Quotation
template: a letterhead, a bordered Client Details panel, the line-item
table, a totals box, notes, and a Terms & Conditions / Authorisation
footer -- see services/export_service.py's build_quotation_document_pdf().
No status change on export.
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException

from config import settings
import services.export_service as export_service
from sqlalchemy.orm import Session

from .admin import _get_quotation_or_404
from .cart import _get_own_or_assigned_quotation_or_404
from .helpers import _rental_days
from .serialize import _get_or_create_draft, _serialize_quotation

# Mirrors frontend/js/components/quotation.js's quotationStatusBadge() labels
# EXACTLY, so the PDF's "Status:" line always reads the same word a person
# just saw on-screen ("Pending Review", "Approved \u00b7 Ready for Pickup", etc.)
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
        email_address = assigned_outsider.get("email") or "—"
        phone_number = assigned_outsider.get("phone_number") or "—"
    elif assigned_to:
        customer_name = assigned_to["name"]
        company_name = "—"
        email_address = assigned_to.get("email") or "—"
        phone_number = "—"
    else:
        customer_name = requester_name
        company_name = "—"
        email_address = "—"
        phone_number = "—"

    approved_by = data.get("approved_by")
    fulfilled_by = data.get("fulfilled_by")
    manager_name = approved_by["name"] if approved_by else "—"
    fulfiller_name = fulfilled_by["name"] if fulfilled_by else "—"

    client_fields = [
        ("Customer Name", customer_name),
        ("Company Name", company_name),
        ("Email", email_address),
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
