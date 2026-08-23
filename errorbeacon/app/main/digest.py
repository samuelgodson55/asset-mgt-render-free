"""
app/main/digest.py
-----------------------
Periodic digest message (top recurring fingerprints + delivery stats over
the last DIGEST_INTERVAL_HOURS) and the maintenance loop that also drives
auto-stale-resolve, purge, and DB-size-warning housekeeping (see db.py).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from shared.errorbeacon_sanitization import redact

from .config import DIGEST_ENABLED, DIGEST_INTERVAL_HOURS, RETENTION_INTERVAL_SECONDS
from .db import auto_stale_resolve, check_db_warning, db, purge_resolved_incidents, _state_get, _state_set, _db_lock
from .email_notify import email_configured, email_recipients, send_email
from .logging_setup import log
from .state import _stop
from .telegram import tg_delivery


def _digest_text():
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=DIGEST_INTERVAL_HOURS)).isoformat()
    with _db_lock:
        c=db()
        total=c.execute('SELECT COUNT(*) FROM incidents WHERE last_seen_at>=?',(cutoff,)).fetchone()[0]
        unresolved=c.execute('SELECT COUNT(*) FROM incidents WHERE resolved=0').fetchone()[0]
        top=c.execute('SELECT fingerprint,app,severity,message,SUM(occurrence_count) occurrences FROM incidents WHERE last_seen_at>=? GROUP BY fingerprint,app,severity,message ORDER BY occurrences DESC LIMIT 5',(cutoff,)).fetchall()
        tg_sent=c.execute("SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND telegram_status='sent'",(cutoff,)).fetchone()[0]
        email_sent=c.execute("SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND fallback_status='sent'",(cutoff,)).fetchone()[0]
        ai_done=c.execute("SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND ai_status='complete'",(cutoff,)).fetchone()[0]
        c.close()
    lines=[f'📊 <b>ErrorBeacon {"daily" if DIGEST_INTERVAL_HOURS<=24 else "periodic"} digest</b>','',f'Window: last {DIGEST_INTERVAL_HOURS}h',f'Incidents: {total}',f'Unresolved: {unresolved}',f'Telegram delivered: {tg_sent} / {total} incidents',f'Email fallback delivered: {email_sent}',f'AI completed: {ai_done}','']
    if top:
        lines.append('<b>Top recurring fingerprints</b>')
        for r in top:
            lines.append(f'• {r["occurrences"]}x · {html.escape(str(r["severity"]).upper())} · {html.escape(str(r["app"]))} · {html.escape(redact(r["message"],220))}')
    else:
        lines.append('No incidents in this window.')
    return '\n'.join(lines)

def send_digest():
    if not DIGEST_ENABLED: return False
    now=datetime.now(timezone.utc)
    last=_state_get('digest_last_sent_at')
    if not last:
        _state_set('digest_last_sent_at',now.isoformat())
        return False
    try:
        if (now-datetime.fromisoformat(last)).total_seconds() < DIGEST_INTERVAL_HOURS*3600: return False
    except ValueError:
        _state_set('digest_last_sent_at',now.isoformat())
        return False
    text=_digest_text()
    result=tg_delivery('sendMessage',text)
    sent=result.sent
    if not sent and email_configured():
        sent=send_email(email_recipients(),'ErrorBeacon digest',re.sub('<[^>]+>','',text))
    if sent: _state_set('digest_last_sent_at',now.isoformat())
    return sent

def retention_and_health_loop():
    while not _stop.is_set():
        try:
            auto_stale_resolve()
            purge_resolved_incidents()
            check_db_warning()
            send_digest()
        except Exception: log.exception('Retention/health maintenance failed')
        _stop.wait(RETENTION_INTERVAL_SECONDS)
