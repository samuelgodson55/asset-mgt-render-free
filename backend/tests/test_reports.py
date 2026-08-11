"""
tests/test_reports.py
-----------------------
Coverage for api/reports_api.py / services/reports_service.py -- the
Manager/Admin business-metrics dashboard (utilization by asset type,
overdue trends, spend by category/department, quotation approval
turnaround time). Mirrors test_permissions.py's role-gate style for the
access-control checks, then exercises each section's shape against the
seeded demo data plus a couple of purpose-built fixtures for the trickier
aggregate math (overdue trend, turnaround time).
"""

import datetime

import pytest


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def test_reports_requires_auth(client):
    for path in [
        "/api/reports/dashboard",
        "/api/reports/utilization",
        "/api/reports/overdue-trend",
        "/api/reports/spend",
        "/api/reports/revenue",
        "/api/reports/quotation-turnaround",
    ]:
        response = client.get(path)
        assert response.status_code == 401, path


@pytest.mark.parametrize("fixture_name", ["as_staff", "as_customer"])
def test_reports_forbidden_for_self_service_roles(fixture_name, request):
    client, headers = request.getfixturevalue(fixture_name)
    for path in [
        "/api/reports/dashboard",
        "/api/reports/utilization",
        "/api/reports/overdue-trend",
        "/api/reports/spend",
        "/api/reports/revenue",
        "/api/reports/quotation-turnaround",
    ]:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, path


def test_reports_reachable_by_manager_and_admin(as_manager, as_admin):
    for client, headers in (as_manager, as_admin):
        response = client.get("/api/reports/dashboard", headers=headers)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Dashboard shape (seeded demo data)
# ---------------------------------------------------------------------------
def test_dashboard_shape_against_seed_data(as_manager):
    client, headers = as_manager
    response = client.get("/api/reports/dashboard", headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert set(body.keys()) == {"period", "utilization_by_asset_type", "overdue", "spend", "revenue", "quotation_turnaround"}

    # Utilization: one row per seeded, non-deleted asset pool, sorted
    # highest utilization first.
    util_rows = body["utilization_by_asset_type"]
    assert len(util_rows) >= 1
    for row in util_rows:
        assert row["total_quantity"] >= row["currently_checked_out"] >= 0
        if row["total_quantity"]:
            assert row["utilization_rate"] == pytest.approx(
                row["currently_checked_out"] / row["total_quantity"], abs=1e-4
            )
    rates = [r["utilization_rate"] for r in util_rows if r["utilization_rate"] is not None]
    assert rates == sorted(rates, reverse=True)

    # Overdue: trend is a contiguous run of months with no gaps, and the
    # live snapshot always agrees with its own by_asset_type/by_department
    # breakdown totals.
    overdue = body["overdue"]
    assert len(overdue["trend"]) >= 1
    assert overdue["total_overdue_now"] == sum(r["overdue_count"] for r in overdue["by_asset_type"])
    assert overdue["total_overdue_now"] == sum(r["overdue_count"] for r in overdue["by_department"])

    # Spend: category/department totals both sum to the same grand total
    # (every priced checkout is counted exactly once on each axis).
    spend = body["spend"]
    cat_total = sum(r["total_spend"] for r in spend["by_category"])
    dept_total = sum(r["total_spend"] for r in spend["by_department"])
    assert cat_total == pytest.approx(dept_total, rel=1e-6)

    revenue = body["revenue"]
    assert revenue["total_revenue"] >= 0
    assert revenue["total_revenue"] == pytest.approx(sum(r["total_revenue"] for r in revenue["by_department"]), rel=1e-6)
    assert all(r["department"] for r in revenue["by_department"])

    # Quotation turnaround: seeded data has no fulfilled quotations, so
    # every average is None with a zero sample size rather than a crash.
    turnaround = body["quotation_turnaround"]
    assert turnaround["sample_size_submit_to_fulfill"] == 0
    assert turnaround["avg_submit_to_fulfill_hours"] is None


def test_individual_section_endpoints_match_dashboard(as_manager):
    client, headers = as_manager
    dashboard = client.get("/api/reports/dashboard", headers=headers).json()

    assert client.get("/api/reports/utilization", headers=headers).json() == dashboard["utilization_by_asset_type"]
    assert client.get("/api/reports/overdue-trend", headers=headers).json() == dashboard["overdue"]
    assert client.get("/api/reports/spend", headers=headers).json() == dashboard["spend"]
    assert client.get("/api/reports/revenue", headers=headers).json() == dashboard["revenue"]
    assert (
        client.get("/api/reports/quotation-turnaround", headers=headers).json()
        == dashboard["quotation_turnaround"]
    )


def test_utilization_category_filter(as_manager):
    client, headers = as_manager
    all_rows = client.get("/api/reports/utilization", headers=headers).json()
    categories = {r["category"] for r in all_rows if r["category"]}
    assert categories, "seed data should have at least one categorized asset pool"
    target = sorted(categories)[0]

    filtered = client.get("/api/reports/utilization", headers=headers, params={"category": target}).json()
    assert filtered
    assert all(r["category"] == target for r in filtered)


# ---------------------------------------------------------------------------
# Overdue trend math against a purpose-built fixture
# ---------------------------------------------------------------------------
def test_overdue_trend_counts_active_and_late_returned_checkouts(db_session, as_manager):
    import models

    client, headers = as_manager
    db = db_session

    asset_type = db.query(models.AssetType).filter(models.AssetType.is_deleted.is_(False)).first()
    staff = db.query(models.User).filter(models.User.role == "staff").first()
    now = models.utc_now()

    # Currently active and overdue -- due last month, never returned.
    active_overdue_due = now.replace(day=1) - datetime.timedelta(days=20)
    db.add(
        models.AssetCheckout(
            asset_id=asset_type.id,
            user_id=staff.id,
            quantity=1,
            checkout_date=active_overdue_due - datetime.timedelta(days=10),
            due_date=active_overdue_due,
            status="active",
        )
    )
    # Returned LATE -- due two months ago, returned a week after due.
    late_due = now.replace(day=1) - datetime.timedelta(days=50)
    db.add(
        models.AssetCheckout(
            asset_id=asset_type.id,
            user_id=staff.id,
            quantity=1,
            checkout_date=late_due - datetime.timedelta(days=5),
            due_date=late_due,
            returned_at=late_due + datetime.timedelta(days=7),
            status="returned",
        )
    )
    # Returned ON TIME -- should never count as "went overdue".
    on_time_due = now.replace(day=1) - datetime.timedelta(days=15)
    db.add(
        models.AssetCheckout(
            asset_id=asset_type.id,
            user_id=staff.id,
            quantity=1,
            checkout_date=on_time_due - datetime.timedelta(days=5),
            due_date=on_time_due,
            returned_at=on_time_due - datetime.timedelta(days=1),
            status="returned",
        )
    )
    db.commit()

    response = client.get(
        "/api/reports/overdue-trend",
        headers=headers,
        params={"start_date": (now - datetime.timedelta(days=90)).date().isoformat()},
    )
    assert response.status_code == 200
    body = response.json()

    total_trend_overdue = sum(r["overdue_count"] for r in body["trend"])
    assert total_trend_overdue >= 2  # the two "went overdue" checkouts above, at minimum
    assert body["total_overdue_now"] >= 1  # the still-active overdue checkout


def test_revenue_report_groups_fulfilled_quote_lines_by_asset_department(db_session, as_manager):
    import models

    client, headers = as_manager
    db = db_session
    staff = db.query(models.User).filter(models.User.role == "staff").first()
    asset = models.AssetType(
        name="Revenue Camera Pool",
        total_quantity=5,
        available_quantity=5,
        category="Production",
        department="Camera",
        price=100.00,
    )
    db.add(asset)
    db.flush()

    now = models.utc_now()
    quote = models.Quotation(
        user_id=staff.id,
        status="fulfilled",
        reference_number="QT-999901",
        submitted_at=now - datetime.timedelta(days=3),
        approved_at=now - datetime.timedelta(days=2),
        fulfilled_at=now,
        discount_percent=10,
    )
    db.add(quote)
    db.flush()
    db.add(models.QuotationItem(
        quotation_id=quote.id,
        asset_id=asset.id,
        quantity=2,
        start_date=(now - datetime.timedelta(days=2)).date(),
        due_date=(now - datetime.timedelta(days=1)).date(),
    ))
    db.commit()

    response = client.get("/api/reports/revenue", headers=headers)
    assert response.status_code == 200
    body = response.json()
    camera = next(row for row in body["by_department"] if row["department"] == "Camera")
    # 2 units × 2 rental days × ₦100, less the 10% quote discount.
    assert camera["total_revenue"] >= 360.0
    assert body["total_revenue"] >= 360.0
    assert camera["item_count"] >= 2

    # Moving the same fulfilled quote to terminal paid must not make earned
    # rental revenue disappear from reporting. Revenue is recognized from
    # fulfillment and remains reportable after payment.
    quote.status = "paid"
    quote.paid_at = now + datetime.timedelta(hours=1)
    db.commit()
    paid_response = client.get("/api/reports/revenue", headers=headers)
    assert paid_response.status_code == 200
    paid_body = paid_response.json()
    paid_camera = next(row for row in paid_body["by_department"] if row["department"] == "Camera")
    assert paid_camera["total_revenue"] >= 360.0
