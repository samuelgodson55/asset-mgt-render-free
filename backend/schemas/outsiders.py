"""
schemas/outsiders.py
----------------------
Request bodies for the Ad-Hoc (Unlinked) Directory (api/outsiders.py) --
external individuals who receive equipment without ever holding a full
system account (see models.py's Outsider model docstring).
"""

from typing import Optional
from pydantic import BaseModel, field_validator


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
