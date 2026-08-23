"""
services/backup/storage.py
---------------------------
Filesystem-level primitives for the local backup cache: the on-disk
`index.json` (backup metadata) and the `pg_dump`/`psql` connection-kwargs
helper. No locking, no restore logic -- just "where do backups live and
what do we know about them".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading

from config import settings

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"
_index_lock = threading.Lock()


def _ensure_backup_dir() -> str:
    os.makedirs(settings.BACKUP_DIR, exist_ok=True)
    return settings.BACKUP_DIR

def _index_path() -> str:
    return os.path.join(_ensure_backup_dir(), INDEX_FILENAME)

def _load_index() -> list[dict]:
    path = _index_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("backup_service: index.json is missing/corrupt -- starting a fresh index.")
        return []

def _save_index(entries: list[dict]) -> None:
    path = _index_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(entries, f, indent=2, default=str)
    os.replace(tmp_path, path)  # atomic on POSIX -- never leaves index.json half-written

def _db_connection_kwargs() -> dict:
    """
    Parses settings.DIRECT_DATABASE_URL (falling back to settings.DATABASE_URL) (a standard postgresql:// URL) into the
    discrete pieces pg_dump/psql want on the command line, and returns them
    alongside a PGPASSWORD-carrying env dict -- passing the password via
    argv (e.g. `-p<password>`) would leak it into `ps aux` output on the
    same box, whereas an env var passed only to this one subprocess does
    not.

    BUG FIX #1 -- "Backup failed: pg_dump failed (exit 1): ... FATAL:
    password authentication failed for user '...' ... FATAL: no
    pg_hba.conf entry for host '...', ..., no encryption" on Azure, on a
    password that was NEVER changed/rotated (ruled that out -- don't
    mis-diagnose this as drift between the GitHub secret and the live
    server, like this comment itself used to).

    BUG FIX #2 -- "Backup failed: Port could not be cast to integer value
    as '<fragment>'" / "Restore failed: ... same" (System Backups panel,
    ANY deployment -- local docker-compose included, not just Azure/VM).
    A password generated with `openssl rand -base64 24` (exactly what
    DEPLOYMENT.md/DEPLOYMENT_VM.md tell you to run) routinely contains
    `+`, `/`, or `=` (base64's alphabet). If that raw password ends up in
    DATABASE_URL without being percent-encoded first -- easy to do
    locally, since nothing enforces it the way infra/main.bicep's
    `uriComponent()` / sync-secrets-vm.yml's `urllib.parse.quote()` do for
    cloud deployments -- a raw unescaped `/` in the password prematurely
    ends the URL's `netloc` (it's the path delimiter), which shoves the
    REAL host/port into what Python's `urllib.parse` now thinks is the
    path, leaving a leftover fragment of the password itself sitting
    where the port should be. `ParseResult.port` then raises exactly this
    ValueError trying to `int()` that fragment (e.g. `int("2by8lh")`).

    This function used to parse DATABASE_URL with `urllib.parse.urlparse()`
    (bug #1's original fix decoded the result with `unquote()`, which
    handled a PROPERLY percent-encoded password fine, but never protected
    against a raw, still-unescaped one reaching urlparse in the first
    place). SQLAlchemy's own `make_url()` -- what `database.py`'s
    `create_engine()` actually uses for every real query the running app
    ever makes -- parses the exact same raw string correctly regardless
    of whether it's percent-encoded, which is exactly why login/
    migrations/normal use all worked even on a raw base64 password while
    backups alone crashed: `_db_connection_kwargs()` was the only
    consumer of DATABASE_URL in this app still using the stricter,
    RFC 3986-literal `urlparse()` instead of SQLAlchemy's own parser.
    Switching to `make_url()` here makes pg_dump/psql see the exact same
    host/port/user/password the live app itself successfully connects
    with, however DATABASE_URL happens to be formatted -- percent-encoded
    or raw -- instead of requiring a second, independent encoding
    convention just for this one function.

    BUG FIX #3 -- `?sslmode=require` was ALSO being silently dropped
    entirely by the old urlparse-based code -- `urlparse(...).query` was
    parsed and then never read anywhere, so with no sslmode communicated
    any other way, libpq fell back to its own default (`prefer`), whose
    plaintext-fallback attempt is what Azure's Flexible Server (which
    enforces SSL -- see infra/main.bicep's `postgresServer` comment)
    rejects with "no encryption", a second FATAL line alongside bug #1's.
    `PGSSLMODE` (an env var libpq itself reads, same as `PGPASSWORD`) is
    honored identically by both `pg_dump` and `psql` without needing to
    touch either's argv, and defaults to `"prefer"` only as a last resort
    if DATABASE_URL genuinely has no `sslmode` at all (e.g. local
    docker-compose, which talks to `db` on the same Docker network with
    no TLS involved) -- every Azure deployment's DATABASE_URL always has
    `sslmode=require` already baked in, so this preserves that exact
    value rather than hardcoding `require` unconditionally here.

    All three bugs independently produced part of that error output --
    none alone was the whole story, which is why past reports of this
    looked like unrelated failures concatenated together.
    """
    from sqlalchemy.engine import make_url

    url = make_url((settings.DIRECT_DATABASE_URL or settings.DATABASE_URL))
    sslmode = url.query.get("sslmode", "prefer")
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    env["PGSSLMODE"] = sslmode
    return {
        "host": url.host or "localhost",
        "port": str(url.port or 5432),
        "user": url.username or "postgres",
        "dbname": url.database or "postgres",
        "env": env,
    }

def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(
            f"'{name}' is not installed in this container. Add the "
            "`postgresql-client` OS package to the Dockerfile (see "
            "Dockerfile.render / backend/Dockerfile) -- pg_dump/psql do NOT "
            "ship with the psycopg2-binary Python package."
        )
