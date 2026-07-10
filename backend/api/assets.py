"""
api/assets.py
-------------
Everything under /assets: pool CRUD, capacity, maintenance exceptions,
reconciliation check-in, the advanced checkout flow, and CSV batch import.
"""

from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Query
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user, require_super_admin, require_privileged_role
from schemas.assets import AssetTypeCreate, ExceptionCreate, AdvancedCheckoutRequest, QuantityUpdateRequest
import services.asset_service as asset_service

router = APIRouter(prefix="/assets", tags=["assets"])


@router.post("", response_model=dict)
def create_asset_type(asset: AssetTypeCreate, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.create_asset_type(db, asset, user)


@router.get("")
def list_assets(
    limit: int = Query(asset_service.DEFAULT_LIMIT, ge=1, le=asset_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large inventory)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against asset pool name"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return asset_service.list_assets(db, limit, offset, search)


@router.get("/{asset_id}/details")
def get_asset_details(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return asset_service.get_asset_details(db, asset_id)


@router.put("/{asset_id}/quantity")
def update_asset_quantity(asset_id: int, payload: QuantityUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.update_asset_quantity(db, asset_id, payload, user)


@router.delete("/{asset_id}")
def delete_asset_type(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return asset_service.delete_asset_type(db, asset_id, user)


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
