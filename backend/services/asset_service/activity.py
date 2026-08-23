"""
services/asset/activity.py
------------------------------
Daily checkout/return activity feeding the Dashboard's activity chart.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

import models
from models import utc_now

from .helpers import _can_see_custody


def get_activity(db: Session, user: dict, days: int = 14) -> list[dict]:
    """
    Daily checkout/return counts for the last `days` days -- feeds the
    Dashboard's "Checkout activity" chart (frontend-app/src/pages/
    Dashboard.tsx). This has no legacy equivalent (the vanilla-JS frontend
    never had a real endpoint behind that chart either -- see
    frontend-app/src/lib/api.ts's loadStats(), which previously always
    returned `activity: []`); it's a genuinely new endpoint, not a port.

    "Checkouts" counts every AssetCheckout row whose checkout_date falls on
    a given day, regardless of current status. "Returns" counts every row
    whose returned_at falls on a given day -- returned_at is only stamped
    once a checkout is FULLY settled (see services/checkout_service.py's
    return_checkout()), so a day's "returns" reflects checkouts that
    finished closing out that day, not every partial-return event against
    them; there's no per-partial-return timestamp to aggregate on.

    SCOPE (mirrors _can_see_custody above / deps.require_privileged_role):
    a Super Admin/Admin/Manager sees org-wide activity across every
    checkout, the same "whole ledger" view the rest of the Overview
    dashboard (Total Pooled Units, Overdue Returns, etc.) already gives
    them. A Staff/Customer is not privileged to see who-has-what org-wide
    (see _can_see_custody's docstring), so their chart is narrowed to only
    checkouts made against their own account (`user_id`) -- matching what
    "My Items" already shows them elsewhere in the app. Outsourced-to-an-
    outsider checkouts (`user_id IS NULL`, see AssetCheckout's docstring)
    never belong to any Staff/Customer, so they're naturally excluded from
    the narrowed view along with everyone else's checkouts.

    Aggregated in Python (not a SQL date_trunc/group-by) to sidestep
    timezone-bucketing edge cases across the checkout_date/returned_at
    columns -- the row counts here are small enough (bounded by `days`,
    capped below) that this comfortably avoids a second, more fragile
    piece of SQL to maintain.
    """
    days = max(1, min(days, 90))
    since = utc_now() - datetime.timedelta(days=days - 1)
    since_day = since.date()

    checkout_query = db.query(models.AssetCheckout.checkout_date).filter(
        models.AssetCheckout.checkout_date >= since
    )
    return_query = db.query(models.AssetCheckout.returned_at).filter(
        models.AssetCheckout.returned_at.isnot(None), models.AssetCheckout.returned_at >= since
    )
    if not _can_see_custody(user):
        checkout_query = checkout_query.filter(models.AssetCheckout.user_id == int(user["sub"]))
        return_query = return_query.filter(models.AssetCheckout.user_id == int(user["sub"]))

    checkout_days = checkout_query.all()
    return_days = return_query.all()

    checkouts_by_day: dict[str, int] = {}
    for (dt,) in checkout_days:
        key = dt.date().isoformat()
        checkouts_by_day[key] = checkouts_by_day.get(key, 0) + 1

    returns_by_day: dict[str, int] = {}
    for (dt,) in return_days:
        key = dt.date().isoformat()
        returns_by_day[key] = returns_by_day.get(key, 0) + 1

    return [
        {
            "date": (since_day + datetime.timedelta(days=i)).isoformat(),
            "checkouts": checkouts_by_day.get((since_day + datetime.timedelta(days=i)).isoformat(), 0),
            "returns": returns_by_day.get((since_day + datetime.timedelta(days=i)).isoformat(), 0),
        }
        for i in range(days)
    ]
