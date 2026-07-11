"""
services/outsider_service.py
------------------------------
"Ad-Hoc Individuals (Unlinked)" are external people who receive equipment
without ever holding a full system account (the `models.Outsider` table --
see models.py's module docstring for how this differs from the
login-capable `role="customer"` User). These mirror the equivalent
user_service functions so the frontend can drive an identical Custody
Ledger experience for them. Used by api/outsiders.py.
"""

import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
import services.export_service as export_service
from services.search_utils import apply_search_filter

# Same reasoning as user_service.DEFAULT_LIMIT/MAX_LIMIT -- bounds how many
# ad-hoc profiles a single request can return (Data Quality & Usability
# requirement #4).
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000


def list_outsiders(db: Session, limit: int = DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None) -> dict:
    """
    Lists every ad-hoc/unlinked individual who currently has (or has ever
    had) items dispatched to them, along with how many units are presently
    outstanding in their custody. Available to Super Admins and Managers,
    same access tier as the regular User Directory (see manager.html's
    "Ad-Hoc Individuals" tab, added alongside "Team Allocation Matrix").

    PAGINATION + SEARCH: same pattern as user_service.list_users -- `search`
    (when present) narrows the directory to rows where name, contact
    details, or company case-insensitively contains it (the same fields
    the Ad-Hoc Directory table's search box has always searched by, see
    js/components/outsiders.js), applied and counted BEFORE slicing, then
    the (per-row) `outstanding_items` aggregation is only computed for the
    page actually being returned.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    query = db.query(models.Outsider)
    query = apply_search_filter(query, search, [
        models.Outsider.name, models.Outsider.contact_details, models.Outsider.company,
    ])
    query = query.order_by(models.Outsider.id)
    total = query.count()
    outsiders = query.offset(offset).limit(limit).all()

    results = []
    for o in outsiders:
        active_checkouts = [c for c in o.checkouts if c.status == "active"]
        outstanding = sum(c.quantity - c.quantity_returned for c in active_checkouts)
        results.append({
            "id": o.id, "name": o.name, "contact_details": o.contact_details,
            "company": o.company, "outstanding_items": outstanding,
            # Same per-person (not per-item) alert flags as
            # user_service.list_users() -- see that function's comment for
            # the full rationale.
            "alerts": {
                "overdue": any(models.is_overdue(c.due_date) for c in active_checkouts),
                "due_soon": any(models.is_due_soon(c.due_date) for c in active_checkouts),
                "pending_extension": any(
                    er.status == "pending" for c in active_checkouts for er in c.extension_requests
                ),
            },
        })
    return {"items": results, "total": total, "limit": limit, "offset": offset}


def get_outsider_assigned_items(db: Session, outsider_id: int) -> dict:
    """Ad-hoc equivalent of user_service.get_user_assigned_items -- powers their Custody Ledger modal."""
    target = db.query(models.Outsider).filter(models.Outsider.id == outsider_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Ad-hoc individual not found")

    active_checkouts = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.outsider_id == outsider_id, models.AssetCheckout.status == "active"
    ).all()
    items = [{
        "checkout_id": c.id, "asset_name": c.asset.name if c.asset else "Unknown Asset",
        "asset_department": c.asset.department if c.asset else None,
        "quantity": c.quantity, "quantity_returned": c.quantity_returned, "outstanding": c.quantity - c.quantity_returned,
        # TIMEZONE FIX -- see services/user_service.py's
        # get_my_assigned_items() for the full explanation of why this is
        # `.isoformat()` and not a pre-formatted `.strftime(...)` string.
        "checkout_date": c.checkout_date.isoformat() if c.checkout_date else None,
        "due_date": c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
        "due_soon": models.is_due_soon(c.due_date),
        "overdue": models.is_overdue(c.due_date),
        "pending_extension": any(er.status == "pending" for er in c.extension_requests),
    } for c in active_checkouts]

    return {
        "outsider_id": target.id, "name": target.name, "contact_details": target.contact_details,
        "company": target.company, "assigned_items": items,
    }


# ---------------------------------------------------------------------------
# PROPERTIES-ASSIGNED EXPORTS (CSV / PDF)
# ---------------------------------------------------------------------------
# Mirrors user_service.py's export_user_assigned_items()/
# export_all_users_items() for the Ad-Hoc (Unlinked) Directory. NOTE:
# neither of these scopes by department -- list_outsiders() above already
# shows every ad-hoc profile to any Super Admin OR Manager alike (outsiders
# aren't tied to a department at all), so "ad-hoc individuals under a
# Manager's purview" means the same thing here as it does everywhere else
# in this codebase: every ad-hoc profile in the system.
_ITEM_EXPORT_HEADERS = ["Asset", "Department", "Quantity", "Quantity Returned", "Outstanding", "Checked Out", "Due Date"]


def _format_export_datetime(iso_string: Optional[str]) -> str:
    """
    Mirrors services/user_service.py's `_format_export_datetime()` -- turns
    a `.isoformat()` checkout_date back into a friendly, explicitly-UTC
    string for a CSV/PDF export cell (a static file has no browser to
    localize it into the viewer's own timezone the way the live UI does).
    """
    if not iso_string:
        return ""
    try:
        return datetime.datetime.fromisoformat(iso_string).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return iso_string  # unparseable -- surface it as-is rather than silently dropping it


def _item_export_rows(items: list) -> list:
    """Turns the `assigned_items` list shape (see get_outsider_assigned_items
    above) into plain rows for export_service.build_csv_bytes()/build_pdf_bytes()."""
    return [
        [i["asset_name"], i.get("asset_department") or "—", i["quantity"], i["quantity_returned"], i["outstanding"], _format_export_datetime(i["checkout_date"]), i["due_date"]]
        for i in items
    ]


def export_outsider_assigned_items(db: Session, outsider_id: int, user: dict, fmt: str = "csv"):
    """Privileged export of one specific ad-hoc individual's custody ledger (Super Admin or Manager)."""
    data = get_outsider_assigned_items(db, outsider_id)
    rows = _item_export_rows(data["assigned_items"])
    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"Properties Assigned To {data['name']} (Ad-Hoc)"
    subtitle_bits = [data["name"], data["contact_details"]]
    if data["company"]:
        subtitle_bits.append(data["company"])
    subtitle = f"{' · '.join(subtitle_bits)} · Exported by {user['email']}"

    if fmt == "pdf":
        pdf_bytes = export_service.build_pdf_bytes(title, subtitle, _ITEM_EXPORT_HEADERS, rows)
        return pdf_bytes, "application/pdf", f"outsider_{outsider_id}_properties_{today}.pdf"
    csv_bytes = export_service.build_csv_bytes(_ITEM_EXPORT_HEADERS, rows)
    return csv_bytes, "text/csv", f"outsider_{outsider_id}_properties_{today}.csv"


def export_all_outsiders_items(db: Session, user: dict, fmt: str = "csv"):
    """
    Bulk export: every currently-ACTIVE checkout across every ad-hoc
    individual on file, one row per checkout (same "one row per checkout,
    not one row per profile" shape as user_service.export_all_users_items).
    """
    outsiders = db.query(models.Outsider).order_by(models.Outsider.id).all()

    headers = ["Individual", "Contact", "Company", "Asset", "Department", "Quantity", "Outstanding", "Checked Out", "Due Date"]
    rows = []
    for o in outsiders:
        for c in o.checkouts:
            if c.status != "active":
                continue
            rows.append([
                o.name, o.contact_details, o.company or "—",
                c.asset.name if c.asset else "Unknown Asset",
                c.asset.department if c.asset and c.asset.department else "—",
                c.quantity, c.quantity - c.quantity_returned,
                c.checkout_date.strftime("%Y-%m-%d %H:%M:%S UTC") if c.checkout_date else "",
                c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
            ])

    today = datetime.date.today().strftime("%Y-%m-%d")
    title = "Properties Assigned — All Ad-Hoc Individuals"
    subtitle = f"Exported by {user['email']} · {len(rows)} active checkout(s) across {len(outsiders)} profile(s)"
    if fmt == "pdf":
        pdf_bytes = export_service.build_pdf_bytes(title, subtitle, headers, rows)
        return pdf_bytes, "application/pdf", f"all_outsiders_properties_{today}.pdf"
    csv_bytes = export_service.build_csv_bytes(headers, rows)
    return csv_bytes, "text/csv", f"all_outsiders_properties_{today}.csv"
