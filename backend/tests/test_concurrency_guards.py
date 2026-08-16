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
