"""
api/quotations.py
------------------
Self-service Equipment Quotation feature: the read-only Asset Catalog
(GET /assets/catalog), each account's own draft Quotation ("saved
order"), SUBMITTING that draft into a permanent, ID-tagged Quotation
(POST /quotations/submit), the Admin/Manager-only "Quotes" tab (list /
view / edit / assign any submitted Quotation), the admin-only global VAT
setting, and PDF export.
"""

from telemetry import trace_operation

from typing import Optional
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user, require_super_admin, require_privileged_role
from schemas.quotations_schema import (
    QuotationItemCreate, QuotationItemQuantityUpdate, VatUpdateRequest,
    QuotationAssignRequest, QuotationMetaUpdate, QuotationCreateRequest,
    QuotationOutsourcedItemCreate, QuotationCheckoutRequest,
    QuotationDiscountUpdateRequest, QuotationNotificationsReadRequest, QuotationPaidRequest,
)
import services.quotation_service as quotation_service

router = APIRouter(tags=["quotations"])


@router.get("/config/public")
def get_public_config(db: Session = Depends(get_db)):
    """Non-secret config the frontend needs before rendering the catalog/cart
    (currency, stock-visibility flag) AND before rendering its own page
    chrome (site_name -- drives the navbar brand + <title> on every page,
    including the unauthenticated login page, via js/ui.js's applySiteName())."""
    return quotation_service.get_public_config(db)


@router.get("/assets/catalog")
def get_asset_catalog(
    limit: int = Query(quotation_service.CATALOG_DEFAULT_LIMIT, ge=1, le=quotation_service.CATALOG_MAX_LIMIT, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip (for paging through a large catalog)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring match against asset name or category"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """The self-service Quotation Catalog -- every active asset pool, shaped by role + CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER.

    limit/offset/search default to CATALOG_DEFAULT_LIMIT rows so existing
    callers that never pass these -- e.g. the Admin/Manager Quote Detail
    drawer's "Add another asset" typeahead, which needs the catalog
    client-side -- keep working unchanged. The Quotations page's own
    browsable catalog table passes real values for true server-side
    paging + search. Either way, CATALOG_MAX_LIMIT (see
    services/quotation_service.py) is a hard cap enforced both by this
    route's `le=` and again inside list_catalog() -- no request can ever
    get an unbounded response."""
    return quotation_service.list_catalog(db, user, limit, offset, search)


# ---------------------------------------------------------------------------
# Self-service: the caller's own draft cart + submission history
# ---------------------------------------------------------------------------
@router.get("/quotations/me")
def get_my_quotation(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """The caller's own OPEN draft order, with live-computed line totals/subtotal/VAT/total."""
    return quotation_service.get_my_quotation(db, user)


@router.get("/quotations/me/history")
def get_my_quotation_history(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Every Quotation the caller has formally submitted -- still editable (qty/remove) below
    while a given one is \"submitted\" (unapproved); an Admin/Manager owns edits from Approved on."""
    return quotation_service.list_my_submitted_quotations(db, user)


@router.get("/quotations/me/notifications")
def get_my_quotation_notifications(limit: int = Query(20, ge=1, le=quotation_service.QUOTATION_NOTIFICATIONS_MAX_ROWS), db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Self-service: every in-app notification (assigned/updated) addressed to the caller,
    newest first -- powers the Notification Bell / Notifications page's "Quotation updates" section."""
    return quotation_service.list_my_quotation_notifications(db, user, limit=limit)


@router.post("/quotations/me/notifications/read")
@trace_operation("quote.notifications.read")
def mark_my_quotation_notifications_read(payload: QuotationNotificationsReadRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Self-service: marks the given notification ids (only ones addressed to the caller) as read."""
    return quotation_service.mark_quotation_notifications_read(db, user, payload.notification_ids)


@router.get("/quotations/me/{quotation_id}")
def get_my_submitted_quotation(quotation_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Full detail for one of the caller's OWN submitted Quotations, by numeric ID -- to reopen it
    (view read-only once Approved/Fulfilled, or adjust it below while still \"submitted\")."""
    return quotation_service.get_my_submitted_quotation_detail(db, user, quotation_id)


@router.get("/quotations/me/{quotation_id}/export")
def export_my_submitted_quotation(quotation_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Self-service PDF export of one of the caller's OWN submitted Quotations (or one assigned
    to them) by ID -- the "My Quotes" history equivalent of the Admin/Manager-only
    /quotations/{quotation_id}/export below. Same requester-or-assignee visibility rule as
    GET /quotations/me/{quotation_id} above."""
    content, media_type, filename = quotation_service.export_my_quotation_pdf_by_id(db, user, quotation_id)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.put("/quotations/me/{quotation_id}/items/{item_id}")
@trace_operation("quote.my_item.update")
def update_my_submitted_quotation_item(quotation_id: int, item_id: int, payload: QuotationItemQuantityUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lets the requester adjust a quantity on their OWN quotation while it's still \"submitted\"
    (unapproved) -- nested under /quotations/me/... so it never collides with the Admin/Manager-only
    /quotations/{quotation_id}/items/{item_id} route below."""
    return quotation_service.update_my_submitted_item_quantity(db, user, quotation_id, item_id, payload)


@router.delete("/quotations/me/{quotation_id}/items/{item_id}")
@trace_operation("quote.my_item.remove")
def remove_my_submitted_quotation_item(quotation_id: int, item_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lets the requester remove a line from their OWN quotation while it's still \"submitted\" (unapproved)."""
    return quotation_service.remove_my_submitted_item(db, user, quotation_id, item_id)


@router.post("/quotations/me/{quotation_id}/items")
@trace_operation("quote.my_item.add")
def add_my_submitted_quotation_item(quotation_id: int, payload: QuotationItemCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lets the requester/assignee add another catalog asset to their OWN quotation while it's
    still \"submitted\" (unapproved) -- nested under /quotations/me/... so it never collides with
    the Admin/Manager-only /quotations/{quotation_id}/items route below."""
    return quotation_service.add_my_submitted_item(db, user, quotation_id, payload)


@router.post("/quotations/items")
@trace_operation("quote.item.add")
def add_quotation_item(payload: QuotationItemCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return quotation_service.add_item(db, user, payload)


@router.put("/quotations/items/{item_id}")
@trace_operation("quote.item.update")
def update_quotation_item(item_id: int, payload: QuotationItemQuantityUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return quotation_service.update_item_quantity(db, user, item_id, payload)


@router.delete("/quotations/items/{item_id}")
@trace_operation("quote.item.remove")
def remove_quotation_item(item_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return quotation_service.remove_item(db, user, item_id)


@router.post("/quotations/submit")
@trace_operation("quote.submit")
def submit_quotation(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Finalizes the caller's current draft: stamps it with a Quotation ID (e.g. "QT-000001") an
    Admin/Manager can pull up in the Quotes tab, adjust, and assign to a user."""
    return quotation_service.submit_my_quotation(db, user)


@router.get("/quotations/export")
def export_quotation(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Downloads the caller's current draft order as a PDF (asset, category, quantity, dates, VAT, total) to share offline."""
    content, media_type, filename = quotation_service.export_quotation_pdf(db, user)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------------------------------------------------------
# Admin/Manager: the "Quotes" tab -- look up any submitted Quotation by ID
# (or by requester), adjust its line items, and assign it to a user.
# ---------------------------------------------------------------------------
@router.get("/quotations")
def list_quotations(
    search: Optional[str] = Query(None, description="Matches the Quotation ID (reference number) or the requester's name/email"),
    status: Optional[str] = Query(None, description="Optionally narrow to a specific status (only 'submitted' exists today)"),
    limit: int = Query(quotation_service.DEFAULT_LIST_LIMIT, ge=1, le=quotation_service.MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: dict = Depends(require_privileged_role),
):
    """Every submitted Quotation -- the Admin/Manager "Quotes" tab."""
    return quotation_service.list_quotations(db, search=search, status=status, limit=limit, offset=offset)


@router.post("/quotations")
@trace_operation("quote.create")
def create_quotation(payload: QuotationCreateRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager starts a brand new Quotation directly from the Quotes tab (e.g. building
    one on a user's behalf), optionally assigned to a user immediately. Starts empty --
    add line items afterward via POST /quotations/{id}/items."""
    return quotation_service.admin_create_quotation(db, user, payload)


@router.delete("/quotations/{quotation_id}")
@trace_operation("quote.delete")
def delete_quotation(quotation_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager-only: permanently deletes a submitted or approved
    quotation. Fulfilled and paid quotations are retained for operational and
    financial history respectively."""
    return quotation_service.delete_quotation(db, user, quotation_id)


@router.get("/quotations/fulfillment-queue")
def get_fulfillment_queue(db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager-only: every Approved / Ready for Pickup Quotation, oldest first --
    the data behind the Fulfillment Drawer's bulk physical checkout. Registered ABOVE
    GET /quotations/{quotation_id} below so "fulfillment-queue" is never swallowed as
    a (non-numeric, 422-ing) quotation_id path param."""
    return quotation_service.get_fulfillment_queue(db)


@router.get("/quotations/{quotation_id}")
def get_quotation(quotation_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Full detail (items, requester, assignment, notes) for one Quotation, by its numeric ID."""
    return quotation_service.get_quotation_detail(db, quotation_id)


@router.put("/quotations/{quotation_id}")
@trace_operation("quote.update")
def update_quotation(quotation_id: int, payload: QuotationMetaUpdate, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Updates the Admin/Manager-facing notes on a Quotation."""
    return quotation_service.update_quotation_meta(db, user, quotation_id, payload)


@router.put("/quotations/{quotation_id}/discount")
@trace_operation("quote.discount.update")
def update_quotation_discount(quotation_id: int, payload: QuotationDiscountUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager sets the discount percentage (0-100) on a single Quotation,
    editable right up until it's fulfilled -- same as any other line on the quote."""
    return quotation_service.update_quotation_discount(db, user, quotation_id, payload)


@router.post("/quotations/{quotation_id}/items")
@trace_operation("quote.item.add")
def add_quotation_item_admin(quotation_id: int, payload: QuotationItemCreate, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager adds (or updates) a line on someone else's submitted Quotation."""
    return quotation_service.admin_add_item(db, user, quotation_id, payload)


@router.put("/quotations/{quotation_id}/items/{item_id}")
@trace_operation("quote.item.update")
def update_quotation_item_admin(quotation_id: int, item_id: int, payload: QuotationItemQuantityUpdate, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return quotation_service.admin_update_item_quantity(db, user, quotation_id, item_id, payload)


@router.delete("/quotations/{quotation_id}/items/{item_id}")
@trace_operation("quote.item.remove")
def remove_quotation_item_admin(quotation_id: int, item_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    return quotation_service.admin_remove_item(db, user, quotation_id, item_id)


@router.post("/quotations/{quotation_id}/outsourced-items")
@trace_operation("quote.outsourced_item.add")
def add_quotation_outsourced_item(quotation_id: int, payload: QuotationOutsourcedItemCreate, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Manager/Admin-only: adds a \"not currently in inventory\" line, with its own
    name/description/price, to a submitted Quotation. The requester can see this
    line during review (merged into the same items list, flagged `is_outsourced`)
    but has no route that can edit or remove it -- only a Manager/Admin can."""
    return quotation_service.admin_add_outsourced_item(db, user, quotation_id, payload)


@router.delete("/quotations/{quotation_id}/outsourced-items/{item_id}")
@trace_operation("quote.outsourced_item.remove")
def remove_quotation_outsourced_item(quotation_id: int, item_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Manager/Admin-only: removes a previously-added outsourced (not-in-inventory) line."""
    return quotation_service.admin_remove_outsourced_item(db, user, quotation_id, item_id)


@router.post("/quotations/{quotation_id}/assign")
@trace_operation("quote.assign")
def assign_quotation(quotation_id: int, payload: QuotationAssignRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Assigns (or clears the assignment of) a submitted Quotation to a user."""
    return quotation_service.assign_quotation(db, user, quotation_id, payload)


@router.post("/quotations/{quotation_id}/approve")
@trace_operation("quote.approve")
def approve_quotation(quotation_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager-only: flips a submitted Quotation to \"approved\" -- the green
    Ready for Pickup badge -- and locks it against further item/notes/assignment edits."""
    return quotation_service.approve_quotation(db, user, quotation_id)


@router.post("/quotations/{quotation_id}/paid")
@trace_operation("quote.paid")
def mark_quotation_paid(quotation_id: int, payload: QuotationPaidRequest, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager-only: records payment for a fulfilled quotation and moves it to terminal `paid`."""
    return quotation_service.mark_quotation_paid(db, user, quotation_id, payload)


@router.post("/quotations/{quotation_id}/checkout")
@trace_operation("quote.checkout")
def checkout_quotation(quotation_id: int, payload: QuotationCheckoutRequest = QuotationCheckoutRequest(), db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager-only: the Fulfillment Drawer's \"physical bulk checkout\" action --
    turns every line item on an approved Quotation into a real AssetCheckout, evaluating
    and deducting stock only at this exact moment, then marks the Quotation fulfilled.

    `payload.outsource_shortfall_items` (optional, defaults to empty) lets the caller
    pre-authorize specific inventory-backed lines to be sourced externally -- rather than
    blocking this whole checkout -- if the authoritative, row-locked stock check inside
    bulk_checkout_quotation() finds them genuinely short at this exact moment. See that
    function's STOCK LOGIC comment for the full mechanics."""
    return quotation_service.bulk_checkout_quotation(db, user, quotation_id, payload.outsource_shortfall_items)


@router.get("/quotations/{quotation_id}/export")
def export_quotation_admin(quotation_id: int, db: Session = Depends(get_db), user: dict = Depends(require_privileged_role)):
    """Admin/Manager PDF export of any Quotation by ID."""
    content, media_type, filename = quotation_service.export_quotation_pdf_by_id(db, quotation_id)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------------------------------------------------------
# Global VAT setting
# ---------------------------------------------------------------------------
@router.get("/settings/vat")
def get_vat_setting(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """The current global VAT percentage -- any authenticated user can read it (it's shown on every Quotation)."""
    return {"vat_percent": float(quotation_service.get_vat_percent(db))}


@router.put("/settings/vat")
@trace_operation("settings.vat.update")
def update_vat_setting(payload: VatUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """Admin/Super Admin only -- changes the global VAT applied to every Quotation immediately."""
    return quotation_service.set_vat_percent(db, payload, user)
