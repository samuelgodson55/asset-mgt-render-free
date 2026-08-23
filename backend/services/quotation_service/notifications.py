"""
services/quotation/notifications.py
--------------------------------------
The in-app QuotationNotification feed: writing one whenever a mutation
should alert the quote's current owner (_notify_quotation_recipient,
called by admin.py alongside the matching AuditLog row), and the two
read-side endpoints -- listing a caller's own notifications and marking
them read.
"""

from __future__ import annotations

import models
from config import settings
from sqlalchemy.orm import Session, joinedload
import services.notification_service as notification_service

from .constants import QUOTATION_NOTIFICATIONS_MAX_ROWS
from .helpers import _display_reference


# -----------------------------------------------------------------------------
# IN-APP NOTIFICATIONS (Quotation assigned / changed) -- see models.py's
# QuotationNotification docstring for the full "why a separate table"
# rationale. This is the ONE place that writes to that table; every
# admin-facing mutation below that should notify someone calls this
# helper right alongside the AuditLog row it already writes for the same
# action, rather than each mutation reimplementing "who's the recipient,
# and should they be notified" on its own.
# -----------------------------------------------------------------------------
def _notify_quotation_recipient(db: Session, quotation: "models.Quotation", actor: dict, kind: str, message: str) -> None:
    """
    Creates an in-app QuotationNotification (and best-effort emails it --
    see services/notification_service.py's send_email(), which is
    fail-soft and never raises) for whoever a Quotation currently belongs
    to, IF that's a real linked user other than the person making the
    change right now.

    Recipient resolution mirrors bulk_checkout_quotation()'s own "who
    does this quote belong to" logic: `assigned_to_id` when the quote has
    been explicitly assigned to a linked user, else falling back to
    `user_id` (the original requester) for a personal self-submitted
    request. Deliberately silent (no-op) when:
      - the quote is assigned to an Ad-Hoc/unlinked Outsider instead
        (`assigned_outsider_id`) -- no login, nothing to notify in-app,
      - there's no resolved recipient at all (an admin-created quote
        that's still fully unassigned), or
      - the resolved recipient IS the actor -- an Admin/Manager
        assigning or editing their own quote shouldn't notify themselves.
    """
    if quotation.assigned_to_id:
        recipient_id = quotation.assigned_to_id
    elif not quotation.assigned_outsider_id:
        recipient_id = quotation.user_id
    else:
        return  # Assigned to an Ad-Hoc Outsider -- no in-app recipient.

    if not recipient_id or recipient_id == int(actor["sub"]):
        return

    recipient = db.query(models.User).filter(models.User.id == recipient_id, ~models.User.is_deleted).first()
    if not recipient:
        return

    db.add(models.QuotationNotification(
        quotation_id=quotation.id, recipient_user_id=recipient.id,
        kind=kind, message=message, created_by=actor.get("email"),
    ))
    # Best-effort email alongside the in-app row above -- gated by its own
    # SEND_QUOTATION_RECIPIENT_EMAILS switch (see config.py's docstring on
    # that setting) on top of the master NOTIFICATIONS_ENABLED one, since
    # some customers find an email for every single line-item tweak
    # intrusive. The in-app QuotationNotification row above is NEVER
    # gated by this -- it's always created either way, so nothing is lost
    # for someone who just doesn't want the email; they still see it next
    # time they open the Notification Center. send_email() itself already
    # no-ops quietly when NOTIFICATIONS_ENABLED is off or the recipient
    # has no usable address, so no extra guard is needed for that part.
    # `_display_reference()` (not the raw column) since this can fire for
    # a still-draft quote assigned before Submit -- see `_display_reference()`'s
    # own docstring.
    if settings.SEND_QUOTATION_RECIPIENT_EMAILS:
        notification_service.enqueue_email_after_commit(
            db=db,
            to=recipient.email,
            subject=f"Quotation {_display_reference(quotation)}",
            body=message,
        )

def list_my_quotation_notifications(db: Session, user: dict, limit: int = 20) -> dict:
    """Every QuotationNotification addressed to the caller, newest first --
    read and unread alike (the frontend distinguishes via `read_at`,
    same as it already does for the bell's unread badge count)."""
    rows = (
        db.query(models.QuotationNotification)
        .options(joinedload(models.QuotationNotification.quotation))
        .filter(models.QuotationNotification.recipient_user_id == int(user["sub"]))
        .order_by(models.QuotationNotification.created_at.desc())
        .limit(min(limit, QUOTATION_NOTIFICATIONS_MAX_ROWS))
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "quotation_id": r.quotation_id,
                # `_display_reference()` (not the raw column) since a
                # notification can be for a still-draft quote assigned
                # before Submit (see assign_quotation()'s `is_own_draft`
                # case) -- the raw `reference_number` column is null
                # until Submit, which used to surface here as a literal
                # "None" instead of the eventual "QT-00000N".
                "reference_number": _display_reference(r.quotation) if r.quotation else None,
                "kind": r.kind,
                "message": r.message,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat(),
                "read_at": r.read_at.isoformat() if r.read_at else None,
            }
            for r in rows
        ],
    }

def mark_quotation_notifications_read(db: Session, user: dict, notification_ids: list[int]) -> dict:
    """Stamps `read_at` on the given notifications -- ONLY ones addressed
    to the caller; any id belonging to someone else is silently ignored
    rather than erroring, since a stale/tampered id list here can't leak
    or alter anyone else's data either way (see the `.filter()` below)."""
    if not notification_ids:
        return {"updated": 0}
    rows = (
        db.query(models.QuotationNotification)
        .filter(
            models.QuotationNotification.id.in_(notification_ids),
            models.QuotationNotification.recipient_user_id == int(user["sub"]),
            models.QuotationNotification.read_at.is_(None),
        )
        .all()
    )
    now = models.utc_now()
    for row in rows:
        row.read_at = now
    db.commit()
    return {"updated": len(rows)}
