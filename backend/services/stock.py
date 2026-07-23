"""
services/stock.py
------------------
The Asset Isolation & Recovery Logic formula, isolated in its own module
since it's shared across the asset, checkout, and CSV-import services:

    Available = Total Capacity - Outbound - Isolated

  Total Capacity -> asset.total_quantity (a fixed number an admin sets;
                    isolating/recalling a unit never changes this number).
  Outbound       -> sum of `quantity` across all ACTIVE checkouts for this
                    asset (units currently out in the field).
  Isolated       -> count of AssetException rows for this asset whose
                    isolation_status is still "isolated" (Under Repair /
                    Stolen / Missing that haven't been recalled yet).

Every place in the API that changes checkouts, isolates a unit, recalls a
unit, or adjusts total capacity calls `recalculate_asset_stock()`
afterwards. That's what guarantees the formula is enforced consistently
everywhere instead of being re-implemented (and potentially getting out of
sync) in several different service functions.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func

import models


def recalculate_asset_stock(db: Session, asset: "models.AssetType") -> dict:
    """
    Recomputes and PERSISTS asset.available_quantity from scratch using the
    live checkout + exception tables, then returns a small breakdown dict
    the API/frontend can render directly. Call this after ANY operation
    that isolates, recalls, checks out, or returns units for this asset so
    the "Available" count is always immediately accurate.
    """
    outbound = db.query(func.coalesce(func.sum(
        models.AssetCheckout.quantity - models.AssetCheckout.quantity_returned
    ), 0)).filter(
        models.AssetCheckout.asset_id == asset.id,
        models.AssetCheckout.status == "active",
    ).scalar() or 0

    isolated = db.query(func.count(models.AssetException.id)).filter(
        models.AssetException.asset_type_id == asset.id,
        models.AssetException.isolation_status == "isolated",
    ).scalar() or 0

    available = asset.total_quantity - outbound - isolated
    asset.available_quantity = available  # keep the cached column in sync

    return {
        "total": asset.total_quantity,
        "outbound": outbound,
        "isolated": isolated,
        "available": available,
    }
