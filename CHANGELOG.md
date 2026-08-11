# Changelog

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
## Unreleased
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
