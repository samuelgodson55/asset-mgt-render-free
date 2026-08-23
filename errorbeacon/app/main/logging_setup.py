"""
app/main/logging_setup.py
----------------------------
The request-id ContextVar (set per-request by RequestContextMiddleware,
see middleware.py) and the JSON structured-logging setup that stamps every
log line with it, so log aggregators can correlate a request's whole log
trail even across background-thread work triggered by it.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

from .config import LOG_LEVEL

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
log = logging.getLogger('errorbeacon')
