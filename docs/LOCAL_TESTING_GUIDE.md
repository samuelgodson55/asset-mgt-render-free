# Local Testing Guide — Snipe-IT Lite (pgbouncer branch)

This walks through testing the repo locally (Docker Desktop + Ubuntu-WSL)
without deploying anywhere. It only tells you **what to run** — none of
these commands have been executed for you.

Repo layout you're working with: `backend/` (FastAPI), `frontend/`
(legacy static site), `frontend-app/` (React/Vite), `errorbeacon/`
(monitoring service), `pgbouncer` + `db` + `redis` (docker-compose
services). This matches what CI (`.github/workflows/ci.yml`) runs, so
passing these locally is a strong predictor of a green CI run.

---

## 0. Recommended setup: do the work inside Ubuntu-WSL

Docker Desktop with the **WSL2 backend** + running commands from your
Ubuntu-WSL shell (not PowerShell) avoids most of the classic Windows
path/line-ending/volume-mount headaches. Confirm this first:

```bash
# In Ubuntu-WSL
docker info | grep -i "operating system"   # should mention Docker Desktop
docker compose version
```

If Docker Desktop isn't visible from WSL, open Docker Desktop →
Settings → Resources → WSL Integration → enable it for your Ubuntu
distro.

Clone/copy the repo **inside the Linux filesystem** (e.g.
`/home/<you>/projects/...`), not under `/mnt/c/...` — builds are
noticeably slower and file-watching/permissions get flaky across the
Windows/Linux boundary.

---

## 1. One-time environment setup

```bash
cd <repo-root>
cp .env.example .env
```

Open `.env` and fill in the placeholders. At minimum:

- `JWT_SECRET_KEY` — must be ≥32 chars for anything other than pure
  local/dev checks (generate one: `openssl rand -hex 32`)
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` — any values,
  they only need to be internally consistent
- Leave `ENVIRONMENT=development` and `USE_PGBOUNCER=true` (default) so
  you're actually exercising the pgbouncer path you're debugging.

You do **not** need to fill in Azure/SMTP/ErrorBeacon-remote values just
to run the stack and tests locally — those only matter for
production-shaped checks.

---

## 2. Bring up the full stack (this is the main pgbouncer test)

```bash
docker compose up --build
```

This starts, in order: `db` → `pgbouncer` → `redis` → `migrate`
(runs `alembic upgrade head` once) → `backend` → `worker` → `beat` →
`frontend` (nginx). Everything is exposed on **one origin**:

- App/API: http://localhost:8080
- Swagger docs: http://localhost:8080/docs

**Which frontend you get:** `docker compose up --build` ships the React
"Ledger" SPA (`frontend-app/`) at http://localhost:8080 by default —
`docker-compose.yml`'s `frontend.build.args.target` now defaults to
`frontend-react-only`, matching what the ACA/VM CI deploy paths already
default to. If you need to test the older vanilla-JS site
(`frontend/index.html`/`admin.html`/etc.) instead, set
`FRONTEND_BUILD_TARGET=frontend-legacy-only` in `.env` and rebuild just
that service:

```bash
docker compose up -d --build frontend
```

Switching back later means setting `FRONTEND_BUILD_TARGET` back to
`frontend-react-only` (or removing the line) and rebuilding the same way
— editing `.env` alone does **not** change an already-running container,
since `target` is a build-time arg, not a runtime env var.

**What to check first**, since this is the pooling fix:

```bash
docker compose ps                      # everything should be "healthy"/"running", no restart loops
docker compose logs pgbouncer --tail=50
docker compose logs backend --tail=100 | grep -i "pool\|pgbouncer\|admission"
```

If `pgbouncer` never becomes healthy, `backend`/`worker`/`beat` will sit
waiting on `depends_on: condition: service_healthy` and never start —
so a stuck pgbouncer container is a common root cause if the whole
stack seems to hang.

To watch just the pool health while you exercise the app:

```bash
curl -s http://localhost:8080/healthz | jq .
curl -s http://localhost:8080/readyz  | jq .     # readyz checks DB reachability + schema version
```

Tear down between runs with `docker compose down` (add `-v` only if you
want to wipe the Postgres/Redis volumes and start from a truly empty
DB).

---

## 3. Backend tests (`backend/tests`)

### Option A — run them inside the running stack

**Heads up:** the `backend` image is a lean, production-shaped runtime —
`backend/Dockerfile`'s builder stage only ever runs
`pip install -r backend/requirements.txt`, and that file has no test
dependencies in it (no `pytest`, `pytest-asyncio`, or `httpx`). So
`docker compose exec backend pytest ...` fails out of the box with
`executable file not found in $PATH` — `pytest` was never installed in
this image, on purpose. (`README.md` documents this same
`docker compose exec backend pytest backend/tests` command; that line
is wrong on two counts — the missing `pytest` binary, and the path, see
below — treat this guide as the corrected version.)

To actually run tests this way, install the test deps into the running
container first (this only affects that container's current process, not
the image — it's gone on the next restart/rebuild):

```bash
docker compose exec backend pip install pytest pytest-asyncio httpx
docker compose exec backend pytest tests -v
```

NOTE: this is NOT `pytest backend/tests` — `backend/Dockerfile` does
`COPY backend/ /app/` (not `COPY backend/ /app/backend/`), so everything
that lives under `backend/` on your host — `tests/`, `main.py`,
`pytest.ini`, etc. — lands directly at `/app/` inside the container, with
no `backend/` subfolder. `docker compose exec backend` drops you into
that container already at `WORKDIR /app`, so the path is just `tests`,
not `backend/tests`. (CI's `pytest backend/tests -v` in
`.github/workflows/ci.yml` is a *different*, native invocation from the
repo root, where `backend/tests` really is the correct relative path —
see Option B below.)

Target one file the same way, e.g.:

```bash
docker compose exec backend pytest tests/test_pool_sizing.py -v
docker compose exec backend pytest tests/test_db_admission_and_notification_resilience.py -v
```

Those two files are the ones most directly relevant to a connection-pool
exhaustion bug, so run them first/in isolation while iterating.

Because the install above doesn't persist, you'll re-run it every time you
recreate the `backend` container. **Option B below doesn't have this
problem** and is the better default for repeated local iteration — reach
for Option A only when you specifically need to test *inside* the built
image/container environment itself.

### Option B — run natively (faster iteration loop, no rebuild needed)

Most of `backend/tests` uses a throwaway SQLite DB per test (see
`backend/tests/conftest.py`), so it doesn't need Docker at all — except
two files that need a **real** Postgres/Redis:

- `test_migrations.py` — runs the real `alembic upgrade head`/`downgrade`
  chain (SQLite can't do the `ALTER TABLE ... ADD FOREIGN KEY` the
  baseline migration needs).
- `test_redbeat_scheduling.py` — exercises the real RedBeat distributed
  lock, needs real Redis.

Set up a venv and point those two at your already-running compose `db`/
`redis` containers.

**If you already have a `.venv` at the repo root** (as in your screenshot)
and don't want a second one inside `backend/`, stay at the repo root and
just point `pip`/`pytest` at the `backend/` paths explicitly instead of
`cd`-ing in:

```bash
# from the repo root, with your existing .venv activated
source .venv/bin/activate
pip install -r backend/requirements.txt
pip install pytest pytest-asyncio httpx ruff==0.15.22

# db/redis ports are already published to localhost by docker-compose.yml
export TEST_POSTGRES_HOST=localhost
export TEST_POSTGRES_PORT=5432
export TEST_POSTGRES_USER=admin          # matches your .env POSTGRES_USER
export TEST_POSTGRES_PASSWORD=<your POSTGRES_PASSWORD from .env>
export TEST_REDIS_HOST=localhost
export TEST_REDIS_PORT=6379

pytest backend/tests -v
```

This works with no `cd` at all — `backend/tests/conftest.py` puts
`backend/` on `sys.path` itself (see its own top-of-file docstring), so
`import main` / `import models` etc. inside the tests resolve correctly
regardless of which directory you invoked `pytest` from. This is also
the exact same invocation CI uses (`pytest backend/tests -v` from the
repo root — see `.github/workflows/ci.yml`), so it's the closest match
to "will this pass CI" of any option in this guide.

Alternatively, a dedicated venv **inside** `backend/` (below) keeps your
root `.venv` free of backend-only packages if you're also using the root
one for something else (e.g. `build-frontend`'s tooling, other scripts):

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx ruff==0.15.22

# db/redis ports are already published to localhost by docker-compose.yml
export TEST_POSTGRES_HOST=localhost
export TEST_POSTGRES_PORT=5432
export TEST_POSTGRES_USER=admin          # matches your .env POSTGRES_USER
export TEST_POSTGRES_PASSWORD=<your POSTGRES_PASSWORD from .env>
export TEST_REDIS_HOST=localhost
export TEST_REDIS_PORT=6379

pytest tests -v
```

Either venv works — pick whichever layout you'd rather maintain. The rest
of this guide assumes either one is active, and calls out `tests` vs.
`backend/tests` per command based on which directory you're in.

(If `db`/`redis` aren't already up, run `docker compose up -d db redis`
first.)

### Speeding up the full run + getting a clear failure summary

At 318 tests, a plain `pytest backend/tests -v` gets slow — mostly setup
overhead, since `conftest.py`'s own docstring notes it deliberately
re-runs `init_db()` + `seed_db()` against a brand-new SQLite file for
*every single test function* (chosen for isolation, not speed). A few
flags make this both faster and easier to read:

```bash
pytest backend/tests --tb=short -ra --durations=15
```

- `--tb=short` — shorter tracebacks per failure (default `--tb=long` is
  noisy at this test count)
- `-ra` — prints a one-line summary of every non-passing test
  (failed/error/skipped) at the very end, so you don't have to scroll
  back through hundreds of lines to see what broke
- `--durations=15` — prints the 15 slowest tests/fixtures at the end, so
  you can see whether slowness is one or two outliers vs. broadly
  distributed per-test setup cost (this suite's design points at the
  latter, per the note above)

Drop `-v` for full-suite runs once you're past initial setup — the
end-of-run `-ra` summary already tells you what failed; the extra
per-test PASSED lines mostly just add terminal I/O overhead across
hundreds of tests.

Note on failing without stopping: pytest's default is to run every
collected test regardless of failures — it only stops early if you pass
`-x` (stop on first failure) or `--maxfail=N`. Neither is needed here;
the flags above just make the full run's *output* easier to read, they
don't change what runs.

**Parallelize with `pytest-xdist`** (usually the single biggest speed
win, since each test's SQLite isolation makes tests safe to run
concurrently):

```bash
pip install pytest-xdist
pytest backend/tests -n auto --tb=short -ra
```

`-n auto` spins up one worker per CPU core. Skip `-v` when using `-n` —
xdist's own per-worker progress output doesn't combine well with
verbose per-test lines.

**Skip the two Postgres/Redis-backed files** while iterating on
something unrelated to migrations/RedBeat — they're the only files in
the suite doing real network I/O, so skipping them removes a chunk of
wall-clock time even before parallelizing:

```bash
pytest backend/tests \
  --ignore=backend/tests/test_migrations.py \
  --ignore=backend/tests/test_redbeat_scheduling.py \
  --tb=short -ra
```

**Target just the file(s) you're actively changing**, and only run the
full suite before committing/pushing:

```bash
pytest backend/tests/test_asset_pools.py --tb=short -v
```

**Keep a log to grep later** instead of scrolling, if you do want the
full `-v` output on hand:

```bash
pytest backend/tests -v --tb=short 2>&1 | tee /tmp/pytest-run.log
# afterward:
grep -E "FAILED|ERROR" /tmp/pytest-run.log
```

**Skip ≠ fail — watch for this specifically with `test_migrations.py`.**
If Postgres isn't reachable at `TEST_POSTGRES_HOST:TEST_POSTGRES_PORT`
(e.g. your `docker compose` stack isn't up, or the env vars from the
block above aren't exported in your current shell), that file calls
`pytest.skip(...)` rather than failing — so a broken/missing Postgres
connection shows up as a quiet skip in the `-ra` summary, not a red
failure. If you're specifically trying to verify a migration change,
double-check the `-ra` summary doesn't show `SKIPPED` for
`test_migrations.py` before trusting a green run.

### Lint + migration-safety gate (also part of CI, cheap to run first)

```bash
ruff check backend/
python scripts/check-migration-safety.py
```

The migration-safety script specifically fails if a new Alembic
migration is destructive/non-backward-compatible — worth running any
time you touched `backend/alembic/versions/`.

---

## 4. PgBouncer-specific smoke/load scripts

These are the dependency-light scripts in `scripts/` built for exactly
this class of bug. Run them against the stack from step 2 (backend
reachable at `http://localhost:8080`):

```bash
# Sustained load to try to reproduce pool exhaustion under concurrency
python3 scripts/load-test.py --url http://localhost:8080/healthz \
  --requests 500 --concurrency 25

# Same, but against an authenticated endpoint (uses your seeded/demo login)
python3 scripts/load-test.py --base-url http://localhost:8080 \
  --login-email <seeded-admin-email> --login-password <seeded-admin-password> \
  --path /api/assets?limit=25 --requests 300 --concurrency 20
```

Watch `docker compose logs pgbouncer -f` and `docker compose logs backend -f`
in another terminal while this runs — this is where you'll actually see
pool-exhaustion errors (`no more connections allowed`, timeouts on
`RESERVE_POOL_TIMEOUT`, etc.) if the fix isn't complete.

There's also a chaos/restart resilience script if you want to test what
happens to in-flight connections across a backend restart:

```bash
cat scripts/chaos-test.sh          # read it first — check what flags/URL it expects
./scripts/chaos-test.sh
```

`scripts/db-restart-resilience-test.py` is a 3-phase script meant for a
real cloud restart between phases (Azure), so it's not a great fit for
plain local Docker Compose — skip it unless you adapt it.

---

## 5. Frontend tests

### Legacy static frontend (`frontend/`)

```bash
cd build-frontend && npm ci && cd ..
cd frontend/tests && npm ci && npm test
```

This checks HTML validity, no duplicate IDs, that every asset
reference/import resolves, and that the login form is correctly wired —
against both raw source and a built `BUILD_ENV` bundle.

### React/Vite frontend (`frontend-app/`)

```bash
cd frontend-app
npm ci
npm run lint     # oxlint
npm test         # vitest run — jsdom + Testing Library
npm run build    # tsc -b && vite build — catches type errors too
```

Requires Node 24 to match CI (`actions/setup-node@v7` pins `node-version: '24'`).
Check your version first:

```bash
node -v
```
If you're on an older Node via WSL, install/switch with `nvm` first.

---

## 6. ErrorBeacon service tests (only if you touched `errorbeacon/`)

```bash
cd errorbeacon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests -v
```

---

## 7. Suggested order (fastest feedback first)

1. `ruff check backend/` + `scripts/check-migration-safety.py` (seconds)
2. `backend` pytest, focused on `test_pool_sizing.py` and
   `test_db_admission_and_notification_resilience.py`
3. Full `docker compose up --build`, watch `pgbouncer`/`backend` logs
   for health
4. `scripts/load-test.py` against the running stack to try to reproduce
   the original exhaustion symptom under load
5. Full `backend` pytest suite (`test_migrations.py` /
   `test_redbeat_scheduling.py` included) — use `-n auto` (pytest-xdist)
   + `--tb=short -ra` if it's dragging; see "Speeding up the full run"
   under section 3 above
6. Frontend suites (`frontend/tests`, `frontend-app`) — lower priority
   if your recent changes were backend/infra-only

---

## 8. Quick troubleshooting notes

- **Whole stack hangs on startup** → check `docker compose logs pgbouncer`
  first; `backend`/`worker`/`beat` all block on its healthcheck.
- **`migrate` service exits non-zero** → `db` came up but schema/migration
  itself failed; `docker compose logs migrate` will show the Alembic
  error directly — fix and re-run `docker compose up --build migrate`
  before the rest of the stack will proceed.
- **Port already in use (5432/6379/8080)** → something else on your
  WSL/Windows host is bound to it; `docker compose down` any other stack
  or change the published port in `docker-compose.yml`/`.env`.
- **Tests pass in Docker but not natively (or vice versa)** → almost
  always an env var mismatch — diff what `docker-compose.yml` sets for
  `backend`/`migrate` against what you exported manually in step 3B.

---

## 9. Verifying the latest pooling-fix round (pgbouncer tag / readyz / test suite)

Three things were checked/fixed in this round; here's how to verify each
one locally without running the full `backend` suite.

### 9a. PgBouncer image tag — missing `v` prefix

**Bug:** both `docker-compose.yml` and `docker-compose.vm.yml` pinned
`image: edoburu/pgbouncer:1.25.2-p0`. Docker Hub's actual tag for that
release is `v1.25.2-p0` (confirmed on
[hub.docker.com/r/edoburu/pgbouncer/tags](https://hub.docker.com/r/edoburu/pgbouncer/tags)
and the [GitHub releases
page](https://github.com/edoburu/docker-pgbouncer/releases) — every tag
in that repo, `v1.24.0-p0` through `v1.25.2-p0`, carries the `v`). The
tag without `v` doesn't exist, so `docker compose up --build` /
`docker compose pull pgbouncer` would fail with a "manifest not found"
error and the stack would never come up.

**Fix applied:** both compose files now pin `edoburu/pgbouncer:v1.25.2-p0`.

To verify:

```bash
docker compose pull pgbouncer   # should succeed, no manifest-not-found error
docker compose up -d pgbouncer
docker compose ps pgbouncer     # should show "healthy"
```

### 9b. `readyz` failing locally — root cause was in `database.py`

**Bug:** the dedicated readiness engine (`_create_readiness_engine_from_url`
in `backend/database.py`) set `connect_args={"options": "-c
statement_timeout=3000"}`. psycopg2 sends `options` as a literal field of
the Postgres *startup packet*, not a query. Since `USE_PGBOUNCER=true` by
default, `/readyz` connects through the local `pgbouncer` service — and
`docker-compose.yml`/`docker-compose.vm.yml`'s pgbouncer service only
allow-lists `extra_float_digits` in `IGNORE_STARTUP_PARAMETERS`, not
`options`. Every readiness connection was rejected with `FATAL: unsupported
startup parameter: options` before it ever got to check `alembic_version`
— so `/readyz` always 503'd locally/on the VM regardless of actual schema
state.

**Fix applied:** `_create_readiness_engine_from_url` now only sends
`connect_timeout` in the startup packet, and sets the statement timeout
via a normal `SET statement_timeout = 3000` query on an on-`connect`
event instead — works identically whether the readiness engine is pointed
directly at Postgres or routed through PgBouncer.

To verify without the full suite:

```bash
docker compose up -d db pgbouncer redis
docker compose up --build migrate      # runs alembic upgrade head once
docker compose up -d backend
curl -s http://localhost:8080/readyz | jq .    # should now report "ready": true
```

Or, natively, just the two focused test files this bug lives in:

```bash
cd backend
pytest tests/test_health.py tests/test_chaos_contract.py -v
```

### 9c. `test_email_publish_disables_celery_retry` bug

**Bug:** the test called `ns._publish_email_task(...)` directly. That
function's `finally` block unconditionally releases
`_EMAIL_DISPATCH_SLOTS` (a `threading.BoundedSemaphore`) — it assumes its
caller (`_dispatch_pending_email_notifications_payloads`) already
acquired a slot. Calling it standalone raised `ValueError: Semaphore
released too many times` before the test's own assertion ever ran.

**Fix applied:** the test now stubs `_EMAIL_DISPATCH_SLOTS` with the same
no-op `FakeSemaphore` pattern already used by the neighboring
`test_email_publish_is_scheduled_off_commit_thread` test.

To verify without the full suite:

```bash
cd backend
pytest tests/test_db_admission_and_notification_resilience.py -v
```

### 9d. General syntax/lint pass

`python3 -m py_compile` across `backend/`, `errorbeacon/`, `scripts/`,
`shared/` and `ruff check backend/ --select F,E9` (Pyflakes +
syntax-error rules only, to skip pure style noise) both came back clean
except for two pre-existing unused imports in
`backend/tests/test_db_admission_and_notification_resilience.py`
(`logging`, `contextlib.contextmanager`) — harmless, left as-is since
they were unrelated to the reported bugs. `scripts/check-migration-safety.py`
also passes clean against the current `backend/alembic/versions/` chain
(single head, `0018_credentials_changed_at`, no destructive ops).
