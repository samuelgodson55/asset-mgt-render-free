# Changelog

All notable changes to this project are documented here, one section per
`git tag v*.*.*` production release. Entries below the marker are inserted
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
