"""
api/outsiders.py
-----------------
Ad-Hoc (Unlinked) Directory: external individuals with no login account.
"""

from telemetry import trace_operation

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from database import get_db
from deps import require_privileged_role
from schemas.outsiders_schema import OutsiderUpdateRequest, OutsiderConvertToUserRequest
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


@router.patch("/{outsider_id}")
@trace_operation("outsider.update")
def update_outsider(outsider_id: int, req: OutsiderUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """
    Edits an ad-hoc individual's name/contact details/company. Both a
    Super Admin/Admin and a Manager may call this (no narrower role
    boundary applies -- see services/outsider_service.py -> update_outsider()).
    """
    return outsider_service.update_outsider(db, outsider_id, req, user)


@router.post("/{outsider_id}/convert-to-user")
@trace_operation("outsider.convert_to_user")
def convert_outsider_to_user(outsider_id: int, req: OutsiderConvertToUserRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """
    Turns an ad-hoc individual into a real, log-in-capable user account
    (see services/outsider_service.py -> convert_outsider_to_user() for
    the full migration/safety rationale). Available to both a Super
    Admin/Admin and a Manager, same as the other id-based outsider
    actions, subject to the same Manager role ceiling a brand-new
    account provisioning gets.
    """
    return outsider_service.convert_outsider_to_user(db, outsider_id, req, user)


@router.delete("/{outsider_id}")
@trace_operation("outsider.delete")
def delete_outsider(outsider_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """
    Deletes an ad-hoc individual's profile (soft delete -- see
    services/outsider_service.py -> delete_outsider() for the full
    rationale). Available to both a Super Admin/Admin and a Manager, same
    as PATCH above; blocked while the profile still has items in active
    custody.
    """
    return outsider_service.delete_outsider(db, outsider_id, user)


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
