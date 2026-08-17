"""Single source of truth for ErrorBeacon-sensitive data sanitization.

This module is intentionally dependency-free so both the backend image and the
standalone ErrorBeacon image can import the exact same redaction rules.
"""
from __future__ import annotations

import re
from typing import Any

SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"session|reset[_-]?token|access[_-]?token|refresh[_-]?token|private[_-]?key)"
)
JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,}\b"
)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
DBURL = re.compile(
    r"(?i)(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^\s]+"
)
ASSIGN = re.compile(
    r"(?i)(authorization|cookie|x-api-key|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|reset[_-]?token|password|passwd|secret|"
    r"session(?:[_-]?id)?)\s*[:=]\s*([^\s,;&]+)"
)
SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|reset_token|code|secret|"
    r"key|password|passwd|api_key|apikey|session|session_id))=[^&#]*"
)


def sanitize_url(value: Any, limit: int = 2000) -> str:
    """Redact sensitive query-string values from a URL/path."""
    try:
        return SENSITIVE_QUERY.sub(
            lambda m: f"{m.group(1)}=[REDACTED]", str(value or "")
        )[:limit]
    except Exception:
        return str(value or "")[:limit]


def redact(value: Any, limit: int = 30000) -> str:
    """Strip common secret shapes from text."""
    if value is None:
        return ""
    text = str(value)
    text = JWT.sub("[REDACTED_JWT]", text)
    text = BEARER.sub("Bearer [REDACTED]", text)
    text = DBURL.sub("[REDACTED_DB_URL]", text)
    text = ASSIGN.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return text[:limit]


def clean(
    value: Any,
    key: str = "",
    depth: int = 0,
    *,
    max_depth: int = 10,
    max_items: int = 100,
    string_limit: int = 4000,
    scalar_limit: int = 4000,
) -> Any:
    """Recursively redact sensitive values with configurable bounds.

    ``max_depth``/``max_items`` protect untrusted context payloads. Callers
    that already validate those bounds can raise them without changing the
    redaction rules themselves.
    """
    if SECRET_KEY.search(key):
        return "[REDACTED]"
    if depth >= max_depth:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, dict):
        return {
            str(k): clean(
                item,
                str(k),
                depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                string_limit=string_limit,
                scalar_limit=scalar_limit,
            )
            for k, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple)):
        return [
            clean(
                item,
                key,
                depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                string_limit=string_limit,
                scalar_limit=scalar_limit,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, str):
        return redact(sanitize_url(value), string_limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(value, scalar_limit)
