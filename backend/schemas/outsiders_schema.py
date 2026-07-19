"""
schemas/outsiders.py
----------------------
Request bodies for the Ad-Hoc (Unlinked) Directory (api/outsiders.py) --
external individuals who receive equipment without ever holding a full
system account (see models.py's Outsider model docstring).
"""

from typing import Optional
from pydantic import BaseModel, field_validator
from security import validate_password_strength


class OutsiderUpdateRequest(BaseModel):
    """
    Body for PATCH /outsiders/{outsider_id} -- edits an ad-hoc individual's
    name, contact details, and/or company. Every field is optional so a
    caller only needs to send the ones that actually changed --
    services/outsider_service.py -> update_outsider() uses Pydantic's
    `exclude_unset` to leave anything omitted exactly as it was.
    """
    name: Optional[str] = None
    contact_details: Optional[str] = None
    company: Optional[str] = None

    # name/contact_details are both `nullable=False` on models.Outsider --
    # a present-but-blank value would silently violate that, so reject it
    # the same way UserUpdateRequest does for name/username/email.
    @field_validator("name", "contact_details")
    @classmethod
    def _reject_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("This field cannot be blank.")
        return value.strip() if value is not None else value

    # Company IS nullable on the model -- an explicit empty string here is
    # treated as "clear the company" by update_outsider() below, same as
    # leaving it blank at ad-hoc dispatch time.
    @field_validator("company")
    @classmethod
    def _strip_company(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value


class OutsiderConvertToUserRequest(BaseModel):
    """
    Body for POST /outsiders/{outsider_id}/convert-to-user -- an Ad-Hoc
    Individual (someone who's been receiving equipment with no login of
    their own) finally decides they want a real account.

    Deliberately mirrors schemas.users.UserCreateRequest's fields (email,
    role, password, department, department_role) rather than introducing a
    parallel shape: services/outsider_service.py's
    convert_outsider_to_user() hands this straight to
    services/user_service.py's _provision_user_row(), the exact same
    role/email-uniqueness/password-strength rules a brand-new account
    creation goes through. `name` is deliberately NOT a field here -- the
    new account's name is always the ad-hoc profile's existing `name`
    (see models.Outsider.name), since this is that same person getting a
    login, not a chance to rename them mid-conversion.
    """
    email: str
    password: str
    role: str
    department: Optional[str] = None
    department_role: Optional[str] = None

    # Same "don't silently accept an empty string" guard as
    # UserUpdateRequest's fields -- an outsider's contact email is
    # required to log in with, so a blank value here would just produce a
    # confusing 500/validation error deeper in _provision_user_row().
    @field_validator("email")
    @classmethod
    def _reject_blank_email(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("This field cannot be blank.")
        return value.strip()

    # Same policy as UserCreateRequest.password -- an account created via
    # conversion must meet the exact same complexity/length rules as any
    # other newly-provisioned login, not a weaker bar just because it
    # started life as an ad-hoc profile.
    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return validate_password_strength(value)
