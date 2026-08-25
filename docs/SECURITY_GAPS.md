# Security & CVE-Hygiene Gap Register

Snapshot from a manual audit of this repo's OWASP-adjacent posture and
CVE/dependency hygiene (2026-08-24) — checked against the actual code and
CI config, not against what the docs claim. Most of the codebase is
well above average (Argon2id hashing, real per-account + cross-replica
IP-based lockout on every credential-guessing endpoint, server-side RBAC,
mandatory audit trail, Alembic-gated schema changes, non-root containers,
path-traversal-safe backup restore, per-route CSP, Actions pinned to
commit SHAs). The gaps below are specific and real, not general advice —
each one was verified against a file/line in this repo.

**How to use this file:** treat it like a backlog, not a one-time report.
When a gap is closed, move its row to "Closed" with the PR/commit that
fixed it instead of deleting it — that history is what tells the next
person "we know about this, it's handled" instead of them re-discovering
it. Re-run this audit (or at least re-check the P0/P1 rows) whenever a new
deployable service is added or `dependabot.yml`/`ci.yml` is restructured.

## Priority scale

| Score | Meaning |
|---|---|
| **P0 — Critical** | Actively exploitable or a live blind spot on production traffic. Fix before or alongside the next release touching that area. |
| **P1 — High** | Not an active hole, but a real, verified gap in detection/coverage that *will* eventually let something through silently. Schedule deliberately, don't let it drift. |
| **P2 — Medium** | Defense-in-depth. Correct as shipped; closing it reduces blast radius of a *future* mistake rather than fixing a current one. |
| **P3 — Low / informational** | Documented, deliberate trade-off, or low-value-if-fixed. Track for awareness; not worth interrupting other work for. |

---

## Needs active revision (P1)

### P1 — CodeQL (SAST) is report-only, not yet enforced
**Where:** `.github/workflows/ci.yml`'s `sast-codeql` job.

CodeQL now runs on every PR/push touching Python or JS/TS (added
2026-08-24 — see "Closed" below), but nothing in this pipeline blocks a
merge on what it finds yet: `codeql-action/analyze` uploads to the
repo's Security → Code scanning tab as alerts and exits 0 regardless, by
design. This is a deliberate, temporary posture (see
`docs/SECURITY_CI_POLICY.md`'s CodeQL row) — a brand-new scanner with no
triaged baseline would otherwise block the very next unrelated PR on
whatever backlog it surfaces on day one — but it means this repo's own
documented Critical/High-blocks policy doesn't actually apply to SAST
findings yet, same category of gap the other three P1s below just
closed.
**Fix:** once the initial findings from the first few runs are triaged
(fixed, or explicitly accepted with a documented reason), turn on a
branch-protection "required status check" against the code-scanning
alert severity threshold (Settings → Code security → Code scanning →
protection rules), the same enforcement model GitHub's own docs
recommend for CodeQL specifically, since the workflow's own exit code
was never the right lever for this tool.

---

## Defense-in-depth (P2 — correct as shipped, worth closing eventually)

### P2 — No CSRF token; relies solely on `SameSite=Lax` + CORS allowlist
**Where:** `backend/api/auth_api.py`'s `_set_session_cookie()`.

The session cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` in
production — a reasonable modern baseline, and every mutating route is
also behind the CORS origin allowlist. But `SameSite=Lax` still permits
the cookie on some top-level cross-site navigations (a plain link click,
not just same-site requests), and this protection silently degrades if a
future contributor ever adds a GET-based mutating endpoint by mistake —
there's nothing today that would catch that at review time.
**Fix (optional hardening, not urgent):** add a double-submit CSRF token
on state-changing routes, or at minimum a lint/test that fails if any
GET route mutates state.

### P2 — Two internal SQL queries interpolate identifiers via f-string
**Where:** `backend/services/backup_service/restore_impl.py:145`
(`f"SELECT * FROM {table_name}"`) and
`backend/services/audit_partition_service.py:169`
(`f"SELECT count(*), ... FROM {partition_name}"`).

Not exploitable today — both `table_name`/`partition_name` values come
from internal enumeration (`sa_inspect(...).has_table()` results, a
`pg_inherits` system-catalog query), never from user input. But neither
call site has an allowlist/regex assertion enforcing that invariant, so
it's one careless refactor (e.g. a future "let an admin pick which table
to snapshot" feature) away from becoming a real injection point with no
guard rail to catch it in review.
**Fix:** add an explicit `assert re.fullmatch(r"[a-zA-Z0-9_]+", name)` (or
equivalent) at each call site, purely as a tripwire for future changes —
functionally redundant today, cheap insurance later.

---

## Low priority / informational (P3 — leave as-is, just be aware)

### P3 — Gitleaks scans working tree only, not full git history
**Where:** `.github/workflows/ci.yml`, `secret-scan` job
(`GITLEAKS_ARGS: "detect --no-git -v"`, `fetch-depth: 50`).

A secret committed and later removed stays undetected by this job — it
only ever sees the current state of files, not history. This is a
documented, deliberate trade-off already (see the job's own comments:
determinism against `.gitleaks.toml`'s allowlist over exhaustive history
coverage), with an explicit fallback (`gitleaks git .` locally) called
out for when full coverage is actually needed. No action needed; just
don't assume this job would catch a secret that was committed and
reverted three months ago.

### P3 — Legacy `frontend/` still gets more Dependabot attention than it needs
**Where:** `.github/dependabot.yml`.

`build-frontend`, `build-tailwind`, and `frontend/tests` are all
frozen/legacy-frontend tooling and still get weekly Dependabot PRs. Not
wrong — frozen code still needs security patches — just worth noting
alongside the P0 above: the *coverage* isn't the problem here, the
*allocation* is (legacy gets 3 entries, the active frontend gets 0).
Once the P0 above is fixed, no further action needed here.

---

## Closed

### P1 — `pip-audit` never fails CI — closed 2026-08-24
**Was:**
```yaml
run: pip-audit -r backend/requirements.txt --desc || true
```
The `|| true` meant a CRITICAL CVE in a pinned backend dependency printed
to a log nobody was required to read — it could never fail a PR or block
a merge, inconsistent with the Trivy image scan two jobs later, which
*does* block on CRITICAL.
**Fix:** replaced with `scripts/security/pip_audit_gate.py`, run in both
the `backend` and `errorbeacon` jobs. Since neither of pip-audit's two
vulnerability backends (PyPI's own advisory DB, OSV) attaches a
CVSS/severity rating to anything they return (verified against
`pip_audit`'s own source — see the script's docstring), the gate uses
fixability as the actionable signal instead: a finding with a
`fix_versions` entry blocks the build (mirrors Trivy's own
`ignore-unfixed` reasoning); one with no fix yet is reported, not
blocking. Explicit, time-boxed exceptions live in
`.security/pip-audit-exceptions.json` (empty as of this audit — every
real entry needs a `reason` and a `review_by` date; an expired one
reverts to blocking and fails the gate script until re-reviewed).
See `docs/SECURITY_CI_POLICY.md` for how this fits the repo-wide
Critical/High-block, Medium/Low-report policy.

### P1 — HSTS is missing on the primary production deploy paths — closed 2026-08-24
**Was:** `Caddyfile` (VM path) set `Strict-Transport-Security`; neither
`nginx/default.conf.template` nor `nginx/default.react.conf.template`
(the Azure Container Apps + Render paths — the two most-used/documented
deploy targets) did, despite both carefully tuning a full CSP.
**Fix:** added `Strict-Transport-Security "max-age=15552000;
includeSubDomains"` (`always`) to every header block in both templates
(each location re-declares its own header set — `add_header` doesn't
inherit into a location that declares any of its own, same nginx gotcha
already called out elsewhere in these files). Sent **unconditionally**,
not gated on `$scheme = https` as first sketched above: this container
never terminates TLS itself (no `ssl_certificate` anywhere in either
template — it just `listen`s on plain `${PORT}`), and both Render and ACA
terminate TLS at their own edge and forward plain HTTP here, so nginx's
own `$scheme` is "http" on every request it ever sees, including the
ones that arrived over HTTPS. A scheme gate would silently never fire in
production. This is safe per RFC 6797 §6.1: a compliant browser only
ever *honors* an HSTS header it received over an actual HTTPS connection
and ignores it over plain HTTP, so sending it unconditionally can't
downgrade anything — it matches the Caddyfile's own unconditional
posture. Full rationale is inline in both templates' comments.

### P1 — No static application security testing (SAST) — closed 2026-08-24
**Was:** Gitleaks (secrets) and pip-audit/Trivy/npm-audit (known-CVE SCA)
were all present, but nothing statically analyzed this repo's *own*
logic — no CodeQL, Semgrep, or Bandit anywhere.
**Fix:** added a `sast-codeql` job to `.github/workflows/ci.yml`,
matrixed over `python` (backend/, errorbeacon/, scripts/) and
`javascript-typescript` (frontend-app/, frontend/js/), gated on the same
`changes` path-filter outputs other jobs already use. Pinned to a commit
SHA (`github/codeql-action@8aad20d...` / v4.36.2), not a mutable tag —
same supply-chain rationale as the Trivy step's own SHA-pin (this repo's
stated policy is *every* third-party Action pinned, no exceptions for
first-party GitHub actions). Report-only for now, deliberately — see the
new P1 immediately above this entry for why, and the concrete follow-up
to actually enforce it.

### P0 — `errorbeacon/` had zero CVE/vulnerability coverage — closed 2026-08-24
**Was:** not in any CI-time Docker build matrix (only built/Trivy-scanned
at release/deploy time via `build-push-images.yml`, so a PR introducing a
vulnerable dependency wasn't caught until release), its CI job ran
`pytest tests` only with no `pip-audit`, and it wasn't listed in
`dependabot.yml` under `pip` or `docker` at all.
**Fix:**
- `.github/workflows/ci.yml`'s `errorbeacon` job now runs `pip-audit -r
  requirements.txt --desc` (report-only, `|| true` — matched backend's
  existing posture at the time; both were later upgraded to the blocking
  `scripts/security/pip_audit_gate.py` — see the separate, now-closed
  "`pip-audit` never fails CI" P1 entry above).
- `.github/workflows/ci.yml`'s `build-and-scan-images` job now builds
  `errorbeacon/Dockerfile` and runs the same CRITICAL (blocking) +
  HIGH (report-only) Trivy scan pair backend/frontend already get,
  gated on `changes.outputs.errorbeacon` the same way the other images
  are.
- `errorbeacon/requirements-dev.txt` now pulls in `pip-audit`.
- `.github/dependabot.yml` now has both a `pip:/errorbeacon` and a
  `docker:/errorbeacon` entry.

### P0 — `frontend-app/` (the active, shipping frontend) had no dependency-vulnerability coverage — closed 2026-08-24
**Was:** `dependabot.yml` tracked `build-frontend`, `build-tailwind`, and
`frontend/tests` (all legacy/frozen-frontend tooling) but not
`frontend-app`, the React/Vite SPA that's the default everywhere and the
only frontend receiving new features; no `npm audit` ran anywhere for it
(`--no-audit` on every `npm ci`); and Trivy only inspects the *built*
runtime image, which discards `node_modules` after `vite build`.
**Fix:**
- `.github/dependabot.yml` now has an `npm:/frontend-app` entry.
- `.github/workflows/ci.yml`'s `frontend-app` job now runs `npm audit
  --omit=dev` (report-only, `|| true` — same posture as backend's/
  errorbeacon's pip-audit at the time) right after `npm ci`. Later split
  into a blocking `--audit-level=high` step plus a separate report-only
  full-severity step — see `docs/SECURITY_CI_POLICY.md`'s scanner table.
