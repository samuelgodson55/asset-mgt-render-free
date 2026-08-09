"""
api/users.py
------------
System-user account provisioning, directory listing, self-service items,
per-user custody lookup, properties-assigned exports, and delete.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user, require_super_admin, require_privileged_role
from schemas.users_schema import UserCreateRequest, UserUpdateRequest, UserPasswordResetRequest, UserConvertToOutsiderRequest
import services.user_service as user_service

router = APIRouter(prefix="/users", tags=["users"])

# Shared by every export route below -- keeps the "unsupported format"
# error message and validation identical no matter which endpoint it's on.
_VALID_EXPORT_FORMATS = ("csv", "pdf")


def _validate_export_format(format: str) -> str:
    fmt = format.lower()
    if fmt not in _VALID_EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'pdf'.")
    return fmt


def _file_response(content: bytes, media_type: str, filename: str) -> Response:
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.post("")
def create_user(req: UserCreateRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return user_service.create_user(db, req, user)


@router.get("")
def get_users(
    limit: int = Query(user_service.DEFAULT_LIMIT, ge=1, le=user_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large directory)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against name, email, role, department, or department_role"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    return user_service.list_users(db, user, limit, offset, search)


@router.get("/deleted")
def get_deleted_users(
    limit: int = Query(user_service.DEFAULT_LIMIT, ge=1, le=user_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large list)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against name, email, role, department, or department_role"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_super_admin),
):
    """Lists soft-deleted accounts so a Super Admin/Admin can find one to restore. Placed ahead of /{user_id}/... routes below purely for readability -- 'deleted' can never collide with a numeric {user_id} path segment."""
    return user_service.list_deleted_users(db, user, limit, offset, search)


@router.get("/me/items")
def get_my_assigned_items(
    limit: int = Query(user_service.DEFAULT_LIMIT, ge=1, le=user_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through My Items)"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Self-service: lets ANY logged-in account (staff, customer, manager,
    super_admin) see their own checked-out items, without needing elevated
    privileges. Powers staff.html and customer.html's "My Items" table,
    with the same true server-side `limit`/`offset` pagination as GET
    /assets and GET /users (default `limit` is generous enough that
    callers which don't care about paging -- the Notification Bell,
    Dashboard, and the CSV/PDF export -- keep seeing everything).
    """
    return user_service.get_my_assigned_items(db, user, limit, offset)


@router.get("/me/items/export")
def export_my_assigned_items(
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Self-service download of the same data as GET /users/me/items, as a CSV or PDF file."""
    fmt = _validate_export_format(format)
    content, media_type, filename = user_service.export_my_assigned_items(db, user, fmt)
    return _file_response(content, media_type, filename)


@router.get("/export")
def export_all_users(
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """
    Bulk download of properties currently assigned to every user in the
    caller's scope -- both a Super Admin and a Manager get the entire
    directory (same scoping as GET /users).
    """
    fmt = _validate_export_format(format)
    content, media_type, filename = user_service.export_all_users_items(db, user, fmt)
    return _file_response(content, media_type, filename)


@router.get("/{user_id}/items")
def get_user_assigned_items(user_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return user_service.get_user_assigned_items(db, user_id, user)


@router.get("/{user_id}/items/export")
def export_user_assigned_items(
    user_id: int,
    format: str = Query("csv", description="Export format: 'csv' or 'pdf'."),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """Download of one specific user's Custody Ledger (same access rule as GET /users/{user_id}/items)."""
    fmt = _validate_export_format(format)
    content, media_type, filename = user_service.export_user_assigned_items(db, user_id, user, fmt)
    return _file_response(content, media_type, filename)


@router.patch("/{user_id}")
def update_user(user_id: int, req: UserUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """
    Edits an existing account's name/username/email. Both a Super
    Admin/Admin and a Manager may call this route -- services/user_service.py
    -> update_user() enforces the narrower Manager boundary (Staff/Customer
    accounts only) server-side.
    """
    return user_service.update_user(db, user_id, req, user)


@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    return user_service.delete_user(db, user_id, user)


@router.post("/{user_id}/convert-to-outsider")
def convert_user_to_outsider(user_id: int, req: UserConvertToOutsiderRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """
    Revokes an account's login access and turns it into an Ad-Hoc
    (no-login) profile instead -- the reverse of POST
    /outsiders/{outsider_id}/convert-to-user (see
    services/user_service.py -> convert_user_to_outsider() for the full
    migration/safety rationale). Available to both a Super Admin/Admin
    and a Manager, same access tier as account provisioning, subject to
    the same Manager role ceiling (Staff/Customer accounts only).
    """
    return user_service.convert_user_to_outsider(db, user_id, req, user)


@router.post("/{user_id}/reset-password")
def reset_user_password(user_id: int, req: UserPasswordResetRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """
    Admin/Super Admin "forgot password" recovery: sets a brand-new password
    for another user's account directly, with no need to know their old
    one. See services/user_service.py -> reset_user_password() docstring.
    """
    return user_service.reset_user_password(db, user_id, req.new_password, req.admin_password, user)


@router.post("/{user_id}/restore")
def restore_user(user_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """Reverses a soft delete: re-enables login and returns the account to the User Directory. See services/user_service.py -> restore_user()."""
    return user_service.restore_user(db, user_id, user)


@router.post("/{user_id}/purge")
def purge_user(user_id: int, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """
    Permanently anonymizes a soft-deleted account's email/username so
    they're free to be reused by a new account. Irreversible -- unlike
    restore, there's no undo. See services/user_service.py -> purge_user().
    """
    return user_service.purge_user(db, user_id, user)
