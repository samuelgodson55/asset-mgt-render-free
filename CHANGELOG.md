## 2026-08-19 — DB exhaustion hardening

- Background Celery/Beat database work now has a deployment-wide Redis admission gate.
- PgBouncer API pool sizing reserves background capacity instead of competing with it.
- Notification and SLA tasks close DB sessions before SMTP/email I/O.
- Celery prefetch is limited to one task so background DB work cannot hide behind a large local broker reservation.

# Changelog

## v13 — balanced DB concurrency and per-operation timeout escape hatch

- Increased ACA's auto-derived PgBouncer server budget from 4x to 5x PostgreSQL vCores.
  The default `Standard_B2s` therefore moves from 8 to 10 server-side connections,
  while retaining the 10% operational headroom and one background connection reserve.
- Changed adaptive SQLAlchemy pool splitting so each process keeps its full calculated
  share in `pool_size` with `max_overflow=0` by default. This avoids needless overflow
  connection churn when PgBouncer is already doing transaction pooling.
- Kept the global 30s PostgreSQL statement timeout. Added a documented, per-operation
  `SET LOCAL statement_timeout` helper for explicitly reviewed heavy operations, so a
  future report can receive a longer timeout without weakening customer-facing queries.
- Added regression coverage for the stable pool split and the per-operation timeout helper.

## Table of Contents

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
