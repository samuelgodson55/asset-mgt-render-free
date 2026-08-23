"""
app/main/telegram.py
-------------------------
Telegram delivery: sending incident cards (with inline resolve/silence
buttons), tracking per-incident delivery status/retry, the recovery loop
that replays anything still pending, and the bot-command side (polling
loop + /help, /stats, /apphealth, etc, plus inline button callbacks).
"""

from __future__ import annotations

import html
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from shared.errorbeacon_sanitization import redact, sanitize_url

from .config import BACKEND_HEALTH_TIMEOUT_SECONDS, BACKEND_URL, TG_CHAT, TG_POLL_SECONDS, TG_THREAD, TG_TOKEN
from .db import classify, db, _event_from_row, _format_duration, _incident_update, incident_view, _silence_seconds
from .logging_setup import log
from .models import ErrorEvent, iso
from .state import _db_lock, _stop


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

def telegram_message(e,i,occ,analysis=None,spike=False,regression=False,provider=None):
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
        header = f'\n<b>🤖 AI Analysis (via {html.escape(provider)})</b>\n' if provider else '\n<b>🤖 AI Analysis</b>\n'
        parts.append(header+html.escape(redact(analysis,1700)))
    if e.traceback:
        parts.append('\n<b>Traceback</b>\n<pre>'+html.escape(redact(e.traceback,1300))+'</pre>')
    text='\n'.join(parts)
    if len(text)>4080 and e.traceback:
        text='\n'.join(parts[:-1])
    if len(text)>4080 and analysis:
        ai_header = f'\n<b>🤖 AI Analysis (via {html.escape(provider)})</b>\n' if provider else '\n<b>🤖 AI Analysis</b>\n'
        ai_part=ai_header+html.escape(redact(analysis,900))
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
        '/apphealth — remote check of the monitored backend\'s /healthz, /readyz, /health/dependencies',
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
    from .alerting import process  # local import: avoids a circular import with alerting.py, which itself calls into this module

    rid=str(uuid.uuid4())
    e=ErrorEvent(app='errorbeacon-test',environment=os.getenv('ENVIRONMENT','production'),severity='warning',error_type='TestException',message='This is a controlled ErrorBeacon Telegram test incident.',request_id=rid,method='TELEGRAM',path=f'/telegram-test/{rid}',status_code=500,category=None)
    i,o,q,s=process(e)
    return f'🧪 <b>Test incident created</b>\nIncident: <code>{html.escape(str(i))}</code>\nOccurrence: {o}\nQueued: {q}\nSilenced: {s}\n\nThe normal Telegram/AI/email pipeline has been exercised.'

def _telegram_stats(window='24h'):
    from . import stats  # local import: `stats` is the /v1/stats route handler in __init__.py, which itself imports from this module

    w=window.strip().lower() or '24h'
    if not re.fullmatch(r'\d+(m|h|d|w)',w):
        raise ValueError('window must look like 30m, 24h, 7d, or 1w')
    s=stats(w)
    delivery=s['delivery']
    return '\n'.join([
        f'📊 <b>ErrorBeacon stats ({html.escape(w)})</b>',
        f'Incidents: {s["incidents"]}',
        f'Unresolved: {s["unresolved"]}',
        f'Spikes: {s["spike_count"]}',
        f'Regressions: {s["deployment_regression_count"]}',
        f'Telegram success: {delivery["telegram"]["success_rate_percent"] if delivery["telegram"]["success_rate_percent"] is not None else "n/a"}%',
        f'Email fallback success: {delivery["email_fallback"]["success_rate_percent"] if delivery["email_fallback"]["success_rate_percent"] is not None else "n/a"}%',
        f'AI completion: {delivery["ai"]["completion_rate_percent"] if delivery["ai"]["completion_rate_percent"] is not None else "n/a"}%'
    ])

def _backend_probe(path):
    """Bounded-timeout GET against one backend health endpoint. Never raises --
    a slow or unreachable backend must degrade to a failed check in the
    Telegram reply, not an exception that drops the whole /apphealth command.
    Returns (ok, status_code_or_None, detail_or_None)."""
    url = BACKEND_URL.rstrip('/') + path
    try:
        r = requests.get(url, timeout=BACKEND_HEALTH_TIMEOUT_SECONDS)
        ok = r.status_code == 200
        detail = None
        if not ok:
            try:
                body = r.json()
                detail = body.get('reason') if isinstance(body, dict) else None
            except ValueError:
                pass
        return ok, r.status_code, detail
    except requests.exceptions.Timeout:
        return False, None, f'timed out after {BACKEND_HEALTH_TIMEOUT_SECONDS:g}s'
    except requests.exceptions.RequestException as ex:
        return False, None, type(ex).__name__

def _telegram_apphealth():
    if not BACKEND_URL:
        return '❌ <b>Backend health check unavailable.</b> BACKEND_URL is not configured.'
    # /healthz: pure liveness, no DB. /readyz: schema/DB readiness. /health/dependencies:
    # currently Redis. Same three endpoints backend/main.py exposes for its own probes/
    # monitoring -- see that file's docstrings for why each is a separate endpoint.
    checks = [
        ('/healthz', 'Liveness'),
        ('/readyz', 'Readiness (DB/schema)'),
        ('/health/dependencies', 'Dependencies (Redis)'),
    ]
    lines = ['🩺 <b>Backend health</b>', f'Target: <code>{html.escape(BACKEND_URL)}</code>', '']
    all_ok = True
    for path, label in checks:
        ok, status, detail = _backend_probe(path)
        all_ok = all_ok and ok
        line = f'{"✅" if ok else "❌"} <b>{label}</b>'
        if status is not None:
            line += f' — HTTP {status}'
        if detail:
            line += f' — {html.escape(str(detail)[:200])}'
        lines.append(line)
    lines.append('')
    lines.append('Overall: ' + ('✅ healthy' if all_ok else '⚠️ one or more checks failed'))
    return '\n'.join(lines)

def handle_telegram_command(text):
    from . import health_payload  # local import: defined in __init__.py, which itself imports from this module
    from .email_notify import _email_diagnostics, email_recipients, send_email

    p=text.strip().split(); cmd=(p[0].split('@',1)[0].lower() if p else '')
    try:
        if cmd=='/help' or cmd=='/start': return _telegram_help()
        if cmd=='/health':
            h=health_payload(); return '\n'.join(['💚 <b>ErrorBeacon health</b>',f'Status: {h["status"]}',f'Telegram configured: {h["telegram_configured"]}',f'Email fallback configured: {h["email_fallback_configured"]}',f'Email provider: {h["email_provider"]}',f'Email missing: {", ".join(h["email"]["missing"]) or "none"}',f'DB: {h["db_size_mb"]:.1f} MB',f'AI queue: {h["ai_queue_depth"]}',f'AI pending: {h["ai_pending"]}'])
        if cmd=='/apphealth': return _telegram_apphealth()
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
