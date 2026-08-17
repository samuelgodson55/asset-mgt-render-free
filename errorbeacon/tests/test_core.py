import os
import pytest
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['ERRORBEACON_INGEST_API_KEY']='test-ingest-key'
os.environ['ERRORBEACON_ADMIN_API_KEY']='test-admin-key'
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

def test_fingerprint_normalizes_ids_in_path():
    # Regression guard: the same recurring bug on different asset IDs must
    # collapse to ONE incident, not one per ID.
    a = ErrorEvent(message='boom', error_type='ValueError', path='/api/assets/142')
    b = ErrorEvent(message='boom', error_type='ValueError', path='/api/assets/143')
    assert fingerprint(a) == fingerprint(b)

def test_fingerprint_keeps_different_routes_distinct():
    asset = ErrorEvent(message='boom', error_type='ValueError', path='/api/assets/142')
    user = ErrorEvent(message='boom', error_type='ValueError', path='/api/users/142')
    assert fingerprint(asset) != fingerprint(user)

def test_fingerprint_keeps_different_errors_distinct():
    value_error = ErrorEvent(message='boom', error_type='ValueError', path='/api/assets/142')
    runtime_error = ErrorEvent(message='boom', error_type='RuntimeError', path='/api/assets/143')
    assert fingerprint(value_error) != fingerprint(runtime_error)


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

def test_healthz_is_public_summary_only():
    payload = main.healthz()
    assert set(payload) == {'status','service','version','db_status'}

def test_detailed_health_is_available_separately():
    payload = main.detailed_health()
    assert 'ai_queue_depth' in payload


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


def test_test_rate_limit_is_independent_and_tighter(monkeypatch):
    main._test_rate.clear(); monkeypatch.setattr(main, 'TEST_ALERTS_PER_MINUTE', 2)
    assert main.limited_test() is True; assert main.limited_test() is True; assert main.limited_test() is False; main._test_rate.clear()

def test_email_fallback_uses_existing_application_recipients_and_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(main,'DATA_DIR',tmp_path); monkeypatch.setattr(main,'DB_PATH',tmp_path/'errorbeacon.db'); monkeypatch.setattr(main,'EMAIL_FALLBACK_ENABLED',True); monkeypatch.setattr(main,'NOTIFICATIONS_ENABLED',True); monkeypatch.setattr(main,'ADMIN_NOTIFICATION_EMAILS','ops@example.com, oncall@example.com'); monkeypatch.setattr(main,'EMAIL_PROVIDER','smtp'); monkeypatch.setattr(main,'SMTP_HOST','smtp.example.com'); monkeypatch.setattr(main,'SMTP_FROM_EMAIL','alerts@example.com'); monkeypatch.setattr(main,'EMAIL_FALLBACK_AFTER_ATTEMPTS',3)
    main.init_db(); e=main.ErrorEvent(message='boom',error_type='ValueError',path='/x',status_code=500); i,*_=main.persist(e)
    with main._db_lock:
        c=main.db(); c.execute("UPDATE incidents SET telegram_status='pending',telegram_attempts=3 WHERE id=?",(i,)); c.commit(); row=c.execute('SELECT * FROM incidents WHERE id=?',(i,)).fetchone(); c.close()
    calls=[]; monkeypatch.setattr(main,'send_email',lambda to,subject,body:calls.append((to,subject,body)) or True)
    assert main._send_email_fallback(row) is True; assert calls[0][0]==['ops@example.com','oncall@example.com']; assert 'boom' in calls[0][2]

def test_email_fallback_requires_existing_app_notification_configuration(monkeypatch):
    monkeypatch.setattr(main,'EMAIL_FALLBACK_ENABLED',True); monkeypatch.setattr(main,'NOTIFICATIONS_ENABLED',False); monkeypatch.setattr(main,'ADMIN_NOTIFICATION_EMAILS','ops@example.com'); assert main.email_configured() is False

def test_retention_purges_only_old_resolved_incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(main,'DATA_DIR',tmp_path); monkeypatch.setattr(main,'DB_PATH',tmp_path/'errorbeacon.db'); monkeypatch.setattr(main,'RETENTION_DAYS',90); main.init_db(); old=(datetime.now(timezone.utc)-timedelta(days=120)).isoformat(); fresh=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    with main._db_lock:
        c=main.db(); c.execute("INSERT INTO incidents(id,created_at,app,environment,severity,message,fingerprint,last_seen_at,resolved) VALUES('old',?,'test','prod','error','old','fp-old',?,1)",(old,old)); c.execute("INSERT INTO incident_events(incident_id,fingerprint,occurred_at,app,environment) VALUES('old','fp-old',?,'test','prod')",(old,)); c.execute("INSERT INTO incidents(id,created_at,app,environment,severity,message,fingerprint,last_seen_at,resolved) VALUES('fresh',?,'test','prod','error','fresh','fp-fresh',?,1)",(fresh,fresh)); c.commit(); c.close()
    assert main.purge_resolved_incidents()==2
    with main._db_lock:
        c=main.db(); assert c.execute("SELECT COUNT(*) FROM incident_events WHERE incident_id='old'").fetchone()[0] == 0; c.close()

def test_keyboard_exposes_state_appropriate_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(main,'DATA_DIR',tmp_path); monkeypatch.setattr(main,'DB_PATH',tmp_path/'errorbeacon.db'); main.init_db(); i,*_=main.persist(main.ErrorEvent(message='boom')); texts=[b['text'] for row in main.keyboard(i)['inline_keyboard'] for b in row]; assert all(x in ' '.join(texts) for x in ('1h','4h','24h'))
    with main._db_lock:
        c=main.db(); c.execute('UPDATE incidents SET resolved=1 WHERE id=?',(i,)); c.commit(); c.close()
    texts=[b['text'] for row in main.keyboard(i)['inline_keyboard'] for b in row]; assert '↩️ Reopen' in texts

def test_silence_duration_parser_supports_human_units():
    assert main._silence_seconds('1h')==3600; assert main._silence_seconds('4h')==14400; assert main._silence_seconds('24h')==86400; assert main._silence_seconds('90m')==5400; assert main._format_duration(5400)=='1h 30m'
    with pytest.raises(ValueError): main._silence_seconds('tomorrow')

def test_telegram_command_dispatches_controls(monkeypatch):
    calls=[]; monkeypatch.setattr(main,'_incident_update',lambda *args: calls.append(args) or True); assert 'resolved' in main.handle_telegram_command('/resolve abc123'); assert 'silenced' in main.handle_telegram_command('/silence abc123 4h'); assert 'reopened' in main.handle_telegram_command('/reopen abc123'); assert 'unsilenced' in main.handle_telegram_command('/unsilence abc123')


def test_http_incident_lifecycle_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    incident, *_ = main.persist(main.ErrorEvent(message='endpoint boom', error_type='RuntimeError'))
    with TestClient(main.app) as client:
        headers = {'X-API-Key': 'test-admin-key'}
        response = client.get('/v1/incidents', headers=headers)
        assert response.status_code == 200
        assert any(row['id'] == incident for row in response.json())
        assert client.post(f'/v1/incidents/{incident}/silence?seconds=86400', headers=headers).status_code == 200
        assert client.post(f'/v1/incidents/{incident}/unsilence', headers=headers).status_code == 200
        assert client.post(f'/v1/incidents/{incident}/resolve', headers=headers).status_code == 200
        assert client.post(f'/v1/incidents/{incident}/reopen', headers=headers).status_code == 200


def test_http_test_endpoint_has_own_rate_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    monkeypatch.setattr(main, 'TEST_ALERTS_PER_MINUTE', 1)
    main._test_rate.clear()
    main.init_db()
    with TestClient(main.app) as client:
        headers = {'X-API-Key': 'test-admin-key'}
        assert client.post('/v1/test', headers=headers).status_code == 200
        assert client.post('/v1/test', headers=headers).status_code == 429
    main._test_rate.clear()


def test_base_ingest_rate_limiter_enforces_configured_limit(monkeypatch):
    main._rate.clear()
    monkeypatch.setattr(main, 'MAX_ALERTS', 2)
    assert main.limited() is True
    assert main.limited() is True
    assert main.limited() is False
    main._rate.clear()


def test_spike_detection_crosses_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    monkeypatch.setattr(main, 'SPIKE_THRESHOLD', 3)
    monkeypatch.setattr(main, 'SPIKE_WINDOW', 300)
    monkeypatch.setattr(main, 'DEDUP', 60)
    main.init_db()
    event = main.ErrorEvent(
        app='spike-test', environment='test',
        message='same failure', error_type='ValueError', path='/same',
    )
    first = main.persist(event)
    second = main.persist(event)
    third = main.persist(event)
    assert first[3] is False
    assert second[3] is False
    assert third[3] is True
    assert third[2] is True


def test_deployment_regression_detected_after_release_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    first = main.ErrorEvent(
        app='regression-test', environment='production',
        message='deployment failure', error_type='RuntimeError',
        path='/health', release='v1.0.0',
    )
    first_id, *_ = main.persist(first)
    with main._db_lock:
        c = main.db()
        c.execute('UPDATE incidents SET resolved=1 WHERE id=?', (first_id,))
        c.commit()
        c.close()

    second = main.ErrorEvent(
        app='regression-test', environment='production',
        message='deployment failure', error_type='RuntimeError',
        path='/health', release='v1.1.0',
    )
    second_id, _, _, _, regression, _ = main.persist(second)
    assert second_id != first_id
    assert regression is True
    with main._db_lock:
        c = main.db()
        row = c.execute(
            'SELECT deployment_regression FROM incidents WHERE id=?', (second_id,)
        ).fetchone()
        c.close()
    assert row['deployment_regression'] == 1


def test_clean_enforces_depth_limit(monkeypatch):
    monkeypatch.setattr(main, 'CLEAN_MAX_DEPTH', 2)
    nested = {'a': {'b': {'c': 'secret'}}}
    out = main.clean(nested)
    assert out['a']['b'] == '[TRUNCATED_DEPTH]'


def test_email_diagnostics_identifies_missing_fallback_configuration(monkeypatch):
    monkeypatch.setattr(main, 'EMAIL_FALLBACK_ENABLED', True)
    monkeypatch.setattr(main, 'NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(main, 'ADMIN_NOTIFICATION_EMAILS', '')
    monkeypatch.setattr(main, 'EMAIL_PROVIDER', 'smtp')
    monkeypatch.setattr(main, 'SMTP_HOST', '')
    monkeypatch.setattr(main, 'SMTP_FROM_EMAIL', '')
    d = main._email_diagnostics()
    assert d['configured'] is False
    assert 'ADMIN_NOTIFICATION_EMAILS' in d['missing']
    assert 'SMTP_HOST' in d['missing']
    assert 'SMTP_FROM_EMAIL' in d['missing']


def test_http_incident_detail_and_stats(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'errorbeacon.db')
    main.init_db()
    e = main.ErrorEvent(app='stats-app', environment='production', severity='error', message='detailed boom', traceback='Traceback\nboom', user_id='user-1', host='host-1', context={'safe':'ok'})
    incident, *_ = main.persist(e)
    with TestClient(main.app) as client:
        headers = {'X-API-Key': 'test-admin-key'}
        detail = client.get(f'/v1/incidents/{incident}', headers=headers)
        assert detail.status_code == 200
        payload = detail.json()
        assert payload['traceback'].startswith('Traceback')
        assert payload['context']['safe'] == 'ok'
        filtered = client.get('/v1/incidents?app=stats-app&severity=error&resolved=false', headers=headers)
        assert filtered.status_code == 200
        assert any(x['id'] == incident for x in filtered.json())
        stats = client.get('/v1/stats?window=24h', headers=headers)
        assert stats.status_code == 200
        data = stats.json()
        assert data['incidents'] >= 1
        assert any(x['app'] == 'stats-app' for x in data['by_app'])
        assert data['top_fingerprints']


def test_telegram_help_lists_operational_and_test_commands():
    help_text = main.handle_telegram_command('/help')
    for command in ['/health', '/incidents', '/incident &lt;id&gt;', '/stats [window]', '/resolve &lt;id&gt;', '/reopen &lt;id&gt;', '/silence &lt;id&gt; &lt;duration&gt;', '/unsilence &lt;id&gt;', '/test', '/testtelegram', '/testemail', '/help']:
        assert command in help_text


def test_unknown_telegram_command_points_to_help():
    assert 'Use /help' in main.handle_telegram_command('/not-a-command')


def test_admin_auth_does_not_trust_spoofed_proxy_ip_by_default(monkeypatch):
    from fastapi import HTTPException
    main._admin_failures.clear()
    monkeypatch.setattr(main, 'TRUST_PROXY_HEADERS', False)
    monkeypatch.setattr(main, 'ADMIN_AUTH_FAILURES_PER_MINUTE', 2)

    class Req:
        def __init__(self, forwarded):
            self.headers = {'x-real-ip': forwarded}
            self.client = type('Client', (), {'host': '203.0.113.10'})()

    for spoofed in ('198.51.100.1', '198.51.100.2'):
        try:
            main.admin_auth(Req(spoofed), 'wrong')
        except HTTPException as exc:
            assert exc.status_code == 401
    assert set(main._admin_failures) == {'203.0.113.10'}
    main._admin_failures.clear()

def test_admin_auth_prefers_leftmost_forwarded_ip_when_proxy_is_trusted(monkeypatch):
    from fastapi import HTTPException
    main._admin_failures.clear()
    monkeypatch.setattr(main, 'TRUST_PROXY_HEADERS', True)
    class Req:
        headers = {'x-forwarded-for': '198.51.100.8, 10.0.0.4', 'x-real-ip': '192.0.2.8'}
        client = type('Client', (), {'host': '203.0.113.10'})()
    try:
        main.admin_auth(Req(), 'wrong')
    except HTTPException as exc:
        assert exc.status_code == 401
    assert set(main._admin_failures) == {'198.51.100.8'}
    main._admin_failures.clear()


def test_admin_auth_uses_proxy_ip_when_explicitly_trusted(monkeypatch):
    from fastapi import HTTPException
    main._admin_failures.clear()
    monkeypatch.setattr(main, 'TRUST_PROXY_HEADERS', True)
    monkeypatch.setattr(main, 'ADMIN_AUTH_FAILURES_PER_MINUTE', 1)

    class Req:
        headers = {'x-real-ip': '198.51.100.7'}
        client = type('Client', (), {'host': '203.0.113.10'})()

    try:
        main.admin_auth(Req(), 'wrong')
    except HTTPException as exc:
        assert exc.status_code == 401
    assert set(main._admin_failures) == {'198.51.100.7'}
    main._admin_failures.clear()


def test_admin_auth_rate_limits_failed_attempts(monkeypatch):
    from fastapi import HTTPException
    main._admin_failures.clear()
    monkeypatch.setattr(main, 'ADMIN_AUTH_FAILURES_PER_MINUTE', 2)
    class Req:
        headers = {}
        client = type('Client', (), {'host':'203.0.113.10'})()
    for _ in range(2):
        try: main.admin_auth(Req(), 'wrong')
        except HTTPException as exc: assert exc.status_code == 401
    try: main.admin_auth(Req(), 'wrong')
    except HTTPException as exc: assert exc.status_code == 429
    else: assert False
    main._admin_failures.clear()

def test_request_size_limit_rejects_large_content_length():
    import asyncio
    sent=[]
    async def app(scope, receive, send):
        raise AssertionError('downstream should not run')
    middleware=main.RequestSizeLimitMiddleware(app, 10)
    async def send(message): sent.append(message)
    scope={'type':'http','method':'POST','path':'/v1/events','headers':[(b'content-length',b'11')]}
    asyncio.run(middleware(scope, lambda: None, send))
    assert sent[0]['status'] == 413

def test_errorbeacon_docs_disabled_in_production_default():
    assert main.ENABLE_DOCS is False
