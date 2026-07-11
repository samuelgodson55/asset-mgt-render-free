"""
models.py
---------
SQLAlchemy ORM table definitions for the Snipe-IT Lite asset registry.

Tables:
  AssetType       - a "pool" of an identical asset (e.g. "MacBook Pro 14 M3")
  AssetException  - a single serial number pulled out of a pool because it's
                    under repair or missing/stolen
  AuditLog        - append-only log of every meaningful action taken
  User            - internal staff/manager/admin accounts that can log in
  Outsider        - external, non-employee people assets can be loaned to
  AssetCheckout   - one active or historical loan of N units of an AssetType
                    to either a User or an Outsider

TIMEZONE HANDLING (beginner-friendly note)
-------------------------------------------
Every timestamp column below is declared `DateTime(timezone=True)`. On
PostgreSQL that maps to a `TIMESTAMPTZ` column instead of a plain
`TIMESTAMP`. The difference matters a lot in practice:

  - A plain `TIMESTAMP` ("naive") column has NO idea what timezone the
    numbers inside it represent. If your app server and your database
    server ever run in different timezones (or one of them changes), you
    silently get wrong answers to "is this checkout overdue yet?" or
    "when exactly was this audit entry logged?".
  - A `TIMESTAMPTZ` ("timezone-aware") column always stores/returns values
    that unambiguously refer to a single instant in time (Postgres
    normalizes everything to UTC internally), and Python's
    `datetime.datetime` objects that come back from a `TIMESTAMPTZ`
    column always carry `tzinfo=datetime.timezone.utc`, so comparisons and
    arithmetic elsewhere in the codebase (e.g. "is `due_date` in the
    past?") can never accidentally mix a naive and an aware datetime and
    raise a `TypeError`, or silently compare the wrong wall-clock hour.

`utc_now()` below is the ONE function every model/service in this project
should call to get "the current time" -- it always returns a
timezone-aware `datetime` stamped as UTC. Never call the bare
`datetime.datetime.utcnow()` (it returns a *naive* datetime that looks like
UTC but isn't labelled as such) -- see services/*.py and security.py for
where this function is imported and reused instead.

Every table is created with these `TIMESTAMPTZ` columns from the start —
see `alembic/versions/0001_baseline_schema.py`, the project's single
baseline migration.
"""

import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, Boolean
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utc_now() -> datetime.datetime:
    """
    The single shared "what time is it right now?" helper for the whole
    backend. Always returns a timezone-AWARE datetime (tzinfo=UTC), never a
    naive one -- import this everywhere instead of calling
    `datetime.datetime.utcnow()` directly (that function is naive-only and
    is being phased out of this codebase on purpose).
    """
    return datetime.datetime.now(datetime.timezone.utc)


# Backwards-compatible alias -- a couple of older comments/imports in this
# project referred to this helper as `get_utc_now`. Keep both names pointing
# at the same timezone-aware implementation so nothing breaks.
get_utc_now = utc_now


def is_due_soon(due_date: "datetime.datetime | None") -> bool:
    """
    Shared "reminder before something goes overdue" test, used everywhere
    a `due_date` gets serialized for a person to look at: the self-service
    My Items table (services/user_service.py's get_my_assigned_items() /
    get_user_assigned_items()) AND the Custody Ledger modal for both Users
    and Ad-Hoc Outsiders (services/outsider_service.py's
    get_outsider_assigned_items()). Kept here, right next to utc_now(),
    rather than duplicated in each of those service modules, so the
    "what counts as due soon" definition can never drift between them.

    True when `due_date` is still in the future but no further out than
    `settings.DUE_SOON_REMINDER_DAYS` -- the exact same rule
    services/checkout_service.py's list_due_soon_checkouts() applies for
    the system-wide "Due Soon" dashboard banner, just evaluated for one
    checkout at a time instead of as a bulk SQL filter. An already-overdue
    or open-ended (`due_date is None`) item is never "due soon".
    """
    if due_date is None:
        return False
    now = utc_now()
    if due_date < now:
        return False
    from config import settings  # deferred: avoids a models<->config import cycle at module load time
    return due_date <= now + datetime.timedelta(days=settings.DUE_SOON_REMINDER_DAYS)


def is_overdue(due_date: "datetime.datetime | None") -> bool:
    """
    The `is_due_soon()` above, but for "has this already passed its due
    date" -- the single-checkout equivalent of
    services/checkout_service.py's list_overdue_checkouts() bulk filter.
    Used wherever a `due_date` needs an at-a-glance overdue flag: per-user
    alert summaries on the User/Ad-Hoc Directory (services/user_service.py
    -> list_users(), services/outsider_service.py -> list_outsiders()) and
    the Custody Ledger modal's per-item rows. An open-ended checkout
    (`due_date is None`) is never overdue -- it was checked out on purpose
    with no fixed return date.
    """
    if due_date is None:
        return False
    return due_date < utc_now()


class AssetType(Base):
    __tablename__ = "asset_types"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)
    total_quantity = Column(Integer, default=0, nullable=False)
    available_quantity = Column(Integer, default=0, nullable=False)
    custom_fields = Column(JSON, default=dict, nullable=True)

    # --- Originating department (OPTIONAL) ---------------------------------
    # Which internal department this pool's equipment originates from/
    # belongs to (e.g. "Engineering", "Design"). Purely descriptive/
    # organizational -- unlike User.department (below), it does NOT scope
    # who can see or dispatch the pool; every role that can see the Asset
    # Inventory today still sees every pool regardless of this value.
    # Settable when a pool is first registered (POST /assets) or via CSV
    # batch import (POST /assets/import, "department" column), and left
    # NULL when not provided since not every org tracks this. Surfaced next
    # to the pool's POOL-{id} tag in the Asset Inventory table, inside the
    # Properties Hub modal, as a "Department" column on every checked-out-
    # items export, and as the filter categories on the Asset Inventory's
    # own export (see services/asset_service.py's
    # export_assets_inventory() / list_asset_departments()).
    department = Column(String, nullable=True)

    # --- Soft delete ------------------------------------------------------
    # We NEVER hard-delete an asset pool row (same rationale as User below):
    # a hard delete would either violate the foreign keys from
    # AssetCheckout.asset_id / AssetException.asset_type_id, or -- if those
    # were CASCADE -- silently erase the historical audit/custody trail for
    # every unit ever checked out of this pool. Instead "deleting" a pool
    # just flips these two flags: it disappears from active inventory
    # listings (is_deleted=True) but every historical checkout/exception
    # record referencing this asset_type_id remains perfectly intact.
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    exceptions = relationship("AssetException", back_populates="asset_type")
    checkouts = relationship("AssetCheckout", back_populates="asset")


class AssetException(Base):
    __tablename__ = "asset_exceptions"

    id = Column(Integer, primary_key=True, index=True)
    asset_type_id = Column(Integer, ForeignKey("asset_types.id"), nullable=False)
    serial_number = Column(String, nullable=False, unique=True)
    status_label = Column(String, nullable=False, default="Undeployable")  # e.g., "Under Repair", "Stolen"
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    # --- Isolation / Recall lifecycle ---------------------------------------
    # "isolated"  -> unit is currently pulled out of the Available pool
    #                (Under Repair / Stolen / Missing). Counts against the
    #                "Isolated" term in the Available formula.
    # "recalled"  -> an administrator has recovered/repaired the unit and
    #                returned it to service. No longer counted as isolated.
    isolation_status = Column(String, nullable=False, default="isolated")
    recalled_at = Column(DateTime(timezone=True), nullable=True)

    asset_type = relationship("AssetType", back_populates="exceptions")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    operator = Column(String, nullable=False)  # email of whoever performed the action
    action = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=False)
    details = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)

    # --- Username login (Data Quality & Usability requirement #6) ---------
    # Auto-derived from the local part of the email address the FIRST time
    # an account is created (see services/user_service.py's
    # `_derive_username()`), e.g. "t.okafor@corp.io" -> "t.okafor". Kept
    # `nullable=True` since this column is created fresh (nothing to
    # backfill) by `alembic/versions/0001_baseline_schema.py`, the
    # project's single baseline migration -- every account created from
    # this point forward always gets one. `POST /auth/login` accepts
    # EITHER this value or the email address interchangeably.
    username = Column(String, unique=True, index=True, nullable=True)

    # "admin" | "manager" | "staff" | "customer" -- NEVER "super_admin".
    # That role is reserved for the single hardcoded root identity (see
    # security.py's super_admin_principal()) and is never stored as a
    # database row; services/user_service.py's create_user() enforces
    # this at the API layer too.
    role = Column(String, default="staff")
    password_hash = Column(String, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # --- Per-account brute-force lockout (SECURITY) ------------------------
    # middleware/rate_limit.py already throttles POST /auth/login by CLIENT
    # IP, but that's coarse: an attacker distributing guesses across many
    # IPs (or sharing a NAT/VPN with legitimate users) isn't meaningfully
    # slowed down by it. These two columns add a SECOND, per-ACCOUNT layer
    # on top of that: services/auth_service.py's login() increments
    # `failed_login_attempts` on every wrong password and, once it reaches
    # `settings.ACCOUNT_LOCKOUT_MAX_ATTEMPTS`, sets `locked_until` far
    # enough in the future that further attempts against THIS account are
    # rejected outright (HTTP 423) no matter which IP they come from --
    # until the lockout window naturally expires, the correct password is
    # tried again after that point, or a Super Admin resets the account's
    # password (which also clears both fields early, as a recovery path).
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)

    # --- Soft delete ------------------------------------------------------
    # We NEVER hard-delete a user row from the database. Deleting the row
    # would either cascade-delete their entire checkout history (destroying
    # the audit trail) or crash on the foreign key constraint from
    # AssetCheckout.user_id -> users.id. Instead, "deleting" a profile just
    # flips these two flags: the account can no longer log in
    # (is_active=False) and disappears from directory listings
    # (is_deleted=True), but every historical checkout record referencing
    # this user_id remains perfectly intact.
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # --- Department scoping (used by the Manager dashboard) ---
    # `department` groups users into teams (e.g. "Engineering", "Design").
    # A manager only ever sees users/audit activity within their own
    # department; a super_admin sees everything regardless of department.
    department = Column(String, nullable=True)
    department_role = Column(String, nullable=True)  # e.g. "Senior Engineer", "Product Designer"

    checkouts = relationship("AssetCheckout", back_populates="user")


class Outsider(Base):
    __tablename__ = "outsiders"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    contact_details = Column(String, nullable=False)
    company = Column(String, nullable=True)

    checkouts = relationship("AssetCheckout", back_populates="outsider")


class AssetCheckout(Base):
    __tablename__ = "asset_checkouts"
    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("asset_types.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    outsider_id = Column(Integer, ForeignKey("outsiders.id"), nullable=True)

    # `quantity` always stays the ORIGINAL amount checked out -- it is the
    # permanent historical record and must never be mutated after creation.
    # `quantity_returned` accumulates how many of those units have been
    # handed back so far (supports partial returns -- see
    # POST /checkouts/{id}/return). The amount still outstanding is always
    # `quantity - quantity_returned`.
    quantity = Column(Integer, default=1, nullable=False)
    quantity_returned = Column(Integer, default=0, nullable=False)
    checkout_date = Column(DateTime(timezone=True), default=utc_now)
    due_date = Column(DateTime(timezone=True), nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, default="active")  # "active" | "returned"

    asset = relationship("AssetType", back_populates="checkouts")
    user = relationship("User", back_populates="checkouts")
    outsider = relationship("Outsider", back_populates="checkouts")
    extension_requests = relationship("ExtensionRequest", back_populates="checkout")


class ExtensionRequest(Base):
    """
    A request to push out an AssetCheckout's `due_date`, submitted either
    by the person who has the equipment (a `User` -- staff/customer/
    manager/admin, self-service, via POST /checkouts/{id}/extension-
    requests) or LOGGED ON BEHALF OF an Ad-Hoc Individual (an `Outsider`,
    who has no login of their own -- a Manager/Admin/Super Admin records
    the request for them, e.g. after a phone call or email).

    This is a separate, append-only-ish table (rather than just letting
    anyone overwrite AssetCheckout.due_date directly) so there's always a
    record of WHO asked for more time, WHY, what they asked for, WHO
    decided it, and what was actually granted -- the same "never silently
    mutate history" principle already used for quantity/quantity_returned
    on AssetCheckout above.

    Lifecycle: "pending" -> "approved" | "denied". Only a Manager, Admin,
    or Super Admin can decide a request (see
    services/extension_service.py's decide_extension_request()) --
    approving one is the ONLY way a checkout's due_date changes after the
    fact. Approving copies `requested_new_due_date` (or a decision-time
    override) onto the checkout's real `due_date` column.
    """
    __tablename__ = "extension_requests"

    id = Column(Integer, primary_key=True, index=True)
    checkout_id = Column(Integer, ForeignKey("asset_checkouts.id"), nullable=False)

    # Free-text label identifying who is ASKING for the extension -- e.g.
    # "Chidinma Okafor (c.okafor@corp.io)" for a self-service User request,
    # or "Ad-Hoc: Femi Adeyemi (Lagos Fintech Ltd.) -- logged by manager"
    # for an Outsider whose request came in by phone/email and was typed in
    # by a Manager/Admin on their behalf. Kept as a plain string (rather
    # than a nullable FK to either users or outsiders) so this table never
    # needs a "which kind of person is this" branch the way AssetCheckout's
    # user_id/outsider_id pair does -- it's a display label, not a join key
    # (the join back to WHO actually holds the item still goes through
    # `checkout.user` / `checkout.outsider` as usual).
    requested_by_label = Column(String, nullable=False)

    previous_due_date = Column(DateTime(timezone=True), nullable=True)
    requested_new_due_date = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String, nullable=True)

    # "pending" | "approved" | "denied"
    status = Column(String, nullable=False, default="pending")

    decided_by = Column(String, nullable=True)  # email of the Manager/Admin/Super Admin who decided it
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    checkout = relationship("AssetCheckout", back_populates="extension_requests")
