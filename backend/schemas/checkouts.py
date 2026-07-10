import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ReturnRequest(BaseModel):
    """
    Powers POST /checkouts/{id}/return. `quantity` is how many units are
    being handed back RIGHT NOW -- it does not have to equal the full
    outstanding amount, which is what enables partial returns (requirement
    #5 from an earlier pass: Quantified Returns).
    """
    quantity: int = Field(..., ge=1)


class ExtensionRequestCreate(BaseModel):
    """
    Powers POST /checkouts/{id}/extension-requests. `new_due_date` is a
    plain calendar date (no time-of-day) -- the same shape the frontend's
    `<input type="date">` fields already use elsewhere (e.g. dispatchDueDate
    in the "Issue Outbound Deployment" form).

    `on_behalf_of_outsider` only matters when the target checkout belongs
    to an Ad-Hoc Individual (Outsider) rather than a logged-in User -- see
    services/extension_service.py's create_extension_request() for the
    full permission rule. Ignored/optional otherwise.
    """
    new_due_date: datetime.date
    reason: Optional[str] = Field(default=None, max_length=500)


class DirectExtensionRequest(BaseModel):
    """
    Powers POST /checkouts/{id}/extend. Same shape as
    ExtensionRequestCreate, but this one skips the request/approval loop
    entirely -- only a Manager/Admin/Super Admin can call it (see
    api/checkouts.py's `require_privileged_role`), and it updates the
    checkout's real due_date immediately. Used by the "Extend" button in
    the Custody Ledger drawer (components/custody.js), for granting more
    time on the spot instead of logging-then-approving a request.
    """
    new_due_date: datetime.date
    reason: Optional[str] = Field(default=None, max_length=500)


class ExtensionDecisionRequest(BaseModel):
    """
    Powers POST /checkouts/extension-requests/{id}/decision. `approve=True`
    grants the request (updating the checkout's real due_date -- to
    `override_due_date` if given, otherwise to whatever was originally
    requested); `approve=False` denies it and leaves the checkout's due_date
    untouched. `note` is an optional short explanation shown back to
    whoever asked (e.g. "Approved for one more week, no further extensions
    on this pool" or "Denied -- this pool is needed back for a training
    session next Monday").
    """
    approve: bool
    override_due_date: Optional[datetime.date] = None
    note: Optional[str] = Field(default=None, max_length=500)
