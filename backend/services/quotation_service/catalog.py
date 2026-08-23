"""
services/quotation/catalog.py
--------------------------------
Public runtime config, the admin-editable global VAT setting, and the
read-only Asset Catalog a caller browses to build a Quotation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import models
from config import settings
from sqlalchemy.orm import Session

from schemas.quotations_schema import VatUpdateRequest
from services.search_utils import apply_search_filter

from .constants import CATALOG_DEFAULT_LIMIT, CATALOG_MAX_LIMIT, VAT_SETTING_KEY, _DEFAULT_VAT_PERCENT
from .helpers import _inventory_status_label


def get_public_config(db: Session | None = None) -> dict:
    # Maintenance is runtime-editable, unlike the environment-backed values below.
    if db is None:
        from database import SessionLocal
        db = SessionLocal()
        try:
            status = __import__("services.maintenance_service", fromlist=["get_status"]).get_status(db)
        finally:
            db.close()
    else:
        status = __import__("services.maintenance_service", fromlist=["get_status"]).get_status(db)
    return {
        "currency_code": settings.CURRENCY_CODE,
        "show_stock_to_staff_customer": settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER,
        "site_name": settings.SITE_NAME,
        "maintenance_mode": status["enabled"],
        "maintenance_message": status["message"],
        # This is deliberately a boolean only. It lets the browser obey the
        # SAME master OTEL_ENABLED switch without exposing exporter URLs,
        # credentials, or any backend-only telemetry configuration.
        "otel_enabled": (
            settings.OTEL_ENABLED
            and bool(settings.OTEL_EXPORTER_OTLP_ENDPOINT)
            and settings.OTEL_EXPORTER_OTLP_PROTOCOL.lower() != "grpc"
        ),
        # Reuse the backend sampling decision so browser-created root traces
        # have the same approximate sampling rate as backend-created roots.
        "otel_trace_sample_ratio": settings.OTEL_TRACES_SAMPLE_RATIO,
    }


# ---------------------------------------------------------------------------
# VAT setting (admin-editable, applies globally to every Quotation)
# ---------------------------------------------------------------------------

def get_vat_percent(db: Session) -> Decimal:
    row = db.query(models.AppSetting).filter(models.AppSetting.key == VAT_SETTING_KEY).first()
    if row is None:
        return _DEFAULT_VAT_PERCENT
    try:
        return Decimal(row.value)
    except Exception:
        return _DEFAULT_VAT_PERCENT

def set_vat_percent(db: Session, payload: VatUpdateRequest, user: dict) -> dict:
    row = db.query(models.AppSetting).filter(models.AppSetting.key == VAT_SETTING_KEY).first()
    previous = row.value if row else "0"
    if row is None:
        row = models.AppSetting(key=VAT_SETTING_KEY, value=str(payload.vat_percent), updated_by=user["email"])
        db.add(row)
    else:
        row.value = str(payload.vat_percent)
        row.updated_by = user["email"]
    db.add(models.AuditLog(
        operator=user["email"], action="VAT_UPDATED", target_type="AppSetting", target_id=0,
        details=f"Changed global VAT from {previous}% to {payload.vat_percent}% -- applies to every saved Quotation immediately.",
    ))
    db.commit()
    return {"vat_percent": payload.vat_percent}


# ---------------------------------------------------------------------------
# Asset Catalog (read-only, for building a Quotation)
# ---------------------------------------------------------------------------

def list_catalog(db: Session, user: dict, limit: int = CATALOG_DEFAULT_LIMIT, offset: int = 0, search: Optional[str] = None) -> dict:
    """
    Every active (non-soft-deleted) asset pool, shaped for the self-
    service Quotation Catalog. Name/category/price are always
    included; available_quantity/status are included only when
    settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is True OR the caller
    is a Manager/Admin/Super Admin (whose own Asset Inventory view
    already shows full stock detail today, so hiding it here would be a
    downgrade for them, not a privacy boundary).

    PAGINATION + SEARCH -- same true server-side `limit`/`offset`/`search`
    contract as services/asset_service.py's list_assets() (which the Asset
    Inventory page, User/Ad-Hoc directories, and Audit Trail already use):
    `search` narrows to pools whose name OR category contains it
    (case-insensitive), applied and counted BEFORE the offset/limit slice
    so `total` always reflects the filtered set. `limit`/`offset` default
    to CATALOG_DEFAULT_LIMIT (generous enough to cover any realistic
    catalog in one page) so existing callers that never pass pagination
    params -- e.g. the Admin/Manager Quote Detail drawer's "Add another
    asset" typeahead, which needs the catalog client-side to search
    against -- keep working unchanged; the Quotations page's own
    browsable catalog table passes real limit/offset/search to get a true
    paged, server-searched view (mirrors PaginationBar's "Asset Inventory,
    User Directory, ..., Quotations" doc comment in lib/pagination.ts).
    Regardless of what any caller passes (or omits), CATALOG_MAX_LIMIT is
    a hard server-side ceiling -- see its comment above -- so this can
    never be turned into an unbounded "return everything" query.
    """
    full_admin_roles = ("super_admin", "admin", "manager")
    show_stock = user["role"] in full_admin_roles or settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER

    limit = max(1, min(limit, CATALOG_MAX_LIMIT))
    offset = max(0, offset)

    query = db.query(models.AssetType).filter(~models.AssetType.is_deleted)
    query = apply_search_filter(query, search, [models.AssetType.name, models.AssetType.category, models.AssetType.department])
    query = query.order_by(models.AssetType.name)
    total = query.count()
    pools = query.offset(offset).limit(limit).all()

    items = []
    for p in pools:
        entry = {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "department": p.department,
            "price": float(p.price) if p.price is not None else None,
        }
        if show_stock:
            entry["available_quantity"] = p.available_quantity
            entry["status"] = _inventory_status_label(p.available_quantity)
        items.append(entry)

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "show_stock": show_stock,
        "currency_code": settings.CURRENCY_CODE,
    }
