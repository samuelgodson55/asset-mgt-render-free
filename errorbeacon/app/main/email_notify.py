"""
app/main/email_notify.py
----------------------------
Secondary/fallback email notification path -- used when Telegram delivery
is unavailable or hasn't confirmed a send within EMAIL_FALLBACK_AFTER_*
(see email_fallback_loop), plus AI-failure emails (see ai.py's
_notify_ai_failure). Supports three provider backends: raw SMTP, Brevo,
and Resend (see EMAIL_PROVIDER).

Named email_notify.py rather than email.py so it doesn't shadow the
stdlib `email` package that smtplib-based sending needs.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Iterable

import requests

from shared.errorbeacon_sanitization import redact, sanitize_url

from .config import (
    ADMIN_NOTIFICATION_EMAILS,
    BREVO_API_KEY,
    EMAIL_FALLBACK_AFTER_ATTEMPTS,
    EMAIL_FALLBACK_AFTER_SECONDS,
    EMAIL_FALLBACK_ENABLED,
    EMAIL_FALLBACK_RETRIES,
    EMAIL_PROVIDER,
    EMAIL_TIMEOUT_SECONDS,
    NOTIFICATIONS_ENABLED,
    RESEND_API_KEY,
    SMTP_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
    SMTP_USERNAME,
)
from .db import classify, db, _event_from_row
from .logging_setup import log
from .models import iso
from .state import _db_lock, _stop


def email_recipients() -> list[str]:
    return [x.strip() for x in ADMIN_NOTIFICATION_EMAILS.split(',') if x.strip()]

def email_configured() -> bool:
    if not EMAIL_FALLBACK_ENABLED or not NOTIFICATIONS_ENABLED or not email_recipients():
        return False
    if EMAIL_PROVIDER == 'smtp':
        auth_ok = bool(SMTP_USERNAME and SMTP_PASSWORD) or (not SMTP_USERNAME and not SMTP_PASSWORD)
        return bool(SMTP_HOST and SMTP_FROM_EMAIL and auth_ok)
    if EMAIL_PROVIDER == 'brevo': return bool(BREVO_API_KEY and SMTP_FROM_EMAIL)
    if EMAIL_PROVIDER == 'resend': return bool(RESEND_API_KEY and SMTP_FROM_EMAIL)
    return False

def _send_email_smtp(recipients, subject, body):
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=SMTP_FROM_EMAIL; msg['To']=', '.join(recipients); msg.set_content(body)
    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=EMAIL_TIMEOUT_SECONDS) as smtp:
            if SMTP_USERNAME: smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=EMAIL_TIMEOUT_SECONDS) as smtp:
            if SMTP_USE_TLS: smtp.starttls()
            if SMTP_USERNAME: smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

def _send_email_brevo(recipients, subject, body):
    r=requests.post('https://api.brevo.com/v3/smtp/email',headers={'api-key':BREVO_API_KEY,'accept':'application/json','content-type':'application/json'},json={'sender':{'email':SMTP_FROM_EMAIL},'to':[{'email':x} for x in recipients],'subject':subject,'textContent':body},timeout=EMAIL_TIMEOUT_SECONDS); r.raise_for_status()

def _send_email_resend(recipients, subject, body):
    r=requests.post('https://api.resend.com/emails',headers={'Authorization':f'Bearer {RESEND_API_KEY}','content-type':'application/json'},json={'from':SMTP_FROM_EMAIL,'to':recipients,'subject':subject,'text':body},timeout=EMAIL_TIMEOUT_SECONDS); r.raise_for_status()

def send_email(to: Iterable[str] | str, subject: str, body: str) -> bool:
    recipients=[to] if isinstance(to,str) else [x for x in to if x]
    recipients=[x.strip() for x in recipients if x and x.strip()]
    if not recipients or not NOTIFICATIONS_ENABLED: return False
    try:
        if EMAIL_PROVIDER=='smtp':
            if not SMTP_HOST or not SMTP_FROM_EMAIL: return False
            _send_email_smtp(recipients,subject,body)
        elif EMAIL_PROVIDER=='brevo':
            if not BREVO_API_KEY or not SMTP_FROM_EMAIL: return False
            _send_email_brevo(recipients,subject,body)
        elif EMAIL_PROVIDER=='resend':
            if not RESEND_API_KEY or not SMTP_FROM_EMAIL: return False
            _send_email_resend(recipients,subject,body)
        else:
            log.warning('Unsupported EMAIL_PROVIDER=%r',EMAIL_PROVIDER); return False
        return True
    except Exception:
        log.warning('ErrorBeacon email delivery failed via %s',EMAIL_PROVIDER,exc_info=True); return False

def email_incident_body(e,i,occ,*,reason='Telegram delivery unavailable',ai_failure=False,ai_error=None):
    lines=['ErrorBeacon secondary notification','',f'Reason: {reason}',f'Incident: {i}',f'Status: {classify(e).upper()}',f'App: {e.app}',f'Environment: {e.environment}',f'Exception: {e.error_type or "Unknown"}',f'Message: {redact(e.message,4000)}',f'Occurrences: {occ}']
    if e.request_id: lines.append(f'Request ID: {e.request_id}')
    if e.path: lines.append(f'Path: {sanitize_url(e.path)[:1000]}')
    if e.release: lines.append(f'Release: {e.release}')
    if e.component: lines.append(f'Component: {e.component}')
    if e.operation: lines.append(f'Operation: {e.operation}')
    if ai_failure: lines.extend(['','AI enrichment: PERMANENTLY FAILED',f'AI error: {ai_error or "unknown"}'])
    return '\n'.join(lines)

def _fallback_eligible(row, now):
    if row['fallback_status'] not in {'pending','failed'} or row['resolved'] or row['telegram_status'] not in {'pending','unknown'}: return False
    if row['fallback_next_retry_at']:
        try:
            if datetime.fromisoformat(row['fallback_next_retry_at'])>now: return False
        except ValueError: pass
    age=max(0,(now-datetime.fromisoformat(row['created_at'])).total_seconds())
    return int(row['telegram_attempts'] or 0)>=EMAIL_FALLBACK_AFTER_ATTEMPTS or age>=EMAIL_FALLBACK_AFTER_SECONDS

def _send_email_fallback(row):
    if not email_configured() or not _fallback_eligible(row,datetime.now(timezone.utc)): return False
    e=_event_from_row(row); subject=f'ErrorBeacon alert: {classify(e).upper()} · {e.app} · {row["id"]}'; body=email_incident_body(e,row['id'],row['occurrence_count'])
    if send_email(email_recipients(),subject,body):
        with _db_lock:
            c=db(); c.execute('UPDATE incidents SET fallback_status=?,fallback_attempts=fallback_attempts+1,fallback_next_retry_at=NULL,fallback_last_error=NULL,fallback_sent_at=? WHERE id=?',('sent',iso(),row['id'])); c.commit(); c.close()
        log.warning('Secondary email notification sent for incident %s because Telegram status=%s',row['id'],row['telegram_status']); return True
    attempts=int(row['fallback_attempts'] or 0)+1; nxt=None if attempts>=EMAIL_FALLBACK_RETRIES else (datetime.now(timezone.utc)+timedelta(seconds=min(3600,60*(2**min(attempts-1,5))))).isoformat()
    with _db_lock:
        c=db(); c.execute('UPDATE incidents SET fallback_status=?,fallback_attempts=?,fallback_next_retry_at=?,fallback_last_error=? WHERE id=?',('failed',attempts,nxt,'EmailDeliveryFailed',row['id'])); c.commit(); c.close()
    return False

def email_fallback_loop():
    while not _stop.is_set():
        try:
            if email_configured():
                cutoff=(datetime.now(timezone.utc)-timedelta(seconds=EMAIL_FALLBACK_AFTER_SECONDS)).isoformat()
                with _db_lock:
                    c=db(); rows=c.execute("SELECT * FROM incidents WHERE resolved=0 AND fallback_status IN ('pending','failed') AND telegram_status IN ('pending','unknown') AND (telegram_attempts>=? OR created_at<=?) ORDER BY created_at ASC LIMIT 25",(EMAIL_FALLBACK_AFTER_ATTEMPTS,cutoff)).fetchall(); c.close()
                for row in rows: _send_email_fallback(row)
        except Exception: log.exception('Secondary email fallback loop failed')
        _stop.wait(15)


def _email_diagnostics():
    recipients=email_recipients()
    missing=[]
    if not EMAIL_FALLBACK_ENABLED: missing.append('ERRORBEACON_EMAIL_FALLBACK_ENABLED')
    if not NOTIFICATIONS_ENABLED: missing.append('NOTIFICATIONS_ENABLED')
    if not recipients: missing.append('ADMIN_NOTIFICATION_EMAILS')
    if EMAIL_PROVIDER=='smtp':
        if not SMTP_HOST: missing.append('SMTP_HOST')
        if not SMTP_FROM_EMAIL: missing.append('SMTP_FROM_EMAIL')
        if bool(SMTP_USERNAME) != bool(SMTP_PASSWORD): missing.append('SMTP_USERNAME/SMTP_PASSWORD')
    elif EMAIL_PROVIDER=='brevo':
        if not BREVO_API_KEY: missing.append('BREVO_API_KEY')
        if not SMTP_FROM_EMAIL: missing.append('SMTP_FROM_EMAIL')
    elif EMAIL_PROVIDER=='resend':
        if not RESEND_API_KEY: missing.append('RESEND_API_KEY')
        if not SMTP_FROM_EMAIL: missing.append('SMTP_FROM_EMAIL')
    else: missing.append(f'unsupported EMAIL_PROVIDER={EMAIL_PROVIDER}')
    return {'enabled':EMAIL_FALLBACK_ENABLED,'notifications_enabled':NOTIFICATIONS_ENABLED,'provider':EMAIL_PROVIDER,'configured':not missing,'recipients_count':len(recipients),'smtp_host_configured':bool(SMTP_HOST),'smtp_username_configured':bool(SMTP_USERNAME),'smtp_password_configured':bool(SMTP_PASSWORD),'from_configured':bool(SMTP_FROM_EMAIL),'missing':missing}
