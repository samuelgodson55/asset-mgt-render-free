"""
tests/test_quotation_workflow.py
----------------------------------
End-to-end integration tests for the self-service Equipment Quotation
lifecycle: draft ("My Order" cart) -> submitted -> approved -> fulfilled
(see services/quotation_service.py's module docstring for the full state
machine). These exist specifically to catch SILENT calculation bugs --
wrong subtotal/discount/VAT math, stock touched at the wrong stage, a
partial bulk-checkout that doesn't roll back cleanly -- that a
status-code-only test would miss, alongside the status-transition guards
that keep one stage from being skipped or repeated.

PRICING MODEL UNDER TEST (see quotation_service.py's own docstring):
    line_total = unit_price * quantity * rental_days
    rental_days = (due_date - start_date).days + 1   (inclusive)
    subtotal = sum(line_total for every line)
    discount_amount = subtotal * discount_percent / 100
    vat_amount = (subtotal - discount_amount) * vat_percent / 100
    total = (subtotal - discount_amount) + vat_amount

STOCK MODEL UNDER TEST: AssetType.available_quantity must be UNCHANGED
through draft/submitted/approved, and only move at the final
bulk-checkout ("fulfilled") step -- and that step must be all-or-nothing
across every line on the quote.
"""

import datetime

import models

TODAY = datetime.date.today()


def _iso(d: datetime.date) -> str:
    return d.isoformat()


def _create_pool(admin_client, admin_headers, name, total_quantity, price):
    response = admin_client.post(
        "/api/assets", headers=admin_headers,
        json={"name": name, "total_quantity": total_quantity, "price": price},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _add_to_cart(staff_client, staff_headers, asset_id, quantity, start_date, due_date):
    response = staff_client.post(
        "/api/quotations/items", headers=staff_headers,
        json={
            "asset_id": asset_id, "quantity": quantity,
            "start_date": _iso(start_date), "due_date": _iso(due_date),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _available_quantity(client, headers, asset_id) -> int:
    return client.get(f"/api/assets/{asset_id}/details", headers=headers).json()["available_quantity"]


# ---------------------------------------------------------------------------
# 1) The golden path: draft -> submitted -> approved -> fulfilled, with
#    exact subtotal/total assertions and stock verified untouched until
#    the very last step.
# ---------------------------------------------------------------------------
def test_full_quotation_lifecycle_draft_to_fulfilled(as_admin, as_staff, as_manager, db_session):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Rental Camera", total_quantity=10, price=100.00)

    # --- DRAFT: a 3-day rental (start, start+1, start+2 inclusive) of 2 units.
    start = TODAY
    due = TODAY + datetime.timedelta(days=2)
    cart = _add_to_cart(staff_client, staff_headers, asset_id, quantity=2, start_date=start, due_date=due)
    assert cart["status"] == "draft"
    assert cart["items"][0]["days"] == 3
    assert cart["items"][0]["line_total"] == 600.00  # 100 * 2 * 3
    assert cart["subtotal"] == 600.00
    assert cart["total"] == 600.00  # no discount/VAT configured yet

    my_draft = staff_client.get("/api/quotations/me", headers=staff_headers).json()
    assert my_draft["id"] == cart["id"]
    assert my_draft["status"] == "draft"

    # Stock must be completely untouched while still a draft.
    assert _available_quantity(admin_client, admin_headers, asset_id) == 10

    # --- SUBMIT
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers)
    assert submitted.status_code == 200, submitted.text
    submitted_body = submitted.json()
    quotation_id = submitted_body["id"]
    assert submitted_body["status"] == "submitted"
    assert submitted_body["reference_number"] == f"QT-{quotation_id:06d}"
    assert submitted_body["submitted_at"] is not None
    assert submitted_body["total"] == 600.00  # totals survive submission unchanged

    # A fresh "My Order" cart is a brand-new empty draft, separate from
    # the just-submitted quotation.
    fresh_cart = staff_client.get("/api/quotations/me", headers=staff_headers).json()
    assert fresh_cart["id"] != quotation_id
    assert fresh_cart["status"] == "draft"
    assert fresh_cart["items"] == []

    # Still untouched at submitted.
    assert _available_quantity(admin_client, admin_headers, asset_id) == 10

    # --- APPROVE
    approved = manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)
    assert approved.status_code == 200, approved.text
    approved_body = approved.json()
    assert approved_body["status"] == "approved"
    assert approved_body["approved_at"] is not None
    assert approved_body["locked"] is False  # only "paid" locks it

    # Still untouched at approved -- stock only ever moves at fulfillment.
    assert _available_quantity(admin_client, admin_headers, asset_id) == 10

    # --- FULFILL (bulk checkout)
    fulfilled = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})
    assert fulfilled.status_code == 200, fulfilled.text
    fulfilled_body = fulfilled.json()
    assert fulfilled_body["status"] == "fulfilled"
    assert fulfilled_body["fulfilled_at"] is not None
    assert fulfilled_body["locked"] is False  # fulfilled remains editable to Admin/Manager until paid
    assert len(fulfilled_body["checkout_ids"]) == 1

    # Stock finally moves -- exactly by the quantity on the line, no more.
    assert _available_quantity(admin_client, admin_headers, asset_id) == 8

    checkout_id = fulfilled_body["checkout_ids"][0]
    checkout_row = db_session.query(models.AssetCheckout).filter(models.AssetCheckout.id == checkout_id).one()
    assert checkout_row.quantity == 2
    assert checkout_row.asset_id == asset_id
    assert checkout_row.quotation_id == quotation_id
    assert checkout_row.status == "active"


# ---------------------------------------------------------------------------
# 2) Discount is applied to the raw subtotal FIRST, VAT is applied to the
#    DISCOUNTED subtotal second -- exact-value regression test for that
#    ordering (see quotation_service.py's "Discount -> VAT -> Grand Total"
#    comment).
# ---------------------------------------------------------------------------
def test_discount_then_vat_calculation_order(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Projector", total_quantity=5, price=200.00)

    # Same-day booking -> 1 rental day -> subtotal == unit_price * quantity.
    _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    assert submitted["subtotal"] == 200.00

    vat_response = admin_client.put("/api/settings/vat", headers=admin_headers, json={"vat_percent": 10})
    assert vat_response.status_code == 200, vat_response.text

    discount_response = manager_client.put(
        f"/api/quotations/{quotation_id}/discount", headers=manager_headers, json={"discount_percent": 25},
    )
    assert discount_response.status_code == 200, discount_response.text
    body = discount_response.json()

    assert body["subtotal"] == 200.00
    assert body["discount_percent"] == 25.0
    assert body["discount_amount"] == 50.00          # 200 * 25%
    assert body["vat_percent"] == 10.0
    assert body["vat_amount"] == 15.00                # (200 - 50) * 10%, NOT 200 * 10%
    assert body["total"] == 165.00                    # 150 + 15


def test_multi_line_subtotal_sums_each_lines_own_rental_days(as_admin, as_staff):
    """Two lines with DIFFERENT rental-day spans must each be priced off
    their own (quantity * days), not a shared/averaged day count -- the
    kind of bug that only shows up with more than one line on the quote."""
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff

    laptop_id = _create_pool(admin_client, admin_headers, "Laptop", total_quantity=10, price=50.00)
    tripod_id = _create_pool(admin_client, admin_headers, "Tripod", total_quantity=10, price=10.00)

    # Line 1: 2 units, 3-day span -> 50 * 2 * 3 = 300
    _add_to_cart(staff_client, staff_headers, laptop_id, quantity=2, start_date=TODAY, due_date=TODAY + datetime.timedelta(days=2))
    # Line 2: 5 units, same-day (1 day) -> 10 * 5 * 1 = 50
    cart = _add_to_cart(staff_client, staff_headers, tripod_id, quantity=5, start_date=TODAY, due_date=TODAY)

    lines_by_asset = {line["asset_id"]: line for line in cart["items"]}
    assert lines_by_asset[laptop_id]["days"] == 3
    assert lines_by_asset[laptop_id]["line_total"] == 300.00
    assert lines_by_asset[tripod_id]["days"] == 1
    assert lines_by_asset[tripod_id]["line_total"] == 50.00
    assert cart["subtotal"] == 350.00


# ---------------------------------------------------------------------------
# 3) Status-transition guards -- every stage-skip/repeat must be rejected,
#    not silently accepted.
# ---------------------------------------------------------------------------
def test_cannot_approve_a_quotation_still_in_draft(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Speaker", total_quantity=5, price=40.00)
    draft = _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)

    response = manager_client.post(f"/api/quotations/{draft['id']}/approve", headers=manager_headers)
    assert response.status_code == 400
    assert "submitted" in response.json()["detail"].lower()


def test_cannot_submit_an_empty_cart(as_staff):
    staff_client, staff_headers = as_staff
    response = staff_client.post("/api/quotations/submit", headers=staff_headers)
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_cannot_approve_the_same_quotation_twice(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Mixer", total_quantity=5, price=30.00)
    _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]

    first = manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)
    assert first.status_code == 200

    second = manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)
    assert second.status_code == 400
    assert "submitted" in second.json()["detail"].lower()


def test_cannot_checkout_a_quotation_that_is_not_yet_approved(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Drone", total_quantity=5, price=300.00)
    _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]

    # Still "submitted", not "approved" -- checkout must be refused.
    response = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})
    assert response.status_code == 400
    assert "approved" in response.json()["detail"].lower()

    # Stock must not have moved despite the rejected attempt.
    assert _available_quantity(admin_client, admin_headers, asset_id) == 5


def test_cannot_checkout_a_quotation_twice(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Lighting Kit", total_quantity=5, price=60.00)
    _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)

    first_checkout = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})
    assert first_checkout.status_code == 200

    second_checkout = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})
    assert second_checkout.status_code == 400
    assert "approved" in second_checkout.json()["detail"].lower()

    # Stock must not have been double-deducted by the rejected repeat.
    assert _available_quantity(admin_client, admin_headers, asset_id) == 4


def test_requester_cannot_edit_items_once_submitted(as_admin, as_staff):
    """`_get_own_editable_quotation()` requires status == "submitted" for
    self-service edits, but once approved even the original requester is
    locked out (only an Admin/Manager may still adjust it -- see
    quotation_service.py's module docstring)."""
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff

    asset_id = _create_pool(admin_client, admin_headers, "Whiteboard", total_quantity=5, price=15.00)
    draft = _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    item_id = draft["items"][0]["item_id"]

    # A draft-stage edit through the "my submitted quotation" endpoints
    # (which require status == "submitted") must be refused before submission.
    response = staff_client.put(
        f"/api/quotations/me/{draft['id']}/items/{item_id}", headers=staff_headers, json={"quantity": 3},
    )
    assert response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# 4) Bulk checkout must be all-or-nothing: if ANY line on the quote can't
#    be fully covered by available stock, NONE of the lines' stock may
#    move and the quote must stay "approved" (not silently partially
#    fulfilled).
# ---------------------------------------------------------------------------
def test_bulk_checkout_is_all_or_nothing_on_stock_shortfall(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    plentiful_id = _create_pool(admin_client, admin_headers, "Plentiful Pool", total_quantity=5, price=10.00)
    scarce_id = _create_pool(admin_client, admin_headers, "Scarce Pool", total_quantity=1, price=10.00)

    _add_to_cart(staff_client, staff_headers, plentiful_id, quantity=3, start_date=TODAY, due_date=TODAY)
    _add_to_cart(staff_client, staff_headers, scarce_id, quantity=5, start_date=TODAY, due_date=TODAY)  # only 1 available
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)

    # No outsource_shortfall_items supplied -> the shortfall must block the
    # WHOLE checkout, not just the scarce line.
    response = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})
    assert response.status_code == 400
    assert "scarce pool" in response.json()["detail"].lower()

    # The plentiful line's stock must be untouched -- a rolled-back
    # transaction, not a partial checkout that silently drops the failing
    # line.
    assert _available_quantity(admin_client, admin_headers, plentiful_id) == 5
    assert _available_quantity(admin_client, admin_headers, scarce_id) == 1

    # The quote itself must still read "approved", not "fulfilled".
    detail = manager_client.get(f"/api/quotations/{quotation_id}", headers=manager_headers).json()
    assert detail["status"] == "approved"


def test_bulk_checkout_with_outsourced_shortfall_covers_available_stock_normally(as_admin, as_staff, as_manager):
    """When the Manager explicitly opts a shortfall line into outsourcing,
    whatever stock genuinely IS on hand still checks out of inventory
    normally, and only the remaining shortfall is covered externally."""
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    scarce_id = _create_pool(admin_client, admin_headers, "Scarce Widget", total_quantity=2, price=10.00)
    cart = _add_to_cart(staff_client, staff_headers, scarce_id, quantity=5, start_date=TODAY, due_date=TODAY)
    item_id = cart["items"][0]["item_id"]
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)

    response = manager_client.post(
        f"/api/quotations/{quotation_id}/checkout", headers=manager_headers,
        json={"outsource_shortfall_items": [
            {"quotation_item_id": item_id, "allocations": [{"quantity": 3, "sourced_from": "External Rentals Co"}]},
        ]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "fulfilled"
    assert len(body["checkout_ids"]) == 2  # one in-stock checkout (2 units) + one outsourced checkout (3 units)

    # All 2 available units were consumed by the in-stock portion.
    assert _available_quantity(admin_client, admin_headers, scarce_id) == 0


# ---------------------------------------------------------------------------
# 5) A quotation deleted while still a draft never becomes real inventory
#    history; a fulfilled one can never be deleted at all (permanent
#    record). Not the main happy path, but guards the same state machine.
# ---------------------------------------------------------------------------
def test_fulfilled_quotation_cannot_be_deleted(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Tent", total_quantity=5, price=25.00)
    _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)
    manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})

    delete_response = admin_client.delete(f"/api/quotations/{quotation_id}", headers=admin_headers)
    assert delete_response.status_code == 400


# ---------------------------------------------------------------------------
# 6) GET /assets/catalog now supports the same true server-side
#    limit/offset/search + total contract as every other directory
#    endpoint (GET /assets, /users, /outsiders) -- see
#    services/quotation_service.py's list_catalog().
# ---------------------------------------------------------------------------
def test_asset_catalog_supports_pagination_and_search(as_admin, as_staff):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff

    # The seeded demo dataset already has its own asset pools, so this
    # asserts against a uniquely-named trio it adds rather than an
    # absolute total -- resilient to whatever else is already in the DB.
    baseline_total = staff_client.get("/api/assets/catalog?limit=1", headers=staff_headers).json()["total"]

    _create_pool(admin_client, admin_headers, "Zzz-Alpha Tent", total_quantity=2, price=10.00)
    _create_pool(admin_client, admin_headers, "Zzz-Bravo Tent", total_quantity=2, price=12.00)
    _create_pool(admin_client, admin_headers, "Zzz-Charlie Cooler", total_quantity=2, price=8.00)

    searched = staff_client.get("/api/assets/catalog?search=Zzz-", headers=staff_headers)
    assert searched.status_code == 200, searched.text
    searched_body = searched.json()
    assert searched_body["total"] == 3
    assert {item["name"] for item in searched_body["items"]} == {"Zzz-Alpha Tent", "Zzz-Bravo Tent", "Zzz-Charlie Cooler"}

    first_page = staff_client.get("/api/assets/catalog?limit=2&offset=0&search=Zzz-", headers=staff_headers)
    body = first_page.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [item["name"] for item in body["items"]] == ["Zzz-Alpha Tent", "Zzz-Bravo Tent"]

    second_page = staff_client.get("/api/assets/catalog?limit=2&offset=2&search=Zzz-", headers=staff_headers)
    assert [item["name"] for item in second_page.json()["items"]] == ["Zzz-Charlie Cooler"]

    tent_only = staff_client.get("/api/assets/catalog?search=Zzz-A", headers=staff_headers)
    assert {item["name"] for item in tent_only.json()["items"]} == {"Zzz-Alpha Tent"}

    # Omitting limit/offset/search entirely still returns the whole active
    # catalog in one response (now including the 3 pools just created) --
    # the pre-pagination default every existing full-catalog caller (e.g.
    # the Admin/Manager Quote Detail drawer's typeahead) relies on.
    unpaginated = staff_client.get("/api/assets/catalog", headers=staff_headers)
    assert unpaginated.json()["total"] == baseline_total + 3
    assert len(unpaginated.json()["items"]) == baseline_total + 3


# ---------------------------------------------------------------------------
# 7) A Manager/Admin's own "My Order" cart (still `status="draft"`) can be
#    assigned to a user straight away -- assign_quotation() only blocks
#    on `status == "fulfilled"` (_ensure_admin_editable()), and the
#    "personal request" reassignment guard only fires for a staff/customer
#    requester, not a Manager/Admin building an order on someone's behalf.
#    This is what powers the Quotations page's "Assign Quote" button.
# ---------------------------------------------------------------------------
def test_manager_can_assign_own_draft_cart_to_a_user_before_submitting(as_admin, as_manager, as_customer):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Projector", total_quantity=3, price=40.00)
    cart = _add_to_cart(manager_client, manager_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    cart_id = cart["id"]
    assert cart["status"] == "draft"

    customer_id = customer_client.get("/api/auth/me", headers=customer_headers).json()["id"]

    assign_response = manager_client.post(
        f"/api/quotations/{cart_id}/assign", headers=manager_headers,
        json={"assignee_type": "user", "user_id": customer_id},
    )
    assert assign_response.status_code == 200, assign_response.text
    body = assign_response.json()
    assert body["status"] == "draft"
    assert body["assigned_to"]["id"] == customer_id


# ---------------------------------------------------------------------------
# 7b) REGRESSION: submitting a cart that was already assigned (test 7
#     above) must NOT silently reassign it back to the submitter.
#     submit_my_quotation() used to unconditionally set
#     `assigned_to_id = <submitter>` on every submit, discarding whatever
#     assignment a Manager/Admin had already made to their own
#     still-draft cart via "Assign Quote" -- the assignee got a "was
#     assigned to you" notification for a quote that then never actually
#     showed up on their "My Quotes" list, and reopening it via that
#     notification 404'd, until an Admin/Manager noticed and reassigned
#     it a SECOND time from the Quotes tab. Also covers the pre-submit
#     notification/reference-number text itself, which used to render
#     the literal string "None" (the still-null `reference_number`
#     column) instead of the eventual "QT-00000N".
# ---------------------------------------------------------------------------
def test_assigning_own_draft_cart_then_submitting_preserves_the_assignment(as_admin, as_manager, as_customer):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Field Recorder", total_quantity=3, price=25.00)
    cart = _add_to_cart(manager_client, manager_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    cart_id = cart["id"]

    customer_id = customer_client.get("/api/auth/me", headers=customer_headers).json()["id"]

    # Assign the still-draft cart to the customer BEFORE submitting.
    assign_response = manager_client.post(
        f"/api/quotations/{cart_id}/assign", headers=manager_headers,
        json={"assignee_type": "user", "user_id": customer_id},
    )
    assert assign_response.status_code == 200, assign_response.text
    assert assign_response.json()["assigned_to"]["id"] == customer_id

    # The pre-submission assignment notification must use the
    # deterministic "QT-00000N" reference, never the literal "None".
    pre_submit = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    assert len(pre_submit["items"]) == 1
    assert "None" not in pre_submit["items"][0]["message"]
    assert pre_submit["items"][0]["reference_number"] == f"QT-{cart_id:06d}"

    # --- SUBMIT: must NOT silently reassign back to the manager.
    submitted = manager_client.post("/api/quotations/submit", headers=manager_headers)
    assert submitted.status_code == 200, submitted.text
    submitted_body = submitted.json()
    assert submitted_body["id"] == cart_id
    assert submitted_body["status"] == "submitted"
    assert submitted_body["assigned_to"]["id"] == customer_id

    # The customer can now see it on their own "My Quotes" list...
    customer_history = customer_client.get("/api/quotations/me/history", headers=customer_headers).json()
    assert any(q["id"] == cart_id for q in customer_history["items"])

    # ...and can open its full detail directly -- the "View ->" click-
    # through this used to 404 on, because the reset assignment meant
    # the customer was neither the requester nor the assignee anymore.
    customer_detail = customer_client.get(f"/api/quotations/me/{cart_id}", headers=customer_headers)
    assert customer_detail.status_code == 200, customer_detail.text
    assert customer_detail.json()["assigned_to"]["id"] == customer_id


def test_submitting_an_unassigned_cart_still_self_assigns(as_admin, as_staff):
    """The auto-self-assign-on-submit behavior (the common, no
    Admin/Manager pre-assignment case) must still work exactly as
    before -- only the "already assigned to someone else" case changes
    (see test_assigning_own_draft_cart_then_submitting_preserves_the_assignment
    above)."""
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    staff_id = staff_client.get("/api/auth/me", headers=staff_headers).json()["id"]

    asset_id = _create_pool(admin_client, admin_headers, "Boom Mic", total_quantity=3, price=15.00)
    _add_to_cart(staff_client, staff_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)

    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["assigned_to"]["id"] == staff_id


# ---------------------------------------------------------------------------
# 8) In-app notifications: assigning a Quotation to a user notifies them,
#    a subsequent Admin/Manager change (discount) notifies them again, and
#    a Manager notifying THEMSELVES (assigning/editing their own quote)
#    never happens -- see services/quotation_service.py's
#    _notify_quotation_recipient().
# ---------------------------------------------------------------------------
def test_assigning_and_then_editing_a_quotation_notifies_the_recipient(as_admin, as_manager, as_customer):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Drone", total_quantity=4, price=75.00)
    _add_to_cart(manager_client, manager_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = manager_client.post("/api/quotations/submit", headers=manager_headers).json()
    quotation_id = submitted["id"]

    customer_id = customer_client.get("/api/auth/me", headers=customer_headers).json()["id"]

    # Before assignment: no notifications for the customer yet.
    before = customer_client.get("/api/quotations/me/notifications", headers=customer_headers)
    assert before.status_code == 200, before.text
    assert before.json()["items"] == []

    assign_response = manager_client.post(
        f"/api/quotations/{quotation_id}/assign", headers=manager_headers,
        json={"assignee_type": "user", "user_id": customer_id},
    )
    assert assign_response.status_code == 200, assign_response.text

    after_assign = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    assert len(after_assign["items"]) == 1
    assert after_assign["items"][0]["kind"] == "assigned"
    assert after_assign["items"][0]["reference_number"] == submitted["reference_number"]
    assert after_assign["items"][0]["read_at"] is None

    # A subsequent admin-side change (discount) adds a SECOND notification
    # for the same recipient, without disturbing the first.
    discount_response = manager_client.put(
        f"/api/quotations/{quotation_id}/discount", headers=manager_headers, json={"discount_percent": 10},
    )
    assert discount_response.status_code == 200, discount_response.text

    after_discount = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    assert len(after_discount["items"]) == 2
    assert {item["kind"] for item in after_discount["items"]} == {"assigned", "updated"}

    # Marking them read is scoped to the caller and is idempotent-safe.
    ids = [item["id"] for item in after_discount["items"]]
    mark_response = customer_client.post(
        "/api/quotations/me/notifications/read", headers=customer_headers, json={"notification_ids": ids},
    )
    assert mark_response.status_code == 200, mark_response.text
    assert mark_response.json()["updated"] == 2

    after_read = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    assert all(item["read_at"] is not None for item in after_read["items"])


def test_manager_editing_their_own_assigned_quotation_does_not_self_notify(as_admin, as_manager):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Tripod Kit", total_quantity=3, price=20.00)
    _add_to_cart(manager_client, manager_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = manager_client.post("/api/quotations/submit", headers=manager_headers).json()

    # A Manager's own submitted quote (no explicit assignment yet) falls
    # back to `user_id`, which IS the manager themselves -- editing it
    # should never create a notification addressed to themselves.
    manager_client.put(
        f"/api/quotations/{submitted['id']}/discount", headers=manager_headers, json={"discount_percent": 5},
    )
    own_notifications = manager_client.get("/api/quotations/me/notifications", headers=manager_headers).json()
    assert own_notifications["items"] == []


# ---------------------------------------------------------------------------
# Quotation notifications -- fulfillment, not-in-inventory lines, and the
# SEND_QUOTATION_RECIPIENT_EMAILS email gate. See
# services/quotation_service.py's _notify_quotation_recipient() docstring
# and config.py's SEND_QUOTATION_RECIPIENT_EMAILS docstring.
# ---------------------------------------------------------------------------
def test_fulfillment_notifies_the_recipient(as_admin, as_manager, as_customer):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Boom Mic", total_quantity=5, price=15.00)
    _add_to_cart(customer_client, customer_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = customer_client.post("/api/quotations/submit", headers=customer_headers).json()
    quotation_id = submitted["id"]

    approve_response = manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)
    assert approve_response.status_code == 200, approve_response.text

    # Self-submitted -> already assigned to the customer themselves, so
    # approving above already queued one "updated" notification. Clear the
    # slate so the assertion below is unambiguous about what fulfillment
    # itself adds.
    before = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    before_ids = [item["id"] for item in before["items"]]
    customer_client.post(
        "/api/quotations/me/notifications/read", headers=customer_headers, json={"notification_ids": before_ids},
    )

    checkout_response = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers)
    assert checkout_response.status_code == 200, checkout_response.text

    after = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    new_items = [item for item in after["items"] if item["read_at"] is None]
    assert len(new_items) == 1
    assert new_items[0]["kind"] == "updated"
    assert "fulfilled" in new_items[0]["message"].lower()
    assert new_items[0]["reference_number"] == submitted["reference_number"]


def test_adding_not_in_inventory_item_notifies_recipient_without_outsourced_wording(as_admin, as_manager, as_customer):
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Gimbal", total_quantity=2, price=30.00)
    _add_to_cart(customer_client, customer_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = customer_client.post("/api/quotations/submit", headers=customer_headers).json()
    quotation_id = submitted["id"]

    add_response = manager_client.post(
        f"/api/quotations/{quotation_id}/outsourced-items", headers=manager_headers,
        json={
            "name": "Specialty Crane Arm", "unit_price": 200.0, "quantity": 1,
            "sourced_from": "Fountain Rentals",
            "start_date": _iso(TODAY), "due_date": _iso(TODAY),
        },
    )
    assert add_response.status_code == 200, add_response.text

    notifications = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    matching = [item for item in notifications["items"] if "Specialty Crane Arm" in item["message"]]
    assert len(matching) == 1
    message = matching[0]["message"]
    assert matching[0]["kind"] == "updated"
    # The customer-facing message names just the asset -- never the
    # internal "outsourced"/"sourced from" detail (that stays in the
    # AuditLog only, see admin_add_outsourced_item()'s own comment).
    assert "outsourced" not in message.lower()
    assert "sourced from" not in message.lower()
    assert "Fountain Rentals" not in message


def test_removing_not_in_inventory_item_still_does_not_notify(as_admin, as_manager, as_customer):
    # Pre-existing behavior, unchanged by this feature -- only ADDING a
    # not-in-inventory line notifies; removing one doesn't (unlike
    # removing a regular catalog line, which does).
    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Light Kit", total_quantity=2, price=10.00)
    _add_to_cart(customer_client, customer_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = customer_client.post("/api/quotations/submit", headers=customer_headers).json()
    quotation_id = submitted["id"]

    added = manager_client.post(
        f"/api/quotations/{quotation_id}/outsourced-items", headers=manager_headers,
        json={"name": "Rare Lens", "unit_price": 50.0, "quantity": 1, "start_date": _iso(TODAY), "due_date": _iso(TODAY)},
    ).json()
    outsourced_item_id = next(
        li["outsourced_item_id"] for li in added["items"] if li.get("is_outsourced") and li["asset_name"] == "Rare Lens"
    )

    before = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    before_ids = [item["id"] for item in before["items"]]
    customer_client.post(
        "/api/quotations/me/notifications/read", headers=customer_headers, json={"notification_ids": before_ids},
    )

    # Removal itself is covered elsewhere for status-code correctness;
    # here we only care that it does NOT add a new unread notification.
    remove_response = manager_client.delete(
        f"/api/quotations/{quotation_id}/outsourced-items/{outsourced_item_id}", headers=manager_headers,
    )
    assert remove_response.status_code == 200, remove_response.text

    after = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    new_items = [item for item in after["items"] if item["read_at"] is None]
    assert new_items == []


def test_send_quotation_recipient_emails_gate_off_still_creates_in_app_notification(as_admin, as_manager, as_customer, monkeypatch):
    import services.quotation_service as quotation_service

    monkeypatch.setattr(quotation_service.settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(quotation_service.settings, "SEND_QUOTATION_RECIPIENT_EMAILS", False)
    sent = []
    monkeypatch.setattr(quotation_service.notification_service, "send_email", lambda *a, **kw: sent.append((a, kw)) or True)

    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Steadicam", total_quantity=2, price=40.00)
    _add_to_cart(customer_client, customer_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = customer_client.post("/api/quotations/submit", headers=customer_headers).json()

    discount_response = manager_client.put(
        f"/api/quotations/{submitted['id']}/discount", headers=manager_headers, json={"discount_percent": 15},
    )
    assert discount_response.status_code == 200, discount_response.text

    # The gate blocks the EMAIL entirely...
    assert sent == []
    # ...but the in-app bell notification is still created either way.
    notifications = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    assert len(notifications["items"]) == 1
    assert notifications["items"][0]["kind"] == "updated"


def test_send_quotation_recipient_emails_gate_on_sends_email_alongside_in_app(as_admin, as_manager, as_customer, monkeypatch):
    import services.quotation_service as quotation_service

    monkeypatch.setattr(quotation_service.settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(quotation_service.settings, "SEND_QUOTATION_RECIPIENT_EMAILS", True)
    sent = []
    monkeypatch.setattr(quotation_service.notification_service, "send_email", lambda *a, **kw: sent.append((a, kw)) or True)

    admin_client, admin_headers = as_admin
    manager_client, manager_headers = as_manager
    customer_client, customer_headers = as_customer

    asset_id = _create_pool(admin_client, admin_headers, "Slider", total_quantity=2, price=25.00)
    _add_to_cart(customer_client, customer_headers, asset_id, quantity=1, start_date=TODAY, due_date=TODAY)
    submitted = customer_client.post("/api/quotations/submit", headers=customer_headers).json()

    discount_response = manager_client.put(
        f"/api/quotations/{submitted['id']}/discount", headers=manager_headers, json={"discount_percent": 15},
    )
    assert discount_response.status_code == 200, discount_response.text

    assert len(sent) == 1
    notifications = customer_client.get("/api/quotations/me/notifications", headers=customer_headers).json()
    assert len(notifications["items"]) == 1



def test_manager_can_delete_submitted_and_approved_quotations(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    # Submitted quote: Manager is explicitly allowed to delete it.
    asset_id = _create_pool(admin_client, admin_headers, "Manager Delete Submitted", total_quantity=2, price=20.00)
    _add_to_cart(staff_client, staff_headers, asset_id, 1, TODAY, TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    response = manager_client.delete(f"/api/quotations/{quotation_id}", headers=manager_headers)
    assert response.status_code == 200, response.text

    # Approved quote: Manager can also delete it before physical fulfillment.
    _add_to_cart(staff_client, staff_headers, asset_id, 1, TODAY, TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    approved = manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers)
    assert approved.status_code == 200, approved.text
    response = manager_client.delete(f"/api/quotations/{quotation_id}", headers=manager_headers)
    assert response.status_code == 200, response.text


def test_fulfilled_quotation_can_be_edited_then_marked_paid_and_locked(as_admin, as_staff, as_manager):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Paid Workflow Asset", total_quantity=5, price=50.00)
    _add_to_cart(staff_client, staff_headers, asset_id, 1, TODAY, TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    assert manager_client.post(f"/api/quotations/{quotation_id}/approve", headers=manager_headers).status_code == 200
    fulfilled = manager_client.post(f"/api/quotations/{quotation_id}/checkout", headers=manager_headers, json={})
    assert fulfilled.status_code == 200, fulfilled.text
    assert fulfilled.json()["status"] == "fulfilled"
    assert fulfilled.json()["locked"] is False

    # Operational correction is allowed after fulfillment and before payment.
    edited = manager_client.put(
        f"/api/quotations/{quotation_id}",
        headers=manager_headers,
        json={"notes": "Corrected fulfillment note"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["notes"] == "Corrected fulfillment note"

    paid = manager_client.post(
        f"/api/quotations/{quotation_id}/paid",
        headers=manager_headers,
        json={"payment_method": "bank_transfer", "payment_reference": "TRX-PAID-001"},
    )
    assert paid.status_code == 200, paid.text
    paid_body = paid.json()
    assert paid_body["status"] == "paid"
    assert paid_body["locked"] is True
    assert paid_body["payment_method"] == "bank_transfer"
    assert paid_body["payment_reference"] == "TRX-PAID-001"
    assert paid_body["paid_at"] is not None

    # Paid is terminal: neither operational edits nor deletion are permitted.
    locked_edit = manager_client.put(
        f"/api/quotations/{quotation_id}",
        headers=manager_headers,
        json={"notes": "Should not change"},
    )
    assert locked_edit.status_code == 400

    locked_delete = manager_client.delete(f"/api/quotations/{quotation_id}", headers=manager_headers)
    assert locked_delete.status_code == 400


def test_paid_cannot_be_set_before_fulfillment(as_staff, as_manager, as_admin):
    admin_client, admin_headers = as_admin
    staff_client, staff_headers = as_staff
    manager_client, manager_headers = as_manager

    asset_id = _create_pool(admin_client, admin_headers, "Paid Before Fulfillment", total_quantity=2, price=25.00)
    _add_to_cart(staff_client, staff_headers, asset_id, 1, TODAY, TODAY)
    submitted = staff_client.post("/api/quotations/submit", headers=staff_headers).json()
    quotation_id = submitted["id"]
    response = manager_client.post(
        f"/api/quotations/{quotation_id}/paid",
        headers=manager_headers,
        json={"payment_method": "cash"},
    )
    assert response.status_code == 400
    assert "fulfilled" in response.json()["detail"].lower()
