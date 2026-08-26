# Changelog

## Table of Contents

- [v1.0.10 — 2026-08-24](#v1010---2026-08-24)
- [v1.0.9 — 2026-08-20](#v109---2026-08-20)
- [v1.0.8 — 2026-08-19](#v108---2026-08-19)
- [v1.0.7 — 2026-08-18](#v107---2026-08-18)
- [v1.0.6 — 2026-08-05](#v106---2026-08-05)
- [v1.0.5 — 2026-08-04](#v105---2026-08-04)
- [v1.0.4 — 2026-08-03](#v104---2026-08-03)
- [v1.0.3 — 2026-08-01](#v103---2026-08-01)
- [v1.0.2 — 2026-08-01](#v102---2026-08-01)
- [v1.0.1 — 2026-08-01](#v101---2026-08-01)
- [v1.0.0 — 2026-08-01](#v100---2026-08-01)

This changelog captures the platform's evolution from early deployment work
into a more resilient, production-ready operating model. Each release
reflects how the system has matured in reliability, deployment discipline,
observability, and operational trust.

<!-- release-notes-insertion-point -->

## [Unreleased]

### Added
- Extremely user-friendly, correlation-ID-bearing error handling across
  `frontend-app/`, replacing raw `alert(err.message)`/status-code-flavored
  error strings throughout the customer/staff self-service flows (order
  submission, checkout/dispatch, custody returns & extensions, extension
  requests, profile/security forms) and the Admin/Manager panels:
  - `src/lib/api/core.ts`: `ApiError`/`MaintenanceModeError` now carry the
    failed request's correlation ID (`X-Request-ID` header, falling back to
    a `request_id` field in the JSON body), not just its message.
  - `src/lib/errors.ts` (new): `friendlyError(err, fallbackTitle, opts)`
    classifies any caught failure by status (401/403/404/409/429/503/5xx/
    network) into a plain-language `{title, message, requestId,
    reassurance, retryable}`, replacing the ~9 near-identical local
    `errMsg()` helpers that used to just forward `ApiError.message`
    untouched. `formatFriendlyErrorLine()` collapses one into a single
    line for the app's compact inline status messages (Profile.tsx, Admin
    panels).
  - `src/components/ui/ErrorBanner.tsx`: new `ErrorState` component --
    title, plain-language explanation, an honest reassurance line (e.g.
    "Nothing was changed." / "Your cart is still saved."), a copyable
    "Reference: ..." chip for support, and Try again/Dismiss/custom action
    buttons -- replacing the flat rust-colored `{error}` boxes across
    `Quotations.tsx`, `Checkouts.tsx`, `MyItems.tsx`, `AssetDrawer.tsx`,
    `CreatePoolModal.tsx`, `DispatchModal.tsx`, `QuoteDetailDrawer.tsx`,
    and `CustodyDrawer.tsx`. Bulk actions (bulk return, bulk extend) now
    name exactly which items failed instead of one flattened error string.
  - `tests/lib/errors.test.ts` (new): status-code classification coverage
    plus an end-to-end check that a correlation ID returned by a real
    failed `fetch()` (via header or body) survives into the `FriendlyError`
    a page renders.
  - Full parity pass: `SystemBackupsPanel.tsx`'s restore/backup/delete
    actions (previously two bare `alert(err.message)` calls and one
    string-only restore-modal error) now use the same `ErrorState`/
    `friendlyError` treatment as the rest of the app; `Login.tsx`'s
    pre-authentication error line now appends a reference ID to genuine
    server errors during login/MFA while deliberately keeping backend's
    own wording for wrong-password/wrong-code 401s (routing those through
    the generic "your session expired" classification used elsewhere would
    have been actively wrong before a session exists).
- Double-click/duplicate-request protection for the app's three highest-
  stakes mutating actions -- submitting a quotation cart, dispatching/
  checking out an asset, and creating a stock pool -- layered on top of
  the existing DB-pool defenses (`middleware/db_concurrency.py`'s
  admission semaphore, `with_for_update()` row locking):
  - `backend/services/idempotency.py` (new): `run_idempotent()`, a
    Redis-backed dedupe helper scoped to `(action, user_id,
    Idempotency-Key)` so a retried/duplicated request replays the
    original result instead of re-running the underlying DB write a
    second time. Deliberately implemented as an in-handler helper (called
    only after `Depends(get_current_user)` has resolved) rather than ASGI
    middleware, specifically to rule out one person's guessed/reused key
    ever replaying another person's cached response. Fails open if Redis
    is unreachable, mirroring `db_admission.py`'s existing philosophy --
    the row-level lock inside each action remains the correctness
    backstop either way. Wired into `POST /quotations/submit`,
    `POST /assets/{id}/checkout_advanced`, and `POST /assets`, all via an
    optional `Idempotency-Key` header with zero behavior change for any
    caller that doesn't send one.
  - `frontend-app/src/lib/idempotency.ts` (new): `newIdempotencyKey()`,
    minted once per guarded click and threaded through to the three
    calls above.
  - `frontend-app/src/lib/useActionGuard.ts` (new): a synchronous
    ref-based guard that makes a second, near-simultaneous invocation of
    a wrapped async action (a double-click/double-tap that outraces
    React's next render of the button's `disabled` state) a silent
    no-op -- the cheaper, first line of defense that stops a duplicate
    request from ever being sent, complementing rather than duplicating
    the server-side Idempotency-Key handling above. Wired into
    `Quotations.tsx`'s `submitOrder`, `DispatchModal.tsx`'s dispatch
    submit, and `CreatePoolModal.tsx`'s create submit.
  - Found and fixed a latent bug while wiring this up:
    `rawFetchInternal`'s request-header merge spread `...init` AFTER a
    pre-computed `headers` object, so any caller supplying its own
    `headers` (as the Idempotency-Key work now does) silently overwrote
    -- rather than merged with -- the default `Content-Type:
    application/json`, which would have broken every JSON POST that also
    sent a custom header. Fixed the spread order; regression-tested in
    `tests/lib/api-retry.test.ts`.
  - `backend/tests/test_idempotency.py` (new, 7 tests) and
    `frontend-app/tests/lib/useActionGuard.test.tsx` (new, 4 tests):
    cover the dedupe-replay, cross-user isolation, failed-attempt
    cleanup, Redis-down fail-open, and real-concurrent-duplicate-waits-
    and-replays cases server-side, and the guard's race-prevention
    behavior client-side.
- `docs/SECURITY_GAPS.md`: a priority-scored register of security/CVE-
  hygiene gaps found in a manual OWASP-adjacent audit — two P0s
  (`errorbeacon/` has no CVE scanning of any kind; `frontend-app/`, the
  active frontend, has no dependency-vulnerability coverage), plus P1s
  (non-blocking `pip-audit`, missing HSTS on the nginx/ACA/Render deploy
  path, no SAST) and lower-priority defense-in-depth notes.

### Changed
- Polished `AGENTS.md`: fixed the backend `pytest` invocation (container
  path is `tests`, not `backend/tests`), and added guidance to scope test
  runs to the area actually changed instead of running full suites by
  default, plus notes on config/env conventions, pre-flight drift/
  migration-safety checks, and which `scripts/` tools to skip unless
  asked.

## [v1.0.10] - 2026-08-24

### Highlights
- Documentation-only maintenance pass: added `AGENTS.md` at the repo root
  for AI coding agents, and formally marked the legacy vanilla-JS frontend
  (`frontend/`) as frozen/going-out-of-phase across the docs, so future
  contributors (human or AI) stop investing maintenance effort there.
- Closed real gaps in `docs/SRE_STRATEGY.md` found by auditing it against
  the actual operational surface: PgBouncer, the VM path's Cloudflare
  Tunnel failure mode, orphaned ACA revisions, and the SLA-escalation
  background jobs had no runbook coverage, and the chaos/load/security-
  boundary/session-Redis test scripts weren't part of any recurring
  cadence.

### Added
- Added `AGENTS.md`: repo map, dev/test/lint commands per component,
  conventions, and pointers into `docs/`, written for coding agents working
  in this repo.
- Added `frontend/README.md`: an explicit deprecation notice for the legacy
  frontend, what "frozen" does and doesn't mean, and where new frontend work
  should go instead.

### Changed
- `docs/README.md`, `docs/DEPLOYMENT.md`, `docs/DEPLOYMENT_VM.md`,
  `docs/LOCAL_TESTING_GUIDE.md`, `docs/SRE_STRATEGY.md`, and
  `docs/ERRORBEACON_COVERAGE.md` (plus `errorbeacon/ERRORBEACON_COVERAGE.md`)
  now call out that `frontend/` is frozen wherever they previously described
  it as an equal, ongoing-maintenance option alongside `frontend-app/`.
- `docs/SRE_STRATEGY.md` now states explicitly that reliability work
  (SLOs, alerting, runbooks) should not grow to cover the legacy frontend.
- `docs/SRE_STRATEGY.md`'s §5 runbooks now cover PgBouncer client
  queueing/break-glass routing, reaping orphaned ACA revisions via
  `reap-stuck-revisions.yml`, recovering a VM whose Cloudflare Tunnel
  token went stale (`repair-tunnel-token-vm.yml` /
  `emergency-tunnel-repair-run-command.sh`), and a silent
  extension/quotation SLA-escalation failure.
- `docs/SRE_STRATEGY.md`'s weekly cadence now checks PgBouncer waiting
  count and that the SLA-escalation/overdue digest jobs actually ran; the
  quarterly cadence now includes running `chaos-test.sh`,
  `security-boundary-test.py`, and `session-redis-test.py`, not just
  `load-test.py`.

## [v1.0.9] - 2026-08-20

### Highlights
- Closed out the remaining background-DB exhaustion risk left after v1.0.8's
  pool-sizing work, so Celery/Beat load can no longer starve API requests of
  PostgreSQL connections.
- Fixed a production-only ErrorBeacon startup failure on Azure Container
  Apps caused by SQLite locking over an Azure Files (SMB) share.
- Completed the global header search feature (quotations, checkout codes,
  and assets) and fixed a related production routing bug it surfaced.

### Added
- Added a deployment-wide Redis admission gate for background Celery/Beat
  database work, so scheduled and queued jobs are throttled the same way
  HTTP requests already are.
- Added a global header search bar that resolves quotations, checkout
  codes (`COxx`), and assets, and jumps the user to the matching custody
  ledger (with auto-highlight/scroll), quotation ledger, or asset ledger,
  including deep-link support (`?highlight=`, `?tab=`, `?openQuote=`) and
  a dedicated classifier (`lib/globalSearch.ts`) with unit test coverage.

### Changed
- PgBouncer's API-facing pool sizing now reserves background capacity
  instead of competing with it for server-side connections.
- Notification and SLA Celery tasks now close their DB session before
  SMTP/email I/O, instead of holding a connection for the duration of the
  send.
- Celery prefetch is limited to one task per worker (`worker_prefetch_multiplier=1`)
  so background DB work can no longer hide behind a large local broker
  reservation.

### Fixed
- Fixed ErrorBeacon's Azure Container Apps deployment failing to start
  with `sqlite3.OperationalError: database is locked`. Root cause was
  Azure Files (SMB) not reliably granting SQLite's POSIX byte-range locks;
  fixed by adding `mountOptions: 'nobrl'` to the `errorbeacon-data` volume
  in `infra/main.bicep`.
- Fixed the production SPA fallback in `nginx/default.react.conf.template`
  404/403'ing on refresh or deep-link of the `/assets` route, because
  Vite's build output directory (`dist/assets/`) collided with the app's
  own `/assets` route under the old `try_files $uri $uri/ /index.html;`
  rule. Changed to `try_files $uri /index.html;` and added a regression
  check in `nginx/test-config.sh`.

## [v1.0.8] - 2026-08-19

### Highlights
- This release tightened the platform's operational spine: reliability,
  deployment automation, and observability all moved to a more
  production-ready state.
- Redis scheduling and ErrorBeacon telemetry were stabilized to reduce
  drift, make failure modes easier to understand, and strengthen the
  system's incident-tracing capability.
- Infrastructure and deployment documentation were brought in line with
  the live Azure, VM, and GitHub Actions reality, reducing ambiguity for
  operators.

### Added
- Added deterministic RedBeat scheduling configuration with explicit Redis
  connect/read timeouts and a regression guard to prevent it from being
  silently removed.
- Added ErrorBeacon security and monitoring hardening, including bounded
  request handling, admin-only diagnostics, per-IP throttling, and stricter
  browser telemetry validation.
- Added AI-analysis output validation for ErrorBeacon to enforce
  deterministic section structure and evidence-first reporting.
- Documented the one-time Windows/Git Bash executable-bit route (`git
  update-index --chmod=+x`) and clarified the deployment troubleshooting
  path.
- Added missing documentation tables of contents and repaired stale
  internal Markdown links, including the renamed nginx resolver entrypoint
  and stale deployment/SRE anchors.

### Changed
- Unified the deployment story across Azure Container Apps and the VM path,
  including deterministic environment naming, Terraform-state bootstrap
  flow, and shared OIDC-based automation patterns.
- Updated CI/deployment documentation to reflect the current GitHub Actions
  flow model and the current repository layout.
- Synchronized the deployment docs with the current Bicep and GitHub Actions
  implementation, including provider registration, stack plan/apply/destroy
  semantics, and environment-specific destroy confirmation logic.
- Synchronized the VM deployment docs with the current automatic Terraform
  state bootstrap process and clarified the optional nature of
  `TF_STATE_*` overrides.

### Fixed
- Fixed RedBeat timeout behavior by explicitly configuring
  `redbeat_redis_options` instead of relying on deprecated Celery fallback
  behavior.
- Hardened ErrorBeacon with admin-gated `/v1/health`, OpenAPI disabling in
  production, request-body limits, and trusted client-IP filtering for
  browser telemetry.
- Fixed ErrorBeacon request correlation by forwarding `X-Request-ID`,
  returning `request_id` in ingestion responses, and wiring request IDs
  through the middleware and chaos validation paths.
- Fixed Azure bootstrap RBAC provisioning by using the ARM
  `Microsoft.Authorization` REST API instead of `az role assignment`,
  preventing tenant-specific `MissingSubscription` failures.
- Ensured Terraform state RBAC assignments are deterministic and idempotent
  across Azure bootstrap flows.
- Corrected stale references to retired resource-group secrets, legacy
  deployment workflows, and outdated deployment assumptions.
- Documented the single unavoidable bootstrap boundary for GitHub Actions
  identity and clarified that Terraform destroy retains state
  infrastructure while destroying the environment stack.

## [v1.0.7] - 2026-08-18

### Highlights
- Improved resilience, deployment stability, and operational diagnostics across the release cycle.
- Hardened ErrorBeacon and its integration points with stronger validation, safer defaults, and better correlation tracing.
- Reduced deployment and CI drift while improving the consistency of the Azure and local infrastructure paths.

### Added
- Added AI analysis support and inline Telegram controls for ErrorBeacon.
- Added deeper request correlation validation across frontend, backend, and ErrorBeacon telemetry.
- Added support for more robust CI and deployment checks, including resilience and drift validation.

### Changed
- Improved extension handling, role/department updates, and global search behavior for operational consistency.
- Refined Azure deployment automation and environment bootstrap logic for cleaner follow-on deploys.
- Updated docs and operational guidance to match the current deployment model and support flows.

### Fixed
- Fixed resilience and deployment issues across P1/P2 checks, Docker Compose stability, and CI health gates.
- Fixed ErrorBeacon request routing, queue handling, Telegram command behavior, and environment display issues.
- Fixed database pooling, audit-log, notification, backup/restore, quote, and React UI edge cases that surfaced across the release cycle.
- Resolved infrastructure drift, Cloudflare bootstrap issues, Terraform state problems, and release pipeline maintenance issues.

## [v1.0.6] - 2026-08-05

### Highlights
- Improved runtime stability, infrastructure automation, and dependency hygiene.
- Expanded the React frontend path and tightened deployment readiness checks.
- Continued hardening the project for CI, local builds, and production deployment consistency.

### Added
- Added a second frontend path and improved the React build experience.
- Added deployment and infrastructure documentation updates for the current project structure.

### Changed
- Updated dependency versions across the backend, frontend, and build tooling to improve compatibility and security posture.
- Refined CI, build, and deployment automation to reduce drift and improve consistency.
- Improved the project documentation and release notes structure.

### Fixed
- Fixed multi-request handling and network-related stability issues.
- Fixed compose drift and deployment drift symptoms across local and cloud workflows.
- Resolved several backend and frontend stability problems affecting rendering, pagination, and operational reliability.

## [v1.0.5] - 2026-08-04

### Highlights
- Refreshed the project branding and overall product presentation.

## [v1.0.4] - 2026-08-03

### Highlights
- Polished the frontend labeling and removed unnecessary clutter in the product experience.
- Improved deployment documentation and cleaned up operational configuration.

### Changed
- Updated frontend labels and removed redundant UI/configuration noise.
- Cleaned up documentation and deployment guidance.

### Fixed
- Restored the executable bit on the ACA deploy status script and removed redundant lean-mode configuration.

## [v1.0.3] - 2026-08-01

### Highlights
- Improved Azure Container Apps deployment visibility and operational monitoring.
- Reduced drift between ACA and VM deployment flows.

### Added
- Added the ACA deploy dashboard and status views.

### Fixed
- Fixed ACA/VM deployment drift issues and resolved deployment status problems in the ACA workflow.

## [v1.0.2] - 2026-08-01

### Highlights
- Improved the live Azure Container Apps deployment experience and deploy-page usability.

### Added
- Added a live ACA deploy dashboard and deployment status surface.

### Fixed
- Fixed ACA deploy page issues and stabilized the deployment UI flow.

## [v1.0.1] - 2026-08-01

### Highlights
- Polished the product experience with branding and notifications improvements.
- Expanded the deployment and identity controls for the operational workflow.

### Changed
- Updated branding and notification behavior.
- Improved CI and MFA-related handling.

### Added
- Added the ACA deployment interface.

## [v1.0.0] - 2026-08-01

### Highlights
- Initial production-ready release of the platform, covering core asset management, deployment automation, security, and operational documentation.

### Added
- Added the initial quote workflow, password recovery, backup handling, and deployment interfaces for ACA and VM environments.
- Added the initial documentation set for deployment, troubleshooting, and operational guidance.

### Changed
- Reworked Terraform and infrastructure structure for OIDC-based deployment and zero-trust state handling.
- Improved environment and secret management, workflows, and deployment reliability.
- Improved strategies for zero-downtime deployment and blue-green rollout support.

### Fixed
- Fixed deployment script issues, Caddy and workflow problems, secret handling, database password processing, backup failures, Cloudflare tunnel issues, handshake errors, and initial Terraform configuration issues.
- Fixed quote refinement, SMTP configuration, token cache behavior, and early product bugs across the first release candidate.
