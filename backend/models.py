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
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, Boolean, Numeric, Date
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


def _as_aware_utc(value: "datetime.datetime") -> "datetime.datetime":
    """
    Normalizes a possibly-naive datetime to timezone-aware UTC before it's
    compared against `utc_now()`.

    BUG FIX: `AssetCheckout.due_date` is declared `DateTime(timezone=True)`
    (see below), which Postgres honors as a real `TIMESTAMPTZ` -- rows read
    back from it are always tz-aware, so `due_date < utc_now()` just works
    in every real deployment of this app (docker-compose/Render always run
    against Postgres). SQLite has no native timezone-aware datetime type
    though, so SQLAlchemy's `timezone=True` is a silent no-op on that
    dialect: a value written as UTC-aware round-trips back out as a NAIVE
    datetime. That's harmless for local dev... until this function tried
    to compare it against `utc_now()`'s aware value and Python raised
    `TypeError: can't compare offset-naive and offset-aware datetimes` --
    turning `GET /users`, `GET /users/me/items`, and any other endpoint
    that serializes a checkout with an active due date into a 500 the
    moment SQLite was involved (see the "throwaway SQLite database"
    testing pattern in README.md's "Testing Your Changes" section, which
    this fix makes actually usable end-to-end). Since every datetime this
    app ever writes is UTC to begin with (see `utc_now()` above), treating
    a naive value as "already UTC" here is correct, not a guess.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


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
    due_date = _as_aware_utc(due_date)
    now = utc_now()
    if due_date < now:
        return False
    from config import settings  # deferred: avoids a models<->config import cycle at module load time
    return due_date <= now + datetime.timedelta(days=settings.DUE_SOON_REMINDER_DAYS)


def checkout_display_name(checkout: "AssetCheckout") -> str:
    """
    The equipment name to show for one AssetCheckout row, whether it's a
    real AssetType pool or a Manager/Admin-added OUTSOURCED (not-in-
    inventory) item -- see AssetCheckout.is_outsourced below. Centralizing
    this (rather than repeating `c.asset.name if c.asset else "Unknown
    Asset"` in every service that lists checkouts) is what keeps an
    outsourced checkout's real item name showing up correctly instead of
    falling into the generic "Unknown Asset" fallback that's meant for a
    genuinely deleted/missing asset pool.

    Callers decide for THEMSELVES whether to also surface
    `checkout.is_outsourced` as a separate "Outsourced" flag/badge --
    this function only ever returns the display name, never a hint that
    the item came from outside inventory (see services/user_service.py's
    get_my_assigned_items() vs get_user_assigned_items() for where that
    visibility line is actually drawn).
    """
    if checkout.is_outsourced:
        return checkout.outsourced_item_name or "Outsourced Item"
    return checkout.asset.name if checkout.asset else "Unknown Asset"


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
    return _as_aware_utc(due_date) < utc_now()


class AssetType(Base):
    __tablename__ = "asset_types"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)
    total_quantity = Column(Integer, default=0, nullable=False)
    available_quantity = Column(Integer, default=0, nullable=False)
    custom_fields = Column(JSON, default=dict, nullable=True)

    # --- Category (OPTIONAL) -------------------------------------------
    # Which internal category this pool's equipment belongs to (e.g.
    # "Engineering", "Design"). Purely descriptive/organizational --
    # unlike User.department (below), it does NOT scope who can see or
    # dispatch the pool; every role that can see the Asset Inventory
    # today still sees every pool regardless of this value. Settable when
    # a pool is first registered (POST /assets) or via CSV batch import
    # (POST /assets/import, "category" column), and left NULL when not
    # provided since not every org tracks this. Surfaced next to the
    # pool's POOL-{id} tag in the Asset Inventory table, inside the
    # Properties Hub modal, as a "Category" column on every checked-out-
    # items export, and as the filter categories on the Asset Inventory's
    # own export (see services/asset_service.py's
    # export_assets_inventory() / list_asset_categories()).
    category = Column(String, nullable=True)

    # --- Per-unit price (OPTIONAL) ------------------------------------------
    # The per-unit purchase/replacement price of this pool's equipment (e.g.
    # 1899.00 for a MacBook Pro). Purely informational, same "descriptive,
    # not access-scoping" treatment as `category` above -- every role that
    # can see the Asset Inventory today still sees every pool regardless of
    # price. Settable when a pool is first registered (POST /assets) or via
    # CSV batch import (POST /assets/import, "price" column), and left NULL
    # when not provided since not every org tracks unit cost. Surfaced next
    # to the pool's POOL-{id} tag / category on the Asset Inventory table
    # and inside the Properties Hub modal, editable there the same way
    # `category` is (PUT /assets/{id}/price -- see
    # services/asset_service.py's update_asset_price()). Numeric(10, 2)
    # rather than Float so currency values round-trip exactly (no binary
    # floating-point drift on repeated reads/writes).
    price = Column(Numeric(10, 2), nullable=True)

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

    # --- Purge (frees up `name` for reuse) ---------------------------------
    # Set once, permanently, by services/asset_service.py's
    # purge_asset_type() -- a Super Admin's deliberate "I'm done with this
    # deleted pool, I want its name back" action from the Restore Deleted
    # Assets panel. `name` carries a DB-level `unique=True` constraint (see
    # above), so a soft-deleted pool's original name stays "reserved"
    # forever and can never be reused by a brand-new pool -- purging
    # renames THIS row to a guaranteed-unique placeholder (see
    # purge_asset_type()'s docstring) so the original name frees up, while
    # the row itself is still never hard-deleted (same FK/audit-trail
    # rationale as the comment above) and every historical
    # checkout/exception still resolves to a real (if renamed) row.
    # Nullable with no backfill needed -- every pre-existing pool predates
    # this feature and was never purged, so NULL ("not purged") is correct
    # for all of them. Once set, list_deleted_assets() excludes the row
    # (nothing left to meaningfully restore under its original identity)
    # and restore_asset_type() refuses to bring it back.
    purged_at = Column(DateTime(timezone=True), nullable=True)

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
    """
    Append-only ledger of every meaningful action taken in the app --
    nothing is ever deleted from it by the running application (see
    services/audit_service.py's module docstring), which means it's the
    one table guaranteed to grow for as long as the deployment exists.

    PRODUCTION SCHEMA NOTE -- this table is PARTITIONED, not plain
    ----------------------------------------------------------------
    In any real deployment (i.e. wherever `alembic upgrade head` is the
    source of truth -- see database.py's module docstring), this is
    actually a native Postgres table PARTITIONED BY RANGE on `timestamp`,
    one partition per calendar year, as set up by
    `alembic/versions/0010_partition_audit_logs.py` -- see that
    migration's own module docstring for the full "why" (query pruning +
    instant, VACUUM-free retirement of old years) and
    SRE_STRATEGY.md's "Audit log partitioning & annual archive" section
    for the human runbook that actually retires an old year.
    `services/audit_partition_service.py` keeps future years' partitions
    pre-created (run daily by `tasks/audit_partition_tasks.py`) so writes
    never fail once the calendar rolls over.

    SQLAlchemy's Core/ORM layer has no first-class way to express
    "PARTITION BY" on a declarative model, so this class intentionally
    still looks like an ordinary table -- `Base.metadata.create_all()`
    (database.py's init_db(), used for AUTO_INIT_DB=true local/dev and by
    every SQLite-backed test in tests/, per that function's own docstring
    on why create_all() is a local/dev-only convenience, never the source
    of truth for a real deployment) creates it as a plain, non-partitioned
    table instead. That's deliberate and harmless: a local/demo dataset is
    never going to be large enough to need partitioning, and Alembic (not
    this model, and NOT `create_all()`) is what actually provisions
    production's schema. One consequence worth knowing if you ever run
    `alembic revision --autogenerate`: it diffs the live database against
    THIS model, and since the model can't describe partitioning, it may
    propose changes that don't actually apply here -- read any
    autogenerated diff touching `audit_logs` carefully before trusting it.

    THE PRIMARY KEY BELOW (JUST `id`) DOES NOT MATCH PRODUCTION'S -- ON PURPOSE
    -------------------------------------------------------------------------------
    Postgres requires a partitioned table's primary key / unique
    constraints to include the partition column, so in PRODUCTION,
    0010_partition_audit_logs.py physically creates the REAL primary key
    as `PRIMARY KEY (timestamp, id)` (`timestamp` first, since that's what
    gives every "order by timestamp" / date-range query -- see
    audit_service.py -- a supporting index it never had before). This
    model class deliberately keeps declaring the ORIGINAL plain `id`-only
    primary key instead of trying to mirror that, for two reasons: (1)
    SQLite -- what `create_all()` above actually builds this table with
    for local/dev/tests -- flatly refuses to autoincrement a composite
    primary key at all ("SQLite does not support autoincrement for
    composite primary keys"), and (2) it doesn't matter functionally
    either way: nothing in this app does an identity-based lookup or
    update on an AuditLog row (no foreign key references
    `audit_logs.id`, no `.filter(models.AuditLog.id == ...)`, no
    `session.get(AuditLog, ...)` anywhere -- it's append-only, see above),
    so the ORM never needs its declared primary key to match the
    database's actual one to do the only thing this model is ever used
    for: INSERT. Just know that a raw `\\d audit_logs` against production
    will show a different (correct, wider) key than this class declares.
    """
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

    # Optional, no uniqueness constraint (unlike email/username above) --
    # a phone number is a convenience contact detail, not a login
    # credential, so several accounts legitimately sharing one (e.g. a
    # shared office line) is fine. Editable via UserUpdateRequest exactly
    # like name/username/email.
    phone_number = Column(String, nullable=True)

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

    # "admin" | "manager" | "staff" | "customer" | "super_admin". Unlike
    # the first four, "super_admin" is never assignable through the app's
    # user-provisioning API (services/user_service.py's create_user()
    # blocks it via RESERVED_ROLES) -- the single row with this role is
    # bootstrapped once by alembic/versions/0002_bootstrap_root_admin.py
    # (production) or database.py's seed_db() (local/dev/test only), and
    # every directory/audit listing in the app explicitly filters it out
    # (see security.py's module docstring for the full rationale).
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

    # --- Two-factor authentication (TOTP) -- SECURITY -----------------------
    # Currently enforced ONLY for role == SUPER_ADMIN_ROLE (see
    # services/auth_service.py's login()) -- the single highest-privilege,
    # non-deletable account in the system, and the one an attacker who
    # somehow obtained its password stands to gain the most from. The
    # columns are on every User row (not a separate table) so the same
    # mechanism can be extended to other roles later without another
    # migration.
    #
    # `totp_secret_encrypted` is NEVER stored in plaintext -- see
    # security.py's encrypt_totp_secret()/decrypt_totp_secret(),
    # Fernet-encrypted with a key derived from JWT_SECRET_KEY (same trust
    # boundary as the JWT signing key already: whoever can read
    # JWT_SECRET_KEY can already forge a session for any account, so this
    # adds no new single point of failure without needing its own separate
    # secret/rotation story). `totp_enabled` stays False until the person
    # actually confirms a live code during enrollment (see
    # auth_service.py's mfa_setup_confirm()) -- a secret that's merely been
    # generated and shown once but never confirmed doesn't count as "2FA is
    # protecting this account yet", so login() re-generates it on the next
    # attempt rather than trusting an unconfirmed one.
    totp_secret_encrypted = Column(String, nullable=True)
    totp_enabled = Column(Boolean, default=False, nullable=False)

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

    # --- Purge (frees up email/username for reuse) -------------------------
    # Set once, permanently, by services/user_service.py's purge_user() -- a
    # Super Admin's deliberate "I'm done with this deleted account, I want
    # its email back" action from the Restore Deleted Users panel. Both
    # `email` and `username` carry DB-level `unique=True` constraints (see
    # above), so a merely soft-deleted account's original email/username
    # stay "reserved" forever and can never be reused by a brand-new
    # account -- purging overwrites THIS row's email/username with a
    # guaranteed-unique placeholder (see purge_user()'s docstring) so the
    # originals free up, while the row itself is still never hard-deleted
    # (same FK/audit-trail rationale as the comment above) and every
    # historical checkout/quotation still resolves to a real (if
    # anonymized) row. `name` is left untouched so the Custody Ledger of
    # anything they held onto stays readable.
    # Nullable with no backfill needed -- every pre-existing user row
    # predates this feature and was never purged, so NULL ("not purged")
    # is correct for all of them. Once set, list_deleted_users() excludes
    # the row (nothing left to meaningfully restore under its original
    # identity) and restore_user() refuses to bring it back.
    purged_at = Column(DateTime(timezone=True), nullable=True)

    # --- Department scoping (used by the Manager dashboard) ---
    # `department` groups users into teams (e.g. "Engineering", "Design").
    # A manager only ever sees users/audit activity within their own
    # department; a super_admin sees everything regardless of department.
    # Distinct from AssetType.category above -- that's which category an
    # asset POOL belongs to, purely descriptive; this is which team a
    # PERSON belongs to, and (for Managers) actually scopes visibility.
    department = Column(String, nullable=True)
    department_role = Column(String, nullable=True)  # e.g. "Senior Engineer", "Product Designer"

    # --- Convert-to-outsider traceability (the reverse of Outsider.
    # converted_to_user_id below) ---------------------------------------
    # Set once, permanently, by services/user_service.py's
    # convert_user_to_outsider() the moment a Super Admin/Admin or Manager
    # revokes THIS account's login access, turning it back into an ad-hoc
    # profile (e.g. someone leaving the company but still needing to be
    # tracked as a custody holder). That function migrates every
    # AssetCheckout.user_id / Quotation.assigned_to_id row pointing at
    # THIS account over to the new models.Outsider row instead, then
    # soft-deletes this row the same way delete_user() does -- the account
    # can no longer log in and drops out of the User Directory, but stays
    # queryable forever.
    #
    # Nullable with no backfill needed -- every pre-existing user row was
    # never converted, so NULL ("not converted") is correct for all of
    # them. Mirrors Outsider.converted_to_user_id exactly, just pointed
    # the other way, so "which account did this ad-hoc profile become?"
    # and "which ad-hoc profile did this now-revoked account become?" can
    # both be answered with a plain join.
    converted_to_outsider_id = Column(Integer, ForeignKey("outsiders.id"), nullable=True)

    checkouts = relationship("AssetCheckout", back_populates="user")
    converted_to_outsider = relationship("Outsider", foreign_keys=[converted_to_outsider_id])
    recovery_codes = relationship("RecoveryCode", back_populates="user", cascade="all, delete-orphan")
    password_reset_tokens = relationship("PasswordResetToken", back_populates="user", cascade="all, delete-orphan")


class Outsider(Base):
    __tablename__ = "outsiders"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # Split into two distinct, both-optional fields (previously a single
    # required free-text `contact_details` column that could hold either
    # an email or a phone number, ambiguously). At least one of the two is
    # still enforced at creation time (see services/asset_service.py's
    # checkout_advanced() and services/quotation_service.py's ad-hoc
    # creation branches), but neither column itself carries a NOT NULL --
    # a profile that only ever gave a phone number, or only an email,
    # is common and shouldn't be forced to fabricate the other.
    email = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)
    company = Column(String, nullable=True)

    # --- Soft delete -------------------------------------------------------
    # Same "never hard-delete" reasoning as User.is_deleted/deleted_at
    # above: a models.Outsider row is what AssetCheckout.outsider_id (and
    # Quotation.assigned_outsider_id) actually point at, and every
    # checkout/quotation listing that shows who an item went to reads
    # `checkout.outsider.name` live off this row rather than a frozen
    # snapshot (see services/checkout_service.py's holder_label/
    # assignee_name, services/outsider_service.py's get_outsider_assigned_
    # items()). Hard-deleting the row would silently blank out every past
    # checkout's "assigned to" display. Instead, "deleting" an ad-hoc
    # profile (Admin/Manager, see services/outsider_service.py ->
    # delete_outsider()) just flips these two flags: the profile can no
    # longer be picked for a NEW dispatch/quote assignment and disappears
    # from the Ad-Hoc Directory, but every historical checkout/quotation
    # referencing this outsider_id keeps resolving its name/company/
    # contact exactly as before. Blocked entirely (same as delete_user())
    # while the profile still has items in active custody, so equipment
    # can never silently lose its assignee.
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # --- Convert-to-user traceability ---------------------------------------
    # Set once, permanently, by services/outsider_service.py's
    # convert_outsider_to_user() the moment this ad-hoc individual decides
    # they want a real login after all. That function migrates every
    # AssetCheckout.outsider_id / Quotation.assigned_outsider_id row
    # pointing at THIS profile over to the new models.User row instead
    # (so their custody history keeps working exactly like any other
    # linked user's going forward), then soft-deletes this row the same
    # way delete_outsider() does -- it's no longer a selectable ad-hoc
    # profile for NEW dispatches/quotes, since anyone dispatching to this
    # person from now on should pick their real account instead.
    #
    # This column is what makes that permanent instead of just a line in
    # the Audit Trail: it lets "which ad-hoc profile did this account
    # originally come from?" (or the reverse) be answered with a plain
    # join, exactly like AssetCheckout.quotation_id exists purely to trace
    # a checkout back to the Quotation that created it (see that column's
    # own comment above). Nullable with no backfill needed -- every
    # pre-existing outsider row was never converted, so NULL ("not
    # converted") is correct for all of them.
    converted_to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    checkouts = relationship("AssetCheckout", back_populates="outsider")
    converted_to_user = relationship("User", foreign_keys=[converted_to_user_id])


class AssetCheckout(Base):
    __tablename__ = "asset_checkouts"
    id = Column(Integer, primary_key=True, index=True)
    # Nullable -- a normal checkout always has a real AssetType pool here,
    # but an OUTSOURCED checkout (see is_outsourced below) never does;
    # there's no inventory row to point at.
    asset_id = Column(Integer, ForeignKey("asset_types.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    outsider_id = Column(Integer, ForeignKey("outsiders.id"), nullable=True)

    # Set only when this checkout was created by the Quote-to-Checkout
    # Fulfillment Drawer's bulk physical checkout (see
    # services/quotation_service.py's bulk_checkout_quotation()) --
    # NULL for every "regular" dispatch made directly from the Asset
    # Inventory's own Dispatch drawer. Purely a traceability link back to
    # the originating Quotation; nothing reads it to change behavior.
    # BUG FIX (migration drift): the Alembic baseline migration
    # (alembic/versions/0001_baseline_schema.py) creates a real index here
    # ("ix_asset_checkouts_quotation_id"), but this model was missing
    # `index=True` -- so a fresh database stood up via `Base.metadata.
    # create_all()` (AUTO_INIT_DB=true; see database.py's init_db()) would
    # NOT get this index, while one stood up via `alembic upgrade head`
    # (the production path -- see DEPLOYMENT.md) would. `index=True` here
    # doesn't re-create anything for databases that already ran the
    # migration (Postgres no-ops an index of the same name), it just makes
    # this model's declared schema match what's actually deployed.
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=True, index=True)

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

    # --- Outsourced (not-in-inventory) items -------------------------------
    # Set when this checkout line came from a Manager/Admin-added
    # QuotationOutsourcedItem (equipment that was never part of the Asset
    # Inventory catalog) rather than a real AssetType pool -- `asset_id`
    # is NULL for these rows and the `asset` relationship resolves to
    # None. `outsourced_item_name`/`outsourced_unit_price` snapshot what
    # the Manager/Admin typed in at add-item time (mirrors
    # QuotationOutsourcedItem.name/unit_price below) since there's no
    # catalog row left to join back to once this becomes a real checkout.
    #
    # VISIBILITY: `is_outsourced` is surfaced to a Manager/Admin/Super
    # Admin viewing someone ELSE's Custody Ledger
    # (services/user_service.py's get_user_assigned_items(),
    # services/outsider_service.py's get_outsider_assigned_items()) as an
    # "Outsourced" tag, but deliberately OMITTED from the self-service "My
    # Items" payload (services/user_service.py's get_my_assigned_items())
    # -- the Staff/Customer/Manager/Admin holding the item themselves just
    # sees it listed like any other checked-out item, never told it was
    # sourced from outside inventory. See models.checkout_display_name()
    # for the shared "what name do I show for this row" helper both sides
    # rely on.
    is_outsourced = Column(Boolean, default=False, nullable=False)
    outsourced_item_name = Column(String, nullable=True)
    outsourced_unit_price = Column(Numeric(10, 2), nullable=True)
    # Vendor/supplier this outsourced item came from (e.g. "Fountain
    # Rentals") -- snapshotted from QuotationOutsourcedItem.sourced_from
    # at checkout time. Same Manager/Admin-only visibility as
    # is_outsourced itself.
    outsourced_source = Column(String, nullable=True)

    asset = relationship("AssetType", back_populates="checkouts")
    user = relationship("User", back_populates="checkouts")
    outsider = relationship("Outsider", back_populates="checkouts")
    quotation = relationship("Quotation")
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


class AppSetting(Base):
    """
    Tiny key/value store for the small number of values that must be
    editable at RUNTIME by an Admin -- unlike everything in config.py,
    which is read once from environment variables at process startup and
    requires a restart to change. Today this holds exactly one key,
    "vat_percent" (see services/quotation_service.py's get_vat_percent()/
    set_vat_percent()), but is generic on purpose so a future runtime-
    editable value doesn't need its own bespoke table.

    Only a Super Admin/Admin can write here (PUT /settings/vat --
    require_super_admin, same gate as update_asset_price()), and every
    change is logged to AuditLog the same way any other admin-only
    mutation in this app is.
    """
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_by = Column(String, nullable=True)  # email of the Admin/Super Admin who last changed it
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=True)


class Quotation(Base):
    """
    A staff/customer account's self-service equipment rental request --
    the "shopping cart" a person builds while browsing the Asset
    Inventory Catalog (see services/quotation_service.py) before either
    sharing it with their manager offline as an exported PDF, or
    formally SUBMITTING it (see `status`/`reference_number` below) so an
    Admin/Manager can pull it up by its quote ID, adjust it, and assign
    it to a user.

    LIFECYCLE: each account has at most one row with `status="draft"` at
    a time (their standing "My Order" cart -- adding an item lazily
    creates this row the first time, see
    quotation_service._get_or_create_draft()). Calling
    quotation_service.submit_my_quotation() stamps that row with a
    generated `reference_number` (e.g. "QT-000001"), flips it to
    `status="submitted"`, and records `submitted_at` -- it then becomes
    read-only to the person who created it (they see it in "My Order"
    history but no longer edit it) and visible/editable to any
    Admin/Manager via the "Quotes" tab. The NEXT item that same person
    adds to their cart lazily creates a brand new `status="draft"` row,
    exactly like the old one-cart-forever behavior. `user_id` therefore
    is NOT unique any more (a person accumulates one row per submitted
    quote, plus at most one open draft).

    QUOTE-TO-CHECKOUT WORKFLOW (status="submitted" -> "approved" ->
    "fulfilled"): a submitted Quotation sits in the Admin/Manager "Quotes"
    master queue where it can still be adjusted (items/notes/assignment)
    exactly like before. Calling quotation_service.approve_quotation()
    flips it to `status="approved"` and stamps `approved_at`/
    `approved_by_id` -- this is the gray "Draft"-style badge turning into
    the sharp green "Approved / Ready for Pickup" badge the frontend
    renders (see components/quotation.js's quotationStatusBadge()).
    Approval only locks the REQUESTER/assignee's own self-service side
    (see quotation_service._get_own_editable_quotation(), which requires
    `status == "submitted"`) -- an Admin/Manager can still keep adjusting
    items/notes/assignment on an approved quote (see
    _ensure_admin_editable() in services/quotation_service.py) right up
    until quotation_service.bulk_checkout_quotation() -- the Fulfillment
    Drawer's "physical bulk checkout" action -- turns EVERY line item
    into a real AssetCheckout row (see AssetCheckout.quotation_id) in one
    atomic transaction, flips this row to `status="fulfilled"`, and
    stamps `fulfilled_at`/`fulfilled_by_id`. THAT is the point the quote
    is truly closed -- fulfilled quotes are locked against every further
    edit, by anyone, Admin/Manager included. Inventory stock is NEVER
    reserved or deducted at "draft", "submitted", or "approved" --
    `AssetType.available_quantity` is only ever touched at this final
    "fulfilled" step (see services/stock.py's recalculate_asset_stock()),
    exactly like a normal single-asset checkout.

    `assigned_to_id` is who an Admin/Manager has designated the quote is
    FOR (who will ultimately receive/check out the equipment) -- distinct
    from `user_id` (who originally built/submitted the request), since a
    manager might submit or adjust a quote on behalf of someone else. It's
    also who bulk_checkout_quotation() dispatches the equipment TO (falling
    back to `user_id`, the original requester, when never explicitly
    assigned).

    Line-item pricing is NEVER snapshotted onto this row or its items --
    every read/export always joins back to the live AssetType.price and
    the live global VAT setting (AppSetting["vat_percent"]), so an
    Admin's global price or VAT edit is reflected immediately in every
    saved order, exactly like "the price changes globally through the
    Admin's edit" from the feature request. `discount_percent` (below) is
    the one exception -- it's a per-quote negotiated value, not a global
    setting, so it IS stored directly on this row.
    """
    __tablename__ = "quotations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    # --- Submission workflow (Quotation ID lookup + Admin/Manager adjust + assign) ---
    # BUG FIX (migration drift): same issue/fix as AssetCheckout.quotation_id
    # above -- the baseline migration creates "ix_quotations_status" but this
    # column was missing `index=True`, so create_all() (dev/local, when
    # AUTO_INIT_DB=true) and `alembic upgrade head` (production) produced two
    # different schemas. Also genuinely useful here: the Quotation Catalog
    # and Admin/Manager dashboards filter quotations by status constantly.
    status = Column(String, default="draft", nullable=False, index=True)  # "draft" | "submitted" | "approved" | "fulfilled"
    reference_number = Column(String, nullable=True, index=True, unique=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    assigned_to_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Ad-Hoc (unlinked) counterpart to assigned_to_id -- set instead of
    # assigned_to_id when an Admin/Manager assigns this Quotation to an
    # Ad-Hoc Individual rather than a linked Staff/Customer account,
    # exactly like AssetCheckout's own user_id/outsider_id split. At most
    # one of assigned_to_id/assigned_outsider_id is ever set at a time --
    # see services/quotation_service.py's admin_create_quotation()/
    # assign_quotation(). bulk_checkout_quotation() (the Fulfillment
    # Drawer) checks an Ad-Hoc-assigned quote out to this Outsider
    # directly, same as it would a linked user.
    assigned_outsider_id = Column(Integer, ForeignKey("outsiders.id"), nullable=True)
    notes = Column(String, nullable=True)

    # --- Quote-to-Checkout workflow: who/when approved this request (the
    # gray "Draft"/"Pending" -> green "Approved / Ready for Pickup" badge
    # flip) and who/when it was physically bulk-checked-out. Both are
    # nullable/None until that step actually happens. See the class
    # docstring above for the full lifecycle. ---
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    fulfilled_at = Column(DateTime(timezone=True), nullable=True)
    fulfilled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # --- Discount (Admin/Manager-only, per-quote) ---------------------------
    # A single percentage (0-100) knocked off THIS quote's subtotal, before
    # VAT is calculated on what remains -- see services/quotation_service.py's
    # _serialize_quotation() for the exact
    # subtotal -> discount -> VAT -> grand total order. Defaults to 0 (no
    # discount) so every pre-existing quote and every brand new one behaves
    # exactly like before this field existed unless an Admin/Manager
    # deliberately sets it. Editable via update_quotation_discount() under
    # the exact same _ensure_admin_editable() lock as every other line
    # item/note/assignment edit on this quote -- i.e. right up until
    # `status="fulfilled"`, same as "the other quote items" per the feature
    # request. Unlike price/VAT, this IS snapshotted onto the row itself
    # (rather than re-derived from a global setting) since it's a
    # per-quote negotiated concession, not a store-wide policy.
    discount_percent = Column(Numeric(5, 2), default=0, nullable=False)

    user = relationship("User", foreign_keys=[user_id])
    assigned_to = relationship("User", foreign_keys=[assigned_to_id])
    assigned_outsider = relationship("Outsider", foreign_keys=[assigned_outsider_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    fulfilled_by = relationship("User", foreign_keys=[fulfilled_by_id])
    items = relationship("QuotationItem", back_populates="quotation", cascade="all, delete-orphan", order_by="QuotationItem.id")
    # Manager/Admin-only "not currently in inventory" lines -- see
    # QuotationOutsourcedItem's own docstring below for the full rationale.
    outsourced_items = relationship(
        "QuotationOutsourcedItem", back_populates="quotation", cascade="all, delete-orphan", order_by="QuotationOutsourcedItem.id",
    )


class QuotationItem(Base):
    """
    One line of a Quotation: "N units of this AssetType pool, from
    start_date to due_date". `quantity` is editable in place (unlike
    AssetCheckout.quantity, there's no real checkout yet to preserve a
    historical record of -- this is still just a draft request), and a
    row is hard-deleted outright when the person removes that asset from
    their order (see services/quotation_service.py's remove_item()) --
    there's no soft-delete/audit requirement for a draft cart line the
    way there is for actual checkouts.

    `start_date`/`due_date` are plain calendar Dates (not TIMESTAMPTZ --
    see the timezone note at the top of this file) since a rental period
    is a whole-day concept ("July 12 through July 15"), not a precise
    instant; the number of rental days -- `(due_date - start_date).days`,
    floored at 1 so a same-day request still bills a full day -- is what
    services/quotation_service.py multiplies against AssetType.price and
    `quantity` to get this line's total, computed fresh on every read.
    """
    __tablename__ = "quotation_items"

    id = Column(Integer, primary_key=True, index=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("asset_types.id"), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    start_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    added_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    quotation = relationship("Quotation", back_populates="items")
    asset = relationship("AssetType")


class QuotationOutsourcedItem(Base):
    """
    A Manager/Admin-only line on a Quotation for equipment that is NOT
    currently part of the Asset Inventory catalog -- e.g. a specialty
    rental the business has to source externally to fulfill a request.
    Added exclusively by services/quotation_service.py's
    admin_add_outsourced_item() (POST /quotations/{id}/outsourced-items,
    require_privileged_role-gated) -- there is no equivalent self-service
    "add to my order" route for this table, which is what makes it
    structurally impossible for a Staff/Customer account to add, edit, or
    remove one of these lines even though they CAN see it once a
    Manager/Admin adds it during review: it's merged into the same
    "items" array a Quotation's regular QuotationItem lines already use,
    flagged `is_outsourced=True`, by _serialize_quotation() in
    services/quotation_service.py -- the requester's own item-level
    routes (PUT/DELETE /quotations/me/{id}/items/{item_id}) only ever
    query the QuotationItem table, so they 404 if pointed at one of
    these, and the frontend never even renders their qty/remove controls
    for a flagged-outsourced line to begin with.

    Unlike QuotationItem (which always prices itself LIVE off the joined
    AssetType.price, so an Admin's global price edit is reflected
    immediately -- see that model's docstring), this table stores its OWN
    `unit_price`: there is no catalog row to join back to, so whatever a
    Manager/Admin typed in when adding the item is the price for that
    line, permanently, exactly like typing a one-off price into an
    invoice.

    QUOTE-TO-CHECKOUT: when the Fulfillment Drawer bulk-checks-out an
    approved Quotation (services/quotation_service.py's
    bulk_checkout_quotation()), each row here becomes a real
    AssetCheckout with `asset_id=NULL` and `is_outsourced=True` (see
    AssetCheckout's own docstring/comment) -- never touching any
    AssetType's stock, since there's no inventory pool to deduct from in
    the first place.
    """
    __tablename__ = "quotation_outsourced_items"

    id = Column(Integer, primary_key=True, index=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    unit_price = Column(Numeric(10, 2), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    # Which external vendor/supplier this line is being sourced from (e.g.
    # "Fountain Rentals") -- purely an internal Manager/Admin tracking
    # note, free-text, optional. Snapshotted onto the resulting
    # AssetCheckout.outsourced_source at checkout time (see
    # bulk_checkout_quotation()) so the sourcing trail survives past this
    # row's own lifetime. Never shown to the requester -- see
    # _serialize_quotation()'s `reveal_sourcing` parameter below.
    sourced_from = Column(String, nullable=True)
    start_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    # Which Manager/Admin added this line -- NULL only for the single
    # hardcoded Super Admin identity (not a real `users` row -- same FK
    # caveat as Quotation.approved_by_id/fulfilled_by_id, see
    # services/quotation_service.py's approve_quotation()).
    added_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    added_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    quotation = relationship("Quotation", back_populates="outsourced_items")
    added_by = relationship("User", foreign_keys=[added_by_id])


class RecoveryCode(Base):
    """
    2FA backup/recovery codes -- see models.py's User.totp_enabled
    docstring for the enrollment side of this feature, and
    services/auth_service.py's login()'s SECURITY note for why 2FA is
    currently required for role == SUPER_ADMIN_ROLE specifically.

    A fresh batch (see security.py's generate_recovery_codes(), 10 codes
    by default) is issued -- and every previously-issued code for that
    user invalidated -- at two points: (1) the moment 2FA enrollment is
    first confirmed (auth_service.py's mfa_setup_confirm()), and (2)
    whenever the account holder explicitly regenerates them
    (auth_service.py's regenerate_recovery_codes(), which requires
    re-entering the current password first -- see that function). Each
    row is ONE single-use code: `mfa_verify()` accepts a correct,
    still-unused code as a full substitute for a TOTP code (e.g. "I lost
    my phone but still have the codes I saved"), and immediately stamps
    `used_at` so it can never be replayed.

    `code_hash` is never the plaintext code -- hashed with the exact same
    Argon2id `hash_password()`/`verify_password()` pair used for account
    passwords (security.py), not reversibly encrypted like
    User.totp_secret_encrypted, because nothing ever needs to read a
    recovery code back out -- only verify a guess against it, same as a
    password.
    """
    __tablename__ = "recovery_codes"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    code_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    # NULL == still valid/unused. Rows are never deleted on use (only
    # stamped) so there's an audit trail of when each one was consumed;
    # they're deleted wholesale only when a fresh batch replaces them
    # (mfa_setup_confirm() / regenerate_recovery_codes() above).
    used_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="recovery_codes")


class PasswordResetToken(Base):
    """
    "Forgot password?" tokens -- see services/auth_service.py's
    request_password_reset()/confirm_password_reset() for the full flow.

    Exists specifically so the SUPER_ADMIN_ROLE account (the one identity
    in the system with no admin "above" it who could otherwise reset a
    locked-out password for it -- see services/user_service.py ->
    reset_user_password()'s is_hidden_root_admin() guard) has a real
    self-recovery path. Not restricted to that role at the model/service
    layer, though -- any account can request one, same self-service
    reasoning as update_password()/update_identity().

    Same hashed, single-use, DB-backed shape as RecoveryCode above (see
    that model's docstring) rather than a stateless JWT: a mailed link
    needs to be explicitly revocable (a second request must invalidate an
    earlier still-unused one) and needs a real "already used" record, and
    a bare JWT can do neither of those without extra bookkeeping of its
    own -- so this reuses the same hash_password()/verify_password() +
    `used_at` timestamp pattern already established here instead of
    inventing a second, JWT-based mechanism for the same kind of problem.

      - `token_hash`  -- Argon2id hash of the plaintext token emailed to
                         the account's registered address. The plaintext
                         is never stored -- only ever available once, at
                         the moment request_password_reset() generates it
                         (see that function).
      - `expires_at`  -- short-lived on purpose (see config.py's
                         PASSWORD_RESET_TOKEN_EXPIRY_MINUTES) -- a mailed
                         link sitting in an inbox indefinitely would
                         otherwise stay a standing way into the account.
      - `used_at`     -- NULL until consumed; stamped (not deleted) on
                         use, same audit-trail reasoning as
                         RecoveryCode.used_at.
    """
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="password_reset_tokens")
