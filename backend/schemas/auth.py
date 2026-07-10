"""
schemas/auth.py
----------------
Request bodies for POST /auth/login and POST /auth/update-password.
"""

from typing import Optional

from pydantic import BaseModel, field_validator
from security import validate_password_strength


class LoginRequest(BaseModel):
    """
    Data Quality & Usability requirement #6: `identifier` accepts EITHER a
    user's email address OR their username interchangeably -- see
    `services/auth_service.py -> login()` for the actual lookup logic. Kept
    as one flexible field (instead of two separate optional fields) since
    the frontend's single login box doesn't ask the person which kind of
    value they're typing.
    """
    identifier: str
    password: str


class PasswordUpdateRequest(BaseModel):
    user_id: int
    new_password: str
    # Required when a person changes their OWN password (see
    # services/auth_service.py -> update_password()'s self-vs-admin check);
    # left None/omitted when a Super Admin is resetting a DIFFERENT user's
    # (e.g. a locked-out account's) password, since that admin naturally
    # doesn't know -- and shouldn't need to know -- the other person's
    # current password.
    current_password: Optional[str] = None

    # Data Quality & Usability requirement #3: reject weak new passwords
    # (used both for a self-service reset and a Super Admin resetting
    # someone else's password) before they ever reach the database.
    @field_validator("new_password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return validate_password_strength(value)
