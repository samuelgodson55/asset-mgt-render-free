"""
app/main/lifecycle.py
--------------------------
FastAPI startup/shutdown: the one-time delivery self-checks logged (and
optionally live-tested via STARTUP_SELF_TEST) at boot, and the `lifespan`
context manager that starts every background thread this service runs
(alert workers, AI workers, Telegram recovery/polling, email fallback,
retention/digest maintenance) and joins them on shutdown.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from .ai import ai_recovery_loop, ai_worker
from .alerting import worker
from .config import (
    ADMIN_API_KEY,
    ALERT_WORKERS,
    AI_WORKERS,
    EMAIL_PROVIDER,
    INGEST_API_KEY,
    STARTUP_SELF_TEST,
    TG_CHAT,
    TG_POLL,
    TG_TOKEN,
)
from .db import init_db
from .digest import retention_and_health_loop
from .email_notify import email_fallback_loop, email_recipients, send_email, _email_diagnostics
from .logging_setup import log
from .state import _ai_workers, _recovery_thread, _stop, _workers
from .telegram import telegram_loop, telegram_recovery_loop, tg_delivery


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
