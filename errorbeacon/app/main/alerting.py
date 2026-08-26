"""
app/main/alerting.py
-------------------------
The alert dispatch pipeline: process() is what POST /v1/events calls after
persisting an event -- decides whether it's notify-worthy, enqueues it,
and the worker thread (see lifecycle.py's lifespan) that drains that queue
and fires off Telegram delivery + AI enrichment for each one.
"""

from __future__ import annotations

import queue

from .ai import enqueue_ai
from .db import persist, should_notify
from .logging_setup import log, request_id_var
from .state import _jobs, _stop
from .telegram import _send_incident_telegram


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
        # This runs on a persistent background THREAD, not inside the
        # request that enqueued it -- request_id_var (a contextvar) does not
        # cross that boundary on its own, so without this the Telegram
        # outcome logged below would carry request_id=null no matter how
        # good the ingest-time log line was. e.request_id is the same
        # correlation ID the frontend/backend already minted, so re-binding
        # it here means "was this delivered?" shows up under the exact same
        # ID `trace-request.sh` is searching for.
        token=request_id_var.set(e.request_id)
        try:
            sent=alert(e,i,o,s,r)
            log.info('Alert delivered: incident=%s telegram_sent=%s',i,sent)
        except Exception:
            log.exception('Alert worker failed')
        finally:
            request_id_var.reset(token)
            _jobs.task_done()

def enqueue(e,i,o,s,r):
    try:
        _jobs.put_nowait((e,i,o,s,r))
        return True
    except queue.Full:
        log.error('Alert queue full; dropped alert for %s',i)
        return False

def process(e):
    i,o,should,s,r,sil=persist(e)
    return i,o,(enqueue(e,i,o,s,r) if should and should_notify(e) and not sil else False),sil
