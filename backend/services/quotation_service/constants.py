"""
services/quotation/constants.py
----------------------------------
Shared constants for the quotation package -- pagination limits, VAT
setting key/default, and rounding precision.
"""

from decimal import Decimal

VAT_SETTING_KEY = "vat_percent"
_DEFAULT_VAT_PERCENT = Decimal("0")

# Pagination defaults for list_catalog() -- CATALOG_DEFAULT_LIMIT is
# deliberately generous (not the small page size the UI defaults to) so a
# caller that omits limit/offset entirely -- e.g. the Admin/Manager Quote
# Detail drawer's "Add another asset" typeahead, which needs the whole
# catalog client-side to filter against -- still gets it in one response
# for any realistically-sized catalog. CATALOG_MAX_LIMIT is the part that
# actually matters for stability: it's a hard ceiling enforced BOTH by
# FastAPI's `le=` on the query param AND again in list_catalog() itself,
# so no caller -- whatever limit they pass, or if they pass none at all --
# can ever force a response larger than 1000 rows. There is no "give me
# everything" escape hatch from this cap. Mirrors services/asset_service.py's
# DEFAULT_LIMIT/MAX_LIMIT.
CATALOG_DEFAULT_LIMIT = 500
CATALOG_MAX_LIMIT = 1000

TWO_PLACES = Decimal("0.01")

DEFAULT_LIST_LIMIT = 10
MAX_LIST_LIMIT = 100

# Defensive server-side ceiling for list_my_submitted_quotations() -- that
# endpoint takes no limit/offset params (it's a self-service "my history"
# panel, not a paged directory), so it needs its own hard cap rather than
# relying on a caller-supplied limit. Generous enough that no real account
# will ever hit it in normal use, but guarantees the query can never grow
# unbounded.
MY_HISTORY_MAX_ROWS = 2000

# Server-side ceiling for list_my_quotation_notifications() -- same
# reasoning as MY_HISTORY_MAX_ROWS above.
QUOTATION_NOTIFICATIONS_MAX_ROWS = 50
