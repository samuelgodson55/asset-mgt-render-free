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
import services.user_service as user_service
from services.search_utils import apply_search_filter
from schemas.outsiders_schema import OutsiderUpdateRequest, OutsiderConvertToUserRequest

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
        models.Outsider.name, models.Outsider.email, models.Outsider.phone_number, models.Outsider.company,
    ])
    query = query.order_by(models.Outsider.id)
    total = query.count()
    outsiders = query.offset(offset).limit(limit).all()

    results = []
    for o in outsiders:
        active_checkouts = [c for c in o.checkouts if c.status == "active"]
        outstanding = sum(c.quantity - c.quantity_returned for c in active_checkouts)
        results.append({
            "id": o.id, "name": o.name, "email": o.email, "phone_number": o.phone_number,
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
    Edits an ad-hoc individual's name, email, phone number, and/or company.
    Available to both a Super Admin/Admin and a Manager (see
    deps.py's require_privileged_role, which the route sits behind), with
    no further role-based restriction beyond that -- ad-hoc profiles aren't
    tied to a system-user role the way models.User rows are, so there's
    nothing narrower to scope a Manager down to here, same reasoning as
    list_outsiders() above giving Managers the full, unscoped Ad-Hoc
    Directory.

    Only the fields actually present on the request are touched (Pydantic
    `exclude_unset`) -- omitting a field leaves it exactly as it was rather
    than blanking it out. An explicit empty string for `email`/
    `phone_number`/`company` DOES clear that field (all three are
    nullable), same as leaving it blank at ad-hoc dispatch time -- except
    that email and phone_number can never BOTH end up blank at once (a
    profile needs at least one way to be reached), checked below using
    whichever of the two isn't being cleared by this same request.
    """
    target = db.query(models.Outsider).filter(
        models.Outsider.id == outsider_id, ~models.Outsider.is_deleted
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Ad-hoc individual not found")

    updates = req.model_dump(exclude_unset=True)
    if "name" in updates:
        target.name = updates["name"]

    # Resolve what email/phone_number would look like AFTER this update,
    # so the "at least one must remain" check below sees the final state
    # rather than just the field(s) actually present on this request.
    resulting_email = updates["email"] or None if "email" in updates else target.email
    resulting_phone = updates["phone_number"] or None if "phone_number" in updates else target.phone_number
    if ("email" in updates or "phone_number" in updates) and not resulting_email and not resulting_phone:
        raise HTTPException(status_code=400, detail="At least one of email or phone number is required.")

    if "email" in updates:
        target.email = updates["email"] or None
    if "phone_number" in updates:
        target.phone_number = updates["phone_number"] or None
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
        "id": target.id, "name": target.name, "email": target.email,
        "phone_number": target.phone_number, "company": target.company,
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


def _release_own_email_if_reclaiming(db: Session, outsider: "models.Outsider", requested_email: str, actor: dict) -> None:
    """
    Called only by convert_outsider_to_user() below, right before it
    provisions a new login. Detects the specific "this ad-hoc profile
    only exists because someone's login access was revoked, and they're
    now reclaiming that exact same email" situation, and auto-releases
    the old, blocking row if so.

    WHY THIS IS SAFE TO DO AUTOMATICALLY (unlike a generic email
    override): it requires ALL of the following, not just a matching
    email string --
      1. There must be a User row whose `converted_to_outsider_id`
         points at THIS SPECIFIC outsider -- i.e. it wasn't just any
         revoked account, it's PROVABLY the very account this profile
         was created from by convert_user_to_outsider(). An unrelated
         real account that merely happens to share an email would never
         match this join.
      2. That row must still be soft-deleted (`is_deleted`) -- if it was
         somehow restored in the meantime (shouldn't normally be
         possible for a converted row -- see restore_user()'s own
         converted-account guard -- but checked here regardless), it's
         a live account and must never be touched.
      3. It must not already be purged (`purged_at IS NULL`) -- if an
         admin already purged it deliberately, its email is already a
         placeholder and won't match `requested_email` anyway, so this
         is mostly a defensive no-op in that case.
      4. The email being requested for the NEW account must match the
         old row's email (case-insensitively, same comparison
         _provision_user_row() itself uses) -- if the admin types a
         DIFFERENT email while converting this person back, there's no
         collision to resolve and nothing here should be touched.

    Only when every one of those holds do we overwrite the old row's
    email/username with a placeholder and stamp `purged_at`, using the
    exact same anonymization shape as services/user_service.py's
    purge_user() (kept in sync deliberately -- see that function's own
    docstring for the full rationale on why this never hard-deletes the
    row). The audit trail still records the original email in cleartext
    before it's overwritten, so it's discoverable later even though the
    `users` row itself no longer carries it.
    """
    origin_user = db.query(models.User).filter(
        models.User.converted_to_outsider_id == outsider.id,
        models.User.is_deleted,
        models.User.purged_at.is_(None),
    ).first()
    if not origin_user:
        return
    if origin_user.email.strip().lower() != requested_email.strip().lower():
        return

    old_email = origin_user.email
    origin_user.email = f"purged-user-{origin_user.id}@purged.invalid"
    origin_user.username = f"purged-user-{origin_user.id}"
    origin_user.purged_at = models.utc_now()

    db.add(models.AuditLog(
        operator=actor["email"], action="USER_PURGED", target_type="User", target_id=origin_user.id,
        details=(
            f"Automatically released email {old_email} from the revoked account this ad-hoc "
            f"profile was created from, so {outsider.name} could reclaim it while converting "
            f"back to a real login."
        ),
    ))
    # Flush (not commit) -- see convert_outsider_to_user()'s call site
    # comment for why this must stay uncommitted until the whole
    # conversion succeeds.
    db.flush()


def convert_outsider_to_user(db: Session, outsider_id: int, req: OutsiderConvertToUserRequest, user: dict) -> dict:
    """
    "The outsider finally decides he wants a login": turns an Ad-Hoc
    Individual (a `models.Outsider` row -- external, non-employee, no
    account) into a real, log-in-capable `models.User` row, while keeping
    every bit of their existing history intact and reachable from their
    new account. Available to the same access tier as every other
    ad-hoc-profile action (Super Admin/Admin or Manager, see
    deps.require_privileged_role) with the same Manager role ceiling a
    brand-new account provisioning gets (see
    services/user_service.py's _provision_user_row() -- a Manager still
    can't hand out "manager"/"admin" through this door either).

    SAFETY / WHAT "SAFELY MIGRATE" MEANS HERE:
      1. PROVISION FIRST, MUTATE SECOND -- _provision_user_row() runs
         (and raises) before this function touches a single checkout or
         quotation row, so a bad email/role/password never leaves the
         database in a half-migrated state. `name` is always carried
         over from the outsider's existing profile (never re-typed),
         so the new account is unambiguously "this same person, now with
         a login" rather than a fresh identity.
      2. CUSTODY HISTORY MOVES, NOT COPIES -- every AssetCheckout row
         (active AND already-returned) that pointed at
         `outsider_id=target.id` is re-pointed at `user_id=new_user.id`
         (and `outsider_id` cleared) in one bulk UPDATE, so their entire
         custody trail -- not just what's currently checked out --
         follows them into the new account and shows up in their own
         self-service "My Items"/history exactly like anyone else's,
         with nothing left orphaned under the old ad-hoc identity.
         Deliberately UNLIKE delete_outsider(): that function blocks
         while anything is still in active custody (a delete should
         never happen while equipment would lose its assignee); this
         one is the opposite case on purpose -- migrating those very
         checkouts over to a real account IS the point, so there's no
         outstanding-items block here at all.
      3. QUOTATION ASSIGNMENTS MOVE TOO -- any Quotation this person was
         the Ad-Hoc assignee of (`assigned_outsider_id`) is re-pointed at
         `assigned_to_id=new_user.id` the same way, so an already
         submitted/approved-but-not-yet-fulfilled quote still resolves to
         the right (now real) person when it's eventually
         bulk-checked-out.
      4. THE OLD AD-HOC PROFILE IS RETIRED, NOT ERASED -- same
         soft-delete flip as delete_outsider() (is_deleted/deleted_at),
         so it drops out of the Ad-Hoc Directory and can never be picked
         for a NEW dispatch/quote again (new ones should go straight to
         the real account instead), but the row itself, its name/
         contact/company, and this migration's own audit trail all stay
         queryable forever. `converted_to_user_id` (see models.Outsider)
         is the permanent link recording exactly which account it became.
      5. ONE ATOMIC COMMIT FOR THE MIGRATION STEPS -- the bulk UPDATEs,
         the outsider's soft-delete flip, and both audit log rows are
         all flushed in the SAME commit at the end (the new user row
         itself was already committed by _provision_user_row(), so a
         crash between the two calls leaves an extra, but perfectly
         valid and harmless, User row rather than any corrupted
         checkout/quotation data).

    Blocked (404, matching every other id-based outsider action) if the
    id doesn't exist, is already soft-deleted, or was already converted
    previously -- a profile that's already gone can't be converted a
    second time, exactly like it can't be deleted a second time.

    RECLAIMING YOUR OWN EMAIL (see _release_own_email_if_reclaiming()
    below) -- this Outsider may itself exist because
    services/user_service.py's convert_user_to_outsider() (login access
    revoked) turned a real account INTO it. That old account still
    holds its original email forever (soft-deleting never frees a
    unique email/username -- see User.purged_at's comment), so without
    special handling, converting this same person straight back to a
    user with that same email would hit _provision_user_row()'s
    existing-email check and fail with "a user with this email already
    exists" -- even though it's unambiguously their own address. Before
    provisioning, we check for exactly that situation (a soft-deleted,
    not-yet-purged User row whose `converted_to_outsider_id` points at
    THIS outsider, with the SAME email being requested) and silently
    release it first, the same way a manual Purge would. Any OTHER
    email collision (a genuinely different, unrelated account) is left
    alone and still correctly blocks the conversion.
    """
    target = db.query(models.Outsider).filter(
        models.Outsider.id == outsider_id, ~models.Outsider.is_deleted
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Ad-hoc individual not found")

    # See docstring's "RECLAIMING YOUR OWN EMAIL" section above -- must
    # run BEFORE _provision_user_row()'s uniqueness check below, and
    # before it commits, so the flushed (uncommitted) release is visible
    # to that very check within this same transaction, but rolls back
    # automatically if anything about THIS request (bad role, weak
    # password, a genuinely different email collision) still fails.
    _release_own_email_if_reclaiming(db, target, req.email, user)

    # Provision the real account FIRST -- see docstring point #1. Raises
    # (400/403) before anything about `target` or its checkouts/
    # quotations is touched if the role is disallowed or the email is
    # already taken.
    new_user = user_service._provision_user_row(
        db, name=target.name, email=req.email, phone_number=(req.phone_number or target.phone_number),
        role=req.role, password=req.password,
        department=req.department, department_role=req.department_role, actor=user,
    )

    # How many of the checkouts about to be migrated are still ACTIVE
    # (equipment genuinely out in this person's hands right now) vs
    # already RETURNED (pure history) -- must be counted BEFORE the bulk
    # UPDATE below, since that update clears `outsider_id` on every
    # matching row and there'd be nothing left to distinguish them by
    # afterward. This is what lets the audit/response wording below say
    # "0 active" instead of just "1 checkout(s)", which on its own reads
    # as "this person currently has equipment out" even when every
    # migrated row is a long-since-returned historical record.
    active_checkouts_count = (
        db.query(models.AssetCheckout)
        .filter(models.AssetCheckout.outsider_id == target.id, models.AssetCheckout.status == "active")
        .count()
    )

    # Move every checkout (active AND historical) over to the new
    # account in one bulk UPDATE -- see docstring point #2. Deliberately
    # a raw bulk update (not a Python loop mutating loaded objects) so
    # this scales to however many checkouts this profile has accumulated
    # without loading them all into memory first; `synchronize_session`
    # is safe to skip here since nothing later in this function re-reads
    # any individual AssetCheckout object.
    checkouts_migrated = (
        db.query(models.AssetCheckout)
        .filter(models.AssetCheckout.outsider_id == target.id)
        .update({"outsider_id": None, "user_id": new_user.id}, synchronize_session=False)
    )

    # Human-readable breakdown used in both the audit log detail and the
    # response message below -- "1 checkout(s)" alone reads as "they had
    # equipment out at the time", which is only true if some of those
    # rows were actually still active.
    if checkouts_migrated:
        checkout_detail = (
            f"{checkouts_migrated} checkout(s) ({active_checkouts_count} still active, "
            f"{checkouts_migrated - active_checkouts_count} already returned)"
        )
    else:
        checkout_detail = "0 checkout(s)"

    # Same treatment for any Quotation this profile was the Ad-Hoc
    # assignee of -- see docstring point #3.
    quotations_migrated = (
        db.query(models.Quotation)
        .filter(models.Quotation.assigned_outsider_id == target.id)
        .update({"assigned_outsider_id": None, "assigned_to_id": new_user.id}, synchronize_session=False)
    )

    # Retire the ad-hoc profile -- see docstring point #4.
    target.is_deleted = True
    target.deleted_at = models.utc_now()
    target.converted_to_user_id = new_user.id

    db.add(models.AuditLog(
        operator=user["email"], action="OUTSIDER_CONVERTED_TO_USER", target_type="Outsider", target_id=target.id,
        details=(
            f"Converted ad-hoc profile for {target.name} into a real login "
            f"(user #{new_user.id}, {new_user.email}). Migrated {checkout_detail} "
            f"and {quotations_migrated} quotation assignment(s)."
        ),
    ))
    db.add(models.AuditLog(
        operator=user["email"], action="USER_PROVISIONED_FROM_OUTSIDER", target_type="User", target_id=new_user.id,
        details=f"Account created from ad-hoc profile #{target.id} ({target.name}).",
    ))
    db.commit()
    db.refresh(new_user)

    return {
        "message": (
            f"{target.name} now has a real login ({new_user.email}). "
            f"{checkout_detail} and {quotations_migrated} quotation "
            f"assignment(s) were moved to their new account."
        ),
        "outsider_id": target.id,
        "user_id": new_user.id,
        "name": new_user.name,
        "email": new_user.email,
        "username": new_user.username,
        "role": new_user.role,
        "checkouts_migrated": checkouts_migrated,
        "quotations_migrated": quotations_migrated,
    }


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
        "outsider_id": target.id, "name": target.name, "email": target.email,
        "phone_number": target.phone_number, "company": target.company, "assigned_items": items,
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
    subtitle_bits = [data["name"]]
    if data["email"]:
        subtitle_bits.append(data["email"])
    if data["phone_number"]:
        subtitle_bits.append(data["phone_number"])
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

    headers = ["Individual", "Email", "Phone", "Company", "Asset", "Category", "Vendor / Source", "Quantity", "Outstanding", "Checked Out", "Due Date"]
    rows = []
    for o in outsiders:
        for c in o.checkouts:
            if c.status != "active":
                continue
            rows.append([
                o.name, o.email or "—", o.phone_number or "—", o.company or "—",
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
