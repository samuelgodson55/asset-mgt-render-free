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
from schemas.outsiders import OutsiderUpdateRequest

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

    # Same "excludes soft-deleted rows" pattern as user_service.list_users()
    # -- a deleted ad-hoc profile disappears from the directory (and from
    # every "existing outsider" picker in the Dispatch drawer / Quote
    # creation, both of which call this same GET /outsiders under the
    # hood) but its historical checkout rows are untouched.
    query = db.query(models.Outsider).filter(~models.Outsider.is_deleted)
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


def update_outsider(db: Session, outsider_id: int, req: OutsiderUpdateRequest, user: dict) -> dict:
    """
    Edits an ad-hoc individual's name, contact details, and/or company.
    Available to both a Super Admin/Admin and a Manager (see
    deps.py's require_privileged_role, which the route sits behind), with
    no further role-based restriction beyond that -- ad-hoc profiles aren't
    tied to a system-user role the way models.User rows are, so there's
    nothing narrower to scope a Manager down to here, same reasoning as
    list_outsiders() above giving Managers the full, unscoped Ad-Hoc
    Directory.

    Only the fields actually present on the request are touched (Pydantic
    `exclude_unset`) -- omitting a field leaves it exactly as it was rather
    than blanking it out. An explicit empty string for `company` DOES clear
    it (company is nullable), same as leaving it blank at ad-hoc dispatch
    time.
    """
    target = db.query(models.Outsider).filter(
        models.Outsider.id == outsider_id, ~models.Outsider.is_deleted
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Ad-hoc individual not found")

    updates = req.model_dump(exclude_unset=True)
    if "name" in updates:
        target.name = updates["name"]
    if "contact_details" in updates:
        target.contact_details = updates["contact_details"]
    if "company" in updates:
        target.company = updates["company"] or None

    db.add(models.AuditLog(
        operator=user["email"], action="OUTSIDER_UPDATED", target_type="Outsider", target_id=target.id,
        details=f"Updated ad-hoc profile for {target.name}.",
    ))
    db.commit()
    db.refresh(target)
    return {
        "message": f"Profile for {target.name} updated successfully.",
        "id": target.id, "name": target.name, "contact_details": target.contact_details, "company": target.company,
    }


def delete_outsider(db: Session, outsider_id: int, user: dict) -> dict:
    """
    Deletes an ad-hoc/unlinked profile. Available to both a Super
    Admin/Admin and a Manager -- same access tier as update_outsider()
    above, and same reasoning: ad-hoc profiles aren't tied to a
    system-user role, so there's no narrower boundary to enforce.

    Mirrors services/user_service.py's delete_user() closely:
      1. SOFT DELETE ONLY -- see models.Outsider.is_deleted's own comment
         for why the row itself is never removed.
      2. OUTSTANDING-CUSTODY BLOCK -- a profile that still has items
         checked out to it cannot be deleted until those items are
         returned, so a piece of equipment can never end up assigned to a
         profile that's no longer selectable/visible anywhere in the app.
      3. ALREADY-DELETED BLOCK -- the same 404 as a genuinely-missing id;
         a profile that's already gone can't be deleted a second time.
    """
    target = db.query(models.Outsider).filter(
        models.Outsider.id == outsider_id, ~models.Outsider.is_deleted
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Ad-hoc individual not found")

    outstanding_items = sum(
        c.quantity - c.quantity_returned for c in target.checkouts if c.status == "active"
    )
    if outstanding_items > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: this ad-hoc individual still has {outstanding_items} item(s) in active custody. Process their returns first.",
        )

    target.is_deleted = True
    target.deleted_at = models.utc_now()

    db.add(models.AuditLog(
        operator=user["email"], action="OUTSIDER_DELETED", target_type="Outsider", target_id=target.id,
        details=f"Deleted ad-hoc profile for {target.name} (checkout history preserved).",
    ))
    db.commit()
    return {"message": f"Ad-hoc profile for {target.name} deleted successfully."}


def _pending_extension_fields(checkout: "models.AssetCheckout") -> dict:
    """Same as user_service.py's helper of the same name -- see that
    docstring for the full rationale (Custody Ledger drawer Approve/Deny
    on the specific pending request, instead of just "Extend")."""
    pending = next((er for er in checkout.extension_requests if er.status == "pending"), None)
    if not pending:
        return {"pending_extension_request_id": None, "pending_extension_new_due_date": None, "pending_extension_reason": None}
    return {
        "pending_extension_request_id": pending.id,
        "pending_extension_new_due_date": pending.requested_new_due_date.strftime("%Y-%m-%d") if pending.requested_new_due_date else None,
        "pending_extension_reason": pending.reason,
    }


def get_outsider_assigned_items(db: Session, outsider_id: int) -> dict:
    """Ad-hoc equivalent of user_service.get_user_assigned_items -- powers their Custody Ledger modal."""
    target = db.query(models.Outsider).filter(models.Outsider.id == outsider_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Ad-hoc individual not found")

    active_checkouts = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.outsider_id == outsider_id, models.AssetCheckout.status == "active"
    ).all()
    items = [{
        "checkout_id": c.id, "asset_name": models.checkout_display_name(c),
        "asset_category": c.asset.category if c.asset else None,
        # This ledger is ALWAYS Manager/Admin-only (Ad-Hoc individuals have
        # no login of their own to view it self-service) -- see
        # models.py's AssetCheckout comment for the visibility rule this
        # mirrors on the linked-User side (get_user_assigned_items()).
        "is_outsourced": c.is_outsourced,
        "outsourced_source": c.outsourced_source,
        "quantity": c.quantity, "quantity_returned": c.quantity_returned, "outstanding": c.quantity - c.quantity_returned,
        # TIMEZONE FIX -- see services/user_service.py's
        # get_my_assigned_items() for the full explanation of why this is
        # `.isoformat()` and not a pre-formatted `.strftime(...)` string.
        "checkout_date": c.checkout_date.isoformat() if c.checkout_date else None,
        "due_date": c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
        "due_soon": models.is_due_soon(c.due_date),
        "overdue": models.is_overdue(c.due_date),
        "pending_extension": any(er.status == "pending" for er in c.extension_requests),
        **_pending_extension_fields(c),
        "vendor_sources": [c.outsourced_source] if c.is_outsourced and c.outsourced_source else [],
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
_ITEM_EXPORT_HEADERS = ["Asset", "Category", "Vendor / Source", "Quantity", "Quantity Returned", "Outstanding", "Checked Out", "Due Date"]


def _format_export_datetime(iso_string: Optional[str]) -> str:
    """
    Mirrors services/user_service.py's `_format_export_datetime()` -- turns
    a `.isoformat()` checkout_date back into a friendly export string.
    Delegates to services/export_service.py's format_export_datetime(), so
    this renders in the same configured DISPLAY_TIMEZONE, with the same
    real zone abbreviation, as every other exporter in the app.
    """
    return export_service.format_export_datetime(iso_string)


def _item_export_rows(items: list) -> list:
    """Turns the `assigned_items` list shape (see get_outsider_assigned_items
    above) into plain rows for export_service.build_csv_bytes()/build_pdf_bytes()."""
    return [
        [
            i["asset_name"],
            i.get("asset_category") or "—",
            ", ".join(i.get("vendor_sources") or []) if i.get("vendor_sources") else "—",
            i["quantity"],
            i["quantity_returned"],
            i["outstanding"],
            _format_export_datetime(i["checkout_date"]),
            i["due_date"],
        ]
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
    outsiders = db.query(models.Outsider).filter(~models.Outsider.is_deleted).order_by(models.Outsider.id).all()

    headers = ["Individual", "Contact", "Company", "Asset", "Category", "Vendor / Source", "Quantity", "Outstanding", "Checked Out", "Due Date"]
    rows = []
    for o in outsiders:
        for c in o.checkouts:
            if c.status != "active":
                continue
            rows.append([
                o.name, o.contact_details, o.company or "—",
                models.checkout_display_name(c),
                c.asset.category if c.asset and c.asset.category else "—",
                c.quantity, c.quantity - c.quantity_returned,
                export_service.format_export_datetime(c.checkout_date),
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
