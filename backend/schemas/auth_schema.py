"""
schemas/auth.py
----------------
Request bodies for POST /auth/login, POST /auth/update-password,
POST /auth/forgot-password, POST /auth/reset-password, and
PATCH /auth/me (identity rotation).
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


class MfaSetupConfirmRequest(BaseModel):
    """POST /auth/mfa/setup/confirm -- completes 2FA enrollment. `mfa_setup_token`
    is the short-lived token login() returned in its mfa_setup_required
    response; `code` is the 6-digit code from the person's authenticator app."""
    mfa_setup_token: str
    code: str


class MfaVerifyRequest(BaseModel):
    """POST /auth/mfa/verify -- completes login for an already-enrolled
    account. `mfa_pending_token` is the short-lived token login() returned
    in its mfa_required response; `code` is the 6-digit authenticator code."""
    mfa_pending_token: str
    code: str


class RecoveryCodesRegenerateRequest(BaseModel):
    """POST /auth/mfa/recovery-codes/regenerate -- invalidates every
    existing recovery code and issues a fresh batch. `password` is the
    CURRENT password, re-confirmed the same way update_password() does,
    since this is a sensitive action taken from an already-authenticated
    session rather than a login."""
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


class ForgotPasswordRequest(BaseModel):
    """
    POST /auth/forgot-password -- `identifier` accepts EITHER an email
    address or a username, same flexible field as LoginRequest above (the
    frontend's single "forgot password" box doesn't ask which kind of
    value they're typing either). See
    services/auth_service.py -> request_password_reset() for the actual
    lookup/email-sending logic -- notably, this always returns the same
    generic response whether or not a matching account exists, so this
    schema intentionally carries no way to distinguish "found" from "not
    found" for a caller probing for valid accounts.
    """
    identifier: str


class ResetPasswordRequest(BaseModel):
    """
    POST /auth/reset-password -- completes a "forgot password?" recovery.
    `token` is the plaintext value from the emailed reset link (see
    services/auth_service.py -> confirm_password_reset(), which hashes it
    and matches against models.PasswordResetToken.token_hash).
    """
    token: str
    new_password: str

    # Same policy as PasswordUpdateRequest.new_password above -- a
    # recovery-issued password must meet the exact same complexity/length
    # rules as any other newly-set password.
    @field_validator("new_password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return validate_password_strength(value)


class IdentityUpdateRequest(BaseModel):
    """
    Body for PATCH /auth/me -- lets the CURRENTLY LOGGED-IN account rotate
    its own name/username/email/phone_number/company, the same
    self-service shape update_password() already established for the
    password itself. See services/auth_service.py -> update_identity() for
    the full flow.

    Distinct from schemas.users_schema.UserUpdateRequest (which an
    Admin/Super Admin uses to edit a DIFFERENT account, and which
    explicitly cannot reach the hidden root admin row -- see
    services/user_service.py's is_hidden_root_admin() guard): this is the
    self-only counterpart that row still needs, since nothing else can
    ever touch it.

    Every field is optional so a caller only sends the ones that actually
    changed (same `exclude_unset` handling as UserUpdateRequest).
    `current_password` is always required, regardless of which fields are
    present -- re-confirming it before ANY identity change is what stops a
    leaked/still-valid session cookie alone from being enough to quietly
    take over an account's login details.
    """
    name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    company: Optional[str] = None
    current_password: str

    # Same "a present-but-blank value would silently blank the field out"
    # guard as UserUpdateRequest's identical validator. Doesn't apply to
    # phone_number/company below -- both are nullable, optional contact
    # details, and an explicit empty string is a legitimate "clear it"
    # (same as UserUpdateRequest.phone_number / OutsiderUpdateRequest's
    # email/phone_number/company fields already treat their own nullable
    # counterparts).
    @field_validator("name", "username", "email")
    @classmethod
    def _reject_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("This field cannot be blank.")
        return value.strip() if value is not None else value

    @field_validator("phone_number", "company")
    @classmethod
    def _strip_contact_detail(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value
