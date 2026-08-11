"""
services/reports_service.py
----------------------------
Business-metrics analytics for the Manager/Admin Reporting dashboard --
utilization by asset type, overdue trends, spend by category/department,
and quotation approval turnaround time. Used by api/reports_api.py.

This is DELIBERATELY separate from telemetry.py's OpenTelemetry tracing.
Tracing answers "why was this one request slow / where did it fail" for an
engineer; this module answers "how is the fleet being used" for a Manager
or Admin -- aggregate business numbers computed straight off the existing
system-of-record tables (AssetType, AssetCheckout, Quotation, User), not
off spans. No new tables and no new write path are introduced here: every
figure below is derived, on read, from data the app was already
capturing for other reasons (the same "audit log + checkout history
already has everything needed" data this feature is built on).

SCOPE / VISIBILITY
-------------------
Every function here is gated at the router level by `require_privileged_role`
(Manager, Admin, or Super Admin) -- see api/reports_api.py. Per main.py's
role model, a Manager already has unscoped, system-wide visibility (no
department restriction), so there is no per-caller filtering to apply
here the way e.g. services/user_service.py scopes a department. The
optional `department`/`category` query filters this module accepts are a
plain narrowing convenience for the person looking at the dashboard, not
a security boundary.

DATE RANGE HANDLING
--------------------
Every public function accepts optional `start_date`/`end_date` (plain
calendar dates, inclusive). When omitted, callers get an ALL-TIME figure
for point-in-time metrics (current utilization, current overdue items)
and a trailing-window default for time-series metrics (overdue trend,
quotation turnaround) -- see each function's own docstring for its
specific default.
"""

import datetime
from typing import Optional

from sqlalchemy.orm import Session

import models
from models import utc_now

# Same reasoning as every other listing module in this project (see
# services/checkout_service.py's DEFAULT_LIMIT comment) -- caps how many
# trend buckets / breakdown rows a single response can carry so a
# pathological date range can't blow up the payload.
MAX_TREND_MONTHS = 24
DEFAULT_TREND_MONTHS = 6


def _month_key(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m")


def _month_label(key: str) -> str:
    dt = datetime.datetime.strptime(key, "%Y-%m")
    return dt.strftime("%b %Y")


def _as_aware_utc(value: "Optional[datetime.datetime]") -> "Optional[datetime.datetime]":
    """Same SQLite-vs-Postgres tz normalization as models._as_aware_utc --
    duplicated here (rather than imported) since that helper is private to
    models.py; kept trivial on purpose so it's obviously equivalent."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _date_range_bounds(
    start_date: "Optional[datetime.date]", end_date: "Optional[datetime.date]"
) -> tuple["Optional[datetime.datetime]", "Optional[datetime.datetime]"]:
    """
    Turns optional inclusive calendar Dates into a [start, end) pair of
    timezone-aware UTC datetimes suitable for comparing against a
    TIMESTAMPTZ column -- `end_date` (a whole day) becomes midnight of the
    FOLLOWING day so a checkout/quotation event any time during the last
    day of the range is still included.
    """
    start_dt = None
    end_dt = None
    if start_date is not None:
        start_dt = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    if end_date is not None:
        end_dt = datetime.datetime.combine(
            end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc
        )
    return start_dt, end_dt


# ---------------------------------------------------------------------------
# 1. UTILIZATION BY ASSET TYPE
# ---------------------------------------------------------------------------
def get_utilization_by_asset_type(
    db: Session,
    start_date: "Optional[datetime.date]" = None,
    end_date: "Optional[datetime.date]" = None,
    category: "Optional[str]" = None,
) -> list[dict]:
    """
    One row per active (non-deleted) AssetType pool: how much of it is in
    use RIGHT NOW (`utilization_rate` = currently-checked-out units /
    total_quantity) alongside how much DEMAND it saw during the requested
    window (`checkout_count`/`total_checkout_days` -- every AssetCheckout
    against this pool whose `checkout_date` falls in range, and the sum of
    each one's duration so far). A pool with `total_quantity == 0` reports
    `utilization_rate: None` rather than dividing by zero.

    `total_checkout_days` for a still-active checkout counts up to NOW
    (not some future date), so it only ever reflects time that has
    actually elapsed -- consistent with how `is_overdue()`/`is_due_soon()`
    elsewhere in this app always reason from `utc_now()`.
    """
    start_dt, end_dt = _date_range_bounds(start_date, end_date)
    now = utc_now()

    query = db.query(models.AssetType).filter(models.AssetType.is_deleted.is_(False))
    if category:
        query = query.filter(models.AssetType.category == category)
    asset_types = query.order_by(models.AssetType.name.asc()).all()

    rows = []
    for asset_type in asset_types:
        checkouts = [c for c in asset_type.checkouts]
        if start_dt is not None:
            checkouts = [c for c in checkouts if c.checkout_date and _as_aware_utc(c.checkout_date) >= start_dt]
        if end_dt is not None:
            checkouts = [c for c in checkouts if c.checkout_date and _as_aware_utc(c.checkout_date) < end_dt]

        checkout_count = len(checkouts)
        total_days = 0.0
        for c in checkouts:
            began = _as_aware_utc(c.checkout_date) or now
            ended = _as_aware_utc(c.returned_at) or now
            total_days += max((ended - began).total_seconds() / 86400.0, 0.0)

        currently_checked_out = asset_type.total_quantity - asset_type.available_quantity
        utilization_rate = (
            round(currently_checked_out / asset_type.total_quantity, 4) if asset_type.total_quantity else None
        )

        rows.append(
            {
                "asset_type_id": asset_type.id,
                "name": asset_type.name,
                "category": asset_type.category,
                "total_quantity": asset_type.total_quantity,
                "available_quantity": asset_type.available_quantity,
                "currently_checked_out": currently_checked_out,
                "utilization_rate": utilization_rate,
                "checkout_count": checkout_count,
                "total_checkout_days": round(total_days, 1),
            }
        )

    # Highest-utilization pools first -- what a Manager/Admin scanning this
    # table almost always wants to see at a glance ("what's running hot").
    rows.sort(key=lambda r: (r["utilization_rate"] is None, -(r["utilization_rate"] or 0)))
    return rows


# ---------------------------------------------------------------------------
# 2. OVERDUE TRENDS
# ---------------------------------------------------------------------------
def get_overdue_trend(
    db: Session,
    start_date: "Optional[datetime.date]" = None,
    end_date: "Optional[datetime.date]" = None,
) -> dict:
    """
    Monthly time series of "how many checkouts went overdue in month X",
    bucketed by each checkout's `due_date`, plus a live snapshot of every
    CURRENTLY overdue checkout broken down by asset type and department.

    A checkout counts as having "gone overdue" in its due_date's month if
    either (a) it's still active and that due_date has already passed, or
    (b) it was eventually returned LATE (`returned_at > due_date`) -- this
    is derived entirely from AssetCheckout's existing due_date/returned_at/
    status columns; nothing new is written to capture it.

    Defaults to the trailing DEFAULT_TREND_MONTHS (6) months ending this
    month when no range is given; the window is capped at MAX_TREND_MONTHS
    (24) months even when an explicit range asks for more.
    """
    now = utc_now()
    if end_date is None:
        range_end = now
    else:
        range_end = datetime.datetime.combine(end_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    if start_date is None:
        range_start = (range_end.replace(day=1) - datetime.timedelta(days=32 * (DEFAULT_TREND_MONTHS - 1))).replace(day=1)
    else:
        range_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.timezone.utc)

    # Build the ordered list of month buckets up front so months with zero
    # overdue events still show up as a 0 rather than a gap in the chart.
    months: list[str] = []
    cursor = range_start.replace(day=1)
    end_cursor = range_end.replace(day=1)
    guard = 0
    while cursor <= end_cursor and guard < MAX_TREND_MONTHS:
        months.append(_month_key(cursor))
        cursor = (cursor + datetime.timedelta(days=32)).replace(day=1)
        guard += 1
    counts = {m: 0 for m in months}

    checkouts = (
        db.query(models.AssetCheckout)
        .filter(models.AssetCheckout.due_date.isnot(None))
        .filter(models.AssetCheckout.due_date >= range_start)
        .filter(models.AssetCheckout.due_date < end_cursor + datetime.timedelta(days=32))
        .all()
    )
    for c in checkouts:
        due = _as_aware_utc(c.due_date)
        went_overdue = (c.status == "active" and due < now) or (
            c.returned_at is not None and _as_aware_utc(c.returned_at) > due
        )
        if not went_overdue:
            continue
        key = _month_key(due)
        if key in counts:
            counts[key] += 1

    trend = [{"month": m, "label": _month_label(m), "overdue_count": counts[m]} for m in months]

    # Live snapshot: every checkout that is overdue RIGHT NOW.
    currently_overdue = (
        db.query(models.AssetCheckout)
        .filter(models.AssetCheckout.status == "active")
        .filter(models.AssetCheckout.due_date.isnot(None))
        .filter(models.AssetCheckout.due_date < now)
        .all()
    )
    by_asset_type: dict[str, int] = {}
    by_department: dict[str, int] = {}
    for c in currently_overdue:
        type_name = c.asset.name if c.asset else (c.outsourced_item_name or "Outsourced Item")
        by_asset_type[type_name] = by_asset_type.get(type_name, 0) + 1
        if c.user is not None:
            dept = c.user.department or "Unassigned"
        elif c.outsider is not None:
            dept = "External / Ad-Hoc"
        else:
            dept = "Unassigned"
        by_department[dept] = by_department.get(dept, 0) + 1

    return {
        "trend": trend,
        "total_overdue_now": len(currently_overdue),
        "by_asset_type": sorted(
            [{"name": k, "overdue_count": v} for k, v in by_asset_type.items()],
            key=lambda r: -r["overdue_count"],
        ),
        "by_department": sorted(
            [{"department": k, "overdue_count": v} for k, v in by_department.items()],
            key=lambda r: -r["overdue_count"],
        ),
    }


# ---------------------------------------------------------------------------
# 3. SPEND BY CATEGORY / DEPARTMENT
# ---------------------------------------------------------------------------
def get_spend_breakdown(
    db: Session,
    start_date: "Optional[datetime.date]" = None,
    end_date: "Optional[datetime.date]" = None,
) -> dict:
    """
    "Spend" here is checked-out VALUE, not a cash ledger this app doesn't
    keep: for every AssetCheckout in range, `quantity * unit price`, where
    the unit price is the live `AssetType.price` for a normal pool item or
    the snapshotted `outsourced_unit_price` for a Manager/Admin-sourced
    outsourced item (see models.AssetCheckout's own docstring on why
    outsourced lines snapshot their price instead of joining one). A
    checkout against an item with no price on file (price never entered)
    is skipped from the totals rather than silently treated as free, and
    `priced_checkout_count`/`unpriced_checkout_count` tell the caller how
    much of the picture that gap covers.

    Grouped two ways over the same underlying checkouts: by
    `AssetType.category` (falls back to "Uncategorized") and by the
    department of whoever the item was checked out to (a linked User's
    `department`, or "External / Ad-Hoc" for an Outsider).
    """
    start_dt, end_dt = _date_range_bounds(start_date, end_date)

    query = db.query(models.AssetCheckout)
    if start_dt is not None:
        query = query.filter(models.AssetCheckout.checkout_date >= start_dt)
    if end_dt is not None:
        query = query.filter(models.AssetCheckout.checkout_date < end_dt)
    checkouts = query.all()

    by_category: dict[str, dict] = {}
    by_department: dict[str, dict] = {}
    priced_count = 0
    unpriced_count = 0

    for c in checkouts:
        if c.is_outsourced:
            unit_price = c.outsourced_unit_price
            category = "Outsourced"
        else:
            unit_price = c.asset.price if c.asset else None
            category = (c.asset.category if c.asset and c.asset.category else "Uncategorized")

        if unit_price is None:
            unpriced_count += 1
            continue
        priced_count += 1
        line_total = float(unit_price) * c.quantity

        cat_bucket = by_category.setdefault(category, {"total_spend": 0.0, "item_count": 0})
        cat_bucket["total_spend"] += line_total
        cat_bucket["item_count"] += c.quantity

        if c.user is not None:
            dept = c.user.department or "Unassigned"
        elif c.outsider is not None:
            dept = "External / Ad-Hoc"
        else:
            dept = "Unassigned"
        dept_bucket = by_department.setdefault(dept, {"total_spend": 0.0, "item_count": 0})
        dept_bucket["total_spend"] += line_total
        dept_bucket["item_count"] += c.quantity

    return {
        "by_category": sorted(
            [{"category": k, **v, "total_spend": round(v["total_spend"], 2)} for k, v in by_category.items()],
            key=lambda r: -r["total_spend"],
        ),
        "by_department": sorted(
            [{"department": k, **v, "total_spend": round(v["total_spend"], 2)} for k, v in by_department.items()],
            key=lambda r: -r["total_spend"],
        ),
        "priced_checkout_count": priced_count,
        "unpriced_checkout_count": unpriced_count,
    }


# ---------------------------------------------------------------------------
# 4. QUOTATION APPROVAL TURNAROUND TIME
# ---------------------------------------------------------------------------
def get_quotation_turnaround(
    db: Session,
    start_date: "Optional[datetime.date]" = None,
    end_date: "Optional[datetime.date]" = None,
) -> dict:
    """
    How long a Quotation actually takes to move through its own lifecycle
    (see models.Quotation's docstring: draft -> submitted -> approved ->
    fulfilled), averaged in hours over every quotation whose
    `submitted_at` falls in the requested window (default: all-time).

    Three separate averages are reported since a quote can stall at either
    hand-off independently:
      - submit_to_approve : `submitted_at` -> `approved_at`
      - approve_to_fulfill: `approved_at`  -> `fulfilled_at`
      - submit_to_fulfill : `submitted_at` -> `fulfilled_at` (the
                             end-to-end figure -- only populated once both
                             steps have happened)
    Each average is computed only over quotations that have actually
    reached the relevant later stage -- a quote still sitting in
    "submitted" contributes to nothing here yet (it isn't stalled by
    definition, it just hasn't been decided), so a slow-moving queue full
    of still-pending quotes won't silently make this number look GOOD by
    omission; it will simply not be counted until it resolves one way or
    another. `sample_size_*` reports exactly how many quotations backed
    each average so a very small denominator is visible rather than
    hidden behind one confident-looking hour figure.
    """
    start_dt, end_dt = _date_range_bounds(start_date, end_date)

    query = db.query(models.Quotation).filter(models.Quotation.submitted_at.isnot(None))
    if start_dt is not None:
        query = query.filter(models.Quotation.submitted_at >= start_dt)
    if end_dt is not None:
        query = query.filter(models.Quotation.submitted_at < end_dt)
    quotations = query.all()

    def _hours(a: "Optional[datetime.datetime]", b: "Optional[datetime.datetime]") -> "Optional[float]":
        a, b = _as_aware_utc(a), _as_aware_utc(b)
        if a is None or b is None:
            return None
        return max((b - a).total_seconds() / 3600.0, 0.0)

    submit_to_approve = []
    approve_to_fulfill = []
    submit_to_fulfill = []
    by_month: dict[str, list] = {}

    for q in quotations:
        s2a = _hours(q.submitted_at, q.approved_at)
        a2f = _hours(q.approved_at, q.fulfilled_at)
        s2f = _hours(q.submitted_at, q.fulfilled_at)
        if s2a is not None:
            submit_to_approve.append(s2a)
        if a2f is not None:
            approve_to_fulfill.append(a2f)
        if s2f is not None:
            submit_to_fulfill.append(s2f)
            key = _month_key(_as_aware_utc(q.submitted_at))
            by_month.setdefault(key, []).append(s2f)

    def _avg(values: list) -> "Optional[float]":
        return round(sum(values) / len(values), 2) if values else None

    monthly = sorted(
        (
            {"month": k, "label": _month_label(k), "avg_submit_to_fulfill_hours": _avg(v), "sample_size": len(v)}
            for k, v in by_month.items()
        ),
        key=lambda r: r["month"],
    )

    return {
        "avg_submit_to_approve_hours": _avg(submit_to_approve),
        "sample_size_submit_to_approve": len(submit_to_approve),
        "avg_approve_to_fulfill_hours": _avg(approve_to_fulfill),
        "sample_size_approve_to_fulfill": len(approve_to_fulfill),
        "avg_submit_to_fulfill_hours": _avg(submit_to_fulfill),
        "sample_size_submit_to_fulfill": len(submit_to_fulfill),
        "total_quotations_submitted": len(quotations),
        "by_month": monthly,
    }


# ---------------------------------------------------------------------------
# COMBINED DASHBOARD
# ---------------------------------------------------------------------------
def get_dashboard(
    db: Session,
    start_date: "Optional[datetime.date]" = None,
    end_date: "Optional[datetime.date]" = None,
    category: "Optional[str]" = None,
) -> dict:
    """
    Single-call bundle of all four sections for the Reporting dashboard's
    initial page load -- avoids the frontend firing four separate requests
    (and four separate loading spinners) for what is, in practice, always
    viewed together. Each section is also individually reachable at its
    own endpoint (see api/reports_api.py) for a caller that only wants one
    slice, or that wants to poll one section on its own filter/refresh
    cadence.
    """
    return {
        "period": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "utilization_by_asset_type": get_utilization_by_asset_type(db, start_date, end_date, category),
        "overdue": get_overdue_trend(db, start_date, end_date),
        "spend": get_spend_breakdown(db, start_date, end_date),
        "quotation_turnaround": get_quotation_turnaround(db, start_date, end_date),
    }
