import datetime
from typing import Optional, Dict
from pydantic import BaseModel, Field, field_validator

# Operations & Observability requirement #4: date validation must happen on
# BOTH sides -- the browser's <input type="date" min="..." max="..."> (see
# frontend/js/components/assets.js -> openDispatchModal(), which sets those
# attributes dynamically every time the dispatch drawer opens) gives the
# person immediate feedback and stops most mistakes before a request is
# even sent, but a client-side-only check is trivially bypassed by anyone
# calling the API directly (curl, Postman, a modified frontend). The
# constants below back that same rule with a REAL server-side check
# (`_validate_due_date` further down), so a checkout can never actually be
# created with a nonsensical due date no matter how the request was made.
MAX_DUE_DATE_YEARS_AHEAD = 5


class AssetTypeCreate(BaseModel):
    name: str
    total_quantity: int
    custom_fields: Optional[Dict[str, str]] = {}


class ExceptionCreate(BaseModel):
    serial_number: str
    status_label: str
    notes: Optional[str] = None


class AdvancedCheckoutRequest(BaseModel):
    assignee_type: str  # "user" | "outsider"
    quantity: int = Field(1, ge=1)  # NOTE: pydantic v2 uses "ge", not "gte"
    user_id: Optional[int] = None
    outsider_name: Optional[str] = None
    outsider_contact: Optional[str] = None
    outsider_company: Optional[str] = None
    due_date: Optional[datetime.date] = None

    # Operations & Observability requirement #4 (server-side half): reject
    # a due_date that is either already in the past (you can't check
    # something out and have it be "due back" yesterday) or absurdly far in
    # the future (almost certainly a typo, e.g. picking the wrong year on a
    # date picker). This mirrors the `min`/`max` set on the frontend's
    # `<input type="date">` in openDispatchModal() -- see
    # frontend/js/components/assets.js -- but that client-side check is
    # only a UX nicety; THIS is the check that actually can't be bypassed.
    @field_validator("due_date")
    @classmethod
    def _validate_due_date(cls, value: Optional[datetime.date]) -> Optional[datetime.date]:
        if value is None:
            return value  # No fixed due date is a valid, deliberate choice.

        today = datetime.date.today()
        if value < today:
            raise ValueError("Due date cannot be in the past.")

        max_allowed = today.replace(year=today.year + MAX_DUE_DATE_YEARS_AHEAD)
        if value > max_allowed:
            raise ValueError(f"Due date cannot be more than {MAX_DUE_DATE_YEARS_AHEAD} years in the future.")

        return value


class QuantityUpdateRequest(BaseModel):
    new_total: int = Field(..., ge=0)
