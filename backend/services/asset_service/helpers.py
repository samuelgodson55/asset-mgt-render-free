"""
services/asset/helpers.py
-----------------------------
Role-visibility checks, the AssetType -> API response serializer, price
validation/normalization, and the inventory status label used by both the
JSON list endpoints and the CSV/PDF export.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

import models
from config import settings

from .constants import _CUSTODY_VISIBLE_ROLES, _STOCK_VISIBLE_ROLES


def _can_see_stock(user: dict) -> bool:
    return user["role"] in _STOCK_VISIBLE_ROLES or settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER


def _can_see_custody(user: dict) -> bool:
    return user["role"] in _CUSTODY_VISIBLE_ROLES

def _serialize_asset_type(asset: "models.AssetType", show_stock: bool) -> dict:
    """
    Shapes an AssetType row for the Asset Inventory list, gating the
    stock-derived fields the same way services/quotation_service.py's
    list_catalog() already gates them for the Quotation Catalog. Name/
    category/price are descriptive, not stock, so they're always
    included -- unchanged from before this fix.
    """
    entry = {
        "id": asset.id,
        "name": asset.name,
        "category": asset.category,
        "department": asset.department,
        "price": float(asset.price) if asset.price is not None else None,
        "custom_fields": asset.custom_fields,
    }
    if show_stock:
        entry["total_quantity"] = asset.total_quantity
        entry["available_quantity"] = asset.available_quantity
    return entry

def _coerce_asset_price(raw_price) -> tuple[Optional[Decimal], Optional[str]]:
    """Validate and normalize per-unit prices for the Numeric(10, 2) DB column."""
    if raw_price is None:
        return None, None
    if isinstance(raw_price, bool):
        return None, "price must be a valid number."

    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError):
        return None, "price must be a valid number."

    price = price.quantize(Decimal("0.01"))
    if price < 0:
        return None, "price cannot be negative."
    if price > Decimal("99999999.99"):
        return None, "price exceeds the supported maximum of 99,999,999.99."
    return price, None

def _inventory_status_label(available_quantity: int) -> str:
    """Mirrors js/ui.js's statusBadge() thresholds exactly, so a pool that
    reads 'Critical Low Stock' on screen exports with that same label."""
    return "Critical Low Stock" if available_quantity <= 3 else "In Stock"
