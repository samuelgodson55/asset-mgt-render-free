from __future__ import annotations
import hashlib
import hmac
import html
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
import fcntl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

APP_VERSION = '3.9.0'
DATA_DIR = Path(os.getenv('DATA_DIR', '/data'))
DB_PATH = DATA_DIR / 'errorbeacon.db'
API_KEY = os.getenv('ERRORBEACON_API_KEY', '')
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TG_CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
TG_THREAD = os.getenv('TELEGRAM_THREAD_ID', '')
TG_POLL = os.getenv('TELEGRAM_POLLING', 'true').lower() in {'1', 'true', 'yes', 'on'}
TG_POLL_SECONDS = int(os.getenv('TELEGRAM_POLL_SECONDS', '20'))
GROQ_KEY = os.getenv('GROQ_API_KEY', '')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.1-8b-instant')
GEMINI_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-lite')
GEMINI_FALLBACK_MODEL = os.getenv('GEMINI_FALLBACK_MODEL', 'gemini-2.5-flash')
OPENROUTER_KEY = os.getenv('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'openrouter/free')
MAX_ALERTS = int(os.getenv('MAX_ALERTS_PER_MINUTE', '30'))
DEDUP = int(os.getenv('DEDUP_SECONDS', '60'))
SPIKE_WINDOW = int(os.getenv('SPIKE_WINDOW_SECONDS', '300'))
SPIKE_THRESHOLD = int(os.getenv('SPIKE_THRESHOLD', '10'))
AI_ENABLED = os.getenv('AI_ENABLED', 'true').lower() in {'1', 'true', 'yes', 'on'}
CHAOS_TEST_ALERTS = os.getenv(
    'CHAOS_TEST_ALERTS',
    'false' if os.getenv('ENVIRONMENT', 'production').lower() in {'prod', 'production'} else 'true',
).lower() in {'1', 'true', 'yes', 'on'}
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
ALERT_Q = max(100, int(os.getenv('ALERT_QUEUE_SIZE', '1000')))
ALERT_WORKERS = max(1, int(os.getenv('ALERT_WORKERS', '3')))
AI_Q = max(50, int(os.getenv('AI_QUEUE_SIZE', '500')))
AI_WORKERS = max(1, int(os.getenv('AI_WORKERS', '1')))
AI_RETRIES = max(1, int(os.getenv('AI_RETRIES', '1')))
AI_RETRY_BASE = float(os.getenv('AI_RETRY_BASE_SECONDS', '30'))
AI_MAX_INCIDENT_RETRIES = max(1, int(os.getenv('AI_MAX_INCIDENT_RETRIES', '3')))
from contextvars import ContextVar
from starlette.types import ASGIApp, Receive, Scope, Send

request_id_var: ContextVar[str | None] = ContextVar('errorbeacon_request_id', default=None)

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': self.formatTime(record, datefmt='%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'request_id': getattr(record, 'request_id', None),
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)

root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
root_logger.handlers.clear()
_handler = logging.StreamHandler()
_handler.addFilter(RequestIdFilter())
_handler.setFormatter(JsonFormatter())
root_logger.addHandler(_handler)
for _name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
    _logger = logging.getLogger(_name)
    _logger.handlers.clear()
    _logger.propagate = True
log=logging.getLogger('errorbeacon')

class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        incoming = None
        for key, value in scope.get('headers', []):
            if key.lower() == b'x-request-id':
                candidate = value.decode('latin-1').strip()
                if candidate:
                    incoming = candidate[:200]
                break
        rid = incoming or str(uuid.uuid4())
        token = request_id_var.set(rid)
        async def send_with_request_id(message):
            if message.get('type') == 'http.response.start':
                headers = list(message.get('headers', []))
                headers = [(k, v) for k, v in headers if k.lower() != b'x-request-id']
                # HTTP header names are case-insensitive; this is the
                # X-Request-ID response header used to correlate each probe/event.
                headers.append((b'x-request-id', rid.encode('latin-1')))
                message['headers'] = headers
            await send(message)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
_db_lock=threading.Lock()
_rate_lock=threading.Lock()
_ai_state_lock=threading.Lock()
_rate=[]
_jobs=queue.Queue(maxsize=ALERT_Q)
_ai_jobs=queue.Queue(maxsize=AI_Q)
_stop=threading.Event()
_workers=[]
_ai_workers=[]
_ai_enqueued=set()
_recovery_thread=None
_SECRET_KEY=re.compile(r'(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|session|reset[_-]?token|access[_-]?token|refresh[_-]?token|private[_-]?key)')
_ASSIGN=re.compile(r'(?i)(authorization|cookie|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|reset[_-]?token|password|passwd|secret|session(?:[_-]?id)?)\s*[:=]\s*([^\s,;&]+)')
_BEARER=re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
_JWT=re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')
_DBURL=re.compile(r'(?i)(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^\s]+')
_SENSITIVE_Q=re.compile(r'(?i)([?&](?:token|access_token|refresh_token|reset_token|code|secret|key|password|passwd|api_key|apikey|session|session_id))=[^&#]*')
_VOLATILE=re.compile(r'\b(?:[0-9a-f]{8,}|\d{3,})\b',re.I)

class ErrorEvent(BaseModel):
    app:str=Field(default='web-app',max_length=120)
    environment:str=Field(default='production',max_length=40)
    severity:str=Field(default='error',max_length=20)
    error_type:str|None=Field(default=None,max_length=300)
    message:str=Field(max_length=5000)
    traceback:str|None=Field(default=None,max_length=30000)
    request_id:str|None=Field(default=None,max_length=200)
    method:str|None=Field(default=None,max_length=20)
    path:str|None=Field(default=None,max_length=2000)
    status_code:int|None=None
    user_id:str|None=Field(default=None,max_length=200)
    release:str|None=Field(default=None,max_length=200)
    host:str|None=Field(default=None,max_length=300)
    component:str|None=Field(default=None,max_length=200)
    operation:str|None=Field(default=None,max_length=300)
    category:str|None=Field(default=None,max_length=100)
    expected:bool=False
    context:dict[str,Any]=Field(default_factory=dict)

def iso():
    return datetime.now(timezone.utc).isoformat()

def sanitize_url(v):
    """Redact sensitive query-string values (tokens, secrets, etc.) from a URL/path."""
    try:
        redacted = _SENSITIVE_Q.sub(lambda m: f'{m.group(1)}=[REDACTED]', str(v or ''))
        return redacted[:2000]
    except Exception:
        return str(v or '')[:2000]

def redact(v, limit=30000):
    """Strip common secret shapes (JWTs, bearer tokens, DB URLs, key=value pairs) from text."""
    if v is None:
        return ''
    s = str(v)
    s = _JWT.sub('[REDACTED_JWT]', s)
    s = _BEARER.sub('Bearer [REDACTED]', s)
    s = _DBURL.sub('[REDACTED_DB_URL]', s)
    s = _ASSIGN.sub(lambda m: f'{m.group(1)}=[REDACTED]', s)
    return s[:limit]

def clean(v, key=''):
    """Recursively redact a context dict/list before it is persisted, sent to AI, or sent to Telegram."""
    if _SECRET_KEY.search(key):
        return '[REDACTED]'
    if isinstance(v, dict):
        return {str(k): clean(x, str(k)) for k, x in list(v.items())[:100]}
    if isinstance(v, (list, tuple)):
        return [clean(x, key) for x in list(v)[:100]]
    if isinstance(v, str):
        return redact(sanitize_url(v), 4000)
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return redact(v, 4000)

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
                            telegram_last_error TEXT
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

def auth(x_api_key:str|None=Header(default=None)):
    if not API_KEY:
        raise HTTPException(503,'ErrorBeacon is not configured with ERRORBEACON_API_KEY')
    if not x_api_key or not hmac.compare_digest(x_api_key,API_KEY):
        raise HTTPException(401,'Invalid API key')
    return True
def limited():
    now=time.time()
    with _rate_lock:
        _rate[:]=[x for x in _rate if x>now-60]
        if len(_rate)>=MAX_ALERTS:
            return False
        _rate.append(now)
        return True
def normalize(s):
    s=re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}','<id>',s,flags=re.I)
    s=re.sub(r'\b(?:req|request|trace|user|quote|quotation|asset)[_-]?[0-9a-f-]{6,}\b','<id>',s,flags=re.I)
    return _VOLATILE.sub('<n>',s).lower().strip()
def fingerprint(e):
    value = '|'.join(
        [
            e.app,
            e.environment,
            e.component or '',
            e.error_type or '',
            normalize(e.message),
            e.method or '',
            e.path or '',
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

def persist(e):
    """Fingerprint/dedupe an incoming event and write it to SQLite.

    Returns a 6-tuple: (incident_id, occurrence_count, should_alert, spike, regression, silenced).
    `should_alert` is only True the first time a spike crosses the threshold, or on a
    brand-new incident -- repeat occurrences within the dedup window are recorded but
    do not re-trigger delivery.
    """
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
        ai_status = 'pending' if AI_ENABLED and _providers() else 'disabled'
        c.execute(
            '''INSERT INTO incidents(
                id,created_at,app,environment,severity,error_type,message,traceback,request_id,
                method,path,status_code,user_id,release,host,component,operation,category,
                context_json,fingerprint,last_seen_at,spike_detected,deployment_regression,ai_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                incident_id, now, e.app, e.environment, classify(e), e.error_type,
                redact(e.message, 5000), redact(e.traceback), e.request_id, e.method,
                sanitize_url(e.path), e.status_code,
                redact(e.user_id, 200) if e.user_id else None,
                redact(e.release, 200) if e.release else None,
                redact(e.host, 300) if e.host else None,
                e.component, e.operation, e.category, context_json, fp, now,
                int(spike), int(regression), ai_status,
            ),
        )
        c.execute('UPDATE incident_events SET incident_id=? WHERE id=?', (incident_id, event_id))
        c.commit()
        c.close()
        return incident_id, 1, True, spike, regression, False

def normalize_ai_analysis(raw):
    """Normalize Gemini output into the four required operational sections."""
    if raw is None:
        return None
    s = str(raw).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return None
    s = re.sub(r'(?im)^\s*```(?:markdown|md|text)?\s*$', '', s)
    s = re.sub(r'(?im)^\s*```\s*$', '', s).strip()

    heading = re.compile(
        r'(?im)^\s*(?:[-*]\s*)?(?:#+\s*)?'
        r'(ROOT\s+CAUSE|IMPACT|NEXT\s+STEPS|CONFIDENCE)\s*:\s*'
    )
    matches = list(heading.finditer(s))
    sections = {}
    for idx, m in enumerate(matches):
        key = re.sub(r'\s+', ' ', m.group(1).upper())
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(s)
        value = s[start:end].strip()
        value = re.sub(r'^[:\-\s]+', '', value).strip()
        if value:
            sections[key] = value

    if not all(sections.get(k) for k in ('ROOT CAUSE', 'IMPACT', 'NEXT STEPS')):
        bare = re.compile(r'(?im)^\s*(?:[-*]\s*)?(?:#+\s*)?(ROOT\s+CAUSE|IMPACT|NEXT\s+STEPS|CONFIDENCE)\s*\*{0,2}\s*$')
        matches = list(bare.finditer(s))
        for idx, m in enumerate(matches):
            key = re.sub(r'\s+', ' ', m.group(1).upper())
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(s)
            value = re.sub(r'^[:\-\s]+', '', s[start:end].strip()).strip()
            if value:
                sections[key] = value

    required = ('ROOT CAUSE', 'IMPACT', 'NEXT STEPS')
    if not all(sections.get(k) for k in required):
        log.warning('Gemini analysis rejected: missing required sections; raw_length=%s headings=%s', len(s), list(sections))
        return None
    confidence = sections.get('CONFIDENCE', 'MEDIUM').strip().upper()
    confidence = re.sub(r'[^A-Z]', '', confidence.split()[0]) if confidence else 'MEDIUM'
    if confidence not in {'HIGH', 'MEDIUM', 'LOW'}:
        confidence = 'MEDIUM'
    result = (
        'ROOT CAUSE:\n' + sections['ROOT CAUSE'][:1600] +
        '\n\nIMPACT:\n' + sections['IMPACT'][:1200] +
        '\n\nNEXT STEPS:\n' + sections['NEXT STEPS'][:1600] +
        '\n\nCONFIDENCE:\n' + confidence
    ).strip()
    return result[:5000] if result else None

def _ai_prompt(e):
    return f"""Analyze this web application incident. Return ONLY these four sections, using these exact headings and nothing else:
ROOT CAUSE:
<concise factual explanation of the most likely technical cause>

IMPACT:
<what users, workflows, data, availability, or security are actually affected
distinguish confirmed impact from likely impact>

NEXT STEPS:
<3-5 concrete, prioritized troubleshooting or remediation actions>

CONFIDENCE:
<HIGH, MEDIUM, or LOW>

Be factual. Do not invent facts. Do not repeat secrets, credentials, tokens, authorization headers, or sensitive values. Use the supplied evidence only. If the root cause is uncertain, say so explicitly and lower confidence. Keep each section concise and operationally useful.
App: {redact(e.app,120)}
Environment: {redact(e.environment,40)}
Severity: {classify(e)}
Component: {redact(e.component,200)}
Operation: {redact(e.operation,300)}
Type: {redact(e.error_type,300)}
Message: {redact(e.message,4000)}
HTTP: {e.method or 'N/A'} {sanitize_url(e.path) if e.path else 'N/A'}{(' status=' + str(e.status_code)) if e.status_code is not None else ''}
Request ID: {redact(e.request_id,200)}
Release: {redact(e.release,200)}
Traceback:
{redact(e.traceback,18000)}
Context:
{json.dumps(clean(e.context),default=str)[:6000]}"""

class AIProviderError(RuntimeError):
    def __init__(self, provider, message, retry_after=None, rate_limited=False):
        super().__init__(message)
        self.provider=provider
        self.retry_after=retry_after
        self.rate_limited=rate_limited

def _retry_after(response):
    try:
        value=response.headers.get('Retry-After')
        if value:
            return max(1.0,float(value))
    except (TypeError,ValueError):
        pass
    m=re.search(r'retry in ([0-9]+(?:\.[0-9]+)?)s', response.text or '', re.I)
    return float(m.group(1)) if m else None

def _extract_ai_text(payload):
    candidates=payload.get('candidates') or []
    if candidates:
        return '\n'.join(str(part.get('text','')) for part in (candidates[0].get('content',{}).get('parts') or []) if part.get('text'))
    choices=payload.get('choices') or []
    if choices:
        return str((choices[0].get('message') or {}).get('content') or '')
    return ''

def _call_gemini(model,prompt):
    r=requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}',json={'contents':[{'parts':[{'text':prompt}]}],'generationConfig':{'temperature':0.1,'maxOutputTokens':850}},timeout=12)
    if not r.ok:
        if r.status_code==429:
            raise AIProviderError('gemini', 'Gemini HTTP 429', _retry_after(r) or 60, True)
        raise AIProviderError('gemini', f'Gemini HTTP {r.status_code}', None, r.status_code in {408,425,500,502,503,504})
    return _extract_ai_text(r.json())

def _call_openai_compatible(provider,url,key,model,prompt,extra_headers=None):
    h={'Authorization':f'Bearer {key}','Content-Type':'application/json'}
    if extra_headers:
        h.update(extra_headers)
    r=requests.post(url,headers=h,json={'model':model,'messages':[{'role':'user','content':prompt}],'temperature':0.1,'max_tokens':850},timeout=15)
    if not r.ok:
        if r.status_code==429:
            raise AIProviderError(provider, f'{provider} HTTP 429', _retry_after(r) or 60, True)
        raise AIProviderError(provider, f'{provider} HTTP {r.status_code}', None, r.status_code in {408,425,500,502,503,504})
    return _extract_ai_text(r.json())

def _providers():
    providers = []

    if GROQ_KEY:
        providers.append(
            (
                'groq',
                lambda prompt: _call_openai_compatible(
                    'groq',
                    'https://api.groq.com/openai/v1/chat/completions',
                    GROQ_KEY,
                    GROQ_MODEL,
                    prompt,
                ),
            )
        )

    if GEMINI_KEY:
        providers.append(
            ('gemini', lambda prompt: _call_gemini(GEMINI_MODEL, prompt))
        )

        if (
            GEMINI_FALLBACK_MODEL
            and GEMINI_FALLBACK_MODEL != GEMINI_MODEL
        ):
            providers.append(
                (
                    'gemini-fallback',
                    lambda prompt: _call_gemini(
                        GEMINI_FALLBACK_MODEL,
                        prompt,
                    ),
                )
            )

    if OPENROUTER_KEY:
        providers.append(
            (
                'openrouter',
                lambda prompt: _call_openai_compatible(
                    'openrouter',
                    'https://openrouter.ai/api/v1/chat/completions',
                    OPENROUTER_KEY,
                    OPENROUTER_MODEL,
                    prompt,
                    {
                        'HTTP-Referer': os.getenv(
                            'OPENROUTER_SITE_URL',
                            'https://errorbeacon.local',
                        ),
                        'X-Title': 'ErrorBeacon',
                    },
                ),
            )
        )

    return providers
def ai(e):
    if not AI_ENABLED:
        return None
    providers=_providers()
    if not providers:
        log.warning('AI analysis unavailable: no AI provider API keys configured')
        return None
    prompt=_ai_prompt(e)
    last=None
    for name,call in providers:
        try:
            raw=call(prompt)
            if not raw.strip():
                raise AIProviderError(name,f'{name} returned empty text')
            result=normalize_ai_analysis(raw)
            if not result:
                raise AIProviderError(name,f'{name} response did not contain usable analysis sections')
            log.info('%s analysis generated for request_id=%s',name,e.request_id)
            return result
        except AIProviderError as ex:
            last=ex
            log.warning('%s rate limited; failing over without retry' if ex.rate_limited else '%s analysis failed; failing over without retry',name)
        except Exception as ex:
            last=AIProviderError(name,type(ex).__name__)
            log.warning('%s analysis failed unexpectedly; failing over',name)
    if last:
        raise last
    return None

def ai_with_retry(e):
    """Try the configured provider chain once per queue attempt; never hammer a rate-limited provider."""
    if not AI_ENABLED or not _providers():
        return None
    attempts=max(1,AI_RETRIES)
    for attempt in range(1,attempts+1):
        try:
            return ai(e)
        except Exception as ex:
            if attempt>=attempts:
                raise
            delay=min(300.0,float(getattr(ex,'retry_after',None) or AI_RETRY_BASE))
            log.warning('AI provider chain exhausted on queue attempt %s/%s; retrying chain in %.1fs',attempt,attempts,delay)
            _stop.wait(delay)
            if _stop.is_set():
                return None
    return None

class TelegramDelivery:
    """Result of an outbound Telegram send.

    `sent` means Telegram acknowledged the message.
    `failed` means Telegram explicitly rejected it before accepting it.
    `unknown` means the transport outcome is ambiguous (for example a timeout
    after the request may already have reached Telegram). Unknown outcomes
    must never be automatically replayed because Telegram Bot API has no
    idempotency key for sendMessage.
    """
    __slots__ = ('status', 'payload', 'error')

    def __init__(self, status, payload=None, error=None):
        self.status = status
        self.payload = payload
        self.error = error

    @property
    def sent(self):
        return self.status == 'sent'

    @property
    def retryable(self):
        return self.status == 'failed'


def tg_delivery(method, text=None, markup=None, http_timeout=5, **kwargs):
    """Send to Telegram while preserving the delivery outcome.

    A transport timeout/connection error is *unknown*, not a normal failure.
    Retrying an unknown send can duplicate an alert because Telegram may have
    accepted the request before the client lost the response.
    """
    if not TG_TOKEN:
        log.error('Telegram %s skipped: TELEGRAM_BOT_TOKEN is not configured', method)
        return TelegramDelivery('failed', error='TelegramNotConfigured')
    if method == 'sendMessage' and not TG_CHAT:
        log.error('Telegram %s skipped: TELEGRAM_CHAT_ID is not configured', method)
        return TelegramDelivery('failed', error='TelegramNotConfigured')

    try:
        p = kwargs or (tg_payload(text, markup) if text is not None else {})
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/{method}',
            json=p,
            timeout=http_timeout,
        )
        # 4xx means Telegram explicitly rejected the request before accepting
        # it, so replaying is safe. A 5xx response is ambiguous because the
        # server may have processed the request before returning the error.
        if r.status_code >= 500:
            log.warning(
                'Telegram %s returned HTTP %s; delivery outcome unknown, automatic replay suppressed',
                method, r.status_code,
            )
            return TelegramDelivery('unknown', error=f'TelegramHTTP{r.status_code}')
        if r.status_code >= 400:
            try:
                payload = r.json()
            except ValueError:
                payload = None
            description = (
                payload.get('description', f'HTTP {r.status_code}')
                if isinstance(payload, dict) else f'HTTP {r.status_code}'
            )
            log.error(
                'Telegram %s rejected request: HTTP %s description=%s',
                method, r.status_code, redact(description, 500),
            )
            return TelegramDelivery(
                'failed',
                payload=payload if isinstance(payload, dict) else None,
                error=f'TelegramHTTP{r.status_code}',
            )
        payload = r.json()
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            description = (
                payload.get('description', 'unknown Telegram API error')
                if isinstance(payload, dict) else 'invalid Telegram response'
            )
            error_code = payload.get('error_code') if isinstance(payload, dict) else None
            log.error(
                'Telegram %s rejected request: error_code=%s description=%s',
                method, error_code, redact(description, 500),
            )
            return TelegramDelivery(
                'failed',
                payload=payload if isinstance(payload, dict) else None,
                error='TelegramAPIRejected',
            )
        return TelegramDelivery('sent', payload=payload)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as ex:
        # IMPORTANT: do not replay. The request may have reached Telegram and
        # only the response was lost.
        log.warning(
            'Telegram %s delivery outcome unknown: %s; automatic replay suppressed',
            method, type(ex).__name__,
        )
        return TelegramDelivery('unknown', error=type(ex).__name__)
    except requests.exceptions.RequestException as ex:
        log.warning(
            'Telegram %s transport outcome unknown: %s; automatic replay suppressed',
            method, type(ex).__name__,
        )
        return TelegramDelivery('unknown', error=type(ex).__name__)
    except Exception as ex:
        log.warning('Telegram %s failed unexpectedly: %s', method, type(ex).__name__)
        return TelegramDelivery('unknown', error=type(ex).__name__)


def tg_payload(text,markup=None):
    p={'chat_id':TG_CHAT,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
    if TG_THREAD:
        try:
            p['message_thread_id']=int(TG_THREAD)
        except ValueError:
            pass
    if markup:
        p['reply_markup']=markup
    return p
def tg(method,text=None,markup=None,http_timeout=5,**kwargs):
    """Backward-compatible Telegram helper for non-incident interactions."""
    result = tg_delivery(method, text, markup, http_timeout, **kwargs)
    return result.payload if result.sent else None


def keyboard(incident_id):
    return {
        'inline_keyboard': [
            [
                {
                    'text': '🔎 View',
                    'callback_data': f'view:{incident_id}',
                },
                {
                    'text': '✅ Resolve',
                    'callback_data': f'resolve:{incident_id}',
                },
                {
                    'text': '🔕 Silence 1h',
                    'callback_data': f'silence:{incident_id}:3600',
                },
            ]
        ]
    }
def telegram_message(e,i,occ,analysis=None,spike=False,regression=False):
    parts=[f'🚨 <b>ErrorBeacon {html.escape(classify(e).upper())}</b>']
    if e.category == 'chaos_test':
        parts.append('🧪 <b>CONTROLLED CHAOS TEST</b>')
    if e.category == 'dependency_degraded':
        parts.append('⚠️ <b>DEPENDENCY DEGRADED · FAIL-OPEN</b>')
    if spike:
        parts.append('🔥 <b>ERROR SPIKE DETECTED</b>')
    if regression:
        parts.append('⚠️ <b>POSSIBLE DEPLOYMENT REGRESSION</b>')
    parts += [
        f'<b>App:</b> {html.escape(e.app[:120])} · <b>Env:</b> {html.escape(e.environment[:40])}',
        f'<b>Exception:</b> <code>{html.escape((e.error_type or "Unknown")[:300])}</code>',
        f'<b>Message:</b> {html.escape(redact(e.message,1200))}',
    ]
    if e.component:
        parts.append(f'<b>Component:</b> {html.escape(e.component[:200])}')
    if e.operation:
        parts.append(f'<b>Operation:</b> {html.escape(e.operation[:300])}')
    if e.status_code is not None:
        parts.append(f'<b>HTTP:</b> {e.status_code} {html.escape(e.method or "")} {html.escape(sanitize_url(e.path)[:500])}')
    if e.request_id:
        parts.append(f'<b>Request ID:</b> <code>{html.escape(e.request_id[:200])}</code>')
    if e.release:
        parts.append(f'<b>Release:</b> <code>{html.escape(e.release[:200])}</code>')
    parts += [f'<b>Occurrences:</b> {occ}', f'<b>Incident:</b> <code>{html.escape(i)}</code>']
    if analysis and str(analysis).strip():
        parts.append('\n<b>🤖 AI Analysis</b>\n'+html.escape(redact(analysis,1700)))
    if e.traceback:
        parts.append('\n<b>Traceback</b>\n<pre>'+html.escape(redact(e.traceback,1300))+'</pre>')
    text='\n'.join(parts)
    if len(text)>4080 and e.traceback:
        text='\n'.join(parts[:-1])
    if len(text)>4080 and analysis:
        ai_part='\n<b>🤖 AI Analysis</b>\n'+html.escape(redact(analysis,900))
        base='\n'.join(parts[:(-2 if e.traceback else -1)])
        text=base+'\n'+ai_part
    if len(text)>4080:
        text=text[:4050]+'\n…'
    return text
def silenced(i):
    with _db_lock:
        c=db()
        r=c.execute('SELECT silenced_until FROM incidents WHERE id=?',(i,)).fetchone()
        c.close()
        return bool(r and r['silenced_until'] and datetime.fromisoformat(r['silenced_until']).timestamp()>time.time())

def _event_from_row(x):
    try:
        context=json.loads(x['context_json'] or '{}')
    except Exception:
        context={}
    return ErrorEvent(app=x['app'],environment=x['environment'],severity=x['severity'],error_type=x['error_type'],message=x['message'],traceback=x['traceback'],request_id=x['request_id'],method=x['method'],path=x['path'],status_code=x['status_code'],user_id=x['user_id'],release=x['release'],host=x['host'],component=x['component'],operation=x['operation'],category=x['category'],context=context)

def _ai_claim(i):
    with _ai_state_lock:
        if i in _ai_enqueued:
            return False
        _ai_enqueued.add(i)
        return True

def _ai_release(i):
    with _ai_state_lock:
        _ai_enqueued.discard(i)

def _ai_mark_retry(i, error):
    now=datetime.now(timezone.utc)
    with _db_lock:
        c=db()
        row=c.execute('SELECT ai_attempts FROM incidents WHERE id=?',(i,)).fetchone()
        attempts=int(row['ai_attempts'] or 0)+1 if row else 1
        if attempts >= AI_MAX_INCIDENT_RETRIES:
            c.execute('UPDATE incidents SET ai_status=?,ai_attempts=?,ai_next_retry_at=NULL,ai_last_error=? WHERE id=?',('failed',attempts,type(error).__name__[:200],i))
            c.commit()
            c.close()
            _ai_release(i)
            log.error('AI analysis permanently failed after %s queue attempts for incident %s',attempts,i)
            return
        provider_delay=getattr(error,'retry_after',None)
        delay=min(900.0, float(provider_delay)) if provider_delay is not None else min(300.0, AI_RETRY_BASE * (2 ** min(max(attempts-1,0),6)))
        nxt=(now+timedelta(seconds=delay)).isoformat()
        c.execute('UPDATE incidents SET ai_status=?,ai_attempts=?,ai_next_retry_at=?,ai_last_error=? WHERE id=?',('pending',attempts,nxt,type(error).__name__[:200],i))
        c.commit()
        c.close()
    _ai_release(i)

def _save_ai_and_notify(e,i,occ,spike,regression,a):
    if not a:
        return False
    with _db_lock:
        c=db()
        c.execute(
            'UPDATE incidents SET ai_analysis=?,ai_status=?,ai_next_retry_at=NULL,ai_last_error=NULL WHERE id=?',
            (redact(a,5000),'telegram_pending',i),
        )
        c.commit()
        c.close()
    _ai_release(i)
    if silenced(i):
        with _db_lock:
            c=db()
            c.execute('UPDATE incidents SET ai_status=? WHERE id=?',('complete',i))
            c.commit()
            c.close()
        return False

    result = tg_delivery(
        'sendMessage',
        telegram_message(e,i,occ,a,spike,regression),
        keyboard(i),
    )
    with _db_lock:
        c=db()
        if result.sent:
            c.execute(
                'UPDATE incidents SET ai_status=?,ai_next_retry_at=NULL,ai_last_error=NULL WHERE id=?',
                ('complete',i),
            )
        elif result.status == 'unknown':
            # Telegram may already have accepted this exact AI notification.
            # Never automatically replay an ambiguous send.
            c.execute(
                'UPDATE incidents SET ai_status=?,ai_next_retry_at=NULL,ai_last_error=? WHERE id=?',
                ('telegram_unknown', result.error or 'TelegramDeliveryOutcomeUnknown', i),
            )
            log.error(
                'Telegram AI delivery outcome unknown for incident %s; automatic replay suppressed',
                i,
            )
        else:
            nxt=(datetime.now(timezone.utc)+timedelta(seconds=15)).isoformat()
            c.execute(
                'UPDATE incidents SET ai_status=?,ai_next_retry_at=?,ai_last_error=? WHERE id=?',
                ('telegram_pending',nxt,result.error or 'TelegramDeliveryError',i),
            )
        c.commit()
        c.close()
    return result.sent

def _retry_ai_telegram(x):
    e=_event_from_row(x)
    a=x['ai_analysis']
    if not a or silenced(x['id']):
        with _db_lock:
            c=db()
            c.execute('UPDATE incidents SET ai_status=? WHERE id=?',('complete',x['id']))
            c.commit()
            c.close()
        return

    result = tg_delivery(
        'sendMessage',
        telegram_message(
            e,x['id'],x['occurrence_count'],a,
            bool(x['spike_detected']),bool(x['deployment_regression']),
        ),
        keyboard(x['id']),
    )
    with _db_lock:
        c=db()
        if result.sent:
            c.execute(
                'UPDATE incidents SET ai_status=?,ai_next_retry_at=NULL,ai_last_error=NULL WHERE id=?',
                ('complete',x['id']),
            )
        elif result.status == 'unknown':
            c.execute(
                'UPDATE incidents SET ai_status=?,ai_next_retry_at=NULL,ai_last_error=? WHERE id=?',
                ('telegram_unknown', result.error or 'TelegramDeliveryOutcomeUnknown', x['id']),
            )
            log.error(
                'Telegram AI recovery delivery outcome unknown for incident %s; automatic replay suppressed',
                x['id'],
            )
        else:
            c.execute(
                'UPDATE incidents SET ai_next_retry_at=?,ai_last_error=? WHERE id=?',
                (
                    (datetime.now(timezone.utc)+timedelta(seconds=15)).isoformat(),
                    result.error or 'TelegramDeliveryError',
                    x['id'],
                ),
            )
        c.commit()
        c.close()


def ai_worker():
    while not _stop.is_set():
        try:
            e,i,occ,spike,regression=_ai_jobs.get(timeout=.5)
        except queue.Empty:
            continue
        try:
            a=ai_with_retry(e)
            if a:
                _save_ai_and_notify(e,i,occ,spike,regression,a)
            else:
                _ai_mark_retry(i, RuntimeError('AI analysis unavailable or rejected'))
        except Exception as ex:
            log.exception('AI analysis worker failed')
            _ai_mark_retry(i,ex)
        finally:
            _ai_jobs.task_done()

def enqueue_ai(e,i,occ,spike,regression):
    if not AI_ENABLED or not _providers():
        return False
    if not _ai_claim(i):
        return False
    try:
        _ai_jobs.put_nowait((e,i,occ,spike,regression))
        return True
    except queue.Full:
        _ai_release(i)
        log.error('AI analysis queue full; incident %s remains pending for durable retry',i)
        return False

def telegram_recovery_loop():
    while not _stop.is_set():
        try:
            now=iso()
            with _db_lock:
                c=db()
                rows=c.execute(
                    "SELECT * FROM incidents WHERE telegram_status='pending' AND (telegram_next_retry_at IS NULL OR telegram_next_retry_at<=?) AND resolved=0 ORDER BY created_at ASC LIMIT 25",
                    (now,)
                ).fetchall()
                c.close()
            for x in rows:
                e=_event_from_row(x)
                _send_incident_telegram(e,x['id'],x['occurrence_count'],bool(x['spike_detected']),bool(x['deployment_regression']))
        except Exception:
            log.exception('Telegram recovery loop failed')
        _stop.wait(5)

def ai_recovery_loop():
    # Durable retry: an incident remains pending in SQLite if Gemini, Telegram,
    # the process, or the in-memory queue fails. This loop rehydrates pending work.
    while not _stop.is_set():
        try:
            now=iso()
            with _db_lock:
                c=db()
                rows=c.execute("SELECT * FROM incidents WHERE ai_status='pending' AND (ai_next_retry_at IS NULL OR ai_next_retry_at<=?) AND ai_analysis IS NULL ORDER BY created_at ASC LIMIT ?",(now,max(1,AI_Q//4))).fetchall()
            tg_rows=c.execute("SELECT * FROM incidents WHERE ai_status='telegram_pending' AND ai_analysis IS NOT NULL AND (ai_next_retry_at IS NULL OR ai_next_retry_at<=?) ORDER BY created_at ASC LIMIT ?",(now,max(1,AI_Q//4))).fetchall()
            c.close()
            for x in rows:
                e=_event_from_row(x)
                enqueue_ai(e,x['id'],x['occurrence_count'],bool(x['spike_detected']),bool(x['deployment_regression']))
            for x in tg_rows:
                _retry_ai_telegram(x)
        except Exception:
            log.exception('AI recovery loop failed')
        _stop.wait(5)

def _telegram_mark_failure(i, error):
    now=datetime.now(timezone.utc)
    with _db_lock:
        c=db()
        row=c.execute('SELECT telegram_attempts FROM incidents WHERE id=?',(i,)).fetchone()
        attempts=int(row['telegram_attempts'] or 0)+1 if row else 1
        delay=min(300, 5 * (2 ** min(attempts-1, 6)))
        nxt=(now+timedelta(seconds=delay)).isoformat()
        c.execute(
            'UPDATE incidents SET telegram_status=?,telegram_attempts=?,telegram_next_retry_at=?,telegram_last_error=? WHERE id=?',
            ('pending',attempts,nxt,type(error).__name__[:200],i),
        )
        c.commit()
        c.close()

def _telegram_mark_unknown(i, error):
    """Persist an ambiguous Telegram send and suppress automatic replay."""
    with _db_lock:
        c=db()
        c.execute(
            'UPDATE incidents SET telegram_status=?,telegram_next_retry_at=NULL,telegram_last_error=? WHERE id=?',
            ('unknown', str(error or 'TelegramDeliveryOutcomeUnknown')[:200], i),
        )
        c.commit()
        c.close()

def _telegram_mark_sent(i):
    with _db_lock:
        c=db()
        c.execute(
            'UPDATE incidents SET telegram_status=?,telegram_next_retry_at=NULL,telegram_last_error=NULL,telegram_sent=1 WHERE id=?',
            ('sent',i),
        )
        c.commit()
        c.close()

def _send_incident_telegram(e,i,occ,spike,regression):
    if silenced(i):
        return False

    result = tg_delivery(
        'sendMessage',
        telegram_message(e,i,occ,None,spike,regression),
        keyboard(i),
    )
    if result.sent:
        _telegram_mark_sent(i)
        return True
    if result.status == 'unknown':
        _telegram_mark_unknown(i, result.error)
        log.error(
            'Telegram immediate delivery outcome unknown for incident %s; automatic replay suppressed',
            i,
        )
        return False

    _telegram_mark_failure(i, RuntimeError(result.error or 'TelegramDeliveryError'))
    log.error(
        'Telegram immediate delivery explicitly rejected for incident %s; retry persisted',
        i,
    )
    return False


def alert(e,i,occ,spike,regression):
    """Deliver the immediate incident page and independently enqueue AI enrichment."""
    sent=_send_incident_telegram(e,i,occ,spike,regression)
    # AI must not depend on Telegram delivery. A Telegram outage or bad chat ID
    # must not prevent root-cause analysis from being generated and retried.
    enqueue_ai(e,i,occ,spike,regression)
    return sent

def worker():
    while not _stop.is_set():
        try:
            e,i,o,s,r=_jobs.get(timeout=.5)
        except queue.Empty:
            continue
        try:
            alert(e,i,o,s,r)
        except Exception:
            log.exception('Alert worker failed')
        finally:
            _jobs.task_done()

def enqueue(e,i,o,s,r):
    try:
        _jobs.put_nowait((e,i,o,s,r))
        return True
    except queue.Full:
        log.error('Alert queue full; dropped alert for %s',i)
        return False

def should_notify(e):

    if e.expected or e.category == 'healthcheck':
        return False
    if e.category == 'chaos_test':
        return CHAOS_TEST_ALERTS
    return classify(e) != 'info'

def process(e):
    i,o,should,s,r,sil=persist(e)
    return i,o,(enqueue(e,i,o,s,r) if should and should_notify(e) and not sil else False),sil

def incident_view(i):
    """Render the detail message shown when a Telegram user taps the 'View' button."""
    with _db_lock:
        c = db()
        x = c.execute('SELECT * FROM incidents WHERE id=?', (i,)).fetchone()
        c.close()
    if not x:
        return '❌ <b>Incident not found.</b>'

    lines = [
        '🔎 <b>ErrorBeacon Incident</b>',
        '',
        f'<b>ID:</b> <code>{x["id"]}</code>',
        f'<b>Status:</b> {"RESOLVED" if x["resolved"] else "OPEN"}',
        f'<b>Severity:</b> {x["severity"]}',
        f'<b>App:</b> {html.escape(x["app"])}',
        f'<b>Occurrences:</b> {x["occurrence_count"]}',
        f'<b>Request ID:</b> <code>{html.escape(x["request_id"] or "N/A")}</code>',
        f'<b>Path:</b> {html.escape(x["path"] or "N/A")[:500]}',
        f'<b>Release:</b> <code>{html.escape(x["release"] or "N/A")}</code>',
        f'<b>First seen:</b> {x["created_at"]}',
        f'<b>Last seen:</b> {x["last_seen_at"]}',
    ]
    return '\n'.join(lines)[:4000]
def callback(u):
    """Handle a Telegram inline-keyboard button tap (View / Resolve / Silence)."""
    query = u.get('callback_query') or {}
    message = query.get('message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    if str(chat_id) != str(TG_CHAT):
        return

    data = str(query.get('data') or '')
    callback_query_id = query.get('id')

    if data.startswith('view:'):
        incident_id = data.split(':', 1)[1]
        tg('answerCallbackQuery', callback_query_id=callback_query_id, text='Loading incident...')
        tg('sendMessage', incident_view(incident_id), keyboard(incident_id))
        return

    if data.startswith('resolve:'):
        incident_id = data.split(':', 1)[1]
        with _db_lock:
            c = db()
            cur = c.execute('UPDATE incidents SET resolved=1 WHERE id=?', (incident_id,))
            c.commit()
            c.close()
        reply_text = 'Incident resolved.' if cur.rowcount else 'Incident not found.'
        tg('answerCallbackQuery', callback_query_id=callback_query_id, text=reply_text)
        return

    if data.startswith('silence:'):
        _, incident_id, seconds_str = data.split(':')
        until = (datetime.now(timezone.utc) + timedelta(seconds=min(int(seconds_str), 86400))).isoformat()
        with _db_lock:
            c = db()
            cur = c.execute('UPDATE incidents SET silenced_until=? WHERE id=?', (until, incident_id))
            c.commit()
            c.close()
        reply_text = 'Silenced for 1 hour.' if cur.rowcount else 'Incident not found.'
        tg('answerCallbackQuery', callback_query_id=callback_query_id, text=reply_text)
def telegram_loop():
    offset=0
    while not _stop.is_set():
        r=tg('getUpdates',http_timeout=TG_POLL_SECONDS+5,timeout=TG_POLL_SECONDS,offset=offset,allowed_updates=['callback_query'])
        if not r or not r.get('ok'):
            _stop.wait(3)
            continue
        for u in r.get('result',[]):
            offset=int(u.get('update_id',offset))+1
            try:
                callback(u)
            except Exception:
                log.exception('Telegram callback failed')
@asynccontextmanager
async def lifespan(_):
    init_db()
    _stop.clear()
    _workers.clear()
    _ai_workers.clear()
    for n in range(ALERT_WORKERS):
        t=threading.Thread(target=worker,name=f'errorbeacon-alert-{n+1}',daemon=True)
        t.start()
        _workers.append(t)
    for n in range(AI_WORKERS):
        t=threading.Thread(target=ai_worker,name=f'errorbeacon-ai-{n+1}',daemon=True)
        t.start()
        _ai_workers.append(t)
    global _recovery_thread
    threading.Thread(target=telegram_recovery_loop,name='errorbeacon-telegram-recovery',daemon=True).start()
    _recovery_thread=threading.Thread(target=ai_recovery_loop,name='errorbeacon-ai-recovery',daemon=True)
    _recovery_thread.start()
    if TG_POLL and TG_TOKEN and TG_CHAT:
        threading.Thread(target=telegram_loop,name='errorbeacon-telegram',daemon=True).start()
    yield
    _stop.set()
    for t in _workers + _ai_workers:
        t.join(timeout=2)
    if _recovery_thread:
        _recovery_thread.join(timeout=2)
app=FastAPI(title='ErrorBeacon Lite',version=APP_VERSION,lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
@app.get('/healthz')
def healthz():
    if not DB_PATH.exists():
        init_db()
    with _db_lock:
        c=db()
        pending=c.execute("SELECT COUNT(*) FROM incidents WHERE ai_status='pending' AND ai_analysis IS NULL").fetchone()[0]
        c.close()
    return {'status':'ok','service':'errorbeacon','version':APP_VERSION,'queue_depth':_jobs.qsize(),'ai_queue_depth':_ai_jobs.qsize(),'ai_pending':pending,'ai_enabled':bool(AI_ENABLED and _providers()),'ai_providers':[p[0] for p in _providers()],'telegram_configured':bool(TG_TOKEN and TG_CHAT)}
@app.get('/')
def root():
    return {
        'service': 'ErrorBeacon Lite',
        'version': APP_VERSION,
        'docs': '/docs',
        'health': '/healthz',
    }
@app.post('/v1/events',dependencies=[Depends(auth)])
def ingest(event:ErrorEvent):
    if not limited():
        raise HTTPException(429,'Alert rate limit exceeded')
    try:
        i,o,q,s=process(event)
        return {'accepted':True,'queued':q,'incident_id':i,'occurrence':o,'request_id':request_id_var.get(),'silenced':s}
    except Exception:
        log.exception('Event ingestion failed')
        raise HTTPException(500,'Event ingestion failed')
@app.post('/v1/test',dependencies=[Depends(auth)])
def test_alert(request:Request):
    rid=request_id_var.get() or request.headers.get('x-request-id') or str(uuid.uuid4())
    # Keep controlled tests independently addressable so a previous test incident cannot
    # suppress the next manual Telegram test through normal fingerprint deduplication.
    e=ErrorEvent(
        app='errorbeacon-test',
        environment=os.getenv('ENVIRONMENT','development'),
        severity='warning',
        error_type='TestException',
        message='This is a controlled ErrorBeacon test alert.',
        request_id=rid,
        method=request.method,
        path=f'/errorbeacon-test/{rid}',
        status_code=500,
        category=None
    )
    i,o,q,s=process(e)
    return {'ok':True,'incident_id':i,'queued':q,'request_id':rid,'silenced':s}
@app.get('/v1/incidents',dependencies=[Depends(auth)])
def incidents(limit:int=50):
    limit=max(1,min(limit,200))
    with _db_lock:
        c=db()
        rows=c.execute('SELECT id,created_at,app,environment,severity,error_type,message,request_id,path,status_code,release,component,operation,occurrence_count,last_seen_at,resolved,telegram_sent,ai_analysis,spike_detected,deployment_regression,silenced_until FROM incidents ORDER BY last_seen_at DESC LIMIT ?',(limit,)).fetchall()
        c.close()
    return [dict(r) for r in rows]
@app.post('/v1/incidents/{incident_id}/resolve',dependencies=[Depends(auth)])
def resolve(incident_id:str):
    with _db_lock:
        c=db()
        cur=c.execute('UPDATE incidents SET resolved=1 WHERE id=?',(incident_id,))
        c.commit()
        c.close()
    if not cur.rowcount:
        raise HTTPException(404,'Incident not found')
    return {'resolved':True,'incident_id':incident_id}
@app.post('/v1/incidents/{incident_id}/silence',dependencies=[Depends(auth)])
def silence(incident_id:str,seconds:int=3600):
    seconds=max(60,min(seconds,86400))
    until=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
    with _db_lock:
        c=db()
        cur=c.execute('UPDATE incidents SET silenced_until=? WHERE id=?',(until,incident_id))
        c.commit()
        c.close()
    if not cur.rowcount:
        raise HTTPException(404,'Incident not found')
    return {'silenced':True,'incident_id':incident_id,'silenced_until':until}
