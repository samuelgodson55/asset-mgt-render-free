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
- ground 1 (6176a23)
- ground o (8a598e9)
- release workflow bug #2 (59e9269)
- VM worflow issue #1 (cd1906d)

## [v1.0.1] - 2026-07-28
- infra fix (a927219)
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
- Documentation (b650bc6)
- Debugging salient bugs using Terraform Plan (b498d59)
- VM Infra Setup (d2d18d4)
- docs(changelog): v1.0.0 (08361b0)

## [v1.0.0] - 2026-07-26
- infra (54be95b)
- Develop (#79) (6eb7be2)
- Feature/infra (#77) (945fdf3)
- Develop (#76) (efb1542)
- Develop (#72) (f5a991a)
- working code (105700e)
- clean slate (5940fa5)
- Bump alembic from 1.13.2 to 1.18.5 in /backend (#67) (905f06d)
- Bump jsdom from 24.1.3 to 29.1.1 in /frontend/tests (#68) (4cbcd42)
- Bump javascript-obfuscator from 4.2.2 to 5.5.0 in /build-frontend (#69) (e170b95)
- Develop (#52) (4dff195)
- missing file added (d58bf76)
- demo index.html strip (01beac3)
- quote refinement and smtp (5cda167)
- trivy tag CI fix (3dd5713)
- quotes and responsiveness (2635dc1)
- local storage to cookies token cache (be3fea0)
- quotes added (db54f86)
- further mobile responsiveness (9703002)
- time uniformity & further responsiveness (46e1bce)
- rearranging layouts and extending backup hours (becb011)
- backup config created and other mobile responsiveness (f58d132)
- polished mobile responsiveness (c31d8c0)
- mobile responsiveness (30ce542)
- redeploying (a48a46b)
- testing 2 (0f037e3)
- testing (1528d12)
- testing pull and merge (db01a10)
- fresh clean (668f7a6)
- Initial Project Structure (2ef1fe1)
