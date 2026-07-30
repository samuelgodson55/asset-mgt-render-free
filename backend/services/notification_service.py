"""
services/notification_service.py
----------------------------------
The ONE place in this codebase that knows how to send an email. Every other
module (services/extension_service.py, tasks/notification_tasks.py) builds
a subject/body and calls `send_email()` here -- nothing else touches
`smtplib`/`requests` directly.

WHY THIS IS DELIBERATELY "BORING" (plain SMTP by default, no vendor SDK
EVER -- just raw HTTP via `requests` for the two alternate providers below)
---------------------------------------------------------------
This app is meant to be self-hostable with zero required third-party
accounts. Plain SMTP (Python's built-in `smtplib`) works unmodified against
a local Postfix/Exim relay, a self-hosted mail server, OR a hosted
provider's SMTP endpoint (SendGrid, Mailgun, AWS SES, etc. all expose one)
-- so there's no vendor lock-in and no extra dependency to install. This is
still `EMAIL_PROVIDER`'s default and the ONLY thing deploy-azure-vm.yml/
deploy-azure-aca.yml ever need.

WHY EMAIL_PROVIDER (config.py) CAN SELECT "brevo"/"resend" INSTEAD
-----------------------------------------------------------------------
One exception, forced by the platform, not a design preference: Render's
Free web service instance type blocks ALL outbound traffic on SMTP ports
25/465/587 at the network level (see EMAIL_PROVIDER's own comment in
config.py) -- no code-level fix reaches around a port block. An HTTP-API
provider sends over port 443 instead, which Free Render services CAN
reach, so `render.yaml` sets `EMAIL_PROVIDER=brevo` (or `resend`) instead
of leaving SMTP configured and silently timing out. `_send_via_brevo()`/
`_send_via_resend()` below are raw `requests.post()` calls against each
provider's plain HTTP API -- no vendor SDK package, same "boring" spirit,
just a transport that isn't blocked.

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
import socket
import threading
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Iterable

import requests
from sqlalchemy.orm import Session

import models
from config import settings
from schemas.notifications_schema import DigestRecipientsUpdateRequest

logger = logging.getLogger(__name__)

# Same generic key/value store used by services/quotation_service.py's
# VAT setting (models.AppSetting) -- runtime-editable by a Super
# Admin/Admin without a restart, unlike everything in config.py.
DIGEST_RECIPIENTS_SETTING_KEY = "digest_recipient_emails"

# Guards the socket.getaddrinfo monkeypatch in _ipv4_only_dns() below --
# without this, two send_email() calls racing on different threads could
# each install/restore the patch mid-connection-attempt of the other,
# leaving either one (or both) resolving DNS with the wrong, or no,
# patch in place. The window is a single SMTP connection's DNS lookup,
# not the whole send, so this is not a meaningful bottleneck even under
# concurrent notification bursts (see tasks/notification_tasks.py).
_dns_patch_lock = threading.Lock()


@contextmanager
def _ipv4_only_dns():
    """
    Forces `socket.getaddrinfo()` -- and therefore `smtplib`, which has no
    public option to pick an address family itself -- to resolve IPv4 (A
    record) addresses only, for the duration of this context.

    WHY THIS EXISTS: smtplib.SMTP/SMTP_SSL always resolve the SMTP host via
    `socket.create_connection()`, which calls `getaddrinfo()` with
    family=AF_UNSPEC and tries whichever address comes back first. Most
    real SMTP providers (Gmail, Microsoft 365, SendGrid, ...) publish BOTH
    an A and an AAAA record. On a host with a working outbound IPv6 route
    (e.g. Azure Container Apps -- see deploy-azure-aca.yml), that's a
    complete no-op either way. On a host WITHOUT one (Render's free/starter
    web services -- see render.yaml), the IPv6 attempt fails immediately
    with `OSError: [Errno 101] Network is unreachable`, and CPython's
    `socket.create_connection()` (3.11+) re-raises the FIRST exception it
    collected rather than falling through to the IPv4 address that would
    have worked -- so the whole send fails even though a working route
    exists in the very same DNS answer. Forcing AF_INET here sidesteps
    that path entirely rather than depending on it working out.
    """
    real_getaddrinfo = socket.getaddrinfo

    def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    with _dns_patch_lock:
        socket.getaddrinfo = ipv4_only_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = real_getaddrinfo


def send_email(to: Iterable[str] | str, subject: str, body: str) -> bool:
    """
    Sends one plain-text email to one or more recipients via whichever
    transport `settings.EMAIL_PROVIDER` selects (see config.py's own
    comment on that setting for why more than just "smtp" exists at all).
    Returns True if the message was handed off successfully, False in
    every other case (notifications disabled, no recipients, misconfigured
    provider settings, or the send itself raising) -- callers should treat
    a False return as "logged, not fatal" rather than retrying inline.
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

    provider = (settings.EMAIL_PROVIDER or "smtp").strip().lower()
    provider_config_ok, provider_config_error = {
        "smtp": (
            bool(settings.SMTP_HOST and settings.SMTP_FROM_EMAIL),
            "SMTP_HOST/SMTP_FROM_EMAIL aren't both configured",
        ),
        "brevo": (
            bool(settings.BREVO_API_KEY and settings.SMTP_FROM_EMAIL),
            "BREVO_API_KEY/SMTP_FROM_EMAIL aren't both configured",
        ),
        "resend": (
            bool(settings.RESEND_API_KEY and settings.SMTP_FROM_EMAIL),
            "RESEND_API_KEY/SMTP_FROM_EMAIL aren't both configured",
        ),
    }.get(provider, (False, f'unrecognized EMAIL_PROVIDER "{provider}" -- must be "smtp", "brevo", or "resend"'))

    if not provider_config_ok:
        logger.warning(
            "send_email: NOTIFICATIONS_ENABLED is true but %s -- cannot send. Subject=%r To=%s",
            provider_config_error, subject, recipients,
        )
        return False

    try:
        if provider == "brevo":
            _send_via_brevo(recipients, subject, body)
        elif provider == "resend":
            _send_via_resend(recipients, subject, body)
        else:
            _send_via_smtp(recipients, subject, body)
        logger.info("send_email: sent %r to %s via %s", subject, recipients, provider)
        return True
    except Exception:
        # Broad `except Exception` is intentional here -- smtplib and the
        # HTTP-API providers below can each raise a long tail of different
        # exception types (connection refused, auth failure, timeout,
        # malformed address, a non-2xx HTTP response, ...) and NONE of them
        # should ever bubble up out of an email-sending helper and take
        # down the calling request/task with them.
        logger.warning("send_email: failed to send %r to %s via %s", subject, recipients, provider, exc_info=True)
        return False


def _send_via_smtp(recipients: list[str], subject: str, body: str) -> None:
    """Plain RFC 5321 SMTP -- see config.py's SMTP_* settings. Raises on failure; send_email() catches."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    # See _ipv4_only_dns()'s own docstring -- fixes "OSError: [Errno 101]
    # Network is unreachable" on hosts with no outbound IPv6 route when the
    # SMTP host's DNS also has an AAAA record; a no-op everywhere else,
    # including ACA. Does NOT fix Render's Free-plan SMTP PORT block (see
    # EMAIL_PROVIDER's own comment) -- that's a different failure mode
    # (ETIMEDOUT, not ENETUNREACH) with no code-level fix, which is why
    # "brevo"/"resend" exist as alternate providers below.
    with _ipv4_only_dns():
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


def _send_via_brevo(recipients: list[str], subject: str, body: str) -> None:
    """
    Brevo's transactional email HTTP API -- https://developers.brevo.com/reference/sendtransacemail
    -- sent over port 443, unaffected by Render's Free-plan SMTP port
    block (see EMAIL_PROVIDER's own comment in config.py). Raises
    `requests.HTTPError` (via raise_for_status()) or any `requests`
    connection-level exception on failure; send_email() catches both.
    """
    response = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": settings.BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "sender": {"email": settings.SMTP_FROM_EMAIL},
            "to": [{"email": addr} for addr in recipients],
            "subject": subject,
            "textContent": body,
        },
        timeout=10,
    )
    response.raise_for_status()


def _send_via_resend(recipients: list[str], subject: str, body: str) -> None:
    """
    Resend's HTTP API -- https://resend.com/docs/api-reference/emails/send-email
    -- sent over port 443, unaffected by Render's Free-plan SMTP port
    block (see EMAIL_PROVIDER's own comment in config.py). Raises
    `requests.HTTPError` (via raise_for_status()) or any `requests`
    connection-level exception on failure; send_email() catches both.
    """
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.SMTP_FROM_EMAIL,
            "to": recipients,
            "subject": subject,
            "text": body,
        },
        timeout=10,
    )
    response.raise_for_status()


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
