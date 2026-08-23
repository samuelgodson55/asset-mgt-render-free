"""
services/quotation/
--------------------
The Quote-to-Checkout workflow: a self-service cart (draft -> submit),
Admin/Manager review and mutation (approve/assign/discount/notes),
physical fulfillment (stock-locked bulk checkout), PDF export, and the
in-app notification feed tied to it all. See models.py's Quotation
docstring for the full draft -> submitted -> approved -> fulfilled
lifecycle this package implements.

This top-level module just re-exports the public API of each submodule so
`services.quotation_service` (the package's own name, see the sibling
services/quotation_service.py shim -- actually this package itself IS
services/quotation_service, see below) keeps working exactly like the old
single-file module:

  constants.py     -- pagination limits, VAT setting key/default
  helpers.py        -- money/date/reference-number/brief-dict formatting
  notifications.py  -- in-app QuotationNotification feed
  catalog.py         -- public config, VAT admin setting, Asset Catalog
  serialize.py        -- the one shared Quotation -> dict shaping function
  cart.py               -- self-service draft/cart mutations
  admin.py               -- Admin/Manager review, assignment, approval
  fulfillment.py           -- the stock-locked bulk checkout transaction
  pdf_export.py             -- printable Quotation PDF
"""

from __future__ import annotations

from config import settings  # noqa: F401 -- re-exported: tests patch quotation_service.settings
import services.notification_service as notification_service  # noqa: F401 -- re-exported: tests patch quotation_service.notification_service

from .constants import (  # noqa: F401
    CATALOG_DEFAULT_LIMIT,
    CATALOG_MAX_LIMIT,
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MY_HISTORY_MAX_ROWS,
    QUOTATION_NOTIFICATIONS_MAX_ROWS,
    TWO_PLACES,
    VAT_SETTING_KEY,
    _DEFAULT_VAT_PERCENT,
)
from .helpers import (  # noqa: F401
    _display_reference,
    _inventory_status_label,
    _money,
    _outsider_brief,
    _reference_number,
    _rental_days,
    _user_brief,
)
from .notifications import (  # noqa: F401
    _notify_quotation_recipient,
    list_my_quotation_notifications,
    mark_quotation_notifications_read,
)
from .catalog import get_public_config, get_vat_percent, set_vat_percent, list_catalog  # noqa: F401
from .serialize import _get_or_create_draft, _serialize_quotation  # noqa: F401
from .cart import (  # noqa: F401
    add_item,
    add_my_submitted_item,
    get_my_quotation,
    get_my_submitted_quotation_detail,
    _get_own_editable_quotation,
    _get_own_or_assigned_quotation_or_404,
    list_my_submitted_quotations,
    remove_item,
    remove_my_submitted_item,
    submit_my_quotation,
    update_item_quantity,
    update_my_submitted_item_quantity,
)
from .admin import (  # noqa: F401
    _ensure_admin_editable,
    _get_quotation_or_404,
    admin_add_item,
    admin_add_outsourced_item,
    admin_create_quotation,
    admin_remove_item,
    admin_remove_outsourced_item,
    admin_update_item_quantity,
    approve_quotation,
    assign_quotation,
    delete_quotation,
    get_quotation_detail,
    list_quotations,
    mark_quotation_paid,
    update_quotation_discount,
    update_quotation_meta,
)
from .fulfillment import bulk_checkout_quotation, get_fulfillment_queue  # noqa: F401
from .pdf_export import (  # noqa: F401
    STATUS_LABELS,
    _DEFAULT_TERMS,
    _build_quotation_pdf,
    _quotation_rental_duration,
    export_my_quotation_pdf_by_id,
    export_quotation_pdf,
    export_quotation_pdf_by_id,
)
