"""
api/checkouts.py
-----------------
GET  /checkouts             -- full org-wide "who has what" table of every active checkout.
POST /checkouts/{id}/return -- quantified return processing.
GET  /checkouts/overdue     -- dashboard alert feed of overdue checkouts.
GET  /checkouts/due-soon    -- dashboard alert feed of checkouts due soon
                                (a reminder BEFORE something goes overdue).

POST /checkouts/{id}/extension-requests               -- request more time
GET  /checkouts/extension-requests                     -- list requests (privileged)
GET  /checkouts/my-extension-decisions                 -- self-service: my own recently decided requests
POST /checkouts/extension-requests/{id}/decision       -- approve/deny (privileged)
POST /checkouts/{id}/extend                            -- direct grant, no request/decision round trip (privileged)
POST /checkouts/bulk-extend                             -- direct grant, one due date applied to many checkouts (privileged)
See services/extension_service.py for the full workflow writeup.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from deps import require_privileged_role, get_current_user
from schemas.checkouts_schema import ReturnRequest, ExtensionRequestCreate, ExtensionDecisionRequest, DirectExtensionRequest, BulkExtendRequest
import services.checkout_service as checkout_service
import services.extension_service as extension_service

router = APIRouter(prefix="/checkouts", tags=["checkouts"])


# NOTE ON ROUTE ORDERING: registered first purely for readability -- this
# is the literal root path ("" under the /checkouts prefix), so it can
# never collide with "/{checkout_id}/return" or any other
# {checkout_id}-parameterized route below regardless of where it's placed.
@router.get("")
def get_active_checkouts(
    limit: int = Query(checkout_service.DEFAULT_LIMIT, ge=1, le=checkout_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large checkout table)"),
    status_filter: Optional[str] = Query(
        None, alias="filter",
        description="Optional subset to filter to server-side: 'overdue' | 'due_soon' | 'active' (not yet overdue). Omit for every active checkout (the 'All' tab).",
    ),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """
    The full org-wide 'who has what' table -- every ACTIVE checkout, not
    just the overdue/due-soon subsets, unless narrowed with `filter`.
    Powers every tab of the Checkouts page (see
    checkout_service.list_active_checkouts()'s `status_filter` param for
    why this reuses that function -- with its is_overdue/is_due_soon
    computation -- instead of delegating to list_overdue_checkouts()/
    list_due_soon_checkouts() below, which return a different row shape).
    """
    return checkout_service.list_active_checkouts(db, user, limit, offset, status_filter)


@router.post("/{checkout_id}/return")
def return_checkout(checkout_id: int, req: ReturnRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return checkout_service.return_checkout(db, checkout_id, req, user)


# NOTE ON ROUTE ORDERING: this is registered AFTER "/{checkout_id}/return"
# in this file, but that's fine -- "/overdue" has no path parameter, so
# FastAPI/Starlette matches it as its own distinct, literal route rather
# than accidentally being captured by "/{checkout_id}/return" (which
# requires a trailing "/return" segment anyway). If you ever add a plain
# "/{checkout_id}" GET route, define "/overdue" ABOVE it in this file, or a
# request for "/checkouts/overdue" could incorrectly match "/checkouts/{id}"
# with checkout_id="overdue" instead.
@router.get("/overdue")
def get_overdue_checkouts(
    limit: int = Query(checkout_service.DEFAULT_LIMIT, ge=1, le=checkout_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large overdue list)"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """Dashboard alert feed: active checkouts whose due date has passed."""
    return checkout_service.list_overdue_checkouts(db, user, limit, offset)


# Same route-ordering reasoning as "/overdue" just above -- "/due-soon" is
# also a literal path with no {checkout_id} segment.
@router.get("/due-soon")
def get_due_soon_checkouts(
    limit: int = Query(checkout_service.DEFAULT_LIMIT, ge=1, le=checkout_service.MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large due-soon list)"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """Dashboard alert feed: active checkouts due within settings.DUE_SOON_REMINDER_DAYS, but not yet overdue -- a reminder BEFORE something goes overdue."""
    return checkout_service.list_due_soon_checkouts(db, user, limit, offset)


@router.post("/{checkout_id}/extension-requests")
def request_extension(
    checkout_id: int, req: ExtensionRequestCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user),
):
    """
    Any logged-in User can request more time on THEIR OWN active checkout.
    A Manager/Admin/Super Admin may also log a request on behalf of an
    Ad-Hoc Individual (Outsider), who has no login of their own -- see
    services/extension_service.py's create_extension_request() for the
    full permission rule.
    """
    return extension_service.create_extension_request(db, checkout_id, req, user)


# NOTE ON ROUTE ORDERING: same reasoning as "/overdue" above -- this is a
# literal path with no {checkout_id} segment, so it's matched as its own
# distinct route rather than being captured by "/{checkout_id}/return" or
# "/{checkout_id}/extension-requests".
@router.get("/extension-requests")
def get_extension_requests(
    status: Optional[str] = Query(None, description="Filter by 'pending' | 'approved' | 'denied'"),
    limit: int = Query(extension_service.DEFAULT_LIMIT, ge=1, le=extension_service.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(require_privileged_role),
):
    """Dashboard panel feed: extension requests awaiting (or already given) a decision."""
    return extension_service.list_extension_requests(db, user, status, limit, offset)


# NOTE ON ROUTE ORDERING: same reasoning as "/overdue" above -- this is a
# literal path with no {checkout_id} segment.
@router.get("/my-extension-decisions")
def get_my_extension_decisions(
    limit: int = Query(10, ge=1, le=extension_service.MAX_LIMIT),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Self-service alert feed: any of the CALLER'S OWN active/former checkouts
    whose extension request was approved or denied recently. Any logged-in
    account can call this (staff/customer/manager/admin) -- it's scoped to
    their own checkouts only, same trust boundary as GET /users/me/items.
    """
    return extension_service.list_my_recent_extension_decisions(db, user, limit)


@router.post("/extension-requests/{request_id}/decision")
def decide_extension_request(
    request_id: int, decision: ExtensionDecisionRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role),
):
    """Approve or deny a pending extension request -- only a Manager, Admin, or Super Admin may decide one."""
    return extension_service.decide_extension_request(db, request_id, decision, user)


@router.post("/{checkout_id}/extend")
def extend_checkout(
    checkout_id: int, req: DirectExtensionRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role),
):
    """
    A Manager/Admin/Super Admin grants more time on an active checkout
    immediately -- no separate extension-request/decision round trip. Lets
    the "Extend" action in the Custody Ledger drawer (User Directory / Ad-
    Hoc Directory on admin.html / manager.html) work in one click, right
    next to "Process Return". Managers have no department-scoping here --
    see services/extension_service.py's extend_checkout_directly().
    """
    return extension_service.extend_checkout_directly(db, checkout_id, req, user)


# NOTE ON ROUTE ORDERING: same reasoning as "/overdue" above -- this is a
# literal path with no {checkout_id} segment.
@router.post("/bulk-extend")
def bulk_extend_checkouts(
    req: BulkExtendRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role),
):
    """
    Applies one new due date to many active checkouts at once -- the
    Custody Ledger drawer's "Bulk Extend Selected" action, reusing the same
    checkbox selection already used for Bulk Process Returns. See
    services/extension_service.py's extend_checkouts_bulk().
    """
    return extension_service.extend_checkouts_bulk(db, req, user)
