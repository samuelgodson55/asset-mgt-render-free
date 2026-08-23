"""
app/main/
-------------
ErrorBeacon Lite: a small, single-service error-tracking/alerting backend
(think a minimal self-hosted Sentry + on-call notifier). Ingests error
events over HTTP, deduplicates them into incidents, classifies severity,
optionally runs AI root-cause analysis, and delivers Telegram (primary)
and email (fallback) notifications -- plus a Telegram bot for
acknowledging/resolving/silencing incidents from chat.

This top-level module wires up the FastAPI `app` object, its HTTP routes,
and startup/shutdown via lifecycle.lifespan. The actual logic lives in:

  config.py        -- env-var configuration
  logging_setup.py -- structured JSON logging + request-id context var
  state.py          -- shared locks/queues/worker-thread bookkeeping
  middleware.py     -- request-size-limit and request-id ASGI middleware
  models.py          -- ErrorEvent request model
  db.py              -- SQLite schema/persistence/incident pipeline
  auth.py            -- API-key auth + rate limiting
  ai.py               -- AI root-cause-analysis pipeline
  email_notify.py     -- fallback email delivery
  telegram.py          -- Telegram delivery + bot commands
  alerting.py           -- alert dispatch (ties db/ai/telegram together)
  digest.py              -- periodic digest + retention housekeeping
  lifecycle.py            -- startup checks + background-thread lifespan
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request

from .ai import _providers
from .alerting import process
from .auth import admin_auth, auth, ingest_auth, limited, limited_test
from .config import (
    ADMIN_API_KEY,
    AI_ENABLED,
    APP_VERSION,
    AUTO_STALE_DAYS,
    DB_WARN_MB,
    DIGEST_ENABLED,
    DIGEST_INTERVAL_HOURS,
    EMAIL_PROVIDER,
    ENABLE_DOCS,
    INGEST_API_KEY,
    MAX_REQUEST_BODY_BYTES,
    RETENTION_DAYS,
    TG_CHAT,
    TG_TOKEN,
)
from .db import db, DB_PATH, init_db, _incident_update, _db_lock
from .email_notify import email_configured, email_recipients, send_email, _email_diagnostics
from .lifecycle import lifespan, startup_delivery_checks
from .logging_setup import log, request_id_var
from .middleware import RequestContextMiddleware, RequestSizeLimitMiddleware
from .models import ErrorEvent, iso
from .state import _ai_jobs, _jobs
from .telegram import telegram_message, tg_delivery

# Re-exported so `from app.main import X` / `from app import main; main.X` keep
# working exactly like the old single-file module -- used directly by
# tests/test_core.py.
from shared.errorbeacon_sanitization import redact  # noqa: F401
from .db import classify, clean, fingerprint, persist, purge_resolved_incidents  # noqa: F401
from .alerting import should_notify, enqueue_ai, alert  # noqa: F401
from .ai import normalize_ai_analysis, _save_ai_and_notify, ai, ai_with_retry  # noqa: F401
from .email_notify import _send_email_fallback  # noqa: F401
from .telegram import TelegramDelivery, keyboard, handle_telegram_command, _silence_seconds, _format_duration  # noqa: F401
from .state import _admin_failures, _rate, _test_rate  # noqa: F401


app=FastAPI(title='ErrorBeacon Lite',version=APP_VERSION,lifespan=lifespan,docs_url='/docs' if ENABLE_DOCS else None,redoc_url='/redoc' if ENABLE_DOCS else None,openapi_url='/openapi.json' if ENABLE_DOCS else None)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
app.add_middleware(RequestContextMiddleware)

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
        c=db(); rows=c.execute(f'SELECT id,created_at,app,environment,severity,error_type,message,request_id,path,status_code,release,component,operation,occurrence_count,last_seen_at,resolved,resolved_at,resolved_reason,telegram_sent,telegram_status,telegram_attempts,telegram_last_error,fallback_status,fallback_attempts,fallback_last_error,fallback_sent_at,ai_status,ai_analysis,ai_attempts,ai_last_error,ai_provider_used,ai_failure_email_sent_at,spike_detected,deployment_regression,silenced_until FROM incidents{where} ORDER BY last_seen_at DESC LIMIT ?',params+[limit]).fetchall(); c.close()
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
