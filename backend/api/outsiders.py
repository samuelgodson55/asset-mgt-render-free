"""
api/outsiders.py
-----------------
Ad-Hoc (Unlinked) Directory: external individuals with no login account.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from database import get_db
from deps import require_privileged_role
import services.outsider_service as outsider_service

router = APIRouter(prefix="/outsiders", tags=["outsiders"])

_VALID_EXPORT_FORMATS = ("csv", "pdf")


def _validate_export_format(format: str) -> str:
    fmt = format.lower()
    if fmt not in _VALID_EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'pdf'.")
    return fmt


def _file_response(content: bytes, media_type: str, filename: str) -> Response:
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("")
def get_outsiders(
    limit: int = Query(outsider_service.DEFAULT_LIMIT, ge=1, le=outsider_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large directory)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against name, contact details, or company"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return outsider_service.list_outsiders(db, limit, offset, search)


@router.get("/export")
def export_all_outsiders(
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """Bulk download of properties currently assigned to every ad-hoc individual on file."""
    fmt = _validate_export_format(format)
    content, media_type, filename = outsider_service.export_all_outsiders_items(db, user, fmt)
    return _file_response(content, media_type, filename)


@router.get("/{outsider_id}/items")
def get_outsider_assigned_items(outsider_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return outsider_service.get_outsider_assigned_items(db, outsider_id)


@router.get("/{outsider_id}/items/export")
def export_outsider_assigned_items(
    outsider_id: int,
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """Download of one specific ad-hoc individual's Custody Ledger."""
    fmt = _validate_export_format(format)
    content, media_type, filename = outsider_service.export_outsider_assigned_items(db, outsider_id, user, fmt)
    return _file_response(content, media_type, filename)
