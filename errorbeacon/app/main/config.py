"""
app/main/config.py
---------------------
All env-var-derived configuration for the ErrorBeacon service, read once
at import time. Kept as plain module-level constants (not a pydantic
Settings object) to match the rest of this service's style and avoid
touching every call site during this split.
"""

from __future__ import annotations

import os
from pathlib import Path

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
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
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
# Reverse direction of ERRORBEACON_URL (see fastapi_errorbeacon.py): that var lets
# `backend` reach this service; this one lets this service reach `backend` for the
# Telegram /apphealth command. Same Docker-style short DNS default as
# ERRORBEACON_URL's own default -- correct for docker-compose.yml/docker-compose.vm.yml
# where both containers share one network, but NOT assumed to resolve on Azure
# Container Apps (see infra/main.bicep's errorBeaconUrl comment on why ACA needs an
# explicit FQDN instead) or on Render, where ErrorBeacon is deployed as a fully
# separate, standalone service (see errorbeacon/render.yaml) with no shared network to
# `backend` at all -- both of those shapes override or omit this explicitly.
BACKEND_URL = os.getenv('BACKEND_URL', 'http://backend:8000').strip()
BACKEND_HEALTH_TIMEOUT_SECONDS = max(1.0, float(os.getenv('BACKEND_HEALTH_TIMEOUT_SECONDS', '5')))
