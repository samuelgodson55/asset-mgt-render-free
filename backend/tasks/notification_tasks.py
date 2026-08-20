"""
tasks/notification_tasks.py
------------------------------
Celery Beat runs two related jobs on a schedule (see celery_app.py's
`beat_schedule`):
  - `send_overdue_notifications` -- emails people about checkouts that are
    ALREADY overdue. The email counterpart to the "Overdue Checkouts"
    dashboard banner (see services/checkout_service.py's
    list_overdue_checkouts(), which already powers that banner and is
    reused here for the exact same "what counts as overdue" logic).
  - `send_due_soon_reminders` -- the proactive counterpart: emails people
    BEFORE a checkout goes overdue, for anything due within
    settings.DUE_SOON_REMINDER_DAYS. The email counterpart to the "Due
    Soon" dashboard banner (see services/checkout_service.py's
    list_due_soon_checkouts()).

Both send the same two kinds of email each run:
  1. One reminder to each affected checkout's own holder, if they're a
     logged-in User with an email address (Outsiders have no account/inbox
     this app knows how to reach -- their `email`/`phone_number` fields are
     both optional and neither is guaranteed to be present, so this
     deliberately does not attempt to notify them).
  2. One combined system-wide summary digest to the admin-configured
     "Digest Recipients" list (see services/notification_service.py's
     get_digest_recipient_emails(), editable at runtime via PUT
     /settings/digest-recipients -- Admin/Super Admin only) PLUS any extra
     addresses in the env-configured ADMIN_NOTIFICATION_EMAILS. This is
     DELIBERATELY NOT "every Manager/Admin account" any more -- being a
     Manager/Admin no longer implies receiving the daily digest; being on
     the configured list does. If that list (and
     ADMIN_NOTIFICATION_EMAILS) is empty, no digest is sent at all, same
     as if there were nothing overdue/due-soon to report.

Runs in the `worker` container/process (see celery_app.py's module
docstring for the split between the API's producer role and this
consumer role) -- like tasks/export_tasks.py, it opens its own standalone
DB session rather than reusing any request-scoped one.
"""

import datetime
import logging
import math

import models
from celery_app import celery_app
from database import SessionLocal
from db_admission import background_db_slot
from models import utc_now
import services.notification_service as notification_service
from services.notification_service import get_digest_recipient_emails
from config import settings

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# GENERIC "SEND ONE EMAIL" TASK
# -----------------------------------------------------------------------------
# Used by any request/response code path that needs to notify someone but
# must NOT block the HTTP response on an SMTP round-trip to do it -- see
# services/extension_service.py's create_extension_request() and
# decide_extension_request(), which used to call
# notification_service.send_email() directly, inline, before returning a
# response. `smtplib.SMTP(..., timeout=10)` (see notification_service.py)
# means a slow/unreachable mail server could hold that request/response
# open for up to ~10+ seconds -- long enough that both the staff/customer
# "Request Extension" modal AND the Manager/Admin "Extension Requests"
# panel visibly hung before clearing. Enqueuing here instead means the API
# commits the DB change and returns immediately; the email itself goes out
# a moment later, out-of-band, in the `worker` container -- exactly the
# same "producer here, consumer there" split celery_app.py already uses
# for tasks.generate_audit_export.
@celery_app.task(name="tasks.send_email_task", bind=True)
def send_email_task(self, to, subject: str, body: str) -> dict:
    """Thin wrapper around notification_service.send_email() so it can run
    on the Celery worker instead of inline in an API request. `to` is
    JSON-safe (a string or a list of strings) -- see celery_app.py's
    `task_serializer="json"` note."""
    sent = notification_service.send_email(to=to, subject=subject, body=body)
    return {"sent": sent}


def _overdue_query(db):
    return db.query(models.AssetCheckout).filter(
        models.AssetCheckout.status == "active",
        models.AssetCheckout.due_date.isnot(None),
        models.AssetCheckout.due_date < utc_now(),
    )


def _due_soon_query(db):
    now = utc_now()
    horizon = now + datetime.timedelta(days=settings.DUE_SOON_REMINDER_DAYS)
    return db.query(models.AssetCheckout).filter(
        models.AssetCheckout.status == "active",
        models.AssetCheckout.due_date.isnot(None),
        models.AssetCheckout.due_date >= now,
        models.AssetCheckout.due_date <= horizon,
    )


def _format_line(c) -> str:
    if c.user:
        holder = c.user.name
    elif c.outsider:
        holder = f"{c.outsider.name} (Ad-Hoc)"
    else:
        holder = "Unknown holder"
    asset_name = c.asset.name if c.asset else "Unknown Asset"
    return f"  - {asset_name} · {holder} · due {c.due_date.strftime('%Y-%m-%d')}"


@celery_app.task(name="tasks.send_overdue_notifications", bind=True, max_retries=5)
def send_overdue_notifications(self) -> dict:
    """Snapshot DB state quickly, close the session, then perform email I/O."""
    sent_individual = 0
    sent_digests = 0
    try:
        with background_db_slot():
            db = SessionLocal()
            try:
                overdue = _overdue_query(db).all()
                if not overdue:
                    logger.info("send_overdue_notifications: no overdue checkouts -- nothing to send.")
                    return {"overdue_count": 0, "individual_emails_sent": 0, "digest_emails_sent": 0}

                individual_messages = []
                if settings.SEND_INDIVIDUAL_HOLDER_REMINDERS:
                    for c in overdue:
                        if c.user and c.user.email and c.user.is_active and not c.user.is_deleted:
                            asset_name = c.asset.name if c.asset else "Unknown Asset"
                            days_overdue = (utc_now() - c.due_date).days
                            individual_messages.append({
                                "to": c.user.email,
                                "subject": f"[{settings.SITE_NAME}] Overdue: {asset_name}",
                                "body": (
                                    f"'{asset_name}' was due back on {c.due_date.strftime('%Y-%m-%d')} "
                                    f"({days_overdue} day{'s' if days_overdue != 1 else ''} overdue).\n\n"
                                    "Please return it, or request an extension from your dashboard if you need more time."
                                ),
                            })

                digest_emails = get_digest_recipient_emails(db)
                digest_emails.extend(settings.admin_notification_email_list)
                digest_emails = list(dict.fromkeys(digest_emails))
                lines = "\n".join(_format_line(c) for c in overdue)
                digest = None
                if digest_emails:
                    digest = {
                        "to": digest_emails,
                        "subject": f"[{settings.SITE_NAME}] {len(overdue)} overdue checkout(s) system-wide",
                        "body": f"The following checkouts are overdue:\n\n{lines}",
                    }
                overdue_count = len(overdue)
            finally:
                db.close()
    except RuntimeError as exc:
        logger.info("send_overdue_notifications: background DB capacity busy; retrying")
        raise self.retry(exc=exc, countdown=2)

    for message in individual_messages:
        if notification_service.send_email(**message):
            sent_individual += 1
    if digest and notification_service.send_email(**digest):
        sent_digests = 1

    logger.info("send_overdue_notifications: done", extra={
        "overdue_count": overdue_count,
        "individual_emails_sent": sent_individual,
        "digest_emails_sent": sent_digests,
    })
    return {
        "overdue_count": overdue_count,
        "individual_emails_sent": sent_individual,
        "digest_emails_sent": sent_digests,
    }


@celery_app.task(name="tasks.send_due_soon_reminders", bind=True, max_retries=5)
def send_due_soon_reminders(self) -> dict:
    """Snapshot DB state quickly, close the session, then perform email I/O."""
    sent_individual = 0
    sent_digests = 0
    try:
        with background_db_slot():
            db = SessionLocal()
            try:
                due_soon = _due_soon_query(db).all()
                if not due_soon:
                    logger.info("send_due_soon_reminders: nothing due soon -- nothing to send.")
                    return {"due_soon_count": 0, "individual_emails_sent": 0, "digest_emails_sent": 0}

                individual_messages = []
                if settings.SEND_INDIVIDUAL_HOLDER_REMINDERS:
                    for c in due_soon:
                        if c.user and c.user.email and c.user.is_active and not c.user.is_deleted:
                            asset_name = c.asset.name if c.asset else "Unknown Asset"
                            days_until_due = max(1, math.ceil((c.due_date - utc_now()).total_seconds() / 86400))
                            individual_messages.append({
                                "to": c.user.email,
                                "subject": f"[{settings.SITE_NAME}] Due soon: {asset_name}",
                                "body": (
                                    f"'{asset_name}' is due back on {c.due_date.strftime('%Y-%m-%d')} "
                                    f"(in {days_until_due} day{'s' if days_until_due != 1 else ''}).\n\n"
                                    "Please return it on time, or request an extension from your dashboard if you need more time."
                                ),
                            })

                digest_emails = get_digest_recipient_emails(db)
                digest_emails.extend(settings.admin_notification_email_list)
                digest_emails = list(dict.fromkeys(digest_emails))
                lines = "\n".join(_format_line(c) for c in due_soon)
                digest = None
                if digest_emails:
                    digest = {
                        "to": digest_emails,
                        "subject": f"[{settings.SITE_NAME}] {len(due_soon)} checkout(s) due soon system-wide",
                        "body": f"The following checkouts are due within {settings.DUE_SOON_REMINDER_DAYS} day(s):\n\n{lines}",
                    }
                due_soon_count = len(due_soon)
            finally:
                db.close()
    except RuntimeError as exc:
        logger.info("send_due_soon_reminders: background DB capacity busy; retrying")
        raise self.retry(exc=exc, countdown=2)

    for message in individual_messages:
        if notification_service.send_email(**message):
            sent_individual += 1
    if digest and notification_service.send_email(**digest):
        sent_digests = 1

    logger.info("send_due_soon_reminders: done", extra={
        "due_soon_count": due_soon_count,
        "individual_emails_sent": sent_individual,
        "digest_emails_sent": sent_digests,
    })
    return {
        "due_soon_count": due_soon_count,
        "individual_emails_sent": sent_individual,
        "digest_emails_sent": sent_digests,
    }

