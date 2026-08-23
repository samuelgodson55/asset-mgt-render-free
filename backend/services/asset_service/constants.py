"""
services/asset/constants.py
------------------------------
Role-visibility gates and pagination/upload-size limits shared across the
asset package.
"""

# --- Stock/custody visibility gates -----------------------------------
# Mirrors services/quotation_service.py's list_catalog() / lib/roles.ts's
# canSeeStock()/isPrivileged(): a Manager/Admin/Super Admin always sees
# live stock counts (available/total quantity, in-stock/low/out status);
# a Staff/Customer sees them only when
# settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is on. This used to be a
# frontend-only distinction -- GET /assets, GET /assets/{id}/details, and
# the CSV/PDF export all returned full stock data to any authenticated
# role regardless of this flag, so a Staff/Customer calling the API
# directly (curl, Postman, a modified frontend) could always see it no
# matter what the setting said or what the UI chose to render. These
# helpers, and the call sites below that use them, are what actually
# enforce the setting server-side.
_STOCK_VISIBLE_ROLES = ("super_admin", "admin", "manager")

# Custody data -- who currently holds each unit, org-wide (assignee_name/
# assignee_type/quantity across every active checkout against a pool) --
# is a DIFFERENT category of information than stock counts, and is never
# gated by CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER: it's who-has-what, not
# how-much-is-available. Mirrors deps.py's require_privileged_role /
# lib/roles.ts's isPrivileged() (used as AssetDrawer's `canDispatch`
# gate) -- Super Admin, Admin, or Manager only, regardless of the stock
# flag's value.
_CUSTODY_VISIBLE_ROLES = ("super_admin", "admin", "manager")

# Same reasoning as user_service.DEFAULT_LIMIT/MAX_LIMIT -- bounds how many
# asset pools a single request can return (Data Quality & Usability
# requirement #4).
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000

# SECURITY: caps how large a CSV import upload can be, in bytes (5 MiB).
# Without a cap, `POST /assets/import` would read an attacker-supplied file
# of ANY size fully into memory (see `file.file.read()` below) before doing
# any validation at all -- a trivial denial-of-service vector (upload a
# multi-gigabyte file repeatedly to exhaust server memory). 5 MiB is
# generous for a plain-text CSV of asset names/quantities (tens of
# thousands of rows) while still bounding worst-case memory use per request.
MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024
