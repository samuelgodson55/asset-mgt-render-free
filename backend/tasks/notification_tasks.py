"""
tasks/notification_tasks.py
------------------------------
`send_overdue_notifications` emails people about overdue checkouts -- the
email counterpart to the "Overdue Checkouts" dashboard banner (see
services/checkout_service.py's list_overdue_checkouts(), which already
powers that banner and is reused here for the exact same "what counts as
overdue" logic).

Two kinds of email go out each run:
  1. One reminder to each overdue checkout's own holder, if they're a
     logged-in User with an email address (Outsiders have no account/inbox
     this app knows how to reach -- their `contact_details` field isn't
     necessarily an email at all, so this deliberately does not try to
     guess-parse it).
  2. One combined system-wide summary digest to every Manager AND every
     Admin/Super Admin + any extra addresses in ADMIN_NOTIFICATION_EMAILS
     (Managers no longer have department-scoping anywhere in this app, so
     they see the exact same full list Admins do).

This used to run inside a separate Celery `worker` container, on a
schedule driven by Celery Beat. Both are gone now -- see backend/jobs.py
and backend/scheduler.py's module docstrings for why (Celery+Redis/a
separate worker/cron process doesn't fit Render's, or most platforms',
free tier). `scheduler.py` calls the plain function below periodically
from a background thread inside the SAME process as the API, and
`api/system.py` exposes a manual-trigger endpoint for use with an
external scheduler as a more reliable alternative. Like the old worker,
this opens its own standalone DB session rather than reusing any
request-scoped one.
"""

import logging

import models
from database import SessionLocal
from models import utc_now
import services.notification_service as notification_service
from config import settings

logger = logging.getLogger(__name__)


def send_email_task(to, subject: str, body: str) -> dict:
    """
    Thin wrapper around notification_service.send_email() so it can run on
    the background thread pool (see jobs.run_async) instead of inline in
    an API request/response cycle -- see
    services/extension_service.py's create_extension_request() and
    decide_extension_request(), which must NOT block their HTTP response
    on an SMTP round-trip. `smtplib.SMTP(..., timeout=10)` (see
    notification_service.py) means a slow/unreachable mail server could
    otherwise hold the request open for up to ~10+ seconds -- long enough
    that both the staff/customer "Request Extension" modal AND the
    Manager/Admin "Extension Requests" panel would visibly hang before
    clearing.
    """
    sent = notification_service.send_email(to=to, subject=subject, body=body)
    return {"sent": sent}


def _overdue_query(db):
    return db.query(models.AssetCheckout).filter(
        models.AssetCheckout.status == "active",
        models.AssetCheckout.due_date.isnot(None),
        models.AssetCheckout.due_date < utc_now(),
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


def send_overdue_notifications() -> dict:
    """
    Sends the individual holder reminders + Manager/Admin digests described
    in this module's docstring. Returns a small summary dict (mostly useful
    for confirming the run worked during manual testing, or as the JSON
    body of the manual-trigger endpoint in api/system.py).
    """
    db = SessionLocal()
    sent_individual = 0
    sent_digests = 0
    try:
        overdue = _overdue_query(db).all()
        if not overdue:
            logger.info("send_overdue_notifications: no overdue checkouts -- nothing to send.")
            return {"overdue_count": 0, "individual_emails_sent": 0, "digest_emails_sent": 0}

        # --- 1. Individual reminders to each overdue item's own holder ---
        for c in overdue:
            if c.user and c.user.email and c.user.is_active and not c.user.is_deleted:
                asset_name = c.asset.name if c.asset else "Unknown Asset"
                days_overdue = (utc_now() - c.due_date).days
                ok = notification_service.send_email(
                    to=c.user.email,
                    subject=f"[Snipe-IT Lite] Overdue: {asset_name}",
                    body=(
                        f"'{asset_name}' was due back on {c.due_date.strftime('%Y-%m-%d')} "
                        f"({days_overdue} day{'s' if days_overdue != 1 else ''} overdue).\n\n"
                        "Please return it, or request an extension from your dashboard if you need more time."
                    ),
                )
                if ok:
                    sent_individual += 1

        # --- 2. Manager + Admin system-wide digest (Managers no longer have
        #        department-scoping, so they get the exact same full list as
        #        Admins) + any extra configured addresses ---
        digest_emails = [
            u.email for u in db.query(models.User).filter(
                models.User.role.in_(("admin", "manager")),
                models.User.is_active.is_(True), models.User.is_deleted.is_(False),
            ).all()
        ]
        digest_emails.extend(settings.admin_notification_email_list)
        digest_emails = list(dict.fromkeys(digest_emails))
        if digest_emails:
            lines = "\n".join(_format_line(c) for c in overdue)
            ok = notification_service.send_email(
                to=digest_emails,
                subject=f"[Snipe-IT Lite] {len(overdue)} overdue checkout(s) system-wide",
                body=f"The following checkouts are overdue:\n\n{lines}",
            )
            if ok:
                sent_digests += 1

        logger.info(
            "send_overdue_notifications: done", extra={
                "overdue_count": len(overdue), "individual_emails_sent": sent_individual, "digest_emails_sent": sent_digests,
            },
        )
        return {"overdue_count": len(overdue), "individual_emails_sent": sent_individual, "digest_emails_sent": sent_digests}
    finally:
        db.close()
