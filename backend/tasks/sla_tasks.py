"""
tasks/sla_tasks.py
--------------------
Celery Beat runs two related jobs on a schedule (see celery_app.py's
`beat_schedule`, `settings.APPROVAL_SLA_CHECK_INTERVAL_MINUTES`) to close a
quiet gap in two of this app's approval queues:

  - `ExtensionRequest` (see models.py's own docstring): "pending" ->
    approved/denied. Only a Manager/Admin/Super Admin can decide one (see
    services/extension_service.py's decide_extension_request()), and
    nothing previously reminded them if they never did.
  - `Quotation` (see models.py's own docstring): "submitted" -> approved.
    Only a Manager/Admin can move it forward (see
    services/quotation_service.py's approve_quotation()), and likewise
    nothing previously nudged anyone if a submitted quote just sat there.

Both are a genuine "pending" state with NO automatic escalation of their
own -- a request/quote can sit unanswered indefinitely unless someone
happens to notice it in the Extension Requests panel or the Quotes tab.
`escalate_pending_extension_requests`/`escalate_pending_quotations` below
fix that the same way tasks/notification_tasks.py's overdue/due-soon
digests already work: anything that's crossed its configured SLA
threshold (`settings.EXTENSION_REQUEST_SLA_HOURS`/
`settings.QUOTATION_SLA_HOURS`) gets rolled into ONE combined digest email
sent to the exact same notification-recipients audience every other alert
in this app uses -- the runtime-editable Digest Recipients list (PUT
/settings/digest-recipients) plus `settings.ADMIN_NOTIFICATION_EMAILS` --
via `_notification_recipients()` below. If that combined list is empty,
nothing is sent, same as every other digest in this app when there's
nobody configured to receive it.

WHY A COLUMN (`sla_last_reminded_at`) INSTEAD OF JUST RE-CHECKING AGE
EVERY RUN
-----------------------------------------------------------------------
Without it, a request/quote that's been pending for a week would get
re-escalated on EVERY `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` tick forever
(by default, once an hour) -- noisy enough that people would learn to
ignore the alert entirely, which defeats the point. Each model's own
`sla_last_reminded_at` column (see models.py's docstrings for both) records
the last time THIS row was escalated, so `_due_for_escalation()` below
only re-fires once `settings.APPROVAL_SLA_ESCALATION_REPEAT_HOURS` has
passed since that last nudge -- repeatedly enough that a long-neglected
item keeps surfacing, not so often that it's spam.

Runs in the `worker` container/process, same split as
tasks/notification_tasks.py -- opens its own standalone DB session rather
than reusing any request-scoped one.
"""

import datetime
import logging

import models
from celery_app import celery_app
from database import SessionLocal
from models import utc_now
import services.notification_service as notification_service
from services.notification_service import get_digest_recipient_emails
from config import settings

logger = logging.getLogger(__name__)


def _as_aware_utc(value: "datetime.datetime | None") -> "datetime.datetime | None":
    """
    Same SQLite-vs-Postgres tz normalization as models._as_aware_utc /
    services/reports_service.py's / services/checkout_service.py's own
    copies (see models.py's docstring for the full "why"): every
    DateTime(timezone=True) column round-trips back tz-AWARE on Postgres
    (every real deployment) but silently comes back tz-NAIVE on SQLite
    (the test-suite's database -- see tests/conftest.py). Comparing one of
    those naive values against `utc_now()`'s aware value below raises
    `TypeError: can't subtract offset-naive and offset-aware datetimes` on
    SQLite specifically. Kept as its own tiny copy here rather than
    imported, same "no cross-service coupling for a one-line rule"
    reasoning as `_notification_recipients()` below.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _notification_recipients(db) -> list[str]:
    """
    The SAME audience-resolution rule used by
    services/extension_service.py's own `_notification_recipients()` and
    tasks/notification_tasks.py's daily digests: the admin-configured
    Digest Recipients list + ADMIN_NOTIFICATION_EMAILS. Kept as its own
    tiny copy here (rather than importing extension_service's version)
    so this module only ever depends on notification_service/config --
    same "no cross-service coupling for a one-line rule" reasoning as
    services/quotation_service.py's own `_inventory_status_label()` copy.
    """
    recipients = get_digest_recipient_emails(db)
    recipients.extend(settings.admin_notification_email_list)
    return list(dict.fromkeys(recipients))  # de-dupe, preserve order


def _due_for_escalation(pending_since: datetime.datetime, last_reminded_at: datetime.datetime | None, sla_hours: float) -> bool:
    """
    True when a row has been sitting unanswered long enough to nudge:
    it must be past the SLA age, AND either never nudged before or past
    the repeat-escalation cooldown since the last nudge. Shared by both
    tasks below so the "nudge, then don't nag" rule can't drift between
    the two queues.
    """
    now = utc_now()
    pending_since = _as_aware_utc(pending_since)
    last_reminded_at = _as_aware_utc(last_reminded_at)
    if now - pending_since < datetime.timedelta(hours=sla_hours):
        return False
    if last_reminded_at is None:
        return True
    return now - last_reminded_at >= datetime.timedelta(hours=settings.APPROVAL_SLA_ESCALATION_REPEAT_HOURS)


def _hours_pending(pending_since: datetime.datetime) -> int:
    return max(0, int((utc_now() - _as_aware_utc(pending_since)).total_seconds() // 3600))


@celery_app.task(name="tasks.escalate_pending_extension_requests", bind=True)
def escalate_pending_extension_requests(self) -> dict:
    """
    Escalates every still-`pending` ExtensionRequest that's crossed
    `settings.EXTENSION_REQUEST_SLA_HOURS` with no Manager/Admin/Super
    Admin decision yet -- see this module's docstring for the full
    "why"/audience/repeat-cooldown rules. Returns a small summary dict,
    mirroring tasks/notification_tasks.py's own tasks (mostly useful for
    confirming the task ran during manual testing).
    """
    db = SessionLocal()
    try:
        candidates = (
            db.query(models.ExtensionRequest)
            .filter(models.ExtensionRequest.status == "pending")
            .order_by(models.ExtensionRequest.created_at.asc())
            .all()
        )
        due = [r for r in candidates if _due_for_escalation(r.created_at, r.sla_last_reminded_at, settings.EXTENSION_REQUEST_SLA_HOURS)]
        if not due:
            logger.info("escalate_pending_extension_requests: nothing past SLA -- nothing to send.")
            return {"escalated_count": 0, "digest_emails_sent": 0}

        recipients = _notification_recipients(db)
        digest_sent = 0
        if recipients:
            lines = []
            for r in due:
                checkout = r.checkout
                asset_name = models.checkout_display_name(checkout) if checkout else "Unknown Asset"
                lines.append(
                    f"  - {asset_name} · requested by {r.requested_by_label} · "
                    f"pending {_hours_pending(r.created_at)}h · "
                    f"requested new due date {r.requested_new_due_date.strftime('%Y-%m-%d')}"
                )
            ok = notification_service.send_email(
                to=recipients,
                subject=f"[{settings.SITE_NAME}] {len(due)} extension request(s) awaiting decision",
                body=(
                    f"The following extension requests have been pending for over "
                    f"{settings.EXTENSION_REQUEST_SLA_HOURS:g} hour(s) with no decision:\n\n"
                    + "\n".join(lines)
                    + "\n\nDecide them from the Extension Requests panel."
                ),
            )
            if ok:
                digest_sent = 1

        now = utc_now()
        for r in due:
            r.sla_last_reminded_at = now
        db.commit()

        logger.info(
            "escalate_pending_extension_requests: done",
            extra={"escalated_count": len(due), "digest_emails_sent": digest_sent},
        )
        return {"escalated_count": len(due), "digest_emails_sent": digest_sent}
    finally:
        db.close()


@celery_app.task(name="tasks.escalate_pending_quotations", bind=True)
def escalate_pending_quotations(self) -> dict:
    """
    Escalates every still-`submitted` Quotation that's crossed
    `settings.QUOTATION_SLA_HOURS` with no Admin/Manager decision
    (approve_quotation()) yet -- same shape/audience/repeat-cooldown
    rules as escalate_pending_extension_requests() above, just for the
    Quote-to-Checkout workflow instead of the due-date extension one.
    Returns a small summary dict for manual-testing confirmation.
    """
    db = SessionLocal()
    try:
        candidates = (
            db.query(models.Quotation)
            .filter(models.Quotation.status == "submitted")
            .order_by(models.Quotation.submitted_at.asc())
            .all()
        )
        due = [
            q for q in candidates
            if q.submitted_at is not None
            and _due_for_escalation(q.submitted_at, q.sla_last_reminded_at, settings.QUOTATION_SLA_HOURS)
        ]
        if not due:
            logger.info("escalate_pending_quotations: nothing past SLA -- nothing to send.")
            return {"escalated_count": 0, "digest_emails_sent": 0}

        recipients = _notification_recipients(db)
        digest_sent = 0
        if recipients:
            lines = []
            for q in due:
                requester = q.user.name if q.user else "Unknown requester"
                lines.append(
                    f"  - {q.reference_number} · requested by {requester} · "
                    f"pending {_hours_pending(q.submitted_at)}h"
                )
            ok = notification_service.send_email(
                to=recipients,
                subject=f"[{settings.SITE_NAME}] {len(due)} quotation(s) awaiting decision",
                body=(
                    f"The following quotations have been awaiting review/approval for over "
                    f"{settings.QUOTATION_SLA_HOURS:g} hour(s):\n\n"
                    + "\n".join(lines)
                    + "\n\nReview them from the Quotes tab."
                ),
            )
            if ok:
                digest_sent = 1

        now = utc_now()
        for q in due:
            q.sla_last_reminded_at = now
        db.commit()

        logger.info(
            "escalate_pending_quotations: done",
            extra={"escalated_count": len(due), "digest_emails_sent": digest_sent},
        )
        return {"escalated_count": len(due), "digest_emails_sent": digest_sent}
    finally:
        db.close()
