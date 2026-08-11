"""
api/assets.py
-------------
Everything under /assets: pool CRUD, capacity, maintenance exceptions,
reconciliation check-in, the advanced checkout flow, and CSV batch import.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Response
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user, require_super_admin, require_privileged_role
from schemas.assets_schema import AssetTypeCreate, ExceptionCreate, AdvancedCheckoutRequest, QuantityUpdateRequest, NameUpdateRequest, CategoryUpdateRequest, PriceUpdateRequest
import services.asset_service as asset_service

router = APIRouter(prefix="/assets", tags=["assets"])

# Shared by the export route below -- same "unsupported format" validation
# as api/users.py's/api/outsiders.py's export routes.
_VALID_EXPORT_FORMATS = ("csv", "pdf")


def _validate_export_format(format: str) -> str:
    fmt = format.lower()
    if fmt not in _VALID_EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'pdf'.")
    return fmt


@router.post("", response_model=dict)
def create_asset_type(asset: AssetTypeCreate, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.create_asset_type(db, asset, user)


@router.get("")
def list_assets(
    limit: int = Query(asset_service.DEFAULT_LIMIT, ge=1, le=asset_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large inventory)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against asset pool name"),
    category: Optional[str] = Query(None, description="Narrow to one category (exact, case-insensitive). 'Uncategorized' matches pools with no category set; omit or pass 'all' for every pool."),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return asset_service.list_assets(db, user, limit, offset, search, category)


@router.get("/deleted")
def get_deleted_assets(
    limit: int = Query(asset_service.DEFAULT_LIMIT, ge=1, le=asset_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large list)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against asset pool name"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_super_admin),
):
    """Lists soft-deleted asset pools so a Super Admin can find one to restore. Placed ahead of /{asset_id}/... routes below purely for readability -- 'deleted' can never collide with a numeric {asset_id} path segment."""
    return asset_service.list_deleted_assets(db, limit, offset, search)


@router.get("/categories")
def get_asset_categories(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Distinct category list, powering the Asset Inventory Export button's per-category download options."""
    return asset_service.list_asset_categories(db)


@router.get("/activity")
def get_asset_activity(
    days: int = Query(14, ge=1, le=90, description="How many trailing days of checkout/return activity to return."),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Daily checkout/return counts feeding the Dashboard's Checkout Activity
    chart. Org-wide for Super Admin/Admin/Manager, narrowed to the caller's
    own checkouts for Staff/Customer -- see asset_service.get_activity()'s
    docstring for the reasoning.
    """
    return asset_service.get_activity(db, user, days)


@router.get("/export")
def export_assets_inventory(
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    category: Optional[str] = Query(None, description="Limit the export to one category; omit or pass 'all' for every pool."),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Downloads the Asset Inventory table itself as a CSV or PDF, optionally narrowed to a single category."""
    fmt = _validate_export_format(format)
    content, media_type, filename = asset_service.export_assets_inventory(db, user, category, fmt)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/{asset_id}/details")
def get_asset_details(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return asset_service.get_asset_details(db, asset_id, user)


@router.put("/{asset_id}/quantity")
def update_asset_quantity(asset_id: int, payload: QuantityUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.update_asset_quantity(db, asset_id, payload, user)


@router.put("/{asset_id}/name")
def update_asset_name(asset_id: int, payload: NameUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.update_asset_name(db, asset_id, payload, user)


@router.put("/{asset_id}/category")
def update_asset_category(asset_id: int, payload: CategoryUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.update_asset_category(db, asset_id, payload, user)


@router.put("/{asset_id}/price")
def update_asset_price(asset_id: int, payload: PriceUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.update_asset_price(db, asset_id, payload, user)


@router.delete("/{asset_id}")
def delete_asset_type(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.delete_asset_type(db, asset_id, user)


@router.post("/{asset_id}/restore")
def restore_asset_type(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """Reverses a soft delete: returns the pool to active inventory. See services/asset_service.py -> restore_asset_type()."""
    return asset_service.restore_asset_type(db, asset_id, user)


@router.post("/{asset_id}/purge")
def purge_asset_type(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """
    Permanently anonymizes a soft-deleted pool's name so it's free to be
    reused by a new pool. Irreversible -- unlike restore, there's no
    undo. See services/asset_service.py -> purge_asset_type().
    """
    return asset_service.purge_asset_type(db, asset_id, user)


@router.post("/{asset_id}/exception")
def flag_asset_exception(asset_id: int, exc: ExceptionCreate, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.flag_asset_exception(db, asset_id, exc, user)


@router.post("/{asset_id}/exception/{exception_id}/recall")
def recall_asset_exception(asset_id: int, exception_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.recall_asset_exception(db, asset_id, exception_id, user)


@router.post("/{asset_id}/checkin")
def checkin_asset(asset_id: int, quantity: int = Query(1, ge=1), db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.checkin_asset(db, asset_id, quantity, user)


@router.post("/{asset_id}/checkout_advanced")
def checkout_advanced(asset_id: int, req: AdvancedCheckoutRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return asset_service.checkout_advanced(db, asset_id, req, user)


@router.post("/import")
def import_assets_from_csv(file: UploadFile = File(...), db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.import_assets_from_csv(db, file, user)
