from typing import Optional
from pydantic import BaseModel, field_validator
from security import validate_password_strength


class UserCreateRequest(BaseModel):
    name: str
    email: str
    role: str
    password: str
    department: Optional[str] = None
    department_role: Optional[str] = None

    # Data Quality & Usability requirement #3: reject weak passwords at
    # account-creation time (Super Admin or Manager provisioning a new
    # login) before they're ever hashed and stored. See
    # security.validate_password_strength for the exact rule set.
    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return validate_password_strength(value)


class UserPasswordResetRequest(BaseModel):
    """
    Body for POST /users/{user_id}/reset-password -- a Super Admin/Admin
    setting a brand-new password for a locked-out user, without needing to
    know (or ask for) that user's current password. See
    services/user_service.py -> reset_user_password() for the full flow.
    """
    new_password: str

    # Same policy as UserCreateRequest.password above -- an admin-issued
    # reset must meet the exact same complexity/length rules as any other
    # newly-set password, not a weaker bar just because an admin is the
    # one typing it in.
    @field_validator("new_password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return validate_password_strength(value)
