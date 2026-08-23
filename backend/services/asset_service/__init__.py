"""
services/asset/
-----------------
All business logic for AssetType pools: create/list/detail/quantity/delete,
maintenance exceptions (isolate/recall), reconciliation check-ins, the
advanced checkout flow, CSV batch import, and CSV/PDF inventory export.
Used by api/assets.py.

  constants.py    -- role-visibility gates + pagination/upload limits
  helpers.py       -- serializer, price validation, visibility checks
  crud.py           -- create/list/detail/update/delete/restore/purge
  activity.py        -- Dashboard checkout/return activity chart
  exceptions.py       -- isolate/recall maintenance exceptions
  checkout.py           -- reconciliation check-in + advanced dispatch
  csv_import.py          -- batch CSV import
  export.py                -- CSV/PDF inventory export
"""

from __future__ import annotations

from .constants import DEFAULT_LIMIT, MAX_LIMIT, MAX_CSV_UPLOAD_BYTES  # noqa: F401
from .helpers import (  # noqa: F401
    _can_see_custody,
    _can_see_stock,
    _coerce_asset_price,
    _inventory_status_label,
    _serialize_asset_type,
)
from .crud import (  # noqa: F401
    create_asset_type,
    delete_asset_type,
    get_asset_details,
    list_asset_categories,
    list_assets,
    list_deleted_assets,
    purge_asset_type,
    restore_asset_type,
    update_asset_category,
    update_asset_department,
    update_asset_name,
    update_asset_price,
    update_asset_quantity,
)
from .activity import get_activity  # noqa: F401
from .exceptions import flag_asset_exception, recall_asset_exception  # noqa: F401
from .checkout import checkin_asset, checkout_advanced  # noqa: F401
from .csv_import import import_assets_from_csv  # noqa: F401
from .export import (  # noqa: F401
    _inventory_export_names,
    _inventory_export_query,
    _inventory_export_row,
    export_assets_inventory_csv,
    export_assets_inventory_pdf,
)
