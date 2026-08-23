"""
app/main/state.py
--------------------
Process-wide mutable state shared across the alert/AI/telegram/email
pipelines: locks, in-memory rate-limit windows, the alert/AI work queues,
and the background worker thread bookkeeping used by lifespan (see
__init__.py) to start them up and join them on shutdown.

Kept as one small module (rather than scattering `threading.Lock()`s next
to whichever function happens to use them first) so every submodule that
needs shared state imports it from a single obvious place.
"""

from __future__ import annotations

import queue
import re
import threading

from .config import ALERT_Q, AI_Q

_db_lock = threading.Lock()
_rate_lock = threading.Lock()
_admin_fail_lock = threading.Lock()
_admin_failures = {}
_ai_state_lock = threading.Lock()
_rate = []
_test_rate = []
_test_rate_lock = threading.Lock()
_jobs = queue.Queue(maxsize=ALERT_Q)
_ai_jobs = queue.Queue(maxsize=AI_Q)
_stop = threading.Event()
_workers = []
_ai_workers = []
_ai_enqueued = set()
_recovery_thread = None
_VOLATILE = re.compile(r'\b(?:[0-9a-f]{8,}|\d{3,})\b', re.I)
