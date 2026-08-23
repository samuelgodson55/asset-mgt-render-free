"""
app/main/models.py
----------------------
The ErrorEvent request body model accepted by POST /v1/events, plus the
tiny iso() timestamp helper used throughout the rest of this service.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ErrorEvent(BaseModel):
    app: str = Field(default='web-app', max_length=120)
    environment: str = Field(default='production', max_length=40)
    severity: str = Field(default='error', max_length=20)
    error_type: str | None = Field(default=None, max_length=300)
    message: str = Field(max_length=5000)
    traceback: str | None = Field(default=None, max_length=30000)
    request_id: str | None = Field(default=None, max_length=200)
    method: str | None = Field(default=None, max_length=20)
    path: str | None = Field(default=None, max_length=2000)
    status_code: int | None = None
    user_id: str | None = Field(default=None, max_length=200)
    release: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=300)
    component: str | None = Field(default=None, max_length=200)
    operation: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    expected: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


def iso():
    return datetime.now(timezone.utc).isoformat()
