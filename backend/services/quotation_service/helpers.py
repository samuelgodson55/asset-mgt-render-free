"""
services/quotation/helpers.py
--------------------------------
Small, pure formatting/derivation helpers shared across the quotation
package: money rounding, rental-day math, reference numbers, and the
compact user/outsider "brief" dicts used throughout serialization.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import models

from .constants import TWO_PLACES


def _money(value: Decimal) -> float:
    return float(value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))

def _rental_days(start_date: datetime.date, due_date: datetime.date) -> int:
    # Inclusive calendar-day count: a same-day booking (start == due) is 1
    # day, start->start+1 is 2 days, start->start+2 is 3 days, etc. -- i.e.
    # the number of calendar days the booking SPANS, not the raw delta
    # between the two dates. `max(1, ...)` stays as a safety net in case
    # due_date < start_date ever slips through (should already be blocked
    # by validation elsewhere).
    return max(1, (due_date - start_date).days + 1)

def _inventory_status_label(available_quantity: int) -> str:
    """Mirrors asset_service._inventory_status_label()'s thresholds -- kept
    as its own tiny copy here rather than importing across service modules
    for what's a one-line, unlikely-to-drift rule."""
    return "Critical Low Stock" if available_quantity <= 3 else "In Stock"

def _reference_number(quotation_id: int) -> str:
    """The human-shareable Quotation ID -- derived straight from the
    (already-unique) primary key rather than a separate counter, so it's
    guaranteed unique for free and never needs its own sequence table."""
    return f"QT-{quotation_id:06d}"

def _display_reference(quotation: "models.Quotation") -> str:
    """Same human-shareable "QT-000001" shape as `_reference_number()`,
    but safe to call on a still-draft Quotation whose `reference_number`
    column hasn't been persisted yet (that only happens at Submit -- see
    submit_my_quotation()/admin_create_quotation()). `_reference_number()`
    is purely a deterministic function of the id, so it's always safe to
    recompute rather than fall back to the literal string "None" -- which
    is what an f-string over the raw (still-null) column produces.
    Needed because assign_quotation() is deliberately allowed to run on
    an Admin/Manager's own still-draft cart (see its `is_own_draft` case)
    and messages/audit details built at that point would otherwise read
    "Quotation None was assigned to you..."."""
    return quotation.reference_number or _reference_number(quotation.id)

def _user_brief(user: Optional["models.User"]) -> Optional[dict]:
    if user is None:
        return None
    return {"id": user.id, "name": user.name, "email": user.email}

def _outsider_brief(outsider: Optional["models.Outsider"]) -> Optional[dict]:
    if outsider is None:
        return None
    return {
        "id": outsider.id, "name": outsider.name, "company": outsider.company,
        "email": outsider.email, "phone_number": outsider.phone_number, "is_outsider": True,
    }
