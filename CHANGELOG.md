## 4.1.3 - ErrorBeacon security hardening

- Minimized public ErrorBeacon `/healthz`; detailed diagnostics moved behind admin authentication at `/v1/health`.
- Disabled ErrorBeacon Swagger/ReDoc/OpenAPI by default in production.
- Added per-IP rate limiting for failed ErrorBeacon admin API-key attempts.
- Added ErrorBeacon request-body size limits.
- Hardened the browser telemetry endpoint with trusted client-IP rate limiting, bounded context depth/items/size, and nginx 128 KiB request cap.
- Confirmed browser telemetry never receives ErrorBeacon API credentials.

## ErrorBeacon AI analysis hardening\n\n- Enforced deterministic ROOT CAUSE, IMPACT, NEXT STEPS, and CONFIDENCE sections.\n- Rejects incomplete AI output instead of sending malformed analysis to Telegram.\n- Adds factuality and evidence-only instructions to the Gemini prompt.\n- Added tests for section parsing, required sections, and confidence normalization.\n\n## v8 — Correlation + Chaos Test Hardening

- Forward the Asset application's `X-Request-ID` to ErrorBeacon ingestion.
- Add ErrorBeacon request-context middleware and structured request IDs to its own Uvicorn access logs.
- Return `request_id` from ErrorBeacon event ingestion responses.
- Make `/v1/test` use the middleware correlation ID.
- Fix the local chaos suite to test backend `/readyz` directly instead of the frontend proxy.
- Add Redis fail-open login validation, ErrorBeacon outage latency validation, PostgreSQL readiness/recovery validation, and end-to-end frontend → backend → ErrorBeacon correlation validation.
- Add correlation unit tests and ErrorBeacon request-ID middleware tests.
- ErrorBeacon version bumped to 3.1.0.

# Changelog

## Table of Contents

- [Unreleased — Infrastructure documentation](#unreleased--infrastructure-documentation)
- [Unreleased](#unreleased)
- [v1.0.6 — 2026-08-05](#v106---2026-08-05)
- [v1.0.5 — 2026-08-04](#v105---2026-08-04)
- [v1.0.4 — 2026-08-03](#v104---2026-08-03)
- [v1.0.3 — 2026-08-01](#v103---2026-08-01)
- [v1.0.2 — 2026-08-01](#v102---2026-08-01)
- [v1.0.1 — 2026-08-01](#v101---2026-08-01)
- [v1.0.0 — 2026-08-01](#v100---2026-08-01)

## Unreleased — Infrastructure documentation

- Documented the one-time Windows/Git Bash executable-bit route (`git update-index --chmod=+x`) and clarified that once the mode changes are committed, executable-permission failures do not require further deployment debugging.
- Added missing documentation TOCs and repaired stale internal/local Markdown links, including the renamed nginx resolver entrypoint (`15-detect-resolver-ip.envsh`) and stale deployment/SRE anchors.

- Fixed Azure bootstrap RBAC provisioning to use the ARM `Microsoft.Authorization` REST API instead of `az role assignment`, avoiding tenant-specific `MissingSubscription` failures. Subscription `Contributor` and Terraform-state `Storage Blob Data Contributor` assignments are now idempotent and use deterministic assignment IDs.

- Synchronized ACA deployment documentation with the current Bicep and
  GitHub Actions implementation: deterministic environment resource-group
  names, automatic provider registration, Deployment Stack plan/apply/destroy
  semantics, environment-specific destroy confirmations, and the shared
  OIDC bootstrap model.
- Corrected stale references to the former `STAGING_RESOURCE_GROUP` /
  `PROD_RESOURCE_GROUP` secrets and the former separate ACA staging/production
  deployment workflows.
- Synchronized the VM deployment documentation with the current automatic
  Terraform-state bootstrap, including the deterministic subscription-derived
  Storage Account name and the fact that `TF_STATE_*` values are optional
  advanced overrides rather than required GitHub Environment configuration.


- Documented the VM Terraform remote-state bootstrap as fully automated: the
  workflow creates/reuses the dedicated state resource group, Storage Account,
  blob container, and Blob Data Contributor RBAC before `terraform init`.
- Removed the old documented requirement to manually provision Terraform state
  storage or configure `TF_STATE_STORAGE_ACCOUNT` / `TF_STATE_CONTAINER`.
- Documented the single unavoidable bootstrap boundary: local `az login`,
  `gh auth login`, and `scripts/bootstrap-azure-github.sh` establish the GitHub
  Actions OIDC identity; all subsequent Azure VM infrastructure lifecycle work
  is owned by GitHub Actions.
- Documented that Terraform destroy retains the remote-state infrastructure
  while destroying the VM stack tracked by the environment state.
- Updated the main deployment documentation to describe the VM and Bicep paths
  consistently as no-manual-resource workflows.

All notable changes to this project are documented here, one section per
`git tag v*.*.*` release. Entries below the marker are inserted
automatically by [`release.yml`](.github/workflows/release.yml) at
tag-push time, generated from `git log <previous-tag>..<new-tag>` (see that
workflow's `changelog` job) — the same previous-tag lookup
(`git tag --sort=-creatordate`) that [`DEPLOYMENT.md`](DEPLOYMENT.md)'s
rollback runbook uses, so "the previous version" always means the same
thing in both places.

Don't hand-edit below the marker — your edits will be overwritten (pushed
further down, not preserved in place) the next time a tag is pushed. Notes
above the marker are yours to keep.

<!-- release-notes-insertion-point -->

## [v1.0.7] - 2026-08-18
- fix: resilience p1p2_v4 (b16c717)
- fix: p1p2_v3 (9e1f759)
- fix: P1/P2 resiliience workflow (8760b9c)
- fix: compose bug (23ceda3)
- fix:extemsion locking & role/dept changes (5659a98)
- add: analysis provider (63a35e6)
- fix: errorbeacon bug (231c770)
- fix: errorbeacon queue (90923a8)
- fix: telegram commands (36cb782)
- fix: ci check_v4 (0a8231e)
- fix: ci build (22c35ff)
- fix: ruff and compose drift (3a7fef5)
- fix: bugs repo wide (1e0271f)
- fix: infra deploy_2 (9cc7114)
- fix: infra deploy (8c3ccbd)
- update: docs (2501af5)
- fix: compose drift_v2 (410459c)
- fix: compose drift (0b070a8)
- add: inline telegram commands for errorbeacon (9aa239f)
- fix: redis headers (da59391)
- fix: deployment bugs (b2986de)
- fix:ACA errorbeacon url (8a7c2b5)
- fix: errorbeam doc (f7474db)
- errorbeacon: env display (6d2fefc)
- fix: errorbeacon-date (738282e)
- fix: error beacon shared mount (efdf694)
- swap: telgram id to secret (298f221)
- fix: comment bug (f12f27d)
- fix: errorbeacon site url (bdefb69)
- fix: CI error flag_v7 diff angle (4403b64)
- fix: CI error flags v_6 (f161716)
- fix: CI error flags v_5 (2176e90)
- fix: CI error flags_v4 (e100822)
- fix: CI error flags_v3 (559091e)
- fix: CI error flags_v2 (76a769d)
- fix: CI flagged errors (e2ffa6f)
- error beacon (3e1ae24)
- build(deps): bump actions/upload-artifact from 4 to 7 (8bcb3d8)
- build(deps-dev): bump terser from 5.49.0 to 5.50.0 in /build-frontend (04d7276)
- build(deps): bump node from 24-slim to 26-slim in /frontend (c546186)
- fix: bugs (9af1b99)
- fix: frontend lint (5c02631)
- fix: guotes bug (7497910)
- fix: audit log (1e74ec4)
- fix: backup/restore bugs (b7d3330)
- fix: global search bug (52aa90f)
- clean infra & subsequent deploys (b5890ec)
- fix: Deploy Infra ACA (fe0d480)
- fix: ACA deploy paths (4cbb308)
- fix: harden ACA and Azure bootstrap (583767d)
- fix: deploy to vm v3 (f589ba2)
- fix: devops infra vm path v2 (3402902)
- fix: devops for pipeline for VM (f6158e1)
- fix: harden ACA deployments and update VM docs (5dc8037)
- fix: resolve existing VM credentials from Terraform state (6ec15ca)
- feat: automatically sync VM and Cloudflare credentials to GitHub (0da8cf9)
- fix: add Cloudflare CI token recovery artifact (f40a3bd)
- fix: upload VM connection details from infra-vm (952b5a1)
- fix: expose non-sensitive VM connection outputs (fcdbd15)
- fix: place cloud-init size precondition in lifecycle (664b228)
- fix: forgotten terraform fmt (a831662)
- fix: compress VM cloud-init custom data (1ef6b2a)
- fix: skip Cloudflare policy probe on fresh state (828fdc8)
- fix: correct Azure state container bootstrap (a0fecf8)
- fix: create terraform state blob container during bootstrap (dbacace)
- debug: needed scripts added (4bc1d99)
- fix: updated infra workflow (4395a8d)
- fix: cleaner builds (143bac0)
- add: snapshot variable enabled (89c20b2)
- fix: cloudflare link (7aac404)
- debug: cloudflare v2 (93b961f)
- debug: cloudflare access (2ca9384)
- debug: cloudflare test (37fec5f)
- debug: cloudflare access policy (e1cac22)
- debug: cloudflare (42f5202)
- cloudflare: errors (cd58092)
- infra-vm: check (69710ef)
- fix: terraform fmt-check (6b3f736)
- fix: terraform fmt (afa9664)
- fix: tf state account (f457874)
- fix: terraform storage state (c950719)
- fix: docs links (505081a)
- fix: mark deployment scripts executable (6ae0787)
- making scripts exec (d9c04ae)
- add:ACA infra summary (4c7653d)
- fix: bootstrap scrips (1719e3c)
- fix: stale comments (dc01568)
- infra auotmation (80037db)
- fix: VM's frontend's image tag (52e6be8)
- fix: remove scheduled reaping (2240744)
- render deploy (cfa6e5c)
- fix: allembic upgrades (ae9eb32)
- runnable CI (27904d5)
- fix: aca infra downtime (7afb945)
- paid quotation status (4284f42)
- global search redirect (2c9dd89)
- improvements (92b0ba4)
- bug fixes (582ffb3)
- fix: more bug fixes (3bfbf67)
- fix: nginx react bug (4538fd0)
- feature: barcode & report (ef45dad)
- continuous bug fixes (4e3d9a8)
- bug fixes (96b5548)
- bug fixes (e19e06c)
- fix: notifications (945f43e)
- ci (9189d18)
- fix: vertical view port (5927798)
- react: faster refreshes & builds (ba04dd8)
- React: UI interactiveness (05dc18c)
- fix: reap removing active revisions (632fc4e)
- fix: database pooling error (e1e42ed)
- fix:blue-green revisions leak (429825c)
- fix/bugs: react front (7a4f539)
- fixes/bugs: react frontend (f07cba7)
- fix: try & load react (4ea9874)
- fix: limit on all server side pagination (ec46115)
- node version -> 24 (015bba3)
- add: alt react frontend (559fb62)
- Bump google-api-python-client from 2.143.0 to 2.198.0 in /backend (e87f403)
- Bump sqlalchemy from 2.0.31 to 2.0.51 in /backend (96f0862)
- fix: compose drift (06e908b)
- add: frontend react build/fix: db pool (72ea97b)
- fix: db overpool (676d541)
- project doc (a5acc6b)
- docs(changelog): v1.0.6 (3d6ab01)
- Bump tailwindcss from 3.4.19 to 4.3.3 in /build-tailwind (34d0f93)
## Unreleased
- Synchronized documentation with the current dual-frontend Docker/Vite architecture, current Azure deployment workflows, and current repository file layout; removed stale references to retired local helper scripts and added the missing VM runbook TOC.
- CI can now be started manually from GitHub Actions in addition to push,
  pull request, and reusable workflow triggers.
- Updated CI/deployment documentation to reflect the current workflow model.

## [v1.0.6] - 2026-08-05
- fix:multi requests fixed again low network (94cec8b)
- docs(changelog): v1.0.5 (7300830)
- docs(changelog): v1.0.4 (5787ffa)
- Bump actions/upload-artifact from 4 to 7 (43cc729)
- Bump actions/setup-python from 6 to 7 (5123bb0)
- Bump alembic from 1.13.2 to 1.18.5 in /backend (865d08f)
- Bump hashicorp/setup-terraform from 3 to 4 (8d5d7f2)
- Bump redis from 5.0.8 to 8.0.1 in /backend (47c0c2b)
- Bump node from 22-alpine to 25-alpine in /frontend (93a6695)
- Bump celery-redbeat from 2.4.1 to 2.4.2 in /backend (3dc2993)
- Bump javascript-obfuscator from 4.2.2 to 5.5.0 in /build-frontend (64f7223)
- Bump pyjwt from 2.9.0 to 2.13.0 in /backend (a617440)

## [v1.0.5] - 2026-08-04
- branding (26e143b)
## [v1.0.4] - 2026-08-03
- Frontend labels (2fe3fd0)
- removing unnecessary comments (7059b73)
- doc update (41302c6)
- fix: restore exec bit on aca-deploy-status.sh (8ea438e)
- remove redundant lean mode (89850bc)
- docs(changelog): v1.0.3 (523c0ec)

## [v1.0.3] - 2026-08-01
- ACA Deploy Dash (72e41f5)
- ACA deploy page debugging (7ceb361)
- check ACA/VM drift #2 (eedd60a)
- fix ACA-VM drift from recent change (d6b8684)
- ACA delpoy status issue #3 (7d26f43)
- ACA deploy status #2 (a0c2c73)
- ACA deploy status #1 (06e4600)
- docs(changelog): v1.0.2 (dba535f)

## [v1.0.2] - 2026-08-01
- ACA deploy page issue #2 (542ed02)
- deploy page issue #1 (3076cdb)
- live ACA deploy site (72a852f)
- live deploy dashboard for ACA included (4636e3f)
- docs(changelog): v1.0.1 (a31a58d)

## [v1.0.1] - 2026-08-01
- fixed site branding & Notifications
- updated ci
- MFA improvement
- docs(changelog): v1.0.0 (838c78d)
- ACA deploy interface

## [v1.0.0] - 2026-08-01
- sync aca secrets
- fix infra workflow
- fix deploy page
- fix caddy
- fix blue-green script issue #2
- fix script error #1
- VM deploy interface
- environment variable values not in quotes bug fix
- infra improvements
- workflows improvements
- zero downtime revision 1 (176aed9)
- guarded allembic race for 011 (8c83722)
- % from database passwords fixed (a8ac7de)
- pass reset + some bugs (3f95351)
- password recovery (63cb255)
- add documentation (83c94a1)
- backup bug fix_2 (53fd675)
- backup bugs_2 (a37b7be)
- backup error fix 1.0 (0eddd45)
- workflow errors (7cf7104)
- recovery method (de2d1eb)
- further terraform fixes (e5b5af2)
- further bug fixes in terraform (4bfd576)
- markdown cleanup (e82bec1)
- VM not provisioned well fix (2660753)
- fix inactive cloudflare tunnel (e0c12a2)
- fix handshake error in deployment (28a7aef)
- troubleshooting documentation (642d505)
- further bug corrections (a0099ad)
- resolving compounding errors (7e39848)
- terraform oidc (08c0c59)
- terraform state and zero trust (3a9e626)
- terraform structure (31cca1b)
- environment & secrets (ec52742)
- Bump alembic from 1.13.2 to 1.18.5 in /backend (#67) (905f06d)
- Bump jsdom from 24.1.3 to 29.1.1 in /frontend/tests (#68) (4cbcd42)
- Bump javascript-obfuscator from 4.2.2 to 5.5.0 in /build-frontend (#69) (e170b95)
- quote refinement and smtp (5cda167)
- trivy tag CI fix (3dd5713)
- local storage to cookies token cache (be3fea0)
- quotes added (db54f86)
- further mobile responsiveness (9703002)
