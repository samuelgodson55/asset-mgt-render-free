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
from typing import Any, Iterable
from email.message import EmailMessage
import smtplib
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

APP_VERSION = '4.1.3'
DATA_DIR = Path(os.getenv('DATA_DIR', '/data'))
DB_PATH = DATA_DIR / 'errorbeacon.db'
INGEST_API_KEY = os.getenv('ERRORBEACON_INGEST_API_KEY', '')
ADMIN_API_KEY = os.getenv('ERRORBEACON_ADMIN_API_KEY', '')
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
MAX_REQUEST_BODY_BYTES = max(16 * 1024, int(os.getenv('ERRORBEACON_MAX_REQUEST_BODY_BYTES', '131072')))
ADMIN_AUTH_FAILURES_PER_MINUTE = max(1, int(os.getenv('ERRORBEACON_ADMIN_AUTH_FAILURES_PER_MINUTE', '10')))
TRUST_PROXY_HEADERS = os.getenv('ERRORBEACON_TRUST_PROXY_HEADERS', 'false').lower() in {'1', 'true', 'yes', 'on'}
ENABLE_DOCS = os.getenv('ERRORBEACON_ENABLE_DOCS', 'false' if os.getenv('ENVIRONMENT', 'production').lower() in {'prod','production'} else 'true').lower() in {'1','true','yes','on'}
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
TEST_ALERTS_PER_MINUTE = max(1, int(os.getenv('TEST_ALERTS_PER_MINUTE', '3')))
RETENTION_DAYS = max(1, int(os.getenv('ERRORBEACON_RETENTION_DAYS', '90')))
RETENTION_INTERVAL_SECONDS = max(300, int(os.getenv('ERRORBEACON_RETENTION_INTERVAL_SECONDS', '3600')))
AUTO_STALE_DAYS = max(1, int(os.getenv('ERRORBEACON_AUTO_STALE_DAYS', '30')))
DIGEST_ENABLED = os.getenv('ERRORBEACON_DIGEST_ENABLED', 'true').lower() in {'1', 'true', 'yes', 'on'}
DIGEST_INTERVAL_HOURS = max(1, int(os.getenv('ERRORBEACON_DIGEST_INTERVAL_HOURS', '24')))
DB_WARN_MB = max(1, float(os.getenv('ERRORBEACON_DB_WARN_MB', '4096')))
EMAIL_FALLBACK_ENABLED = os.getenv('ERRORBEACON_EMAIL_FALLBACK_ENABLED', 'true').lower() in {'1','true','yes','on'}
EMAIL_FALLBACK_AFTER_ATTEMPTS = max(1, int(os.getenv('ERRORBEACON_EMAIL_FALLBACK_AFTER_ATTEMPTS', '3')))
EMAIL_FALLBACK_AFTER_SECONDS = max(60, int(os.getenv('ERRORBEACON_EMAIL_FALLBACK_AFTER_SECONDS', '300')))
EMAIL_FALLBACK_RETRIES = max(1, int(os.getenv('ERRORBEACON_EMAIL_FALLBACK_RETRIES', '3')))
NOTIFICATIONS_ENABLED = os.getenv('NOTIFICATIONS_ENABLED', 'true').lower() in {'1','true','yes','on'}
EMAIL_PROVIDER = os.getenv('EMAIL_PROVIDER', 'smtp').strip().lower()
SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USERNAME = os.getenv('SMTP_USERNAME', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SMTP_USE_TLS = os.getenv('SMTP_USE_TLS', 'true').lower() in {'1','true','yes','on'}
SMTP_USE_SSL = os.getenv('SMTP_USE_SSL', 'false').lower() in {'1','true','yes','on'}
SMTP_FROM_EMAIL = os.getenv('SMTP_FROM_EMAIL', '')
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '')
RESEND_API_KEY = os.getenv('RESEND_API_KEY', '')
ADMIN_NOTIFICATION_EMAILS = os.getenv('ADMIN_NOTIFICATION_EMAILS', '')
EMAIL_TIMEOUT_SECONDS = max(3, int(os.getenv('ERRORBEACON_EMAIL_TIMEOUT_SECONDS', '10')))
STARTUP_SELF_TEST = os.getenv('ERRORBEACON_STARTUP_SELF_TEST', 'false').lower() in {'1', 'true', 'yes', 'on'}
from contextvars import ContextVar
from starlette.types import ASGIApp, Receive, Scope, Send
from shared.errorbeacon_sanitization import clean as _shared_clean, redact, sanitize_url

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

class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers', []))
        content_length = headers.get(b'content-length')
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    body = (f'Request body too large; maximum is {self.max_bytes} bytes').encode()
                    await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'text/plain'),(b'content-length',str(len(body)).encode())]})
                    await send({'type':'http.response.body','body':body})
                    return
            except ValueError:
                pass

        total = 0
        finished = False
        async def limited_receive():
            nonlocal total, finished
            message = await receive()
            if message.get('type') == 'http.request':
                chunk = message.get('body', b'') or b''
                total += len(chunk)
                if total > self.max_bytes:
                    finished = True
                    raise ValueError('request body too large')
                if not message.get('more_body', False):
                    finished = True
            return message
        try:
            await self.app(scope, limited_receive, send)
        except ValueError as exc:
            if str(exc) == 'request body too large' and not finished:
                body = (f'Request body too large; maximum is {self.max_bytes} bytes').encode()
                await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'text/plain'),(b'content-length',str(len(body)).encode())]})
                await send({'type':'http.response.body','body':body})
            elif str(exc) == 'request body too large':
                body = (f'Request body too large; maximum is {self.max_bytes} bytes').encode()
                try:
                    await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'text/plain'),(b'content-length',str(len(body)).encode())]})
                    await send({'type':'http.response.body','body':body})
                except RuntimeError:
                    pass
            else:
                raise

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
_admin_fail_lock=threading.Lock()
_admin_failures={}
_ai_state_lock=threading.Lock()
_rate=[]
_test_rate=[]
_test_rate_lock=threading.Lock()
_jobs=queue.Queue(maxsize=ALERT_Q)
_ai_jobs=queue.Queue(maxsize=AI_Q)
_stop=threading.Event()
_workers=[]
_ai_workers=[]
_ai_enqueued=set()
_recovery_thread=None
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

CLEAN_MAX_DEPTH = max(1, int(os.getenv('ERRORBEACON_CLEAN_MAX_DEPTH', '10')))
CLEAN_MAX_ITEMS = max(1, int(os.getenv('ERRORBEACON_CLEAN_MAX_ITEMS', '100')))

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
                            resolved_reason TEXT
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

def _client_ip_from_request(request: Request) -> str:
    # Proxy headers are attacker-controlled unless this service is known to
    # sit behind a trusted ingress/reverse proxy that overwrites them. Render
    # Free exposes ErrorBeacon publicly, so the safe default is to use the
    # immediate peer address. Controlled ACA deployments may opt in explicitly
    # with ERRORBEACON_TRUST_PROXY_HEADERS=true.
    if TRUST_PROXY_HEADERS:
        xff = request.headers.get('x-forwarded-for')
        if xff:
            return xff.split(',', 1)[0].strip()[:128] or 'unknown'
        forwarded = request.headers.get('x-real-ip')
        if forwarded:
            return forwarded.strip()[:128] or 'unknown'
    return request.client.host if request.client else 'unknown'

def _check_api_key(x_api_key: str | None, configured: str, label: str) -> bool:
    if not configured:
        raise HTTPException(503, f'ErrorBeacon is not configured with {label}')
    if not x_api_key or not hmac.compare_digest(x_api_key, configured):
        raise HTTPException(401, 'Invalid API key')
    return True

def ingest_auth(x_api_key: str | None = Header(default=None)):
    return _check_api_key(x_api_key, INGEST_API_KEY, 'ERRORBEACON_INGEST_API_KEY')

def admin_auth(request: Request, x_api_key: str | None = Header(default=None)):
    ip = _client_ip_from_request(request)
    now = time.time()
    with _admin_fail_lock:
        bucket = _admin_failures.setdefault(ip, [])
        bucket[:] = [t for t in bucket if t > now - 60]
        if len(bucket) >= ADMIN_AUTH_FAILURES_PER_MINUTE:
            raise HTTPException(429, 'Too many invalid admin authentication attempts; try again later')
    try:
        return _check_api_key(x_api_key, ADMIN_API_KEY, 'ERRORBEACON_ADMIN_API_KEY')
    except HTTPException as exc:
        if exc.status_code == 401:
            with _admin_fail_lock:
                bucket = _admin_failures.setdefault(ip, [])
                bucket.append(now)
                if len(_admin_failures) > 5000:
                    oldest = sorted(_admin_failures.items(), key=lambda item: max(item[1]) if item[1] else 0)[:500]
                    for key, _ in oldest:
                        _admin_failures.pop(key, None)
        raise

def auth(request: Request, x_api_key: str | None = Header(default=None)):
    return admin_auth(request, x_api_key)
def limited():
    now=time.time()
    with _rate_lock:
        _rate[:]=[x for x in _rate if x>now-60]
        if len(_rate)>=MAX_ALERTS:
            return False
        _rate.append(now)
        return True

def limited_test():
    now=time.time()
    with _test_rate_lock:
        _test_rate[:]=[x for x in _test_rate if x>now-60]
        if len(_test_rate)>=TEST_ALERTS_PER_MINUTE:
            return False
        _test_rate.append(now)
        return True
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

def _notify_ai_failure(e,i,occ,error):
    with _db_lock:
        c=db(); row=c.execute('SELECT * FROM incidents WHERE id=?',(i,)).fetchone(); c.close()
    if not row or row['resolved'] or silenced(i): return
    result=tg_delivery('sendMessage',telegram_message(e,i,occ)+ '\n\n❌ <b>AI enrichment permanently failed</b>\n<code>'+html.escape(str(error or 'unknown')[:300])+'</code>',keyboard(i))
    if result.sent: return
    if row['ai_failure_email_sent_at'] or not email_configured(): return
    if send_email(email_recipients(),f'ErrorBeacon AI failure: {classify(e).upper()} · {e.app} · {i}',email_incident_body(e,i,occ,reason='Telegram could not deliver the AI failure notification',ai_failure=True,ai_error=error)):
        with _db_lock:
            c=db(); c.execute('UPDATE incidents SET ai_failure_email_sent_at=? WHERE id=?',(iso(),i)); c.commit(); c.close()

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
    with _db_lock:
        c=db(); row=c.execute('SELECT resolved,silenced_until FROM incidents WHERE id=?',(incident_id,)).fetchone(); c.close()
    if not row: return {'inline_keyboard':[[{'text':'🔎 View','callback_data':f'view:{incident_id}'}]]}
    buttons=[{'text':'🔎 View','callback_data':f'view:{incident_id}'}]
    if row['resolved']:
        buttons.append({'text':'↩️ Reopen','callback_data':f'reopen:{incident_id}'})
    else:
        buttons.append({'text':'✅ Resolve','callback_data':f'resolve:{incident_id}'})
        active=bool(row['silenced_until'] and datetime.fromisoformat(row['silenced_until']).timestamp()>time.time())
        if active: buttons.append({'text':'🔔 Unsilence','callback_data':f'unsilence:{incident_id}'})
        else:
            buttons.extend([{'text':'🔕 1h','callback_data':f'silence:{incident_id}:3600'},{'text':'🔕 4h','callback_data':f'silence:{incident_id}:14400'},{'text':'🔕 24h','callback_data':f'silence:{incident_id}:86400'}])
    return {'inline_keyboard':[buttons[:4],buttons[4:]] if len(buttons)>4 else [buttons]}

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
            try:
                with _db_lock:
                    c2=db(); incident_row=c2.execute('SELECT * FROM incidents WHERE id=?',(i,)).fetchone(); c2.close()
                if incident_row: _notify_ai_failure(_event_from_row(incident_row),i,incident_row['occurrence_count'],type(error).__name__)
            except Exception: log.exception('AI permanent-failure notification failed for incident %s',i)
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

def _authorized_telegram_update(u):
    message=u.get('message') or u.get('callback_query',{}).get('message') or {}; return str((message.get('chat') or {}).get('id'))==str(TG_CHAT)

def _incidents_summary(limit=10):
    with _db_lock:
        c=db(); rows=c.execute('SELECT id,severity,app,message,occurrence_count,resolved,telegram_status,fallback_status,ai_status FROM incidents ORDER BY last_seen_at DESC LIMIT ?',(limit,)).fetchall(); c.close()
    if not rows: return 'No incidents found.'
    lines=['📋 <b>Recent ErrorBeacon incidents</b>','']
    for r in rows:
        status='RESOLVED' if r['resolved'] else 'OPEN'; lines.append(f'<code>{r["id"]}</code> · <b>{html.escape(r["severity"].upper())}</b> · {status} · {html.escape(r["app"])} · {r["occurrence_count"]}x'); lines.append(f'{html.escape(redact(r["message"],300))} · TG:{r["telegram_status"]} · Email:{r["fallback_status"]} · AI:{r["ai_status"]}')
    return '\n'.join(lines)[:4000]

def _telegram_help():
    return '\n'.join([
        '🛠️ <b>ErrorBeacon commands</b>',
        '',
        '<b>Monitoring</b>',
        '/health — service health, queues, DB and email diagnostics',
        '/incidents — recent incidents with status and delivery state',
        '/incident &lt;id&gt; — full detail for one incident',
        '/stats [window] — aggregate stats; e.g. /stats 24h or /stats 7d',
        '',
        '<b>Triage</b>',
        '/resolve &lt;id&gt; — resolve an incident',
        '/reopen &lt;id&gt; — reopen an incident',
        '/silence &lt;id&gt; &lt;duration&gt; — e.g. 30m, 1h, 24h',
        '/unsilence &lt;id&gt; — remove an incident silence',
        '',
        '<b>Delivery tests</b>',
        '/test — create a controlled test incident and exercise the alert pipeline',
        '/testtelegram — send a direct Telegram delivery test',
        '/testemail — send a direct email delivery test',
        '',
        '/help — show this command list',
        '',
        '🔒 All commands are restricted to the configured Telegram chat.'
    ])

def _telegram_test_incident():
    rid=str(uuid.uuid4())
    e=ErrorEvent(app='errorbeacon-test',environment=os.getenv('ENVIRONMENT','production'),severity='warning',error_type='TestException',message='This is a controlled ErrorBeacon Telegram test incident.',request_id=rid,method='TELEGRAM',path=f'/telegram-test/{rid}',status_code=500,category=None)
    i,o,q,s=process(e)
    return f'🧪 <b>Test incident created</b>\nIncident: <code>{html.escape(str(i))}</code>\nOccurrence: {o}\nQueued: {q}\nSilenced: {s}\n\nThe normal Telegram/AI/email pipeline has been exercised.'

def _telegram_stats(window='24h'):
    w=window.strip().lower() or '24h'
    if not re.fullmatch(r'\d+(m|h|d|w)',w):
        raise ValueError('window must look like 30m, 24h, 7d, or 1w')
    stats=get_stats(w)
    delivery=stats['delivery']
    return '\n'.join([
        f'📊 <b>ErrorBeacon stats ({html.escape(w)})</b>',
        f'Incidents: {stats["incidents"]}',
        f'Unresolved: {stats["unresolved"]}',
        f'Spikes: {stats["spike_count"]}',
        f'Regressions: {stats["deployment_regression_count"]}',
        f'Telegram success: {delivery["telegram"]["success_rate_percent"] if delivery["telegram"]["success_rate_percent"] is not None else "n/a"}%',
        f'Email fallback success: {delivery["email_fallback"]["success_rate_percent"] if delivery["email_fallback"]["success_rate_percent"] is not None else "n/a"}%',
        f'AI completion: {delivery["ai"]["completion_rate_percent"] if delivery["ai"]["completion_rate_percent"] is not None else "n/a"}%'
    ])

def handle_telegram_command(text):
    p=text.strip().split(); cmd=(p[0].split('@',1)[0].lower() if p else '')
    try:
        if cmd=='/help' or cmd=='/start': return _telegram_help()
        if cmd=='/health':
            h=healthz(); return '\n'.join(['💚 <b>ErrorBeacon health</b>',f'Status: {h["status"]}',f'Telegram configured: {h["telegram_configured"]}',f'Email fallback configured: {h["email_fallback_configured"]}',f'Email provider: {h["email_provider"]}',f'Email missing: {", ".join(h["email"]["missing"]) or "none"}',f'DB: {h["db_size_mb"]:.1f} MB',f'AI queue: {h["ai_queue_depth"]}',f'AI pending: {h["ai_pending"]}'])
        if cmd=='/incidents': return _incidents_summary()
        if cmd=='/incident':
            if len(p)!=2: return 'Usage: /incident <incident_id>'
            return incident_view(p[1])
        if cmd=='/stats': return _telegram_stats(p[1] if len(p)>1 else '24h')
        if cmd=='/test': return _telegram_test_incident()
        if cmd=='/testtelegram':
            result=tg_delivery('sendMessage','🧪 <b>ErrorBeacon Telegram delivery test</b>\n\nTelegram delivery is working.')
            return '✅ Telegram test succeeded.' if result.sent else f'❌ Telegram test failed: {html.escape(result.error or "unknown error")}'
        if cmd=='/testemail':
            diagnostics=_email_diagnostics()
            if not diagnostics['configured']:
                return '❌ Email test unavailable. Missing: '+', '.join(diagnostics['missing'])
            ok=send_email(email_recipients(),'ErrorBeacon email test','This is an administrative ErrorBeacon email delivery test. No incident was generated.')
            return '✅ Email test succeeded.' if ok else '❌ Email test failed. Check the ErrorBeacon logs and SMTP configuration.'
        if cmd in {'/resolve','/reopen','/unsilence'}:
            if len(p)!=2: return f'Usage: {cmd} <incident_id>'
            sql={'/resolve':'UPDATE incidents SET resolved=1,resolved_at=?,resolved_reason=? WHERE id=?','/reopen':'UPDATE incidents SET resolved=0,resolved_at=NULL,resolved_reason=NULL WHERE id=?','/unsilence':'UPDATE incidents SET silenced_until=NULL WHERE id=?'}[cmd]
            params=(iso(),'manual',p[1]) if cmd=='/resolve' else (p[1],)
            return {'/resolve':'Incident resolved.','/reopen':'Incident reopened.','/unsilence':'Incident unsilenced.'}[cmd] if _incident_update(p[1],sql,params) else 'Incident not found.'
        if cmd=='/silence':
            if len(p)!=3: return 'Usage: /silence <incident_id> <duration> (e.g. 1h, 4h, 24h)'
            sec=_silence_seconds(p[2]); until=(datetime.now(timezone.utc)+timedelta(seconds=sec)).isoformat(); return f'Incident silenced for {_format_duration(sec)}.' if _incident_update(p[1],'UPDATE incidents SET silenced_until=? WHERE id=?',(until,p[1])) else 'Incident not found.'
    except ValueError as e: return '❌ '+html.escape(str(e))
    except Exception:
        log.exception('Telegram command failed: %s',cmd)
        return '❌ Something went wrong running that command. Check the server logs.'
    return 'Unknown command. Use /help to see all available commands.'

def _refresh_callback_message(q):
    message=q.get('message') or {}
    chat_id=(message.get('chat') or {}).get('id')
    message_id=message.get('message_id')
    if chat_id is None or message_id is None:
        return
    incident_id=None
    data=str(q.get('data') or '')
    if ':' in data:
        incident_id=data.split(':',2)[1]
    if incident_id:
        tg('editMessageReplyMarkup',chat_id=chat_id,message_id=message_id,reply_markup=keyboard(incident_id))

def callback(u):
    if not _authorized_telegram_update(u):
        chat_id=((u.get('callback_query') or {}).get('message') or {}).get('chat',{}).get('id')
        log.warning('Ignored Telegram callback_query from unauthorized chat_id=%s',chat_id)
        return
    q=u.get('callback_query') or {}; data=str(q.get('data') or ''); qid=q.get('id')
    if data.startswith('view:'):
        i=data.split(':',1)[1]; tg('answerCallbackQuery',callback_query_id=qid,text='Loading incident...'); tg('sendMessage',incident_view(i),keyboard(i)); return
    if data.startswith('resolve:'):
        i=data.split(':',1)[1]; ok=_incident_update(i,'UPDATE incidents SET resolved=1,resolved_at=?,resolved_reason=? WHERE id=?',(iso(),'manual',i)); tg('answerCallbackQuery',callback_query_id=qid,text='Incident resolved.' if ok else 'Incident not found.'); _refresh_callback_message(q); return
    if data.startswith('reopen:'):
        i=data.split(':',1)[1]; ok=_incident_update(i,'UPDATE incidents SET resolved=0,resolved_at=NULL,resolved_reason=NULL WHERE id=?',(i,)); tg('answerCallbackQuery',callback_query_id=qid,text='Incident reopened.' if ok else 'Incident not found.'); _refresh_callback_message(q); return
    if data.startswith('unsilence:'):
        i=data.split(':',1)[1]; ok=_incident_update(i,'UPDATE incidents SET silenced_until=NULL WHERE id=?',(i,)); tg('answerCallbackQuery',callback_query_id=qid,text='Incident unsilenced.' if ok else 'Incident not found.'); _refresh_callback_message(q); return
    if data.startswith('silence:'):
        _,i,ss=data.split(':'); sec=max(60,min(int(ss),86400)); until=(datetime.now(timezone.utc)+timedelta(seconds=sec)).isoformat(); ok=_incident_update(i,'UPDATE incidents SET silenced_until=? WHERE id=?',(until,i)); tg('answerCallbackQuery',callback_query_id=qid,text=f'Silenced for {_format_duration(sec)}.' if ok else 'Incident not found.'); _refresh_callback_message(q)

def telegram_loop():
    offset=0
    while not _stop.is_set():
        r=tg('getUpdates',http_timeout=TG_POLL_SECONDS+5,timeout=TG_POLL_SECONDS,offset=offset,allowed_updates=['callback_query','message'])
        if not r or not r.get('ok'): _stop.wait(3); continue
        for u in r.get('result',[]):
            offset=int(u.get('update_id',offset))+1
            try:
                if 'callback_query' in u: callback(u)
                elif 'message' in u:
                    if not _authorized_telegram_update(u):
                        chat_id=((u.get('message') or {}).get('chat') or {}).get('id')
                        log.warning('Ignored Telegram message from unauthorized chat_id=%s',chat_id)
                        continue
                    text=str((u.get('message') or {}).get('text') or '')
                    if text.startswith('/'): tg('sendMessage',handle_telegram_command(text))
            except Exception: log.exception('Telegram update failed')

def startup_delivery_checks():
    if TG_TOKEN:
        result=tg_delivery('getMe')
        if result.sent:
            log.info('Telegram startup validation succeeded')
        else:
            log.warning('Telegram startup validation failed: %s',result.error or 'unknown')
    elif TG_CHAT:
        log.warning('TELEGRAM_CHAT_ID is set but TELEGRAM_BOT_TOKEN is missing')
    diagnostics=_email_diagnostics()
    if diagnostics['configured']:
        log.info('Email fallback configuration is complete via %s',EMAIL_PROVIDER)
        if STARTUP_SELF_TEST:
            ok=send_email(email_recipients(),'ErrorBeacon startup test','ErrorBeacon startup self-test: email delivery is working.')
            if ok: log.info('Email startup self-test succeeded')
            else: log.warning('Email startup self-test failed')
    else:
        log.warning('Email fallback is not fully configured; missing=%s',diagnostics['missing'])
    if STARTUP_SELF_TEST and TG_TOKEN and TG_CHAT:
        result=tg_delivery('sendMessage','🧪 <b>ErrorBeacon startup test</b>\n\nTelegram delivery is working.')
        if result.sent: log.info('Telegram startup self-test succeeded')
        else: log.warning('Telegram startup self-test failed: %s',result.error or 'unknown')

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
    threading.Thread(target=email_fallback_loop,name='errorbeacon-email-fallback',daemon=True).start()
    threading.Thread(target=retention_and_health_loop,name='errorbeacon-maintenance',daemon=True).start()
    _recovery_thread=threading.Thread(target=ai_recovery_loop,name='errorbeacon-ai-recovery',daemon=True)
    _recovery_thread.start()
    if os.getenv('ENVIRONMENT', 'production').lower() in {'prod','production'}:
        missing_keys = [name for name, value in (('ERRORBEACON_INGEST_API_KEY', INGEST_API_KEY), ('ERRORBEACON_ADMIN_API_KEY', ADMIN_API_KEY)) if not value]
        if missing_keys:
            log.error('Production ErrorBeacon is missing required scoped API keys: %s', ', '.join(missing_keys))
    startup_delivery_checks()
    if TG_POLL and TG_TOKEN and TG_CHAT:
        threading.Thread(target=telegram_loop,name='errorbeacon-telegram',daemon=True).start()
    yield
    _stop.set()
    for t in _workers + _ai_workers:
        t.join(timeout=2)
    if _recovery_thread:
        _recovery_thread.join(timeout=2)
app=FastAPI(title='ErrorBeacon Lite',version=APP_VERSION,lifespan=lifespan,docs_url='/docs' if ENABLE_DOCS else None,redoc_url='/redoc' if ENABLE_DOCS else None,openapi_url='/openapi.json' if ENABLE_DOCS else None)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
app.add_middleware(RequestContextMiddleware)
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

def health_payload():
    if not DB_PATH.exists(): init_db()
    with _db_lock:
        c=db(); pending=c.execute("SELECT COUNT(*) FROM incidents WHERE ai_status='pending' AND ai_analysis IS NULL").fetchone()[0]; incidents_count=c.execute('SELECT COUNT(*) FROM incidents').fetchone()[0]; events_count=c.execute('SELECT COUNT(*) FROM incident_events').fetchone()[0]; unresolved=c.execute('SELECT COUNT(*) FROM incidents WHERE resolved=0').fetchone()[0]; c.close()
    size=DB_PATH.stat().st_size if DB_PATH.exists() else 0; mb=size/(1024*1024); status='warning' if mb>=DB_WARN_MB else 'ok'
    email=_email_diagnostics()
    return {'status':status,'service':'errorbeacon','version':APP_VERSION,'queue_depth':_jobs.qsize(),'ai_queue_depth':_ai_jobs.qsize(),'ai_pending':pending,'ai_enabled':bool(AI_ENABLED and _providers()),'ai_providers':[p[0] for p in _providers()],'telegram_configured':bool(TG_TOKEN and TG_CHAT),'email_fallback_configured':email['configured'],'email_provider':EMAIL_PROVIDER,'email':email,'incidents_count':incidents_count,'unresolved_incidents':unresolved,'incident_events_count':events_count,'db_size_bytes':size,'db_size_mb':mb,'db_status':status,'retention_days':RETENTION_DAYS,'auto_stale_days':AUTO_STALE_DAYS,'digest_enabled':DIGEST_ENABLED,'digest_interval_hours':DIGEST_INTERVAL_HOURS}

@app.get('/healthz')
def healthz():
    h=health_payload()
    return {'status':h['status'],'service':h['service'],'version':h['version'],'db_status':h['db_status']}
@app.get('/v1/health',dependencies=[Depends(admin_auth)])
def detailed_health():
    return health_payload()
@app.get('/')
def root():
    result={'service':'ErrorBeacon Lite','version':APP_VERSION,'health':'/healthz'}
    if ENABLE_DOCS:
        result['docs']='/docs'
    return result
@app.post('/v1/events',dependencies=[Depends(ingest_auth)])
def ingest(event:ErrorEvent):
    if not limited(): raise HTTPException(429,'Alert rate limit exceeded')
    try:
        i,o,q,s=process(event)
        return {'accepted':True,'queued':q,'incident_id':i,'occurrence':o,'request_id':request_id_var.get(),'silenced':s}
    except Exception:
        log.exception('Event ingestion failed')
        raise HTTPException(500,'Event ingestion failed')
@app.post('/v1/test',dependencies=[Depends(admin_auth)])
def test_alert(request:Request):
    if not limited_test(): raise HTTPException(429,'Test alert rate limit exceeded')
    rid=request_id_var.get() or request.headers.get('x-request-id') or str(uuid.uuid4())
    e=ErrorEvent(app='errorbeacon-test',environment=os.getenv('ENVIRONMENT','development'),severity='warning',error_type='TestException',message='This is a controlled ErrorBeacon test alert.',request_id=rid,method=request.method,path=f'/errorbeacon-test/{rid}',status_code=500,category=None)
    i,o,q,s=process(e)
    return {'ok':True,'incident_id':i,'queued':q,'request_id':rid,'silenced':s}
@app.get('/v1/incidents',dependencies=[Depends(admin_auth)])
def incidents(limit:int=50, app_name:str|None=None, app:str|None=None, environment:str|None=None, severity:str|None=None, resolved:bool|None=None, start_date:str|None=None, end_date:str|None=None):
    limit=max(1,min(limit,200)); app_filter=app if app is not None else app_name
    clauses=[]; params=[]
    if app_filter: clauses.append('app=?'); params.append(app_filter)
    if environment: clauses.append('environment=?'); params.append(environment)
    if severity: clauses.append('severity=?'); params.append(severity)
    if resolved is not None: clauses.append('resolved=?'); params.append(1 if resolved else 0)
    if start_date: clauses.append('last_seen_at>=?'); params.append(start_date)
    if end_date: clauses.append('last_seen_at<=?'); params.append(end_date)
    where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
    with _db_lock:
        c=db(); rows=c.execute(f'SELECT id,created_at,app,environment,severity,error_type,message,request_id,path,status_code,release,component,operation,occurrence_count,last_seen_at,resolved,resolved_at,resolved_reason,telegram_sent,telegram_status,telegram_attempts,telegram_last_error,fallback_status,fallback_attempts,fallback_last_error,fallback_sent_at,ai_status,ai_analysis,ai_attempts,ai_last_error,ai_failure_email_sent_at,spike_detected,deployment_regression,silenced_until FROM incidents{where} ORDER BY last_seen_at DESC LIMIT ?',params+[limit]).fetchall(); c.close()
    return [dict(r) for r in rows]

@app.get('/v1/incidents/{incident_id}',dependencies=[Depends(admin_auth)])
def incident_detail(incident_id:str):
    with _db_lock:
        c=db(); x=c.execute('SELECT * FROM incidents WHERE id=?',(incident_id,)).fetchone(); events=c.execute('SELECT id,occurred_at,app,environment,release,request_id FROM incident_events WHERE incident_id=? ORDER BY occurred_at DESC LIMIT 500',(incident_id,)).fetchall(); c.close()
    if not x: raise HTTPException(404,'Incident not found')
    result=dict(x);
    try: result['context']=json.loads(result.pop('context_json') or '{}')
    except Exception: result['context']=result.pop('context_json') or {}
    result['events']=[dict(r) for r in events]
    return result

@app.get('/v1/stats',dependencies=[Depends(admin_auth)])
def stats(window:str='24h'):
    m=re.fullmatch(r'(?i)(\d+)(m|h|d|w)',window.strip())
    if not m: raise HTTPException(400,'window must look like 30m, 24h, 7d, or 1w')
    seconds=int(m.group(1))*{'m':60,'h':3600,'d':86400,'w':604800}[m.group(2).lower()]; seconds=max(60,min(seconds,365*86400)); cutoff=(datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat()
    with _db_lock:
        c=db()
        total=c.execute('SELECT COUNT(*) FROM incidents WHERE last_seen_at>=?',(cutoff,)).fetchone()[0]
        unresolved=c.execute('SELECT COUNT(*) FROM incidents WHERE resolved=0 AND last_seen_at>=?',(cutoff,)).fetchone()[0]
        by_severity=[dict(r) for r in c.execute('SELECT severity,COUNT(*) count,SUM(occurrence_count) occurrences FROM incidents WHERE last_seen_at>=? GROUP BY severity ORDER BY count DESC',(cutoff,))]
        by_app=[dict(r) for r in c.execute('SELECT app,COUNT(*) count,SUM(occurrence_count) occurrences FROM incidents WHERE last_seen_at>=? GROUP BY app ORDER BY count DESC LIMIT 25',(cutoff,))]
        by_environment=[dict(r) for r in c.execute('SELECT environment,COUNT(*) count,SUM(occurrence_count) occurrences FROM incidents WHERE last_seen_at>=? GROUP BY environment ORDER BY count DESC',(cutoff,))]
        top_fingerprints=[dict(r) for r in c.execute('SELECT fingerprint,app,severity,message,SUM(occurrence_count) occurrences,MAX(spike_detected) spike_detected,MAX(deployment_regression) deployment_regression FROM incidents WHERE last_seen_at>=? GROUP BY fingerprint,app,severity,message ORDER BY occurrences DESC LIMIT 20',(cutoff,))]
        spikes=c.execute('SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND spike_detected=1',(cutoff,)).fetchone()[0]
        regressions=c.execute('SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND deployment_regression=1',(cutoff,)).fetchone()[0]
        tg_sent=c.execute("SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND telegram_status='sent'",(cutoff,)).fetchone()[0]
        tg_attempts=c.execute('SELECT COALESCE(SUM(telegram_attempts),0) FROM incidents WHERE last_seen_at>=?',(cutoff,)).fetchone()[0]
        tg_total=c.execute('SELECT COUNT(*) FROM incidents WHERE last_seen_at>=?',(cutoff,)).fetchone()[0]
        email_sent=c.execute("SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND fallback_status='sent'",(cutoff,)).fetchone()[0]
        email_attempts=c.execute('SELECT COALESCE(SUM(fallback_attempts),0) FROM incidents WHERE last_seen_at>=?',(cutoff,)).fetchone()[0]
        ai_complete=c.execute("SELECT COUNT(*) FROM incidents WHERE last_seen_at>=? AND ai_status='complete'",(cutoff,)).fetchone()[0]
        ai_attempts=c.execute('SELECT COALESCE(SUM(ai_attempts),0) FROM incidents WHERE last_seen_at>=?',(cutoff,)).fetchone()[0]
        c.close()
    rate=lambda a,b: round((a/b)*100,2) if b else None
    return {'window':window,'since':cutoff,'incidents':total,'unresolved':unresolved,'by_severity':by_severity,'by_app':by_app,'by_environment':by_environment,'top_fingerprints':top_fingerprints,'spike_count':spikes,'deployment_regression_count':regressions,'delivery':{'telegram':{'sent_incidents':tg_sent,'incidents_in_window':tg_total,'retry_failures':tg_attempts,'success_rate_percent':rate(tg_sent,tg_total)},'email_fallback':{'sent_incidents':email_sent,'attempts':email_attempts,'success_rate_percent':rate(email_sent,email_attempts)},'ai':{'completed':ai_complete,'attempts':ai_attempts,'completion_rate_percent':rate(ai_complete,ai_complete+ai_attempts)}}}

@app.post('/v1/test-telegram',dependencies=[Depends(admin_auth)])
def test_telegram():
    result=tg_delivery('sendMessage','🧪 <b>ErrorBeacon Telegram test</b>\n\nThis is an administrative delivery test.')
    if not result.sent: raise HTTPException(502, result.error or 'Telegram delivery failed')
    return {'ok':True,'channel':'telegram'}

@app.post('/v1/test-email',dependencies=[Depends(admin_auth)])
def test_email():
    if not email_configured(): raise HTTPException(503,detail={'message':'Email fallback is not configured','email':_email_diagnostics()})
    ok=send_email(email_recipients(),'ErrorBeacon email test','This is an administrative ErrorBeacon email delivery test. No incident was generated.')
    if not ok: raise HTTPException(502,'Email delivery failed')
    return {'ok':True,'channel':'email','recipients_count':len(email_recipients()),'provider':EMAIL_PROVIDER}

@app.post('/v1/incidents/{incident_id}/resolve',dependencies=[Depends(admin_auth)])
def resolve(incident_id:str):
    if not _incident_update(incident_id,'UPDATE incidents SET resolved=1,resolved_at=?,resolved_reason=? WHERE id=?',(iso(),'manual',incident_id)): raise HTTPException(404,'Incident not found')
    return {'resolved':True,'incident_id':incident_id}

@app.post('/v1/incidents/{incident_id}/reopen',dependencies=[Depends(admin_auth)])
def reopen(incident_id:str):
    if not _incident_update(incident_id,'UPDATE incidents SET resolved=0,resolved_at=NULL,resolved_reason=NULL WHERE id=?',(incident_id,)): raise HTTPException(404,'Incident not found')
    return {'reopened':True,'incident_id':incident_id}

@app.post('/v1/incidents/{incident_id}/silence',dependencies=[Depends(admin_auth)])
def silence(incident_id:str,seconds:int=3600):
    seconds=max(60,min(seconds,86400)); until=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
    if not _incident_update(incident_id,'UPDATE incidents SET silenced_until=? WHERE id=?',(until,incident_id)): raise HTTPException(404,'Incident not found')
    return {'silenced':True,'incident_id':incident_id,'silenced_until':until,'seconds':seconds}

@app.post('/v1/incidents/{incident_id}/unsilence',dependencies=[Depends(admin_auth)])
def unsilence(incident_id:str):
    if not _incident_update(incident_id,'UPDATE incidents SET silenced_until=NULL WHERE id=?',(incident_id,)): raise HTTPException(404,'Incident not found')
    return {'unsilenced':True,'incident_id':incident_id}
