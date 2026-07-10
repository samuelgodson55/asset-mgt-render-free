"""
services/user_service.py
--------------------------
System-user account CRUD and self-service/custody lookups. Used by
api/users.py.
"""

from typing import Optional
import datetime
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

import models
from models import utc_now
from config import settings
from security import hash_password, verify_password, SUPER_ADMIN_ID, SUPER_ADMIN_ROLE, SUPER_ADMIN_PASSWORD_HASH
from schemas.users import UserCreateRequest
import services.export_service as export_service
from services.search_utils import apply_search_filter

# Roles a Manager is allowed to hand out when provisioning a new login.
# A Manager can create Staff and Customer accounts, but must NEVER be able
# to create another Manager or an Admin account for themselves -- that
# would be an easy privilege-escalation hole. Admins/Super Admin are not
# limited by this list (checked in create_user below).
MANAGER_PROVISIONABLE_ROLES = ("staff", "customer")

# "super_admin" is reserved for the single hardcoded root identity (see
# security.py's super_admin_principal()) -- it is never a valid role for a
# database-backed account, no matter who is provisioning it. Anyone who
# needs Super-Admin-equivalent privileges on a normal, deletable account
# gets the "admin" role instead (see deps.py's _FULL_ADMIN_ROLES).
RESERVED_ROLES = (SUPER_ADMIN_ROLE,)

# Directories can grow large over time -- these caps stop a single request
# from ever having to load an unbounded number of rows into memory at once
# (Data Quality & Usability requirement #4). `DEFAULT_LIMIT` is generous
# enough that the existing frontend (which still does its own fast
# client-side search/pagination over whatever page it receives -- see
# js/ui.js's `filterAndPaginate()`) behaves exactly as before for any
# realistic demo/small-team dataset, while `MAX_LIMIT` guarantees a request
# can never accidentally (or maliciously) ask for "everything".
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000


def _derive_username(db: Session, email: str) -> str:
    """
    Auto-derives a login username from the local part of an email address
    (the part before '@'), e.g. "t.okafor@corp.io" -> "t.okafor".

    If that base username is already taken by another account (two people
    could share the same local part on different domains, e.g.
    "j.smith@corp.io" and "j.smith@partner.io"), we append "2", "3", etc.
    until we find one that's free -- so every account always ends up with a
    guaranteed-unique username without asking whoever is provisioning the
    account to type one in separately.
    """
    base = email.split("@")[0].strip().lower()
    candidate = base
    suffix = 2
    # Also steer clear of the reserved Super Admin username (not a `users`
    # row, so the query above alone wouldn't catch it) -- letting a real
    # account share it would let that account get silently shadowed by the
    # hardcoded Super Admin login path, which checks that identifier FIRST
    # (see auth_service.py -> login()).
    reserved = settings.SUPER_ADMIN_USERNAME.strip().lower()
    while candidate == reserved or db.query(models.User).filter(models.User.username == candidate).first():
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def create_user(db: Session, req: UserCreateRequest, user: dict) -> dict:
    """
    Provisions a brand-new login. Both Super Admins and Managers may call
    this, but a Manager's power here is intentionally narrower on ROLE:

      1. A Manager may only create "staff" or "customer" accounts -- never
         "manager" or "admin" (and never "super_admin" either, which is
         reserved for the hardcoded root account and blocked for EVERY
         caller, not just Managers -- see RESERVED_ROLES below). This is
         enforced on the BACKEND (not just hidden in the UI), so a Manager
         can't grant themselves admin rights via a raw API call either.

    Department assignment is NOT restricted for Managers any more -- they
    can set (or leave blank) whatever department they like on a "staff"
    account, exactly like a Super Admin, since Managers no longer have any
    department-scoping elsewhere in the app either. A "customer" account
    still never gets a department (customers aren't tied to any internal
    department, regardless of who creates them).

    Super Admins are unrestricted, exactly like before.
    """
    requested_role = req.role.lower()

    # "super_admin" is reserved for the one hardcoded root identity -- it
    # can never be assigned to a database-backed account, even by another
    # Super Admin/Admin. See RESERVED_ROLES above.
    if requested_role in RESERVED_ROLES:
        raise HTTPException(
            status_code=400,
            detail="The 'super_admin' role is reserved for the hardcoded root account and cannot be assigned. Use 'admin' instead.",
        )

    if user["role"] == "manager" and requested_role not in MANAGER_PROVISIONABLE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Managers may only provision Staff or Customer accounts.",
        )

    existing = db.query(models.User).filter(models.User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists.")

    # Work out the department to actually save. Both Super Admins and
    # Managers get whatever was typed in the form for a "staff" account;
    # "customer" accounts never get a department, regardless of caller.
    if requested_role == "customer":
        department = None
    else:
        department = req.department

    new_user = models.User(
        name=req.name, email=req.email, role=requested_role,
        username=_derive_username(db, req.email),
        password_hash=hash_password(req.password),
        department=department, department_role=req.department_role,
        is_verified=False, is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    db.add(models.AuditLog(
        operator=user["email"], action="USER_PROVISIONED", target_type="User", target_id=new_user.id,
        details=f"Created account for {new_user.name} ({new_user.role}).",
    ))
    db.commit()
    return {"message": f"User {new_user.name} created successfully."}


def list_users(db: Session, user: dict, limit: int = DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None) -> dict:
    """
    Managers now see the entire User Directory, same as a Super Admin --
    every department's accounts, not just their own. (Previously a Manager
    was scoped to their own department plus every "customer" account; that
    restriction has been lifted so Managers have full account visibility,
    matching Admin.)

    We build a plain dict per user with only the fields the frontend
    actually needs, so `password_hash` (and other internal-only columns)
    never leave the server. We also compute `checkout_count` here (sum of
    outstanding units across that user's active checkouts) so the "Custody"
    column on the User Directory / Team Allocation Matrix shows a real
    number instead of always reading 0.

    PAGINATION + SEARCH (Data Quality & Usability requirement #4, extended
    to true server-side search): `limit`/`offset` bound how many rows a
    single request can return -- see the DEFAULT_LIMIT/MAX_LIMIT constants
    above this function. `search` -- when present -- narrows the directory
    to rows where name, email, role, department, or department_role
    case-insensitively contains it, the same set of fields the User
    Directory table's search box has always searched by (see
    js/components/users.js). We run `query.count()` for the (search-scoped)
    total BEFORE slicing with `.offset()/.limit()`, so the caller always
    knows the true total size of the directory even though it only
    received one page of it -- and we only compute the (relatively
    expensive) per-user `checkout_count` aggregation for the rows actually
    being returned, not the entire table.
    """
    # Managers get the same unscoped view as a Super Admin here -- no
    # department filter is applied for either role. (See list_users()'s
    # docstring above.)
    query = db.query(models.User).filter(models.User.is_deleted == False)
    query = apply_search_filter(query, search, [
        models.User.name, models.User.email, models.User.role,
        models.User.department, models.User.department_role,
    ])

    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    total = query.count()
    users = query.order_by(models.User.id).offset(offset).limit(limit).all()

    results = []
    for u in users:
        active_checkouts = [c for c in u.checkouts if c.status == "active"]
        outstanding = sum((c.quantity - c.quantity_returned) for c in active_checkouts)

        # Data Quality & Usability follow-up: the User Directory's small
        # alert icon is computed PER PERSON, not per item -- someone with
        # ten checked-out items and one overdue one gets exactly one
        # "overdue" flag, not ten. See js/components/users.js's row
        # template for how these three booleans become the icon(s), and
        # the dashboard banners (js/components/overdue.js,
        # components/due-soon.js, components/extensions.js) for the exact
        # same "one line per person" idea applied there.
        results.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "username": u.username,
            "role": u.role,
            "department": u.department,
            "department_role": u.department_role,
            "checkout_count": outstanding,
            "alerts": {
                "overdue": any(models.is_overdue(c.due_date) for c in active_checkouts),
                "due_soon": any(models.is_due_soon(c.due_date) for c in active_checkouts),
                "pending_extension": any(
                    er.status == "pending" for c in active_checkouts for er in c.extension_requests
                ),
            },
        })
    return {"items": results, "total": total, "limit": limit, "offset": offset}


def get_my_assigned_items(db: Session, user: dict) -> dict:
    """
    Self-service version of get_user_assigned_items: lets ANY logged-in
    account (staff, customer, manager, super_admin) see their own
    checked-out items, without needing elevated privileges. Powers
    staff.html and customer.html.
    """
    target = db.query(models.User).filter(models.User.id == int(user["sub"])).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    active_checkouts = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.user_id == target.id, models.AssetCheckout.status == "active"
    ).all()
    items = [{
        "checkout_id": c.id, "asset_name": c.asset.name if c.asset else "Unknown Asset",
        "quantity": c.quantity, "quantity_returned": c.quantity_returned, "outstanding": c.quantity - c.quantity_returned,
        # TIMEZONE FIX: `checkout_date` used to be pre-formatted here with
        # `.strftime("%Y-%m-%d %H:%M:%S")` -- that prints the raw UTC wall-
        # clock numbers with no timezone marker at all, and the frontend
        # (js/components/myitems.js's "My Checked-Out Items" table) just
        # displayed that string verbatim. Anyone outside UTC saw a
        # checkout hour that was wrong by their UTC offset (e.g. an hour
        # behind for Lagos/WAT, UTC+1) with no indication it was even a
        # UTC value in the first place.
        #
        # `.isoformat()` instead keeps the UTC offset on the wire (e.g.
        # "2026-07-10T14:23:01.123456+00:00"), exactly like AuditLog rows
        # already do (services/audit_service.py's get_audit_logs() returns
        # the ORM objects directly, which FastAPI's default JSON encoder
        # serializes to this same ISO-with-offset shape) -- and the
        # frontend's `formatTimestamp()` (js/ui.js), which the Audit Trail
        # table already relies on, does the actual UTC -> browser-local
        # conversion via `new Date(...).toLocaleString()`. Applying that
        # same helper to `checkout_date` in myitems.js is what actually
        # fixes the displayed hour -- this ISO change is what makes that
        # possible.
        "checkout_date": c.checkout_date.isoformat() if c.checkout_date else None,
        "due_date": c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
        "due_soon": models.is_due_soon(c.due_date),
        "overdue": models.is_overdue(c.due_date),
        "pending_extension": any(er.status == "pending" for er in c.extension_requests),
    } for c in active_checkouts]

    return {
        "user_id": target.id, "name": target.name, "email": target.email, "role": target.role,
        "department": target.department, "department_role": target.department_role, "assigned_items": items,
    }


def get_user_assigned_items(db: Session, user_id: int, user: dict) -> dict:
    target = db.query(models.User).filter(models.User.id == user_id, models.User.is_deleted == False).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Managers may now inspect custody for anyone in the directory, not
    # just their own department -- consistent with list_users() above
    # giving them the full, unscoped User Directory.

    active_checkouts = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.user_id == user_id, models.AssetCheckout.status == "active"
    ).all()
    items = [{
        "checkout_id": c.id, "asset_name": c.asset.name if c.asset else "Unknown Asset",
        "quantity": c.quantity, "quantity_returned": c.quantity_returned, "outstanding": c.quantity - c.quantity_returned,
        # TIMEZONE FIX -- see get_my_assigned_items() above for the full
        # explanation of why this is `.isoformat()` and not a
        # pre-formatted `.strftime(...)` string.
        "checkout_date": c.checkout_date.isoformat() if c.checkout_date else None,
        "due_date": c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
        "due_soon": models.is_due_soon(c.due_date),
        "overdue": models.is_overdue(c.due_date),
        "pending_extension": any(er.status == "pending" for er in c.extension_requests),
    } for c in active_checkouts]

    return {
        "user_id": target.id, "name": target.name, "email": target.email, "role": target.role,
        "department": target.department, "department_role": target.department_role, "assigned_items": items,
    }


# ---------------------------------------------------------------------------
# PROPERTIES-ASSIGNED EXPORTS (CSV / PDF)
# ---------------------------------------------------------------------------
# Three flavors, all sharing the same row shape/headers via
# `_build_items_export()` below:
#   - export_my_assigned_items    -- self-service (staff/customer/anyone):
#                                    only their own items.
#   - export_user_assigned_items  -- Super Admin/Manager: one specific
#                                    user's items (same access rule as
#                                    get_user_assigned_items above).
#   - export_all_users_items      -- Super Admin/Manager: EVERY user in
#                                    their scope, one row per active
#                                    checkout (not one row per user).
# Each returns (file_bytes, media_type, filename) so the router
# (api/users.py) only has to wrap it in a `Response`.
_ITEM_EXPORT_HEADERS = ["Asset", "Quantity", "Quantity Returned", "Outstanding", "Checked Out", "Due Date"]


def _format_export_datetime(iso_string: Optional[str]) -> str:
    """
    Turns one of the `.isoformat()` timestamps now used throughout this
    module's dicts (checkout_date, deleted_at -- see get_my_assigned_items()'s
    "TIMEZONE FIX" comment above for why they're ISO in the first place)
    back into a friendly string for a CSV/PDF export cell.

    Unlike the live UI (js/ui.js's formatTimestamp(), which converts to the
    browser's local timezone), a static export file has no viewer-specific
    timezone to convert to at generation time -- so this keeps it in UTC
    and says so explicitly, rather than risk it being misread as local
    time the way the old un-labeled pre-formatted string could be.
    """
    if not iso_string:
        return ""
    try:
        return datetime.datetime.fromisoformat(iso_string).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return iso_string  # unparseable -- surface it as-is rather than silently dropping it


def _item_export_rows(items: list) -> list:
    """Turns the `assigned_items` list shape (see get_my_assigned_items /
    get_user_assigned_items above) into plain rows for
    export_service.build_csv_bytes()/build_pdf_bytes()."""
    return [
        [i["asset_name"], i["quantity"], i["quantity_returned"], i["outstanding"], _format_export_datetime(i["checkout_date"]), i["due_date"]]
        for i in items
    ]


def _build_items_export(title: str, subtitle: str, items: list, fmt: str, filename_stub: str):
    """Shared CSV/PDF builder used by all three export_* functions below."""
    rows = _item_export_rows(items)
    today = utc_now().strftime("%Y-%m-%d")
    if fmt == "pdf":
        pdf_bytes = export_service.build_pdf_bytes(title, subtitle, _ITEM_EXPORT_HEADERS, rows)
        return pdf_bytes, "application/pdf", f"{filename_stub}_{today}.pdf"
    csv_bytes = export_service.build_csv_bytes(_ITEM_EXPORT_HEADERS, rows)
    return csv_bytes, "text/csv", f"{filename_stub}_{today}.csv"


def export_my_assigned_items(db: Session, user: dict, fmt: str = "csv"):
    """Self-service export of GET /users/me/items -- any logged-in account
    (staff, customer, manager, super_admin) can download their OWN custody
    ledger as a CSV or PDF, no elevated privileges required."""
    data = get_my_assigned_items(db, user)
    subtitle = f"{data['name']} ({data['email']}) · Exported {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"
    return _build_items_export("Properties Assigned To Me", subtitle, data["assigned_items"], fmt, "my_properties")


def export_user_assigned_items(db: Session, user_id: int, user: dict, fmt: str = "csv"):
    """
    Privileged export of one specific user's custody ledger. Reuses
    get_user_assigned_items() above, so it automatically inherits the same
    access rule: both a Super Admin and a Manager may export anyone
    (Managers have no department-scoping).
    """
    data = get_user_assigned_items(db, user_id, user)
    subtitle = f"{data['name']} ({data['email']}) · Exported by {user['email']}"
    return _build_items_export(f"Properties Assigned To {data['name']}", subtitle, data["assigned_items"], fmt, f"user_{user_id}_properties")


def export_all_users_items(db: Session, user: dict, fmt: str = "csv"):
    """
    Bulk export: every currently-ACTIVE checkout across every user in the
    caller's scope, one row per checkout (so a single person holding
    multiple different assets still gets one row per asset, not one
    combined row). Scope mirrors list_users() exactly: both a Super Admin
    and a Manager get the entire directory.
    """
    query = db.query(models.User).filter(models.User.is_deleted == False)
    users = query.order_by(models.User.id).all()

    headers = ["User", "Email", "Department", "Role", "Asset", "Quantity", "Outstanding", "Checked Out", "Due Date"]
    rows = []
    for u in users:
        for c in u.checkouts:
            if c.status != "active":
                continue
            rows.append([
                u.name, u.email, u.department or "—", u.role,
                c.asset.name if c.asset else "Unknown Asset",
                c.quantity, c.quantity - c.quantity_returned,
                c.checkout_date.strftime("%Y-%m-%d %H:%M:%S UTC") if c.checkout_date else "",
                c.due_date.strftime("%Y-%m-%d") if c.due_date else "No Fixed Due Date",
            ])

    today = utc_now().strftime("%Y-%m-%d")
    title = "Properties Assigned — All Users"
    subtitle = f"Exported by {user['email']} · {len(rows)} active checkout(s) across {len(users)} account(s)"
    if fmt == "pdf":
        pdf_bytes = export_service.build_pdf_bytes(title, subtitle, headers, rows)
        return pdf_bytes, "application/pdf", f"all_users_properties_{today}.pdf"
    csv_bytes = export_service.build_csv_bytes(headers, rows)
    return csv_bytes, "text/csv", f"all_users_properties_{today}.csv"


def delete_user(db: Session, user_id: int, user: dict) -> dict:
    """
    Deleting an account is a Super Admin-only action. Managers cannot do this.

    Safeguards:
      1. SOFT DELETE ONLY -- we never `db.delete()` the row. A hard delete
         would either violate the foreign key from AssetCheckout.user_id
         (if RESTRICT) or silently wipe that user's name out of the
         historical custody ledger (if CASCADE/SET NULL) -- neither is
         acceptable for an audit trail. Instead we flip is_deleted/is_active
         so the row -- and every checkout that references it -- stays
         intact forever, while the account can no longer log in or appear
         in directory listings.
      2. SUPER ADMIN SELF-DELETE BLOCK -- a logged-in Super Admin can never
         delete their own account, even via a raw API call, regardless of
         what the frontend does.
      3. ACTIVE CUSTODY GUARD -- an account still holding outstanding
         checked-out items cannot be deleted until those items are returned,
         so inventory can't silently "disappear" with the deleted account.
    """
    if user_id == int(user["sub"]):
        raise HTTPException(status_code=403, detail="You cannot delete your own account while logged in as it.")

    # Defense in depth: the hardcoded Super Admin (see security.py's
    # SUPER_ADMIN_ID) isn't a `users` table row, so the query below would
    # already return nothing for it -- this just gives a clearer error
    # than a generic 404 if it's ever targeted directly.
    if user_id == SUPER_ADMIN_ID:
        raise HTTPException(status_code=400, detail="The Super Admin account cannot be deleted.")

    target = db.query(models.User).filter(models.User.id == user_id, models.User.is_deleted == False).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    outstanding_items = db.query(func.coalesce(func.sum(
        models.AssetCheckout.quantity - models.AssetCheckout.quantity_returned
    ), 0)).filter(
        models.AssetCheckout.user_id == user_id, models.AssetCheckout.status == "active"
    ).scalar() or 0
    if outstanding_items > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: this user still has {outstanding_items} item(s) in active custody. Process their returns first.",
        )

    target.is_deleted = True
    target.is_active = False
    target.deleted_at = utc_now()

    db.add(models.AuditLog(
        operator=user["email"], action="USER_DELETED", target_type="User", target_id=user_id,
        details=f"Soft-deleted account for {target.name} (login disabled, checkout history preserved).",
    ))
    db.commit()
    return {"message": "User profile successfully removed"}


# ---------------------------------------------------------------------------
# ADMIN-ISSUED PASSWORD RESET ("forgot password" recovery path)
# ---------------------------------------------------------------------------
def reset_user_password(db: Session, user_id: int, new_password: str, admin_password: str, admin_user: dict) -> dict:
    """
    Lets a Super Admin or Admin (see require_super_admin in deps.py, which
    -- despite the name -- also allows the "admin" role, same as everywhere
    else in this file) directly set a NEW password for someone else's
    account. This is the "a user forgot their password and needs it reset"
    recovery path, distinct from the self-service `POST
    /auth/update-password` flow (services/auth_service.py's
    update_password()): that one requires re-confirming the account's
    CURRENT password, which is exactly what a locked-out user doesn't have.
    An admin performing a reset never needs to know -- or be asked for --
    the TARGET's old password.

    SECURITY: even though the target's old password is never needed, the
    ACTING admin must re-confirm their OWN current password (`admin_password`)
    before this proceeds -- same "step-up" idea as update_password()'s
    self-service current-password check, just guarding a different account.
    Without this, anyone who got hold of a still-valid admin/super-admin JWT
    (an unattended logged-in browser tab, a leaked token, etc.) could reset
    any other account's password -- including handing themselves a path to
    another privileged account -- without ever having to prove they still
    are who the token says they are.

    Mirrors update_password()'s recovery behavior: a successful reset also
    clears any accumulated brute-force lockout state (failed_login_attempts
    / locked_until), since a fresh admin-issued password is just as
    legitimate a recovery event as the user finally remembering their own.
    """
    # Re-authentication step: verify the ACTING admin's own current
    # password. The Super Admin's hash is the precomputed constant from
    # security.py (it has no `users` table row); any other admin/manager
    # is looked up by their own JWT subject id. A missing/unresolvable
    # admin account or an incorrect password both fail closed.
    if str(admin_user["sub"]) == str(SUPER_ADMIN_ID):
        admin_hash = SUPER_ADMIN_PASSWORD_HASH
    else:
        admin_row = db.query(models.User).filter(models.User.id == int(admin_user["sub"])).first()
        admin_hash = admin_row.password_hash if admin_row else None

    if not admin_hash or not admin_password or not verify_password(admin_password, admin_hash):
        raise HTTPException(status_code=400, detail="Your password is incorrect.")

    # The hardcoded Super Admin's password lives only in the
    # SUPER_ADMIN_PASSWORD environment variable (see security.py) -- it has
    # no `users` table row to update, so it can never be reset from within
    # the app itself. Change it by updating the environment and restarting
    # the backend instead.
    if user_id == SUPER_ADMIN_ID:
        raise HTTPException(
            status_code=400,
            detail="The Super Admin password is set via the server environment and cannot be changed from the app.",
        )

    # Deliberately excludes soft-deleted accounts -- a deleted user has no
    # password to reset until a Super Admin restores it first (see
    # restore_user() below). Trying to reset a deleted account's password
    # would just be confusing: the account still couldn't log in afterward.
    target = db.query(models.User).filter(models.User.id == user_id, models.User.is_deleted == False).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Password complexity/length is already enforced up front by
    # schemas.users.UserPasswordResetRequest's field_validator -- by the
    # time execution reaches here, new_password is guaranteed to meet policy.
    target.password_hash = hash_password(new_password)
    target.is_verified = True
    target.failed_login_attempts = 0
    target.locked_until = None

    db.add(models.AuditLog(
        operator=admin_user["email"], action="PASSWORD_RESET_BY_ADMIN", target_type="User", target_id=user_id,
        details=f"Password reset for {target.name} by {admin_user['email']}.",
    ))
    db.commit()
    return {"message": f"Password for {target.name} has been reset. Share the new password with them securely."}


# ---------------------------------------------------------------------------
# SOFT-DELETED USER RESTORE ("oops, wrong person" recovery)
# ---------------------------------------------------------------------------
def list_deleted_users(db: Session, user: dict, limit: int = DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None) -> dict:
    """
    Mirror of list_users() above, scoped to soft-deleted accounts only --
    powers the "Restore Deleted Users" panel. Super Admin/Admin only (see
    require_super_admin gate in api/users.py): unlike the main User
    Directory, Managers do not get visibility into deleted accounts.

    Same limit/offset/search shape as list_users() so the frontend can
    reuse the exact same pagination/search plumbing (see
    js/ui.js's renderServerPaginationBar()) against a second, independent
    table state.
    """
    query = db.query(models.User).filter(models.User.is_deleted == True)
    query = apply_search_filter(query, search, [
        models.User.name, models.User.email, models.User.role,
        models.User.department, models.User.department_role,
    ])

    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    total = query.count()
    # Most-recently-deleted first -- that's almost always the row someone
    # opening this panel is looking for ("oops, I just deleted the wrong
    # person").
    users = query.order_by(models.User.deleted_at.desc()).offset(offset).limit(limit).all()

    results = [{
        "id": u.id, "name": u.name, "email": u.email, "username": u.username,
        "role": u.role, "department": u.department, "department_role": u.department_role,
        # TIMEZONE FIX -- see get_my_assigned_items() above for the full
        # explanation of why this is `.isoformat()` and not a
        # pre-formatted `.strftime(...)` string.
        "deleted_at": u.deleted_at.isoformat() if u.deleted_at else None,
    } for u in users]
    return {"items": results, "total": total, "limit": limit, "offset": offset}


def restore_user(db: Session, user_id: int, admin_user: dict) -> dict:
    """
    Reverses delete_user() above: flips is_deleted/is_active back to their
    normal values and clears deleted_at, so the account can log in again
    and reappears in the main User Directory. Super Admin/Admin only,
    same gate as delete_user() itself.

    No email/username collision handling is needed here: create_user()'s
    duplicate-email check and _derive_username()'s uniqueness check both
    query ALL `users` rows regardless of is_deleted (see their comments),
    so a soft-deleted account's original email and username were never up
    for grabs while it sat deleted -- restoring it can't collide with
    anything provisioned in the meantime.
    """
    if user_id == SUPER_ADMIN_ID:
        raise HTTPException(status_code=400, detail="The Super Admin account cannot be deleted, so it never needs restoring.")

    target = db.query(models.User).filter(models.User.id == user_id, models.User.is_deleted == True).first()
    if not target:
        raise HTTPException(status_code=404, detail="Deleted user not found.")

    target.is_deleted = False
    target.is_active = True
    target.deleted_at = None

    db.add(models.AuditLog(
        operator=admin_user["email"], action="USER_RESTORED", target_type="User", target_id=user_id,
        details=f"Restored account for {target.name} (login re-enabled, deletion reversed).",
    ))
    db.commit()
    return {"message": f"User {target.name} has been restored."}
