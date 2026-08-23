"""
app/main/db.py
-------------------
SQLite persistence: schema init/migration, connection helper, the
incident dedup/fingerprint/classify/persist pipeline that POST /v1/events
runs every event through, small key-value app-state helpers, and
retention/cleanup housekeeping (auto-resolve stale incidents, purge old
resolved ones, DB-file-size warning).
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from shared.errorbeacon_sanitization import clean as _shared_clean, redact, sanitize_url

from .config import (
    AI_ENABLED,
    AUTO_STALE_DAYS,
    CHAOS_TEST_ALERTS,
    DATA_DIR,
    DB_PATH,
    DB_WARN_MB,
    DEDUP,
    RETENTION_DAYS,
    SPIKE_THRESHOLD,
    SPIKE_WINDOW,
)
from .logging_setup import log
from .models import ErrorEvent, iso
from .state import _VOLATILE, _db_lock

CLEAN_MAX_DEPTH = max(1, int(os.getenv('ERRORBEACON_CLEAN_MAX_DEPTH', '10')))
CLEAN_MAX_ITEMS = max(1, int(os.getenv('ERRORBEACON_CLEAN_MAX_ITEMS', '100')))


def should_notify(e):
    if e.expected or e.category == 'healthcheck':
        return False
    if e.category == 'chaos_test':
        return CHAOS_TEST_ALERTS
    return classify(e) != 'info'


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # SQLite is local to ErrorBeacon. Serialize startup initialization across
    # Uvicorn worker processes as well as threads, then retry transient locks.
    lock_path = DATA_DIR / 'errorbeacon.init.lock'
    with open(lock_path, 'w') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with _db_lock:
                last_error = None
                c = None
                configured_journal = os.getenv('SQLITE_JOURNAL_MODE', 'WAL').upper()
                configured_journal = configured_journal if configured_journal in {'WAL','DELETE','TRUNCATE','PERSIST'} else 'WAL'
                # WAL mode needs a shared-memory (-shm) mmap that network filesystems
                # (Azure Files/SMB, NFS, etc.) don't support reliably -- on those mounts
                # every PRAGMA journal_mode=WAL attempt fails with "database is locked",
                # even for a single writer, and retrying the same mode never helps. Retry
                # the configured mode first (transient locks do happen on local disks
                # too), then fall back to DELETE -- a plain rollback journal that needs
                # no shared memory -- instead of failing startup forever.
                journal_attempts = [configured_journal]
                if configured_journal != 'DELETE':
                    journal_attempts.append('DELETE')
                for journal in journal_attempts:
                    for attempt in range(5):
                        try:
                            c = sqlite3.connect(DB_PATH, timeout=15)
                            c.execute(f'PRAGMA journal_mode={journal}')
                            c.execute('PRAGMA busy_timeout=15000')
                            last_error = None
                            break
                        except sqlite3.OperationalError as exc:
                            last_error = exc
                            if c is not None:
                                try:
                                    c.close()
                                except Exception:
                                    pass
                                c = None
                            time.sleep(0.25 * (attempt + 1))
                    if last_error is None:
                        break
                    log.warning('ErrorBeacon SQLite journal_mode=%s unavailable (%s); trying fallback', journal, last_error)
                if last_error is not None or c is None:
                    raise last_error or sqlite3.OperationalError('Unable to initialize ErrorBeacon SQLite database')
                try:
                    c.execute('''
                        CREATE TABLE IF NOT EXISTS incidents(
                            id TEXT PRIMARY KEY,
                            created_at TEXT NOT NULL,
                            app TEXT NOT NULL,
                            environment TEXT NOT NULL,
                            severity TEXT NOT NULL,
                            error_type TEXT,
                            message TEXT NOT NULL,
                            traceback TEXT,
                            request_id TEXT,
                            method TEXT,
                            path TEXT,
                            status_code INTEGER,
                            user_id TEXT,
                            release TEXT,
                            host TEXT,
                            component TEXT,
                            operation TEXT,
                            category TEXT,
                            context_json TEXT,
                            fingerprint TEXT NOT NULL,
                            occurrence_count INTEGER NOT NULL DEFAULT 1,
                            last_seen_at TEXT NOT NULL,
                            resolved INTEGER NOT NULL DEFAULT 0,
                            telegram_sent INTEGER NOT NULL DEFAULT 0,
                            ai_analysis TEXT,
                            spike_detected INTEGER NOT NULL DEFAULT 0,
                            deployment_regression INTEGER NOT NULL DEFAULT 0,
                            silenced_until TEXT,
                            telegram_status TEXT NOT NULL DEFAULT 'pending',
                            telegram_attempts INTEGER NOT NULL DEFAULT 0,
                            telegram_next_retry_at TEXT,
                            telegram_last_error TEXT,
                            fallback_status TEXT NOT NULL DEFAULT 'pending',
                            fallback_attempts INTEGER NOT NULL DEFAULT 0,
                            fallback_next_retry_at TEXT,
                            fallback_last_error TEXT,
                            fallback_sent_at TEXT,
                            ai_failure_email_sent_at TEXT,
                            resolved_at TEXT,
                            resolved_reason TEXT,
                            ai_provider_used TEXT
                        )
                    ''')
                    c.execute('''
                        CREATE TABLE IF NOT EXISTS service_state(
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                    ''')
                    c.execute('''
                        CREATE TABLE IF NOT EXISTS incident_events(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            incident_id TEXT,
                            fingerprint TEXT NOT NULL,
                            occurred_at TEXT NOT NULL,
                            app TEXT NOT NULL,
                            environment TEXT NOT NULL,
                            release TEXT,
                            request_id TEXT
                        )
                    ''')

                    # Columns added after the initial release. Each is applied only if
                    # missing, so this stays safe to run against an existing database.
                    migrations = {
                        'component': 'ALTER TABLE incidents ADD COLUMN component TEXT',
                        'operation': 'ALTER TABLE incidents ADD COLUMN operation TEXT',
                        'category': 'ALTER TABLE incidents ADD COLUMN category TEXT',
                        'spike_detected': 'ALTER TABLE incidents ADD COLUMN spike_detected INTEGER NOT NULL DEFAULT 0',
                        'deployment_regression': 'ALTER TABLE incidents ADD COLUMN deployment_regression INTEGER NOT NULL DEFAULT 0',
                        'silenced_until': 'ALTER TABLE incidents ADD COLUMN silenced_until TEXT',
                        'ai_status': "ALTER TABLE incidents ADD COLUMN ai_status TEXT NOT NULL DEFAULT 'pending'",
                        'ai_attempts': 'ALTER TABLE incidents ADD COLUMN ai_attempts INTEGER NOT NULL DEFAULT 0',
                        'ai_next_retry_at': 'ALTER TABLE incidents ADD COLUMN ai_next_retry_at TEXT',
                        'ai_last_error': 'ALTER TABLE incidents ADD COLUMN ai_last_error TEXT',
                        'telegram_status': "ALTER TABLE incidents ADD COLUMN telegram_status TEXT NOT NULL DEFAULT 'pending'",
                        'telegram_attempts': 'ALTER TABLE incidents ADD COLUMN telegram_attempts INTEGER NOT NULL DEFAULT 0',
                        'telegram_next_retry_at': 'ALTER TABLE incidents ADD COLUMN telegram_next_retry_at TEXT',
                        'telegram_last_error': 'ALTER TABLE incidents ADD COLUMN telegram_last_error TEXT',
                        'fallback_status': "ALTER TABLE incidents ADD COLUMN fallback_status TEXT NOT NULL DEFAULT 'pending'",
                        'fallback_attempts': 'ALTER TABLE incidents ADD COLUMN fallback_attempts INTEGER NOT NULL DEFAULT 0',
                        'fallback_next_retry_at': 'ALTER TABLE incidents ADD COLUMN fallback_next_retry_at TEXT',
                        'fallback_last_error': 'ALTER TABLE incidents ADD COLUMN fallback_last_error TEXT',
                        'fallback_sent_at': 'ALTER TABLE incidents ADD COLUMN fallback_sent_at TEXT',
                        'ai_failure_email_sent_at': 'ALTER TABLE incidents ADD COLUMN ai_failure_email_sent_at TEXT',
                        'resolved_at': 'ALTER TABLE incidents ADD COLUMN resolved_at TEXT',
                        'resolved_reason': 'ALTER TABLE incidents ADD COLUMN resolved_reason TEXT',
                        'ai_provider_used': 'ALTER TABLE incidents ADD COLUMN ai_provider_used TEXT',
                    }
                    cols = {r[1] for r in c.execute('PRAGMA table_info(incidents)')}
                    for column_name, alter_sql in migrations.items():
                        if column_name not in cols:
                            c.execute(alter_sql)

                    c.execute('CREATE INDEX IF NOT EXISTS idx_inc_fp ON incidents(fingerprint)')
                    c.execute('CREATE INDEX IF NOT EXISTS idx_inc_seen ON incidents(last_seen_at DESC)')
                    c.execute('CREATE INDEX IF NOT EXISTS idx_evt_fp_time ON incident_events(fingerprint,occurred_at DESC)')
                    c.execute('CREATE INDEX IF NOT EXISTS idx_inc_ai_retry ON incidents(ai_status,ai_next_retry_at)')
                    c.execute('CREATE INDEX IF NOT EXISTS idx_inc_tg_retry ON incidents(telegram_status,telegram_next_retry_at)')
                    c.execute('CREATE INDEX IF NOT EXISTS idx_inc_fallback_retry ON incidents(fallback_status,fallback_next_retry_at)')
                    c.commit()
                finally:
                    c.close()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

def db():
    c=sqlite3.connect(DB_PATH,timeout=15)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000')
    return c

def normalize(s):
    s=re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}','<id>',s,flags=re.I)
    s=re.sub(r'\b(?:req|request|trace|user|quote|quotation|asset)[_-]?[0-9a-f-]{6,}\b','<id>',s,flags=re.I)
    return _VOLATILE.sub('<n>',s).lower().strip()

def fingerprint(e):
    # BUG FIX: `path` used to be hashed raw (e.g. '/api/assets/42'), while
    # `message` was already run through normalize() to strip volatile IDs.
    # The same bug on '/assets/42' vs '/assets/43' produced two DIFFERENT
    # fingerprints -- two separate incidents (and two separate Telegram
    # alerts) for what's really one recurring bug on a parameterized route.
    # Reuse the existing normalize()/_VOLATILE machinery here too instead
    # of a second ID-stripping implementation.
    value = '|'.join(
        [
            e.app,
            e.environment,
            e.component or '',
            e.error_type or '',
            normalize(e.message),
            e.method or '',
            normalize(e.path or ''),
            str(e.status_code or ''),
        ]
    )
    return hashlib.sha256(value.encode()).hexdigest()[:20]

def classify(e):
    if e.expected or e.category in {'healthcheck', 'chaos_test'}:
        return 'info'
    if e.category == 'dependency_degraded':
        return 'warning'

    severity = e.severity.lower()
    text = f'{e.error_type or ""} {e.message}'.lower()

    if severity in {'critical', 'fatal'}:
        return 'critical'

    critical_terms = (
        'database unavailable',
        'connection refused',
        'integrityerror',
        'outage',
        'startup',
        'migration',
        'payment',
    )
    if e.status_code and e.status_code >= 500 and any(
        term in text for term in critical_terms
    ):
        return 'critical'

    if e.status_code and e.status_code >= 500:
        return 'error'

    return severity if severity in {'info', 'warning', 'error', 'critical'} else 'error'

def clean(v, key='', depth=0):
    """Recursively redact context with both breadth and depth limits."""
    return _shared_clean(
        v,
        key,
        depth,
        max_depth=CLEAN_MAX_DEPTH,
        max_items=CLEAN_MAX_ITEMS,
        string_limit=4000,
        scalar_limit=4000,
    )

def persist(e):
    """Fingerprint/dedupe an incoming event and write it to SQLite.

    Returns a 6-tuple: (incident_id, occurrence_count, should_alert, spike, regression, silenced).
    `should_alert` is only True the first time a spike crosses the threshold, or on a
    brand-new incident -- repeat occurrences within the dedup window are recorded but
    do not re-trigger delivery.
    """
    from .ai import _providers  # local import: avoids a circular import with ai.py, which itself imports from this module
    fp = fingerprint(e)
    now = iso()
    spike_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SPIKE_WINDOW)).isoformat()
    regression_lookback = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with _db_lock:
        c = db()

        # A "deployment regression" is this same fingerprint reappearing under a
        # *different* release within the last week -- i.e. something that looked
        # fixed came back after a deploy.
        prior_release_incident = None
        if e.release:
            prior_release_incident = c.execute(
                'SELECT 1 FROM incidents WHERE app=? AND environment=? AND fingerprint=? '
                'AND (release IS NULL OR release!=?) AND created_at>=? LIMIT 1',
                (e.app, e.environment, fp, e.release, regression_lookback),
            ).fetchone()
        regression = bool(prior_release_incident)

        existing = c.execute(
            'SELECT * FROM incidents WHERE fingerprint=? ORDER BY last_seen_at DESC LIMIT 1',
            (fp,),
        ).fetchone()

        # Record this occurrence in the raw event log regardless of whether it
        # matches an existing open incident, a resolved one, or is brand new.
        linked_incident_id = existing['id'] if existing and not existing['resolved'] else None
        cur = c.execute(
            'INSERT INTO incident_events(incident_id,fingerprint,occurred_at,app,environment,release,request_id) '
            'VALUES(?,?,?,?,?,?,?)',
            (linked_incident_id, fp, now, e.app, e.environment, e.release, e.request_id),
        )
        event_id = cur.lastrowid

        recent_count = c.execute(
            'SELECT COUNT(*) FROM incident_events WHERE fingerprint=? AND occurred_at>=?',
            (fp, spike_cutoff),
        ).fetchone()[0]
        spike = recent_count >= SPIKE_THRESHOLD

        is_repeat_within_dedup_window = (
            existing
            and not existing['resolved']
            and time.time() - datetime.fromisoformat(existing['last_seen_at']).timestamp() < DEDUP
        )
        if is_repeat_within_dedup_window:
            occurrence_count = existing['occurrence_count'] + 1
            silenced = bool(
                existing['silenced_until']
                and datetime.fromisoformat(existing['silenced_until']).timestamp() > time.time()
            )
            c.execute(
                'UPDATE incidents SET occurrence_count=?,last_seen_at=?,spike_detected=?,'
                'deployment_regression=?,request_id=COALESCE(?,request_id),traceback=COALESCE(?,traceback) '
                'WHERE id=?',
                (occurrence_count, now, int(spike), int(regression), e.request_id, redact(e.traceback), existing['id']),
            )
            c.commit()
            c.close()
            just_crossed_spike_threshold = spike and occurrence_count == SPIKE_THRESHOLD and not silenced
            return existing['id'], occurrence_count, just_crossed_spike_threshold, spike, regression, silenced

        # New incident (either genuinely new fingerprint, or the prior one was resolved).
        incident_id = uuid.uuid4().hex[:12]
        context_json = json.dumps(clean(e.context), default=str)
        # should_notify() decides whether this incident is ever supposed to
        # page (info-classified, healthcheck, expected, or a non-alerted
        # chaos test). That decision was previously only checked once, by
        # process() before calling enqueue() -- it was never written to the
        # DB. Because telegram_status/ai_status defaulted to 'pending'
        # regardless, telegram_recovery_loop()/ai_recovery_loop() -- which
        # only look at DB status, not should_notify() -- would pick these
        # incidents up a few seconds later and deliver them anyway. Persist
        # the suppression as a terminal status so the recovery loops' own
        # 'pending' queries never match these rows in the first place.
        notify = should_notify(e)
        ai_status = ('pending' if AI_ENABLED and _providers() else 'disabled') if notify else 'suppressed'
        telegram_status = 'pending' if notify else 'suppressed'
        c.execute(
            '''INSERT INTO incidents(
                id,created_at,app,environment,severity,error_type,message,traceback,request_id,
                method,path,status_code,user_id,release,host,component,operation,category,
                context_json,fingerprint,last_seen_at,spike_detected,deployment_regression,ai_status,
                telegram_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                incident_id, now, e.app, e.environment, classify(e), e.error_type,
                redact(e.message, 5000), redact(e.traceback), e.request_id, e.method,
                sanitize_url(e.path), e.status_code,
                redact(e.user_id, 200) if e.user_id else None,
                redact(e.release, 200) if e.release else None,
                redact(e.host, 300) if e.host else None,
                e.component, e.operation, e.category, context_json, fp, now,
                int(spike), int(regression), ai_status, telegram_status,
            ),
        )
        c.execute('UPDATE incident_events SET incident_id=? WHERE id=?', (incident_id, event_id))
        c.commit()
        c.close()
        return incident_id, 1, True, spike, regression, False

def _state_get(key, default=None):
    with _db_lock:
        c=db(); row=c.execute('SELECT value FROM service_state WHERE key=?',(key,)).fetchone(); c.close()
    return row['value'] if row else default

def _state_set(key, value):
    with _db_lock:
        c=db(); c.execute('INSERT INTO service_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,str(value),iso())); c.commit(); c.close()

def auto_stale_resolve():
    cutoff=(datetime.now(timezone.utc)-timedelta(days=AUTO_STALE_DAYS)).isoformat()
    with _db_lock:
        c=db()
        rows=c.execute("SELECT id FROM incidents WHERE resolved=0 AND last_seen_at<=? LIMIT 500",(cutoff,)).fetchall()
        if rows:
            now=iso()
            c.executemany('UPDATE incidents SET resolved=1,resolved_at=?,resolved_reason=? WHERE id=?',[(now,f'auto-stale: no new occurrence for {AUTO_STALE_DAYS} days',r['id']) for r in rows])
            c.commit()
        c.close()
    if rows: log.info('Auto-stale resolution closed %d incident(s)',len(rows))
    return len(rows)

def purge_resolved_incidents():
    cutoff=(datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)).isoformat()
    with _db_lock:
        c=db()
        ids=[r['id'] for r in c.execute('SELECT id FROM incidents WHERE resolved=1 AND COALESCE(resolved_at,last_seen_at)<=?',(cutoff,)).fetchall()]
        old_event_count=c.execute('SELECT COUNT(*) FROM incident_events WHERE occurred_at<=?',(cutoff,)).fetchone()[0]
        if ids:
            c.executemany('DELETE FROM incident_events WHERE incident_id=?',[(i,) for i in ids]); c.executemany('DELETE FROM incidents WHERE id=?',[(i,) for i in ids])
        c.execute('DELETE FROM incident_events WHERE occurred_at<=?',(cutoff,))
        c.commit(); c.close()
    if ids or old_event_count: log.info('Retention purge removed %d resolved incident(s) and %d old event(s)',len(ids),old_event_count)
    return len(ids) + old_event_count

def _db_warning_message(mb):
    return (f'⚠️ <b>ErrorBeacon DB warning</b>\n\nDatabase size is <b>{mb:.1f} MB</b>, which has crossed the configured warning threshold of <b>{DB_WARN_MB:.1f} MB</b>.\n\nRetention: {RETENTION_DAYS} days\nAuto-stale: {AUTO_STALE_DAYS} days')

def check_db_warning():
    from .email_notify import email_configured, email_recipients, send_email  # local import: avoids a circular import (email_notify.py imports from this module)
    from .telegram import tg_delivery  # local import: avoids a circular import (telegram.py imports from this module)

    size=DB_PATH.stat().st_size if DB_PATH.exists() else 0
    mb=size/(1024*1024)
    if mb >= DB_WARN_MB:
        if _state_get('db_warn_active','0') != '1':
            _state_set('db_warn_active','1')
            text=_db_warning_message(mb)
            tg_result=tg_delivery('sendMessage',text)
            if not tg_result.sent and email_configured():
                send_email(email_recipients(),'ErrorBeacon DB warning',re.sub('<[^>]+>','',text))
            log.warning('ErrorBeacon DB size crossed warning threshold: %.1f MB >= %.1f MB',mb,DB_WARN_MB)
            return True
    elif _state_get('db_warn_active','0') == '1':
        _state_set('db_warn_active','0')
        log.info('ErrorBeacon DB size returned below warning threshold: %.1f MB',mb)
    return False

def _event_from_row(x):
    try:
        context=json.loads(x['context_json'] or '{}')
    except Exception:
        context={}
    return ErrorEvent(app=x['app'],environment=x['environment'],severity=x['severity'],error_type=x['error_type'],message=x['message'],traceback=x['traceback'],request_id=x['request_id'],method=x['method'],path=x['path'],status_code=x['status_code'],user_id=x['user_id'],release=x['release'],host=x['host'],component=x['component'],operation=x['operation'],category=x['category'],context=context)

def incident_view(i):
    with _db_lock:
        c=db(); x=c.execute('SELECT * FROM incidents WHERE id=?',(i,)).fetchone(); c.close()
    if not x: return '❌ <b>Incident not found.</b>'
    active=bool(x['silenced_until'] and datetime.fromisoformat(x['silenced_until']).timestamp()>time.time())
    lines=['🔎 <b>ErrorBeacon Incident</b>','',f'<b>ID:</b> <code>{x["id"]}</code>',f'<b>Status:</b> {"RESOLVED" if x["resolved"] else "OPEN"}',f'<b>Silence:</b> {"ACTIVE until "+x["silenced_until"] if active else "none"}',f'<b>Telegram:</b> {html.escape(x["telegram_status"])} ({x["telegram_attempts"]} attempts)',f'<b>Email fallback:</b> {html.escape(x["fallback_status"])}',f'<b>AI:</b> {html.escape(x["ai_status"])}',f'<b>Severity:</b> {x["severity"]}',f'<b>App:</b> {html.escape(x["app"])}',f'<b>Occurrences:</b> {x["occurrence_count"]}',f'<b>Request ID:</b> <code>{html.escape(x["request_id"] or "N/A")}</code>',f'<b>Path:</b> {html.escape(x["path"] or "N/A")[:500]}',f'<b>Release:</b> <code>{html.escape(x["release"] or "N/A")}</code>',f'<b>First seen:</b> {x["created_at"]}',f'<b>Last seen:</b> {x["last_seen_at"]}']
    return '\n'.join(lines)[:4000]

def _incident_update(i,sql,params=()):
    with _db_lock:
        c=db(); cur=c.execute(sql,params); c.commit(); changed=cur.rowcount; c.close()
    return bool(changed)

def _format_duration(seconds):
    seconds=max(0,int(seconds))
    days, rem=divmod(seconds,86400); hours, rem=divmod(rem,3600); minutes=rem//60
    parts=[]
    if days: parts.append(f'{days}d')
    if hours: parts.append(f'{hours}h')
    if minutes or not parts: parts.append(f'{minutes}m')
    return ' '.join(parts)

def _silence_seconds(value):
    m=re.fullmatch(r'(?i)(\d+)(m|h|d)?',value.strip())
    if not m: raise ValueError('duration must look like 30m, 1h, 4h, or 24h')
    return max(60,min(int(m.group(1))*{'m':60,'h':3600,'d':86400}[ (m.group(2) or 'm').lower() ],86400))
