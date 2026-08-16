import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['ERRORBEACON_API_KEY']='test-key'
os.environ['DATA_DIR']='/tmp/errorbeacon-test'
from app import main
from app.main import ErrorEvent, fingerprint, redact, clean, telegram_message, RequestContextMiddleware, request_id_var

def test_fingerprint_is_stable():
    e = ErrorEvent(message='boom', error_type='ValueError', path='/x')
    assert fingerprint(e) == fingerprint(e)

def test_event_defaults():
    e = ErrorEvent(message='boom')
    assert e.severity == 'error'
    assert e.environment == 'production'

def test_fingerprint_normalizes_volatile_ids():
    a = ErrorEvent(message='duplicate quotation 123456 for request abcdef123456789', error_type='IntegrityError', path='/api/quotations')
    b = ErrorEvent(message='duplicate quotation 987654 for request 999999999999999', error_type='IntegrityError', path='/api/quotations')
    assert fingerprint(a) == fingerprint(b)

def test_redaction_covers_credentials_and_db_urls():
    value = 'Authorization: Bearer abc.def.ghi password=supersecret postgresql://admin:pw@db:5432/app'
    out = redact(value)
    assert 'supersecret' not in out
    assert 'abc.def.ghi' not in out
    assert 'postgresql://admin:pw' not in out
    assert '[REDACTED]' in out or '[REDACTED_DB_URL]' in out

def test_context_redaction_is_recursive():
    out = clean({'password': 'secret', 'nested': {'api_key': 'abc', 'safe': 'ok'}})
    assert out['password'] == '[REDACTED]'
    assert out['nested']['api_key'] == '[REDACTED]'
    assert out['nested']['safe'] == 'ok'

def test_telegram_contains_request_id_and_closes_pre():
    e = ErrorEvent(message='boom', error_type='ValueError', request_id='req-123', traceback='Traceback\nline')
    msg = telegram_message(e, 'abc123', 1)
    assert 'req-123' in msg
    assert '<pre>' in msg
    assert '</pre>' in msg


def test_request_context_generates_and_returns_request_id():
    import asyncio

    seen = {}
    async def app(scope, receive, send):
        seen["rid"] = request_id_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestContextMiddleware(app)
    sent = []
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": []}
    asyncio.run(middleware(scope, lambda: None, send))
    header = dict(sent[0]["headers"])[b"x-request-id"].decode()
    assert header == seen["rid"]
    assert len(header) >= 20


def test_request_context_preserves_incoming_request_id():
    import asyncio
    seen = {}
    async def app(scope, receive, send):
        seen["rid"] = request_id_var.get()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    middleware = RequestContextMiddleware(app)
    sent = []
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": [(b"x-request-id", b"known-req-123")]}
    asyncio.run(middleware(scope, lambda: None, send))
    assert seen["rid"] == "known-req-123"
    assert dict(sent[0]["headers"])[b"x-request-id"] == b"known-req-123"


def test_normalize_ai_analysis_enforces_structured_sections():
    from app.main import normalize_ai_analysis
    raw = """### ROOT CAUSE:
Database connection pool exhausted.

### IMPACT:
Checkout requests fail for affected users.

### NEXT STEPS:
1. Check pool saturation.
2. Review recent deployment.

### CONFIDENCE:
HIGH
"""
    out = normalize_ai_analysis(raw)
    assert out.startswith("ROOT CAUSE:")
    assert "\n\nIMPACT:\n" in out
    assert "\n\nNEXT STEPS:\n" in out
    assert "\n\nCONFIDENCE:\nHIGH" in out

def test_normalize_ai_analysis_rejects_missing_required_section():
    from app.main import normalize_ai_analysis
    assert normalize_ai_analysis("ROOT CAUSE:\nSomething failed.\n\nIMPACT:\nUsers affected.") is None

def test_normalize_ai_analysis_normalizes_invalid_confidence():
    from app.main import normalize_ai_analysis
    out = normalize_ai_analysis(
        "ROOT CAUSE:\nCause.\n\nIMPACT:\nImpact.\n\nNEXT STEPS:\nFix it.\n\nCONFIDENCE:\ncertain"
    )
    assert out.endswith("CONFIDENCE:\nMEDIUM")


def test_client_event_has_no_fake_http_status_and_marks_chaos_test():
    from app.main import ErrorEvent
    e = ErrorEvent(message='controlled', category='chaos_test', method='CLIENT', status_code=None)
    msg = telegram_message(e, 'abc123', 1)
    assert 'HTTP:' not in msg
    assert 'CONTROLLED CHAOS TEST' in msg

def test_healthcheck_events_are_non_alerting():
    from app.main import should_notify
    e = ErrorEvent(message='probe', category='healthcheck', severity='warning')
    assert should_notify(e) is False

def test_dependency_degradation_is_not_reported_as_http_503():
    from app.main import ErrorEvent, telegram_message
    e = ErrorEvent(message='redis unavailable; fail-open', category='dependency_degraded', severity='warning', component='rate_limit', operation='redis_check')
    msg = telegram_message(e, 'abc123', 1)
    assert 'HTTP:' not in msg
    assert 'rate_limit' in msg


def test_dependency_degraded_is_warning_and_not_http_outage():
    e = main.ErrorEvent(message="redis unavailable", severity="warning", category="dependency_degraded", status_code=503)
    assert main.classify(e) == "warning"

def test_chaos_test_is_info_classification():
    e = main.ErrorEvent(message="controlled", severity="error", category="chaos_test", status_code=None)
    assert main.classify(e) == "info"

def test_healthz_exposes_ai_queue():
    payload = main.healthz()
    assert "ai_queue_depth" in payload


def test_incident_schema_has_durable_ai_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    c = main.db()
    cols = {r['name'] for r in c.execute('PRAGMA table_info(incidents)').fetchall()}
    c.close()
    assert {'ai_status','ai_attempts','ai_next_retry_at','ai_last_error'} <= cols


def test_duplicate_events_increment_occurrences(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    monkeypatch.setattr(main, 'DEDUP', 300)
    main.init_db()
    e = ErrorEvent(app='test', environment='development', message='database unavailable', error_type='OperationalError', path='/readyz', status_code=500)
    first = main.persist(e)
    second = main.persist(e)
    assert first[0] == second[0]
    assert second[1] == 2


def test_chaos_test_is_alertable_when_enabled(monkeypatch):
    monkeypatch.setattr(main, 'CHAOS_TEST_ALERTS', True)
    e = main.ErrorEvent(message="controlled", severity="error", category="chaos_test")
    assert main.should_notify(e) is True


def test_alert_sends_immediate_telegram_and_enqueues_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    e = main.ErrorEvent(
        app='test', environment='development', message='boom',
        error_type='ValueError', request_id='req-123', path='/x', status_code=500
    )
    incident, *_ = main.persist(e)
    sent = []
    monkeypatch.setattr(main, 'tg_delivery', lambda *a, **k: main.TelegramDelivery('sent', {'ok': True}))
    monkeypatch.setattr(main, 'enqueue_ai', lambda *a, **k: sent.append(True))
    assert main.alert(e, incident, 1, False, False) is True
    assert sent == [True]
    c = main.db()
    row = c.execute('SELECT telegram_sent,telegram_status FROM incidents WHERE id=?',(incident,)).fetchone()
    c.close()
    assert row['telegram_sent'] == 1 and row['telegram_status'] == 'sent'


def test_alert_failure_is_persisted_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    e = main.ErrorEvent(app='test', environment='development', message='boom', error_type='ValueError', path='/x', status_code=500)
    incident, *_ = main.persist(e)
    monkeypatch.setattr(main, 'tg_delivery', lambda *a, **k: main.TelegramDelivery('failed', error='TelegramAPIRejected'))
    assert main.alert(e, incident, 1, False, False) is False
    c = main.db()
    row = c.execute('SELECT telegram_status,telegram_attempts FROM incidents WHERE id=?',(incident,)).fetchone()
    c.close()
    assert row['telegram_status'] == 'pending' and row['telegram_attempts'] == 1


def test_ai_with_retry_retries_and_returns_normalized_analysis(monkeypatch):
    monkeypatch.setattr(main, 'AI_ENABLED', True)
    monkeypatch.setattr(main, 'GEMINI_KEY', 'test-key')
    monkeypatch.setattr(main, 'AI_RETRIES', 3)
    monkeypatch.setattr(main, 'AI_RETRY_BASE', 0)
    e = main.ErrorEvent(app='test', environment='development', message='database down', error_type='OperationalError')
    attempts = {'n': 0}

    def fake_ai(event):
        attempts['n'] += 1
        if attempts['n'] < 2:
            raise RuntimeError('temporary provider failure')
        return 'ROOT CAUSE:\nDatabase unavailable\n\nIMPACT:\nRequests depending on the database fail.\n\nNEXT STEPS:\nRestore database connectivity.\n\nCONFIDENCE:\nHIGH'

    monkeypatch.setattr(main, 'ai', fake_ai)
    result = main.ai_with_retry(e)
    assert attempts['n'] == 2
    assert result is not None
    assert 'ROOT CAUSE:' in result and 'IMPACT:' in result and 'NEXT STEPS:' in result


def test_telegram_timeout_is_unknown_and_not_automatically_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    e = ErrorEvent(
        app='test', environment='development', message='boom',
        error_type='ValueError', path='/x', status_code=500
    )
    incident, *_ = main.persist(e)
    calls = {'n': 0}

    def fake_send(*args, **kwargs):
        calls['n'] += 1
        return main.TelegramDelivery('unknown', error='ConnectTimeout')

    monkeypatch.setattr(main, 'tg_delivery', fake_send)
    assert main.alert(e, incident, 1, False, False) is False

    c = main.db()
    row = c.execute(
        'SELECT telegram_status,telegram_attempts,telegram_next_retry_at,telegram_last_error '
        'FROM incidents WHERE id=?', (incident,)
    ).fetchone()
    c.close()

    assert calls['n'] == 1
    assert row['telegram_status'] == 'unknown'
    assert row['telegram_attempts'] == 0
    assert row['telegram_next_retry_at'] is None
    assert row['telegram_last_error'] == 'ConnectTimeout'


def test_telegram_explicit_rejection_remains_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    e = ErrorEvent(
        app='test', environment='development', message='boom',
        error_type='ValueError', path='/x', status_code=500
    )
    incident, *_ = main.persist(e)

    monkeypatch.setattr(
        main, 'tg_delivery',
        lambda *a, **k: main.TelegramDelivery('failed', error='TelegramAPIRejected'),
    )
    assert main.alert(e, incident, 1, False, False) is False

    c = main.db()
    row = c.execute(
        'SELECT telegram_status,telegram_attempts FROM incidents WHERE id=?', (incident,)
    ).fetchone()
    c.close()

    assert row['telegram_status'] == 'pending'
    assert row['telegram_attempts'] == 1


def test_ai_telegram_timeout_is_not_replayed(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    e = ErrorEvent(
        app='test', environment='development', message='boom',
        error_type='ValueError', path='/x', status_code=500
    )
    incident, *_ = main.persist(e)

    monkeypatch.setattr(
        main, 'tg_delivery',
        lambda *a, **k: main.TelegramDelivery('unknown', error='ConnectTimeout'),
    )
    assert main._save_ai_and_notify(
        e, incident, 1, False, False,
        'ROOT CAUSE:\nCause\n\nIMPACT:\nImpact\n\nNEXT STEPS:\nFix\n\nCONFIDENCE:\nHIGH',
    ) is False

    c = main.db()
    row = c.execute(
        'SELECT ai_status,ai_next_retry_at FROM incidents WHERE id=?', (incident,)
    ).fetchone()
    c.close()

    assert row['ai_status'] == 'telegram_unknown'
    assert row['ai_next_retry_at'] is None


def test_telegram_5xx_is_unknown_not_retryable(monkeypatch):
    monkeypatch.setattr(main, 'TG_TOKEN', 'test-token')
    monkeypatch.setattr(main, 'TG_CHAT', '123')
    class Response:
        status_code = 502
        def json(self):
            return {'ok': False, 'description': 'gateway error'}

    monkeypatch.setattr(main.requests, 'post', lambda *a, **k: Response())
    result = main.tg_delivery('sendMessage', 'test', {})
    assert result.status == 'unknown'
    assert result.error == 'TelegramHTTP502'


def test_telegram_4xx_is_definitely_failed_and_retryable(monkeypatch):
    monkeypatch.setattr(main, 'TG_TOKEN', 'test-token')
    monkeypatch.setattr(main, 'TG_CHAT', '123')
    class Response:
        status_code = 400
        def json(self):
            return {'ok': False, 'description': 'bad request'}

    monkeypatch.setattr(main.requests, 'post', lambda *a, **k: Response())
    result = main.tg_delivery('sendMessage', 'test', {})
    assert result.status == 'failed'
    assert result.retryable is True
    assert result.error == 'TelegramHTTP400'
