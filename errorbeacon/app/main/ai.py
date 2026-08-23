"""
app/main/ai.py
-------------------
AI-enrichment pipeline: build the prompt from an incident, call whichever
provider is configured (Gemini / Groq / OpenRouter, tried in that order --
see _providers()), and the queue/retry/recovery machinery that makes this
durable across process restarts (an incident's ai_status lives in SQLite,
not just in the in-memory queue).
"""

from __future__ import annotations

import html
import json
import os
import queue
import re
from datetime import datetime, timedelta, timezone

import requests

from shared.errorbeacon_sanitization import redact, sanitize_url

from .config import (
    AI_ENABLED,
    AI_MAX_INCIDENT_RETRIES,
    AI_Q,
    AI_RETRIES,
    AI_RETRY_BASE,
    GEMINI_FALLBACK_MODEL,
    GEMINI_KEY,
    GEMINI_MODEL,
    GROQ_KEY,
    GROQ_MODEL,
    OPENROUTER_KEY,
    OPENROUTER_MODEL,
)
from .db import classify, clean, db, _event_from_row
from .email_notify import email_configured, email_incident_body, email_recipients, send_email
from .logging_setup import log
from .models import iso
from .state import _ai_enqueued, _ai_jobs, _ai_state_lock, _db_lock, _stop
from .telegram import keyboard, silenced, telegram_message, tg_delivery


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
    """Returns (provider_name, analysis_text) on success, or None if AI is
    disabled/unconfigured. Raises the last provider's error if every
    configured provider in the chain failed."""
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
            return name,result
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
    """Try the configured provider chain once per queue attempt; never hammer a rate-limited provider.
    Returns (provider_name, analysis_text) on success, or None."""
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

def _save_ai_and_notify(e,i,occ,spike,regression,a,provider_name=None):
    if not a:
        return False
    with _db_lock:
        c=db()
        c.execute(
            'UPDATE incidents SET ai_analysis=?,ai_status=?,ai_next_retry_at=NULL,ai_last_error=NULL,ai_provider_used=? WHERE id=?',
            (redact(a,5000),'telegram_pending',provider_name,i),
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
        telegram_message(e,i,occ,a,spike,regression,provider=provider_name),
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
            provider=x['ai_provider_used'],
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
            result=ai_with_retry(e)
            if result:
                provider_name,a=result
                _save_ai_and_notify(e,i,occ,spike,regression,a,provider_name)
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
    # Persist the claim immediately. Without this, a freshly-created incident
    # sits with ai_status='pending' in the DB for the entire window between
    # insert and the in-memory claim above, so ai_recovery_loop's periodic
    # scan (a separate thread, on its own timer) can independently claim and
    # fully process the same brand-new incident before this call ever runs,
    # producing a second, duplicate AI analysis + Telegram send. Moving the
    # row out of 'pending' here closes that race at the DB level, which is
    # what the recovery loop's query actually reads.
    with _db_lock:
        c=db()
        c.execute("UPDATE incidents SET ai_status='queued' WHERE id=? AND ai_status='pending'",(i,))
        c.commit()
        c.close()
    try:
        _ai_jobs.put_nowait((e,i,occ,spike,regression))
        return True
    except queue.Full:
        _ai_release(i)
        with _db_lock:
            c=db()
            c.execute("UPDATE incidents SET ai_status='pending' WHERE id=?",(i,))
            c.commit()
            c.close()
        log.error('AI analysis queue full; incident %s remains pending for durable retry',i)
        return False

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
