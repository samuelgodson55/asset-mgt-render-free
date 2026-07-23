"""
tests/test_health.py
---------------------
Covers the /healthz vs /readyz split added to close a real gap: the
deploy pipelines (.github/workflows/deploy-azure-*.yml) already run
`alembic upgrade head` as its own blocking step before rolling out a new
image, but nothing previously verified that a given RUNNING container's
schema still matched what its own code expects -- /healthz was (and
still is) a static "yes I'm up" with zero database awareness, so a
bypassed/skipped migration step would have gone undetected by both the
health probe and the deploy pipeline's own smoke test.

/healthz stays dependency-free (see main.py's health_check() docstring
for why a DB check does NOT belong on the LIVENESS probe). /readyz is
the new endpoint that actually queries the database and compares its
current Alembic revision against database.py's get_schema_status().
"""

from sqlalchemy import text

import database


def test_healthz_never_touches_the_database(client):
    """/healthz must stay a pure liveness check -- 200 regardless of
    migration state, since a DB/schema problem should stop traffic being
    routed here (readiness) rather than getting this container killed
    and restarted (liveness), which wouldn't fix either problem anyway."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_readyz_not_ready_when_alembic_version_table_missing(client):
    """The test DB is created via database.init_db()'s create_all() (see
    conftest.py's db_engine fixture), which never creates an
    'alembic_version' table -- the same state a real database would be
    in if `alembic upgrade head` had never been run against it. /readyz
    must report 503 + ready:false with a clear reason, not a crash and
    not a false 200."""
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert "alembic_version" in body["reason"]
    assert body["current_heads"] == []
    assert body["expected_heads"]  # the code's own expected head is always resolvable


def test_readyz_ready_when_alembic_version_matches_expected_head(client, db_session):
    """Once `alembic_version` records exactly the revision this code
    expects, /readyz must report 200 + ready:true."""
    status = database.get_schema_status()
    expected_head = status["expected_heads"][0]

    db_session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": expected_head})
    db_session.commit()

    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["current_heads"] == [expected_head]
    assert body["expected_heads"] == [expected_head]


def test_readyz_not_ready_when_alembic_version_is_stale(client, db_session):
    """A DB stuck on an OLD revision (e.g. the exact "new image, old/wrong
    schema" scenario a bypassed `alembic upgrade head` step would leave
    behind) must be reported as not ready, not silently accepted."""
    db_session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": "some_old_stale_revision"})
    db_session.commit()

    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["current_heads"] == ["some_old_stale_revision"]
    assert "does not match" in body["reason"]
