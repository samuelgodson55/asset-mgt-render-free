import pytest

import database


class _FakeConn:
    def __init__(self):
        self.statements = []

    def exec_driver_sql(self, statement):
        self.statements.append(statement)


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeSession:
    def __init__(self):
        self.conn = _FakeConn()

    def connection(self):
        return self.conn

    def get_bind(self):
        return _FakeBind()


def test_per_operation_statement_timeout_uses_set_local(monkeypatch):
    fake = _FakeSession()

    database.set_transaction_statement_timeout(fake, 60000)

    assert fake.conn.statements == ["SET LOCAL statement_timeout = 60000"]


def test_per_operation_statement_timeout_rejects_non_positive(monkeypatch):
    fake = _FakeSession()

    with pytest.raises(ValueError):
        database.set_transaction_statement_timeout(fake, 0)
