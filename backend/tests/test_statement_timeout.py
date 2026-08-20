# Covers database.set_transaction_statement_timeout(): it must apply the
# limit via `SET LOCAL` (scoped to the current transaction only, so it
# can't leak onto a pooled connection's next, unrelated transaction) and
# reject non-positive millisecond values outright. Uses lightweight fakes
# instead of a real Postgres connection so this test needs no DB at all
# and can assert on the exact SQL string that would have been sent.
import pytest

import database


class _FakeConn:
    """Stand-in DB-API connection that just records every statement sent
    to it via exec_driver_sql, instead of actually executing anything."""

    def __init__(self):
        self.statements = []

    def exec_driver_sql(self, statement):
        self.statements.append(statement)


class _FakeDialect:
    # set_transaction_statement_timeout branches on dialect name (this
    # timeout mechanism is Postgres-specific); postgresql is the only
    # dialect this app ever runs against, so it's the only one exercised.
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeSession:
    """Minimal stand-in for a SQLAlchemy Session exposing just the two
    methods set_transaction_statement_timeout actually calls."""

    def __init__(self):
        self.conn = _FakeConn()

    def connection(self):
        return self.conn

    def get_bind(self):
        return _FakeBind()


def test_per_operation_statement_timeout_uses_set_local(monkeypatch):
    fake = _FakeSession()

    database.set_transaction_statement_timeout(fake, 60000)

    # Must be SET LOCAL, not plain SET -- SET LOCAL automatically reverts
    # at the end of the transaction, so a pooled (e.g. PgBouncer) connection
    # handed to a different request afterward never inherits this timeout.
    assert fake.conn.statements == ["SET LOCAL statement_timeout = 60000"]


def test_per_operation_statement_timeout_rejects_non_positive(monkeypatch):
    fake = _FakeSession()

    # 0 (or negative) would mean "no timeout" in Postgres semantics, which
    # defeats the whole point of calling this helper -- fail fast instead.
    with pytest.raises(ValueError):
        database.set_transaction_statement_timeout(fake, 0)
