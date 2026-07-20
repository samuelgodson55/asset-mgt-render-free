"""
services/user_service.py
--------------------------
System-user account CRUD and self-service/custody lookups. Used by
api/users.py.
"""

from typing import Optional
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

import models
from models import utc_now
from config import settings
from security import hash_password, verify_password, SUPER_ADMIN_ROLE
from schemas.users_schema import UserCreateRequest, UserUpdateRequest, UserConvertToOutsiderRequest
import services.export_service as export_service
from services.search_utils import apply_search_filter

# Roles a Manager is allowed to hand out when provisioning a new login.
# A Manager can create Staff and Customer accounts, but must NEVER be able
# to create another Manager or an Admin account for themselves -- that
# would be an easy privilege-escalation hole. Admins/Super Admin are not
# limited by this list (checked in create_user below).
MANAGER_PROVISIONABLE_ROLES = ("staff", "customer")

# "super_admin" is reserved for the single hardcoded-IDENTITY root account
# (see security.py's module docstring) -- it can never be assigned via
# THIS provisioning API, no matter who's calling it. The one row that has
# this role is bootstrapped directly by a migration/seed routine instead
# (bypassing create_user() entirely). Anyone who needs Super-Admin-
# equivalent privileges on a normal, deletable account gets the "admin"
# role instead (see deps.py's _FULL_ADMIN_ROLES).
RESERVED_ROLES = (SUPER_ADMIN_ROLE,)


def _visible_users_query(db: Session):
    """
    Shared base query for every User Directory / bulk-export listing in
    this file: excludes soft-deleted accounts (as before) AND the single
    hardcoded root admin row (role=SUPER_ADMIN_ROLE). The root account is
    a real `users` row now (see security.py's module docstring), but it's
    a "secure door for the developer" -- it must never appear in the
    directory, in bulk exports, or (see services/audit_service.py) in the
    Audit Trail UI, even though it's a completely normal, queryable,
    audited row at the database level.
    """
    return db.query(models.User).filter(~models.User.is_deleted, models.User.role != SUPER_ADMIN_ROLE)


def is_hidden_root_admin(target: "models.User") -> bool:
    """
    True for the one hardcoded root admin row. Any endpoint that targets a
    SPECIFIC user by id (edit, delete, per-user item export, password
    reset, restore, ...) checks this and responds exactly as if the id
    didn't exist (404) -- never a 403 -- so the account's existence isn't
    revealed even to someone probing ids directly. Self-service routes
    (GET /users/me/items, POST /auth/update-password acting on your own
    account) are unaffected -- the root admin can still fully manage
    itself when logged in as itself; this guard only stops OTHER callers
    (and generic id-based listings) from reaching it.
    """
    return target.role == SUPER_ADMIN_ROLE

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


def _provision_user_row(
    db: Session, *, name: str, email: str, phone_number: Optional[str], role: str, password: str,
    department: Optional[str], department_role: Optional[str], actor: dict,
) -> "models.User":
    """
    Shared core of "create one brand-new login row", factored out of
    create_user() below so services/outsider_service.py's
    convert_outsider_to_user() -- an Ad-Hoc Individual finally deciding
    they want a real account -- enforces the EXACT same role restrictions,
    email-uniqueness check, and username-derivation logic as a normal
    Admin/Manager-provisioned account, with no risk of the two paths ever
    drifting apart.

    A Manager's power here is intentionally narrower on ROLE:

      1. A Manager may only provision "staff" or "customer" accounts --
         never "manager" or "admin" (and never "super_admin" either,
         which is reserved for the hardcoded root account and blocked for
         EVERY caller, not just Managers -- see RESERVED_ROLES above).
         This is enforced on the BACKEND (not just hidden in the UI), so
         a Manager can't grant themselves admin rights via a raw API
         call either.

    Department assignment is NOT restricted for Managers -- they can set
    (or leave blank) whatever department they like on a "staff" account,
    exactly like a Super Admin. A "customer" account never gets a
    department, regardless of who provisions it.

    Super Admins/Admins are unrestricted, exactly like before.

    Commits the new row and returns it, but does NOT write an audit log
    entry or build a response message -- callers own both of those, since
    "created fresh" vs "converted from an ad-hoc profile" deserve
    different audit trails and messages.
    """
    requested_role = role.lower()

    # "super_admin" is reserved for the one hardcoded root identity -- it
    # can never be assigned to a database-backed account, even by another
    # Super Admin/Admin. See RESERVED_ROLES above.
    if requested_role in RESERVED_ROLES:
        raise HTTPException(
            status_code=400,
            detail="The 'super_admin' role is reserved for the hardcoded root account and cannot be assigned. Use 'admin' instead.",
        )

    if actor["role"] == "manager" and requested_role not in MANAGER_PROVISIONABLE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Managers may only provision Staff or Customer accounts.",
        )

    # Case-insensitive on purpose: login (see auth_service.py) matches
    # email/username case-insensitively, so allowing e.g. "T.Okafor@corp.io"
    # and "t.okafor@corp.io" to exist as two separate accounts would make
    # that lookup ambiguous. Blocking the clash here keeps it impossible.
    existing = db.query(models.User).filter(func.lower(models.User.email) == email.strip().lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email already exists.")

    # Work out the department to actually save. Both Super Admins and
    # Managers get whatever was typed in the form for a "staff" account;
    # "customer" accounts never get a department, regardless of caller.
    if requested_role == "customer":
        department_value = None
    else:
        department_value = department

    new_user = models.User(
        name=name, email=email, phone_number=phone_number, role=requested_role,
        username=_derive_username(db, email),
        password_hash=hash_password(password),
        department=department_value, department_role=department_role,
        is_verified=False, is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


def create_user(db: Session, req: UserCreateRequest, user: dict) -> dict:
    """
    Provisions a brand-new login. Both Super Admins and Managers may call
    this -- see _provision_user_row() above for the full role/department
    permission model this delegates to.
    """
    new_user = _provision_user_row(
        db, name=req.name, email=req.email, phone_number=req.phone_number, role=req.role, password=req.password,
        department=req.department, department_role=req.department_role, actor=user,
    )

    db.add(models.AuditLog(
        operator=user["email"], action="USER_PROVISIONED", target_type="User", target_id=new_user.id,
        details=f"Created account for {new_user.name} ({new_user.role}).",
    ))
    db.commit()
    return {"message": f"User {new_user.name} created successfully."}


def update_user(db: Session, user_id: int, req: UserUpdateRequest, user: dict) -> dict:
    """
    Edits an existing account's identity details (name, username, email).
    Distinct from create_user() (provisioning a brand-new login) and
    reset_user_password() (credential recovery) -- this never touches
    role, department, or password_hash.

    PERMISSIONS:
      - A Super Admin/Admin (see deps.py's require_privileged_role, which
        the route sits behind) may edit ANY account, including other
        Admins and Managers.
      - A Manager may only edit "staff" or "customer" accounts -- the same
        MANAGER_PROVISIONABLE_ROLES boundary create_user() already
        enforces when PROVISIONING a new login applies equally here when
        EDITING an existing one, so a Manager can never touch a Manager or
        Admin account's details, even via a raw API call.

    Only the fields actually present on the request are touched (Pydantic
    `exclude_unset`) -- omitting a field leaves it exactly as it was rather
    than blanking it out. The hardcoded root admin row IS reachable by the
    lookup below now (it's a real `users` row -- see security.py's module
    docstring), but its identity is fixed/hardcoded and it must stay
    invisible everywhere, so is_hidden_root_admin() below turns that into
    the same 404 an actually-nonexistent id would produce.
    """
    target = db.query(models.User).filter(models.User.id == user_id, ~models.User.is_deleted).first()
    if not target or is_hidden_root_admin(target):
        raise HTTPException(status_code=404, detail="User not found.")

    if user["role"] == "manager" and target.role not in MANAGER_PROVISIONABLE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Managers may only edit Staff or Customer accounts.",
        )

    updates = req.model_dump(exclude_unset=True)

    if "email" in updates and updates["email"].strip().lower() != target.email.strip().lower():
        # Case-insensitive clash check -- same rationale as create_user()
        # above: login matches email/username case-insensitively, so two
        # accounts differing only by case must never both exist.
        clash = db.query(models.User).filter(
            func.lower(models.User.email) == updates["email"].strip().lower(), models.User.id != user_id
        ).first()
        if clash:
            raise HTTPException(status_code=400, detail="A user with this email already exists.")
        target.email = updates["email"]

    if "username" in updates and updates["username"] != target.username:
        candidate = updates["username"].strip().lower()
        # Same reserved-username guard as _derive_username() above -- an
        # edit can't be used to sneak a real account onto the identifier
        # the hardcoded Super Admin login path checks first.
        reserved = settings.SUPER_ADMIN_USERNAME.strip().lower()
        if candidate == reserved:
            raise HTTPException(status_code=400, detail="That username is reserved.")
        clash = db.query(models.User).filter(models.User.username == candidate, models.User.id != user_id).first()
        if clash:
            raise HTTPException(status_code=400, detail="That username is already taken.")
        target.username = candidate

    if "name" in updates:
        target.name = updates["name"]

    if "phone_number" in updates:
        target.phone_number = updates["phone_number"] or None

    db.add(models.AuditLog(
        operator=user["email"], action="USER_UPDATED", target_type="User", target_id=target.id,
        details=f"Updated account details for {target.name}.",
    ))
    db.commit()
    db.refresh(target)
    return {
        "message": f"User {target.name} updated successfully.",
        "id": target.id, "name": target.name, "username": target.username, "email": target.email,
        "phone_number": target.phone_number,
    }


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
    # docstring above.) The hidden root admin row is also excluded here --
    # see _visible_users_query()'s docstring.
    query = _visible_users_query(db)
    query = apply_search_filter(query, search, [
        models.User.name, models.User.email, models.User.phone_number, models.User.role,
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
            "phone_number": u.phone_number,
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


def _pending_extension_fields(checkout: "models.AssetCheckout") -> dict:
    """
    Surfaces the actual pending ExtensionRequest (if any) alongside the
    plain `pending_extension` boolean already computed above -- lets the
    Custody Ledger drawer (components/custody.js) swap that item's
    "Extend" button for Approve/Deny buttons acting on THIS SPECIFIC
    request, instead of a Manager/Admin having to open the notification
    bell separately or (worse) firing off a brand new direct extension
    while one is already awaiting a decision.
    """
    pending = next((er for er in checkout.extension_requests if er.status == "pending"), None)
    if not pending:
        return {"pending_extension_request_id": None, "pending_extension_new_due_date": None, "pending_extension_reason": None}
    return {
        "pending_extension_request_id": pending.id,
        "pending_extension_new_due_date": pending.requested_new_due_date.strftime("%Y-%m-%d") if pending.requested_new_due_date else None,
        "pending_extension_reason": pending.reason,
    }


def _group_assigned_items(checkouts: list["models.AssetCheckout"], include_outsourced_details: bool) -> list[dict]:
    """Collapse quote-based checkout splits into one row per quote item for self-service, but split outsourced shortfalls by vendor for Manager/Admin custody views."""
    grouped_items = []
    grouped_lookup = {}

    def make_payload(checkout: "models.AssetCheckout") -> dict:
        payload = {
            "checkout_id": checkout.id,
            "asset_name": models.checkout_display_name(checkout),
            "asset_category": checkout.asset.category if checkout.asset else None,
            "quantity": checkout.quantity,
            "quantity_returned": checkout.quantity_returned,
            "outstanding": checkout.quantity - checkout.quantity_returned,
            "checkout_date": checkout.checkout_date.isoformat() if checkout.checkout_date else None,
            "due_date": checkout.due_date.strftime("%Y-%m-%d") if checkout.due_date else "No Fixed Due Date",
            "due_soon": models.is_due_soon(checkout.due_date),
            "overdue": models.is_overdue(checkout.due_date),
            "pending_extension": any(er.status == "pending" for er in checkout.extension_requests),
            **_pending_extension_fields(checkout),
            "checkout_ids": [checkout.id],
            "vendor_sources": [],
        }
        if include_outsourced_details:
            payload.update({
                "is_outsourced": checkout.is_outsourced,
                "outsourced_source": checkout.outsourced_source,
            })
        if include_outsourced_details and checkout.is_outsourced and checkout.outsourced_source:
            payload["vendor_sources"] = [checkout.outsourced_source]
        return payload

    for checkout in checkouts:
        if checkout.quotation_id is None:
            grouped_items.append(make_payload(checkout))
            continue

        if include_outsourced_details and checkout.is_outsourced and checkout.outsourced_source:
            group_source = checkout.outsourced_source
        else:
            group_source = None

        key = (checkout.quotation_id, models.checkout_display_name(checkout), checkout.due_date, group_source)
        if key not in grouped_lookup:
            payload = make_payload(checkout)
            grouped_lookup[key] = payload
            grouped_items.append(payload)
            continue

        payload = grouped_lookup[key]
        payload["quantity"] += checkout.quantity
        payload["quantity_returned"] += checkout.quantity_returned
        payload["outstanding"] += checkout.quantity - checkout.quantity_returned
        payload["checkout_ids"].append(checkout.id)
        if include_outsourced_details and checkout.is_outsourced and checkout.outsourced_source:
            existing_sources = payload.get("vendor_sources", [])
            if checkout.outsourced_source not in existing_sources:
                existing_sources.append(checkout.outsourced_source)
                payload["vendor_sources"] = existing_sources

    return grouped_items


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
    items = _group_assigned_items(active_checkouts, include_outsourced_details=False)

    return {
        "user_id": target.id, "name": target.name, "email": target.email, "role": target.role,
        "department": target.department, "department_role": target.department_role, "assigned_items": items,
    }


def get_user_assigned_items(db: Session, user_id: int, user: dict) -> dict:
    target = db.query(models.User).filter(models.User.id == user_id, ~models.User.is_deleted).first()
    if not target or is_hidden_root_admin(target):
        raise HTTPException(status_code=404, detail="User not found")

    # Managers may now inspect custody for anyone in the directory, not
    # just their own department -- consistent with list_users() above
    # giving them the full, unscoped User Directory.

    active_checkouts = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.user_id == user_id, models.AssetCheckout.status == "active"
    ).all()
    items = _group_assigned_items(active_checkouts, include_outsourced_details=True)

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
_ITEM_EXPORT_HEADERS = ["Asset", "Category", "Vendor / Source", "Quantity", "Quantity Returned", "Outstanding", "Checked Out", "Due Date"]


def _format_export_datetime(iso_string: Optional[str]) -> str:
    """
    Turns one of the `.isoformat()` timestamps now used throughout this
    module's dicts (checkout_date, deleted_at -- see get_my_assigned_items()'s
    "TIMEZONE FIX" comment above for why they're ISO in the first place)
    back into a friendly string for a CSV/PDF export cell.

    Delegates to services/export_service.py's format_export_datetime() so
    this export renders in the same DISPLAY_TIMEZONE (see config.py), with
    the same real zone abbreviation, as every other exporter in the app --
    that shared helper is what keeps this in sync with the Audit Trail's
    on-screen (browser-local) time instead of silently drifting an hour
    behind it the way a hardcoded "UTC" label used to.
    """
    return export_service.format_export_datetime(iso_string)


def _item_export_rows(items: list) -> list:
    """Turns the `assigned_items` list shape (see get_my_assigned_items /
    get_user_assigned_items above) into plain rows for
    export_service.build_csv_bytes()/build_pdf_bytes()."""
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
    _exported_at = export_service.display_now()
    subtitle = f"{data['name']} ({data['email']}) · Exported {_exported_at.strftime('%Y-%m-%d %H:%M')} {_exported_at.tzname()}"
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
    query = _visible_users_query(db)
    users = query.order_by(models.User.id).all()

    headers = ["User", "Email", "User Department", "Role", "Asset", "Asset Category", "Vendor / Source", "Quantity", "Outstanding", "Checked Out", "Due Date"]
    rows = []
    for u in users:
        for c in u.checkouts:
            if c.status != "active":
                continue
            rows.append([
                u.name, u.email, u.department or "—", u.role,
                models.checkout_display_name(c),
                c.asset.category if c.asset and c.asset.category else "—",
                c.outsourced_source or "—",
                c.quantity, c.quantity - c.quantity_returned,
                export_service.format_export_datetime(c.checkout_date),
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
      4. ROOT ADMIN GUARD -- the single hardcoded root admin row
         (role=SUPER_ADMIN_ROLE, bootstrapped by
         alembic/versions/0002_bootstrap_root_admin.py) can never be
         deleted, even by another Super Admin/Admin. It responds with the
         same 404 an actually-nonexistent id would -- never a clearer
         "that's the root account" message -- so this endpoint can't be
         used to fingerprint which id it lives at either.
    """
    if user_id == int(user["sub"]):
        raise HTTPException(status_code=403, detail="You cannot delete your own account while logged in as it.")

    target = db.query(models.User).filter(models.User.id == user_id, ~models.User.is_deleted).first()
    if not target or is_hidden_root_admin(target):
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
# REVOKE LOGIN ACCESS ("the reverse of Outsider -> User")
# ---------------------------------------------------------------------------
def convert_user_to_outsider(db: Session, user_id: int, req: UserConvertToOutsiderRequest, user: dict) -> dict:
    """
    "This person no longer needs (or shouldn't have) a login, but is still
    tracked as a custody holder": the reverse of
    services/outsider_service.py's convert_outsider_to_user(). Turns a
    real, log-in-capable `models.User` row into an Ad-Hoc (no-login)
    `models.Outsider` row, migrating every bit of their existing custody
    history over to the new profile intact.

    Available to the same access tier as account provisioning (Super
    Admin/Admin or Manager, see deps.require_privileged_role), with the
    same Manager role ceiling create_user()/_provision_user_row() apply
    in the other direction: a Manager may only revoke login access from a
    "staff" or "customer" account -- never from another Manager or an
    Admin. This is deliberately narrower than delete_user() (Super Admin
    only) because unlike a hard account removal, this is reversible in
    spirit (the person can always be converted back via
    services/outsider_service.py's convert_outsider_to_user()) and is the
    natural Manager-level counterpart to provisioning a Staff/Customer
    account in the first place.

    SAFETY / WHAT "SAFELY MIGRATE" MEANS HERE (mirrors
    convert_outsider_to_user()'s docstring, just pointed the other way):
      1. SELF-REVOKE BLOCK -- a caller can never revoke their OWN login
         access while logged in as it (same guard as delete_user()'s
         self-delete block) -- that would lock them out mid-session with
         no way back in.
      2. ROOT ADMIN / ALREADY-CONVERTED GUARD -- 404 (matching every
         other id-based user action) if the id doesn't exist, is already
         soft-deleted, was already converted previously, or is the single
         hardcoded root admin row (is_hidden_root_admin()) -- that
         account can never lose its login through this door.
      3. CUSTODY HISTORY MOVES, NOT COPIES -- every AssetCheckout row
         (active AND already-returned) that pointed at `user_id=target.id`
         is re-pointed at `outsider_id=new_outsider.id` (and `user_id`
         cleared) in one bulk UPDATE, so nothing is left orphaned under
         the now-revoked account. Deliberately no outstanding-items block
         (unlike delete_user()'s): migrating those very checkouts over to
         the ad-hoc profile IS the point here, same reasoning as
         convert_outsider_to_user().
      4. QUOTATION ASSIGNMENTS MOVE TOO -- any Quotation this account was
         the assignee of (`assigned_to_id`) is re-pointed at
         `assigned_outsider_id=new_outsider.id` the same way.
      5. THE OLD ACCOUNT IS RETIRED, NOT ERASED -- same soft-delete flip
         as delete_user() (is_deleted/is_active/deleted_at), so it can no
         longer log in and drops out of the User Directory, but the row
         itself and this migration's audit trail stay queryable forever.
         `converted_to_outsider_id` (see models.User) is the permanent
         link recording exactly which ad-hoc profile it became.
      6. ONE ATOMIC COMMIT FOR THE MIGRATION STEPS -- the new Outsider
         row, both bulk UPDATEs, the account's soft-delete flip, and both
         audit log rows are all flushed in the SAME commit, so a crash
         mid-way can't leave checkout/quotation data half-migrated.
    """
    if user_id == int(user["sub"]):
        raise HTTPException(status_code=403, detail="You cannot revoke your own login access while logged in as it.")

    target = db.query(models.User).filter(models.User.id == user_id, ~models.User.is_deleted).first()
    if not target or is_hidden_root_admin(target):
        raise HTTPException(status_code=404, detail="User not found")

    if user["role"] == "manager" and target.role not in MANAGER_PROVISIONABLE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Managers may only revoke login access for Staff or Customer accounts.",
        )

    # `name` is always carried over from the account's existing profile
    # (never re-typed) -- see docstring point above. `email` defaults to
    # the account's existing login email, and `phone_number` to its
    # existing phone_number, when not supplied; company has no automatic
    # default (see schemas.users.UserConvertToOutsiderRequest's docstring).
    outsider_email = req.email or target.email
    outsider_phone = req.phone_number or target.phone_number
    new_outsider = models.Outsider(name=target.name, email=outsider_email, phone_number=outsider_phone, company=req.company)
    db.add(new_outsider)
    db.flush()  # assigns new_outsider.id without ending this function's single commit

    # Move every checkout (active AND historical) over to the new ad-hoc
    # profile in one bulk UPDATE -- see docstring point #3. We separately
    # count how many of those were *active* (still outstanding) before the
    # bulk UPDATE runs, purely for the confirmation message below: the
    # profile drawer's "Custody" line only ever counts active checkouts
    # (see get_users_list()'s `outstanding` aggregation above), so a
    # message that just said "N checkout(s) ... moved" -- with N including
    # long-since-returned history -- could read as contradicting a "0
    # items checked out" custody line the caller had just seen, even
    # though both numbers are correct for what each one measures.
    active_checkouts_migrated = (
        db.query(models.AssetCheckout)
        .filter(models.AssetCheckout.user_id == target.id, models.AssetCheckout.status == "active")
        .count()
    )
    checkouts_migrated = (
        db.query(models.AssetCheckout)
        .filter(models.AssetCheckout.user_id == target.id)
        .update({"user_id": None, "outsider_id": new_outsider.id}, synchronize_session=False)
    )
    historical_checkouts_migrated = checkouts_migrated - active_checkouts_migrated

    # Same treatment for any Quotation this account was the assignee of --
    # see docstring point #4.
    quotations_migrated = (
        db.query(models.Quotation)
        .filter(models.Quotation.assigned_to_id == target.id)
        .update({"assigned_to_id": None, "assigned_outsider_id": new_outsider.id}, synchronize_session=False)
    )

    # Retire the login -- see docstring point #5.
    target.is_deleted = True
    target.is_active = False
    target.deleted_at = utc_now()
    target.converted_to_outsider_id = new_outsider.id

    db.add(models.AuditLog(
        operator=user["email"], action="USER_CONVERTED_TO_OUTSIDER", target_type="User", target_id=target.id,
        details=(
            f"Revoked login access for {target.name} and converted their account into an "
            f"ad-hoc profile (outsider #{new_outsider.id}). Migrated {checkouts_migrated} "
            f"checkout(s) ({active_checkouts_migrated} active, {historical_checkouts_migrated} "
            f"past) and {quotations_migrated} quotation assignment(s)."
        ),
    ))
    db.add(models.AuditLog(
        operator=user["email"], action="OUTSIDER_PROVISIONED_FROM_USER", target_type="Outsider", target_id=new_outsider.id,
        details=f"Ad-hoc profile created from revoked account #{target.id} ({target.name}).",
    ))
    db.commit()
    db.refresh(new_outsider)

    # Build the confirmation message piece by piece so it never implies
    # outstanding items were involved when there were none (e.g. "0 active
    # checkout(s)" reads as a non-sequitur) -- only mention the active
    # count at all when it's actually nonzero, and always spell out that
    # the rest is past/returned history, not something still checked out.
    checkout_bits = []
    if active_checkouts_migrated:
        checkout_bits.append(f"{active_checkouts_migrated} currently checked-out item(s)")
    if historical_checkouts_migrated:
        checkout_bits.append(f"{historical_checkouts_migrated} past (already-returned) checkout record(s)")
    checkout_summary = " and ".join(checkout_bits) if checkout_bits else "no checkout history"

    return {
        "message": (
            f"{target.name}'s login access has been revoked. "
            f"{checkout_summary} and {quotations_migrated} quotation "
            f"assignment(s) were moved to their new ad-hoc profile."
        ),
        "user_id": target.id,
        "outsider_id": new_outsider.id,
        "name": new_outsider.name,
        "email": new_outsider.email,
        "phone_number": new_outsider.phone_number,
        "company": new_outsider.company,
        "checkouts_migrated": checkouts_migrated,
        "active_checkouts_migrated": active_checkouts_migrated,
        "historical_checkouts_migrated": historical_checkouts_migrated,
        "quotations_migrated": quotations_migrated,
    }


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

    SECURITY CHANGE: resetting the root admin's (role=SUPER_ADMIN_ROLE)
    own password used to be permanently blocked here, because its
    password lived only in the SUPER_ADMIN_PASSWORD environment variable
    and had no `users` row to update. Now that it's a real row (see
    security.py's module docstring), it can be reset through this exact
    same audited path as any other account -- that's the whole point of
    moving its credential into the database: rotation and auditing
    through the normal mechanisms, not a permanent "can only be changed by
    editing the server environment" exception.
    """
    # Re-authentication step: verify the ACTING admin's own current
    # password by looking up their own row -- the root admin is a real
    # `users` row too now, so this is the same lookup for every caller.
    admin_row = db.query(models.User).filter(models.User.id == int(admin_user["sub"])).first()
    admin_hash = admin_row.password_hash if admin_row else None

    if not admin_hash or not admin_password or not verify_password(admin_password, admin_hash):
        raise HTTPException(status_code=400, detail="Your password is incorrect.")

    # Deliberately excludes soft-deleted accounts -- a deleted user has no
    # password to reset until a Super Admin restores it first (see
    # restore_user() below). Trying to reset a deleted account's password
    # would just be confusing: the account still couldn't log in afterward.
    target = db.query(models.User).filter(models.User.id == user_id, ~models.User.is_deleted).first()
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

    EXCLUDES REVOKED/CONVERTED ACCOUNTS -- convert_user_to_outsider()
    above also flips is_deleted (it reuses delete_user()'s soft-delete
    flip so the retired login drops out of the User Directory), but that
    account's custody history and identity have already been migrated
    forward onto a brand-new Outsider row -- it isn't "deleted" in the
    undo-able sense this panel is for, it moved to the Ad-Hoc Directory
    on purpose. Surfacing it here would let someone "restore" a login
    whose checkouts/quotations no longer point at it at all, producing a
    duplicate identity (the live Outsider profile AND a resurrected
    User row). `converted_to_outsider_id IS NULL` filters those out,
    leaving only accounts removed via delete_user().

    EXCLUDES PURGED ACCOUNTS -- purge_user() below overwrites a row's
    email/username with an anonymized placeholder and stamps
    `purged_at`, specifically so its original identity's uniqueness
    lock is released. There's nothing meaningful left to "restore" under
    its original name at that point, so purged rows are filtered out
    here too, same reasoning as the converted-account exclusion above.
    """
    query = db.query(models.User).filter(
        models.User.is_deleted,
        models.User.role != SUPER_ADMIN_ROLE,
        models.User.converted_to_outsider_id.is_(None),
        models.User.purged_at.is_(None),
    )
    query = apply_search_filter(query, search, [
        models.User.name, models.User.email, models.User.phone_number, models.User.role,
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

    NOT FOR REVOKED/CONVERTED ACCOUNTS -- list_deleted_users() above
    already keeps these out of the "Restore Deleted Users" panel, but
    this check is repeated here in case restore_user() is ever called
    directly (e.g. a stale id from before a conversion). A row with
    `converted_to_outsider_id` set had its checkouts/quotations migrated
    onto that Outsider by convert_user_to_outsider() -- reactivating the
    login here wouldn't bring any of that back, it would just create a
    second, hollow identity alongside the live ad-hoc profile.

    NOT FOR PURGED ACCOUNTS EITHER -- same reasoning, repeated here as a
    second line of defense: purge_user() below has already overwritten
    this row's email/username with an anonymized placeholder, so
    "restoring" it wouldn't bring the person's original login back at
    all -- it would just re-enable a login under a placeholder email
    nobody can log into.
    """
    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.is_deleted,
        models.User.converted_to_outsider_id.is_(None),
        models.User.purged_at.is_(None),
    ).first()
    if not target or is_hidden_root_admin(target):
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


# ---------------------------------------------------------------------------
# PURGE ("I'm done with this deleted account, free up its email/username")
# ---------------------------------------------------------------------------
def purge_user(db: Session, user_id: int, admin_user: dict) -> dict:
    """
    Permanently releases a soft-deleted account's email/username so a
    brand-new account can reuse them -- called from the "Purge" button
    that sits next to Restore on the Restore Deleted Users panel.
    Super Admin only, same gate as delete_user()/restore_user().

    WHY THIS EXISTS: `users.email` and `users.username` both carry
    DB-level `unique=True` constraints (see models.py). delete_user()
    only ever flips is_deleted/is_active -- it never touches those
    columns -- so a deleted account's original email stays permanently
    "reserved" and create_user() will keep rejecting it for a new
    account (see _provision_user_row()'s existing-email check, which
    deliberately queries ALL rows regardless of is_deleted). Previously
    the only way around that was to restore the old account first
    (re-enabling its login) purely so it could be renamed -- this button
    skips that detour.

    WHAT "PURGE" DOES *NOT* DO: it is NOT a hard delete. We never
    `db.delete()` this row, for the exact same reason delete_user()
    doesn't -- historical AssetCheckout.user_id / Quotation rows still
    point at it, and hard-deleting would either violate that foreign
    key or silently erase this person's name out of the custody ledger.
    Instead:
      1. `email`/`username` are overwritten with a placeholder that
         embeds this row's own (permanent, unique) id --
         "purged-user-{id}@purged.invalid" / "purged-user-{id}" -- so
         it can never collide with any other row, purged or not.
      2. `name` is left untouched, so anything they still show up as
         the historical holder of (Custody Ledger, exports, the Audit
         Trail) keeps reading like a real name instead of a placeholder.
      3. `purged_at` is stamped, which both list_deleted_users() and
         restore_user() above check -- once purged, the row drops out
         of the "Restore Deleted Users" panel entirely and can no
         longer be restored (there'd be nothing meaningful to log back
         into).
      4. The account's original email is recorded in the audit log
         entry below (in cleartext, before it's overwritten) so it's
         still discoverable later via the Audit Trail even though the
         `users` row itself no longer carries it.

    Irreversible in the same sense delete_user()'s soft delete is
    reversible and this is not: there's no "unpurge". A caller who
    isn't sure yet should use Restore, not Purge.

    Only ever reachable for rows list_deleted_users() would surface
    (already soft-deleted, never converted to an outsider, not already
    purged) -- the same three-part filter is repeated here so a raw API
    call can't purge a live account, a converted one, or one that's
    already been purged.
    """
    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.is_deleted,
        models.User.converted_to_outsider_id.is_(None),
        models.User.purged_at.is_(None),
    ).first()
    if not target or is_hidden_root_admin(target):
        raise HTTPException(status_code=404, detail="Deleted user not found.")

    original_name = target.name
    original_email = target.email

    target.email = f"purged-user-{target.id}@purged.invalid"
    target.username = f"purged-user-{target.id}"
    target.purged_at = utc_now()

    db.add(models.AuditLog(
        operator=admin_user["email"], action="USER_PURGED", target_type="User", target_id=user_id,
        details=(
            f"Permanently purged deleted account for {original_name} (was {original_email}). "
            f"Their email and username are now free to be reused by a new account; login "
            f"history and custody records remain intact under this now-anonymized row."
        ),
    ))
    db.commit()
    return {"message": f"{original_name}'s account has been purged. Their email is now free to be reused."}
