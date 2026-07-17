"""
services/notification_service.py
----------------------------------
The ONE place in this codebase that knows how to send an email. Every other
module (services/extension_service.py, tasks/notification_tasks.py) builds
a subject/body and calls `send_email()` here -- nothing else touches
`smtplib` directly.

WHY THIS IS DELIBERATELY "BORING" (plain SMTP, no vendor SDK)
---------------------------------------------------------------
This app is meant to be self-hostable with zero required third-party
accounts. Plain SMTP (Python's built-in `smtplib`) works unmodified against
a local Postfix/Exim relay, a self-hosted mail server, OR a hosted
provider's SMTP endpoint (SendGrid, Mailgun, AWS SES, etc. all expose one)
-- so there's no vendor lock-in and no extra dependency to install.

WHY EVERY CALL IS "FAIL-SOFT" (never raises)
-----------------------------------------------
A checkout being returned late, or a Manager approving an extension
request, is a real business action that already happened and was already
committed to the database by the time this module gets called -- an SMTP
server being down/misconfigured should never turn into a 500 error on an
otherwise-successful API request, and should never crash the nightly
Celery Beat notification task either (which would silently stop *every*
overdue reminder, not just fail to send one email). Every function here
catches its own exceptions, logs a clear warning, and returns a simple
True/False success flag instead.

WHY NOTIFICATIONS_ENABLED EXISTS AS ITS OWN FLAG (separate from "is
SMTP_HOST set")
-----------------------------------------------------------------------
Two different failure modes need two different messages: "notifications
are turned off on purpose" (NOTIFICATIONS_ENABLED=false, the default --
totally normal for local dev) vs. "notifications are turned ON but
SMTP_HOST is empty" (a real misconfiguration worth a louder log line, in
case someone flipped the switch and forgot the rest of the settings).
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable

from sqlalchemy.orm import Session

import models
from config import settings
from schemas.notifications import DigestRecipientsUpdateRequest

logger = logging.getLogger(__name__)

# Same generic key/value store used by services/quotation_service.py's
# VAT setting (models.AppSetting) -- runtime-editable by a Super
# Admin/Admin without a restart, unlike everything in config.py.
DIGEST_RECIPIENTS_SETTING_KEY = "digest_recipient_emails"


def send_email(to: Iterable[str] | str, subject: str, body: str) -> bool:
    """
    Sends one plain-text email to one or more recipients. Returns True if
    the message was handed off to the SMTP server successfully, False in
    every other case (notifications disabled, no recipients, misconfigured
    SMTP settings, or the send itself raising) -- callers should treat a
    False return as "logged, not fatal" rather than retrying inline.
    """
    recipients = [to] if isinstance(to, str) else [addr for addr in to if addr]
    recipients = [addr.strip() for addr in recipients if addr and addr.strip()]

    if not recipients:
        logger.debug("send_email: no valid recipients for subject %r -- skipping.", subject)
        return False

    if not settings.NOTIFICATIONS_ENABLED:
        # Normal, expected state for local dev (see config.py's
        # NOTIFICATIONS_ENABLED docstring) -- log what WOULD have been
        # sent at DEBUG level so a developer can still see the content
        # while working on this feature, without it flooding INFO/WARNING
        # logs in every environment that simply hasn't turned email on.
        logger.debug(
            "send_email: NOTIFICATIONS_ENABLED is false -- not sending. Subject=%r To=%s\n%s",
            subject, recipients, body,
        )
        return False

    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        logger.warning(
            "send_email: NOTIFICATIONS_ENABLED is true but SMTP_HOST/SMTP_FROM_EMAIL "
            "aren't both configured -- cannot send. Subject=%r To=%s",
            subject, recipients,
        )
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        if settings.SMTP_USE_SSL:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
                if settings.SMTP_USERNAME:
                    smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
                if settings.SMTP_USE_TLS:
                    smtp.starttls()
                if settings.SMTP_USERNAME:
                    smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                smtp.send_message(message)
        logger.info("send_email: sent %r to %s", subject, recipients)
        return True
    except Exception:
        # Broad `except Exception` is intentional here -- smtplib can raise
        # a long tail of different exception types (connection refused,
        # auth failure, timeout, malformed address, ...) and NONE of them
        # should ever bubble up out of an email-sending helper and take
        # down the calling request/task with them.
        logger.warning("send_email: failed to send %r to %s", subject, recipients, exc_info=True)
        return False


# -----------------------------------------------------------------------------
# DIGEST RECIPIENTS (admin-editable, runtime, no restart required)
# -----------------------------------------------------------------------------
# Who gets the once-a-day overdue/due-soon SYSTEM-WIDE DIGEST (see
# tasks/notification_tasks.py) used to be computed automatically -- every
# Manager + every Admin/Super Admin, full stop, with no way to trim that
# list without changing someone's role. That's wrong for a digest
# specifically: not every Manager/Admin wants (or should get) an
# operational inbox flood, and a distribution list or a single ops
# person's inbox often ISN'T a `users` row at all. This list decouples
# "receives the daily digest" from "is a Manager/Admin account" entirely
# -- it's a plain list of email addresses, editable at runtime by a Super
# Admin/Admin (PUT /settings/digest-recipients), that is the SOLE
# audience for the digest (see notification_tasks.py, which no longer
# queries `users` by role for this). Being a Manager/Admin no longer
# implies receiving the digest; being on this list is what does.
#
# Addresses here don't need to correspond to a `users` row at all -- this
# is intentionally a superset use case (an ops distribution list, an
# on-call pager address, etc.), not a subset-of-admins picker. Stored as a
# newline-joined string in the same generic `AppSetting` key/value table
# services/quotation_service.py's VAT setting uses, rather than a comma-
# joined one -- an email's local-part is technically allowed to contain a
# comma, so comma-joining could silently corrupt an edge-case address on
# read-back; newlines can never appear inside a valid address.
def get_digest_recipient_emails(db: Session) -> list[str]:
    """The current admin-configured digest audience. Empty list means no
    digest is sent at all (see notification_tasks.py) -- there is no
    automatic fallback to "every Admin/Manager" any more."""
    row = db.query(models.AppSetting).filter(models.AppSetting.key == DIGEST_RECIPIENTS_SETTING_KEY).first()
    if not row or not row.value:
        return []
    return [line.strip() for line in row.value.split("\n") if line.strip()]


def set_digest_recipient_emails(db: Session, payload: DigestRecipientsUpdateRequest, user: dict) -> dict:
    """Replaces the ENTIRE digest recipients list (see
    DigestRecipientsUpdateRequest's docstring for why this is a full
    replace rather than an add/remove-one endpoint). `payload.emails` has
    already been trimmed, lowercased, de-duplicated, and format-checked by
    the schema's validator by the time it reaches here."""
    row = db.query(models.AppSetting).filter(models.AppSetting.key == DIGEST_RECIPIENTS_SETTING_KEY).first()
    previous = get_digest_recipient_emails(db)
    value = "\n".join(payload.emails)
    if not row:
        row = models.AppSetting(key=DIGEST_RECIPIENTS_SETTING_KEY, value=value, updated_by=user["email"])
        db.add(row)
    else:
        row.value = value
        row.updated_by = user["email"]

    db.add(models.AuditLog(
        operator=user["email"], action="DIGEST_RECIPIENTS_UPDATED", target_type="AppSetting", target_id=0,
        details=f"Changed daily digest recipients from {len(previous)} address(es) to {len(payload.emails)} address(es).",
    ))
    db.commit()
    return {"emails": payload.emails}
