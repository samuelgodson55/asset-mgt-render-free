from typing import Optional
from pydantic import BaseModel, field_validator
from security import validate_password_strength


class UserCreateRequest(BaseModel):
    name: str
    email: str
    phone_number: Optional[str] = None
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

    @field_validator("phone_number")
    @classmethod
    def _strip_phone(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value


class UserUpdateRequest(BaseModel):
    """
    Body for PATCH /users/{user_id} -- edits an existing account's basic
    identity details (name, username, email) and, when authorized, the RBAC
    role. Distinct from UserCreateRequest (provisioning) and
    UserPasswordResetRequest (credential recovery): this never touches
    password_hash.

    Every field is optional so a caller only needs to send the ones that
    actually changed -- services/user_service.py -> update_user() uses
    Pydantic's `exclude_unset` to leave anything omitted exactly as it was,
    rather than blanking it out. See that function's docstring for the full
    Super Admin/Admin-vs-Manager permission model.
    """
    name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None

    # A field that IS present must not be an empty/whitespace-only string --
    # that would silently blank out someone's name/username/email, which is
    # never what "edit this field" means. Omit the field entirely (leave it
    # unset) if it shouldn't change.
    @field_validator("name", "username", "email")
    @classmethod
    def _reject_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("This field cannot be blank.")
        return value.strip() if value is not None else value

    # phone_number/company ARE nullable -- an explicit empty string clears
    # them, same as OutsiderUpdateRequest's email/phone_number/company.
    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip().lower()
        allowed = {"staff", "manager", "admin", "customer"}
        if normalized not in allowed:
            raise ValueError("Invalid role. Choose Staff, Manager, Admin, or Customer.")
        return normalized

    @field_validator("phone_number", "company")
    @classmethod
    def _strip_phone(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value


class UserConvertToOutsiderRequest(BaseModel):
    """
    Body for POST /users/{user_id}/convert-to-outsider -- the reverse of
    schemas.outsiders.OutsiderConvertToUserRequest: revokes a real
    account's login access and turns it into an Ad-Hoc (no-login)
    profile instead (e.g. someone leaving the company, but who still
    needs to be tracked as a custody holder for equipment they haven't
    returned yet).

    Every field is optional -- services/user_service.py's
    convert_user_to_outsider() sensibly defaults `email`/`phone_number`
    from the account being converted (its existing email/phone_number)
    when omitted. `company` has no automatic default (a user's
    `department` is an internal team, not an external company, so it
    would be misleading to silently reuse it) -- leave it blank if the
    person isn't affiliated with an outside company. `name` is
    deliberately NOT a field here, same reasoning as
    OutsiderConvertToUserRequest.email being tied to the source profile --
    this is that same person losing their login, not a chance to rename
    them mid-conversion.
    """
    email: Optional[str] = None
    phone_number: Optional[str] = None
    company: Optional[str] = None

    # Same "a present-but-blank value would silently override a sensible
    # default with nothing" guard as OutsiderUpdateRequest's fields --
    # both email and phone_number are optional on models.Outsider, but an
    # explicit empty string here is ambiguous (did they mean "clear it",
    # or did the field just get submitted empty by mistake?), so treat it
    # the same as "omitted" by rejecting it outright, forcing an
    # intentional choice.
    @field_validator("email", "phone_number")
    @classmethod
    def _reject_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("This field cannot be blank.")
        return value.strip() if value is not None else value

    # Company IS nullable -- an explicit empty string clears it, same as
    # OutsiderUpdateRequest.company.
    @field_validator("company")
    @classmethod
    def _strip_company(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value


class UserPasswordResetRequest(BaseModel):
    """
    Body for POST /users/{user_id}/reset-password -- a Super Admin/Admin
    setting a brand-new password for a locked-out user, without needing to
    know (or ask for) that user's current password. See
    services/user_service.py -> reset_user_password() for the full flow.

    `admin_password` is a DIFFERENT check from the target's password: it's
    the acting Super Admin/Admin re-confirming their OWN current password
    before this high-privilege action is allowed to proceed, exactly like
    re-entering your password before changing account security settings
    elsewhere. See reset_user_password()'s docstring for the full
    rationale.
    """
    new_password: str
    admin_password: str

    # Same policy as UserCreateRequest.password above -- an admin-issued
    # reset must meet the exact same complexity/length rules as any other
    # newly-set password, not a weaker bar just because an admin is the
    # one typing it in.
    @field_validator("new_password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return validate_password_strength(value)
