"""Regression guards for the transaction locks that protect inventory state.

These are intentionally source-level contract tests because SQLite (used by
most unit tests) does not implement PostgreSQL row locks. The real lock
semantics are validated in production/staging PostgreSQL; these tests ensure a
future refactor cannot silently remove the required `.with_for_update()` call.
"""

import inspect

from services import asset_service, checkout_service, quotation_service


def _source(fn):
    return inspect.getsource(fn)


def test_quotation_fulfillment_locks_the_quotation_and_assets():
    source = _source(quotation_service.bulk_checkout_quotation)
    assert source.count("with_for_update()") >= 2


def test_returns_lock_checkout_and_asset():
    source = _source(checkout_service.return_checkout)
    assert source.count("with_for_update()") >= 2


def test_all_stock_changing_asset_operations_lock_the_asset_row():
    for fn in (
        asset_service.flag_asset_exception,
        asset_service.recall_asset_exception,
        asset_service.checkin_asset,
        asset_service.checkout_advanced,
    ):
        assert "with_for_update()" in _source(fn), fn.__name__


def test_request_path_notifications_are_queued_not_sent_inline():
    import inspect
    from services import auth_service, quotation_service

    auth_source = inspect.getsource(auth_service.request_password_reset)
    quotation_source = inspect.getsource(quotation_service._notify_quotation_recipient)
    assert "enqueue_email_after_commit" in auth_source
    assert "notification_service.send_email(" not in auth_source
    assert "enqueue_email_after_commit" in quotation_source
    assert "notification_service.send_email(" not in quotation_source


def test_api_registers_db_concurrency_guard():
    import main
    source = inspect.getsource(main)
    assert "DBConcurrencyMiddleware" in source


def test_post_commit_notification_state_is_cleared_on_rollback():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("services", "notification_service.py").read_text()
    assert 'event.listen(Session, "after_rollback", _clear_pending_email_notifications)' in source
    assert 'session.info.pop("pending_email_notifications", None)' in source


def test_notification_send_path_calls_transport_once():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("services", "notification_service.py").read_text()
    assert source.count("_send_via_smtp(recipients, subject, body)") == 1


def test_background_db_tasks_do_not_hold_sessions_during_email_io():
    from pathlib import Path
    for filename, function_name in (("notification_tasks.py", "send_overdue_notifications"), ("notification_tasks.py", "send_due_soon_reminders"), ("sla_tasks.py", "escalate_pending_extension_requests"), ("sla_tasks.py", "escalate_pending_quotations")):
        source = Path(__file__).resolve().parents[1].joinpath("tasks", filename).read_text()
        start = source.index(f"def {function_name}")
        body = source[start:]
        assert body.index("db.close()") < body.index("notification_service.send_email"), (filename, function_name)


def test_background_db_admission_is_distributed_and_bounded():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("db_admission.py").read_text()
    assert "nx=True" in source
    assert "ex=300" in source
    assert "eval(_RELEASE_SCRIPT" in source


def test_celery_prefetch_does_not_hide_background_db_queue():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("celery_app.py").read_text()
    assert "worker_prefetch_multiplier=1" in source
