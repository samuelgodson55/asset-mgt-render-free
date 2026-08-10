import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

# Same reasoning as schemas/assets.py's MAX_DUE_DATE_YEARS_AHEAD -- bounds
# how far in the future a rental's due_date can be, so a typo'd year can't
# silently create an absurd multi-decade rental line.
MAX_DUE_DATE_YEARS_AHEAD = 5


class QuotationItemCreate(BaseModel):
    """Adds one asset pool to the caller's own draft Quotation (POST /quotations/items)."""

    asset_id: int
    quantity: int = Field(1, ge=1)
    start_date: datetime.date
    due_date: datetime.date

    @field_validator("due_date")
    @classmethod
    def _due_date_not_absurd(cls, value: datetime.date) -> datetime.date:
        today = datetime.date.today()
        max_allowed = today.replace(year=today.year + MAX_DUE_DATE_YEARS_AHEAD)
        if value > max_allowed:
            raise ValueError(f"Due date cannot be more than {MAX_DUE_DATE_YEARS_AHEAD} years in the future.")
        return value

    # Server-side half of the same "due date can't be before the start
    # date" check the frontend's <input type="date" min="..."> already
    # nudges toward (see components/quotation.js) -- the check that
    # actually can't be bypassed by a direct API call.
    @model_validator(mode="after")
    def _due_on_or_after_start(self) -> "QuotationItemCreate":
        if self.due_date < self.start_date:
            raise ValueError("Due date cannot be before the start date.")
        return self


class QuotationOutsourcedItemCreate(BaseModel):
    """Manager/Admin-only: adds a \"not currently in inventory\" line to a submitted
    Quotation, with its own one-off name/description/price -- POST
    /quotations/{quotation_id}/outsourced-items. The requester can see this line
    once it's added (merged into the same items list as any regular asset, flagged
    `is_outsourced`) but has no route that can edit or remove it -- only a
    Manager/Admin can, via this endpoint or DELETE .../outsourced-items/{item_id}."""

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    unit_price: float = Field(..., ge=0)
    quantity: int = Field(1, ge=1)
    # Which external vendor/supplier this is being sourced from (e.g.
    # "Fountain Rentals") -- optional, free-text, internal tracking only.
    sourced_from: Optional[str] = Field(None, max_length=200)
    start_date: datetime.date
    due_date: datetime.date

    @field_validator("due_date")
    @classmethod
    def _due_date_not_absurd(cls, value: datetime.date) -> datetime.date:
        today = datetime.date.today()
        max_allowed = today.replace(year=today.year + MAX_DUE_DATE_YEARS_AHEAD)
        if value > max_allowed:
            raise ValueError(f"Due date cannot be more than {MAX_DUE_DATE_YEARS_AHEAD} years in the future.")
        return value

    @model_validator(mode="after")
    def _due_on_or_after_start(self) -> "QuotationOutsourcedItemCreate":
        if self.due_date < self.start_date:
            raise ValueError("Due date cannot be before the start date.")
        return self


class QuotationOutsourceAllocation(BaseModel):
    """One external source's share of a line's stock shortfall -- lets the
    Manager/Admin split what inventory can't cover across more than one
    outsourcing company (or just one) instead of the whole shortfall having
    to come from a single vendor. `sourced_from` and `unit_price` mirror
    QuotationOutsourcedItemCreate's own fields (optional vendor note /
    price-per-day override, falling back to the depleted AssetType's own
    catalog price when omitted)."""

    quantity: int = Field(..., ge=1)
    sourced_from: Optional[str] = Field(None, max_length=200)
    unit_price: Optional[float] = Field(None, ge=0)


class QuotationOutsourceShortfallItem(BaseModel):
    """One line item's worth of \"source the shortfall externally instead\" instruction
    for the Fulfillment Drawer's bulk physical checkout (POST /quotations/{id}/checkout).

    `quotation_item_id` must reference a real QuotationItem on the Quotation being
    checked out -- see bulk_checkout_quotation()'s STOCK LOGIC comment in
    services/quotation_service.py for exactly how this is applied: it is ONLY ever
    used for a line whose live, row-locked stock check at that exact moment comes up
    short, and only for the SHORTFALL portion -- whatever inventory DOES have on hand
    for that line still gets checked out of stock normally first. A decision submitted
    for a line that turns out to have enough stock after all is silently ignored (that
    line checks out of inventory normally) -- this keeps the Fulfillment Drawer's
    advisory `stock_shortfall` flag (computed when the queue was last loaded, possibly
    stale by the time the Manager/Admin actually clicks \"Check Out Selected\") from
    ever forcing an unnecessary outsourced substitution the authoritative check didn't
    actually require.

    `allocations` must add up to EXACTLY the live shortfall quantity (requested minus
    what's actually available at checkout time) -- one allocation per external source
    covering however much of the shortfall that source is taking, so the two together
    (in-stock checkout + one-or-more outsourced checkouts) always account for the
    full requested quantity with nothing left over or double-counted."""

    quotation_item_id: int
    allocations: list[QuotationOutsourceAllocation] = Field(..., min_length=1)


class QuotationCheckoutRequest(BaseModel):
    """Body of POST /quotations/{quotation_id}/checkout -- the Fulfillment Drawer's
    bulk physical checkout. Optional/defaults-to-empty on purpose: a Manager/Admin
    who never encounters a stock shortfall (or who wants an unresolved shortfall to
    keep blocking the checkout exactly like before this feature existed) can POST
    with no body/an empty list and nothing about the existing all-or-nothing
    behavior changes."""

    outsource_shortfall_items: list[QuotationOutsourceShortfallItem] = Field(default_factory=list)


class QuotationItemQuantityUpdate(BaseModel):
    """Changes the quantity on one existing line of the caller's own draft Quotation."""

    quantity: int = Field(..., ge=1)


class VatUpdateRequest(BaseModel):
    """Admin-only: sets the single global VAT percentage applied to every Quotation."""

    vat_percent: float = Field(..., ge=0, le=100)

    @field_validator("vat_percent")
    @classmethod
    def _round_vat(cls, value: float) -> float:
        return round(value, 2)


class QuotationAssignRequest(BaseModel):
    """Admin/Manager-only: designates who a submitted Quotation is for (POST
    /quotations/{id}/assign) -- either a linked Staff/Customer account
    (`assignee_type="user"`, `user_id` set) or an Ad-Hoc/unlinked individual
    (`assignee_type="outsider"`, `outsider_name` plus at least one of
    `outsider_email`/`outsider_phone` set, `outsider_company` optional),
    exactly like the Issue/Dispatch drawer's own Staff/Customer/Ad-Hoc
    split. Omit `assignee_type` (or pass `user_id: null`) to clear the
    assignment back to \"Unassigned\"."""

    assignee_type: Optional[str] = None  # "user" | "outsider" | None (clears assignment)
    user_id: Optional[int] = None
    # Same EXISTING-vs-BRAND-NEW split as schemas/assets.py's
    # AdvancedCheckoutRequest.outsider_id -- set this to assign to an
    # ad-hoc profile already on file instead of creating a new one via
    # outsider_name/outsider_email/outsider_phone/outsider_company.
    outsider_id: Optional[int] = None
    outsider_name: Optional[str] = None
    outsider_email: Optional[str] = None
    outsider_phone: Optional[str] = None
    outsider_company: Optional[str] = None

    @model_validator(mode="after")
    def _validate_assignee(self) -> "QuotationAssignRequest":
        if self.assignee_type == "user" and not self.user_id:
            raise ValueError("user_id is required when assignee_type is \"user\".")
        if self.assignee_type == "outsider" and not self.outsider_id and (not self.outsider_name or not (self.outsider_email or self.outsider_phone)):
            raise ValueError("outsider_id, or outsider_name plus at least one of outsider_email/outsider_phone, are required when assignee_type is \"outsider\".")
        return self


class QuotationMetaUpdate(BaseModel):
    """Admin/Manager-only: freeform notes on a submitted Quotation (PUT /quotations/{id})."""

    notes: Optional[str] = Field(None, max_length=2000)


class QuotationDiscountUpdateRequest(BaseModel):
    """Admin/Manager-only: sets the discount percentage (0-100) applied to a
    single Quotation's subtotal, before VAT (PUT /quotations/{id}/discount).
    Editable right up until the quote is fulfilled, exactly like the other
    line items on the quote -- see services/quotation_service.py's
    _ensure_admin_editable()."""

    discount_percent: float = Field(..., ge=0, le=100)

    @field_validator("discount_percent")
    @classmethod
    def _round_discount(cls, value: float) -> float:
        return round(value, 2)


class QuotationCreateRequest(BaseModel):
    """Admin/Manager-only: starts a brand new, already-submitted Quotation
    directly from the Quotes tab (e.g. building one on a user's behalf
    over the phone), optionally assigning it immediately -- to a linked
    Staff Member/Customer Account (`assignee_type="user"`, `assigned_user_id`
    set) or an Ad-Hoc/unlinked individual (`assignee_type="outsider"`,
    `outsider_name` plus at least one of `outsider_email`/`outsider_phone`
    set, `outsider_company` optional), same three-way split as the
    Issue/Dispatch drawer. Leave `assignee_type` unset to start unassigned.
    Starts with zero line items -- the caller adds them afterward via POST
    /quotations/{id}/items, same as any other submitted Quotation."""

    assignee_type: Optional[str] = None  # "user" | "outsider" | None (starts unassigned)
    assigned_user_id: Optional[int] = None
    # Same EXISTING-vs-BRAND-NEW split as schemas/assets.py's
    # AdvancedCheckoutRequest.outsider_id -- see QuotationAssignRequest
    # above for the full explanation.
    outsider_id: Optional[int] = None
    outsider_name: Optional[str] = None
    outsider_email: Optional[str] = None
    outsider_phone: Optional[str] = None
    outsider_company: Optional[str] = None

    @model_validator(mode="after")
    def _validate_assignee(self) -> "QuotationCreateRequest":
        if self.assignee_type == "user" and not self.assigned_user_id:
            raise ValueError("assigned_user_id is required when assignee_type is \"user\".")
        if self.assignee_type == "outsider" and not self.outsider_id and (not self.outsider_name or not (self.outsider_email or self.outsider_phone)):
            raise ValueError("outsider_id, or outsider_name plus at least one of outsider_email/outsider_phone, are required when assignee_type is \"outsider\".")
        return self


class QuotationNotificationsReadRequest(BaseModel):
    """Body for POST /quotations/me/notifications/read -- ids of the
    caller's own QuotationNotification rows to stamp as read. Any id not
    actually addressed to the caller is silently ignored server-side
    (see services/quotation_service.py's mark_quotation_notifications_read())
    rather than validated here, since that check needs the database."""

    notification_ids: list[int] = Field(default_factory=list)
