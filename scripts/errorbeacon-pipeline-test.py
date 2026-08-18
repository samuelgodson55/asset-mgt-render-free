#!/usr/bin/env python3
"""ErrorBeacon end-to-end pipeline test (P0 item #4 + P2 item #14 combined).

RUN THIS INSIDE THE BACKEND CONTAINER, not from your laptop:

  az containerapp exec --name backend --resource-group rg-snipeit-lite-prod

ErrorBeacon's ingress is internal-only (see security checklist item #16 --
"ErrorBeacon public URL -> inaccessible" is a REQUIREMENT, not a bug), so
$ERRORBEACON_URL only resolves from inside the ACA environment. The backend
container already has both $ERRORBEACON_ADMIN_API_KEY and
$ERRORBEACON_INGEST_API_KEY set (see infra/main.bicep's sharedSecretEnvRefs),
so this reuses them instead of asking you to hunt down a separate ErrorBeacon
exec session. This script needs the ADMIN key specifically -- /v1/test,
/v1/health and /v1/incidents are all admin_auth-protected endpoints (see
app/main.py); the ingest key is for /v1/events only and won't work here.

HOW TO GET THIS FILE INTO THE EXEC SESSION
--------------------------------------------
The backend and errorbeacon images are deliberately minimal (no curl/wget),
and `az containerapp exec` is an SSH-like session where large multi-line
pastes can get mangled by some terminals (seen in practice with Git Bash/
MINGW64 on Windows -- lines arrive out of order or get prefixed with stray
`>`). Paste the WHOLE block below (from `cat << 'PYEOF' > ...` down through
the final `PYEOF` line) into the exec session in one go:

    cat << 'PYEOF' > /tmp/errorbeacon-pipeline-test.py
    <the full contents of this file>
    PYEOF

Then sanity-check the line count matches this file (see the count at the
bottom of this docstring block in your editor / `wc -l` on your own machine)
before running it:

    wc -l /tmp/errorbeacon-pipeline-test.py
    python3 /tmp/errorbeacon-pipeline-test.py

Using a quoted delimiter ('PYEOF' with quotes, not EOF) stops the shell from
expanding `$ERRORBEACON_URL`-style variables or backticks inside the script
while it's being written to disk -- that's what a plain unquoted heredoc
would otherwise silently corrupt. If your terminal still mangles multi-line
pastes even with this, paste it in smaller chunks (e.g. split at the
function boundaries) using `cat >> /tmp/errorbeacon-pipeline-test.py <<
'PYEOF'` for each chunk after the first, or use `az containerapp exec` with
`--command "python3 -c \"...\""` for very short one-off checks instead.

WHAT THIS PROVES, END TO END
-----------------------------
POST /v1/test only proves ErrorBeacon ACCEPTED and QUEUED the event (see
app/main.py's test_alert()) -- it does NOT prove Telegram or the AI
provider actually did anything, since both are delivered asynchronously
by background workers (see alert()/worker()/ai_worker()). This script
polls GET /v1/incidents/{id} afterward until the incident record's
telegram_status/ai_status reach a TERMINAL state, so a "test passed" here
means the FULL pipeline actually ran, not just that ErrorBeacon answered
the HTTP call.

Three bugs from earlier versions of this script are fixed here:

1. It authenticated every call with $ERRORBEACON_INGEST_API_KEY, but
   /v1/test, /v1/health and /v1/incidents are all admin_auth-protected --
   that key is for /v1/events only. Every admin call would 401. Fixed by
   reading $ERRORBEACON_ADMIN_API_KEY instead.
2. It read ai_enabled/telegram_configured off /healthz, which only ever
   returns {status, service, version, db_status} (see app/main.py's
   healthz() vs. the admin-only /v1/health). Those fields were always
   None there, so the script always concluded "not configured" and
   silently skipped verifying anything. Fixed by calling /v1/health
   instead.
3. Its "is delivery done yet" check was `telegram_sent in (0, 1)`, but 0
   is ALSO the untouched default before the worker has done anything --
   so the poll loop exited on its very first iteration, before the
   background worker had a chance to run, and reported the untouched
   "pending" record as a settled final state. Fixed by tracking
   telegram_status/ai_status against explicit terminal-state sets
   instead of trusting the telegram_sent int alone.
4. (NEW) The "leaving pending" fix from #3 was itself incomplete for
   ai_status: enqueue_ai() in app/main.py flips ai_status from
   'pending' -> 'queued' SYNCHRONOUSLY, the instant a job is handed to
   the in-memory queue -- well before ai_worker() actually picks it up
   and calls the AI provider. 'queued' is just as in-flight as
   'pending'; it is not a terminal outcome. The old `!= "pending"`
   check treated 'queued' as "done" and exited the poll loop
   immediately, reporting a false FAIL (ai_status=queued, ai_attempts=0,
   ai_last_error=None -- i.e. the worker hadn't even started yet).
   Fixed by polling against an explicit AI_TERMINAL set and treating
   'pending', 'queued', and 'telegram_pending' as still-in-flight.

WHAT THIS DOES **NOT** TEST
----------------------------
This exercises the pipeline as CURRENTLY CONFIGURED. It does not itself
break Telegram or the AI provider -- see the printed guidance at the end
for how to do that (temporarily clearing TELEGRAM_BOT_TOKEN / the AI
provider key via `az containerapp update --set-env-vars`, re-running this
script, then reverting) if you want to exercise the degradation path
specifically, per P2 item #14.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

# Terminal ai_status values -- once the incident reaches one of these, no
# background loop (ai_worker, ai_recovery_loop) is going to change it further
# on its own. Everything else ('pending', 'queued', 'telegram_pending') is
# still mid-flight and must keep being polled.
AI_TERMINAL = {"complete", "failed", "disabled", "suppressed", "telegram_unknown"}

# Terminal telegram_status values for the *primary* incident page (separate
# from the AI-enrichment Telegram send tracked via ai_status above).
TELEGRAM_TERMINAL = {"sent", "failed", "unknown"}

results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717", "WARN": "!"}[status]
    print(f"[{marker}] {step}: {detail}")


def call(method: str, url: str, api_key: str, timeout: float = 10.0) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, method=method, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        status = e.code
    except urllib.error.URLError as e:
        return -1, str(e.reason)
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


def main() -> int:
    base_url = os.environ.get("ERRORBEACON_URL", "").rstrip("/")
    api_key = os.environ.get("ERRORBEACON_ADMIN_API_KEY", "")

    if not record_env_check("ERRORBEACON_URL set", bool(base_url), base_url):
        print_summary()
        return 1
    if not record_env_check("ERRORBEACON_ADMIN_API_KEY set", bool(api_key), "(non-empty)" if api_key else "(empty)"):
        print_summary()
        return 1
    if base_url in ("http://errorbeacon:8000",):
        record("ERRORBEACON_URL looks like the compose default, not an ACA FQDN", WARN, base_url)

    # 1. Liveness -- deliberately minimal, no auth, no config detail (see app/main.py healthz()).
    status, body = call("GET", f"{base_url}/healthz", api_key)
    if not expect("ErrorBeacon /healthz", status == 200 and isinstance(body, dict) and body.get("status") == "ok", f"HTTP {status}: {body}", f"HTTP {status}: {body}"):
        print_summary()
        return 1

    # 2. Real config snapshot -- admin-protected, this is where ai_enabled/telegram_configured actually live.
    status, body = call("GET", f"{base_url}/v1/health", api_key)
    if not expect("ErrorBeacon /v1/health", status == 200 and isinstance(body, dict) and body.get("status") == "ok", f"HTTP {status}: {body}", f"HTTP {status}: {body}"):
        print_summary()
        return 1

    ai_enabled = body.get("ai_enabled")
    ai_providers = body.get("ai_providers", [])
    telegram_configured = body.get("telegram_configured")
    print(f"    config snapshot: ai_enabled={ai_enabled} ai_providers={ai_providers} telegram_configured={telegram_configured}")
    if not ai_enabled:
        record("AI analysis is configured", WARN, "ai_enabled=false -- AI step below will short-circuit to ai_status='disabled', not a failure")
    if not telegram_configured:
        record("Telegram is configured", WARN, "telegram_configured=false -- Telegram delivery below will fail with TelegramNotConfigured, not a transport error")

    # 3. Fire a controlled test event
    status, body = call("POST", f"{base_url}/v1/test", api_key)
    if not expect("POST /v1/test accepted", status == 200 and isinstance(body, dict) and body.get("ok") is True, f"HTTP {status}: {body}", f"HTTP {status}: {body}"):
        print_summary()
        return 1
    incident_id = body.get("incident_id")
    queued = body.get("queued")
    print(f"    incident_id={incident_id} queued={queued} request_id={body.get('request_id')}")
    expect("event was queued for delivery", queued is True, "queued=true", f"queued={queued} -- event was accepted but NOT enqueued (check should_notify()/severity classification, or that it wasn't silenced)")

    # 4. Poll /v1/incidents/{id} until telegram_status/ai_status both reach a
    #    TERMINAL state (or timeout). 'queued' and 'telegram_pending' are
    #    mid-flight, not terminal -- see AI_TERMINAL / TELEGRAM_TERMINAL above
    #    and bug #4 in the module docstring for why this distinction matters.
    deadline = time.time() + 60
    final: dict | None = None
    last_seen_status: tuple[str | None, str | None] | None = None
    while time.time() < deadline:
        status, body = call("GET", f"{base_url}/v1/incidents/{incident_id}", api_key)
        if status == 200 and isinstance(body, dict):
            telegram_status = body.get("telegram_status")
            ai_status = body.get("ai_status")
            if (telegram_status, ai_status) != last_seen_status:
                print(f"    ... telegram_status={telegram_status} ai_status={ai_status}")
                last_seen_status = (telegram_status, ai_status)
            telegram_done = telegram_status in TELEGRAM_TERMINAL
            ai_done = (not ai_enabled) or (ai_status in AI_TERMINAL)
            if telegram_done and ai_done:
                final = body
                break
        time.sleep(2)

    if final is None:
        record("pipeline settled within 60s", WARN, f"incident {incident_id} never reached a terminal telegram_status/ai_status in time (last seen: {last_seen_status}) -- either it's slow right now, or check ErrorBeacon logs for stuck workers")
        status, body = call("GET", f"{base_url}/v1/incidents/{incident_id}", api_key)
        final = body if status == 200 and isinstance(body, dict) else {}

    telegram_status = final.get("telegram_status")
    telegram_sent = final.get("telegram_sent")
    expect(
        "Telegram delivery",
        telegram_status == "sent" or not telegram_configured,
        f"telegram_status={telegram_status} telegram_sent={telegram_sent}" + ("" if telegram_configured else " (Telegram not configured, so this is expected)"),
        f"telegram_status={telegram_status} telegram_sent={telegram_sent} even though telegram_configured=true -- check the ErrorBeacon container logs for the TelegramDelivery status/error (TelegramHTTP4xx/5xx, ConnectionError, etc.), and telegram_last_error={final.get('telegram_last_error')!r}",
    )

    ai_status = final.get("ai_status")
    ai_analysis = final.get("ai_analysis")
    expect(
        "AI analysis",
        (ai_status in ("complete", "telegram_pending", "telegram_unknown") and bool(ai_analysis)) or not ai_enabled,
        f"ai_status={ai_status} ai_analysis={'present' if ai_analysis else 'absent'}" + ("" if ai_enabled else " (AI not enabled, so this is expected)"),
        f"ai_status={ai_status} ai_analysis={'present' if ai_analysis else 'absent'} even though ai_enabled=true -- ai_last_error={final.get('ai_last_error')!r}",
    )

    print(f"\nFull incident record for {incident_id}:")
    print(json.dumps(final, indent=2, default=str))

    print(
        "\nTo actually exercise degradation (P2 #14 -- AI provider unavailable, then Telegram\n"
        "unavailable), from your own machine (not this exec session):\n\n"
        "  az containerapp update --name errorbeacon --resource-group rg-snipeit-lite-prod \\\n"
        "    --set-env-vars TELEGRAM_BOT_TOKEN=invalid-token-for-test\n\n"
        "  # re-run this script -- expect telegram_status=failed/telegram_sent=0 and NO crash/500, AI still completes\n\n"
        "  az containerapp update --name errorbeacon --resource-group rg-snipeit-lite-prod \\\n"
        "    --set-env-vars TELEGRAM_BOT_TOKEN=<the real token back>\n\n"
        "Swap in whichever AI provider key env var you use (see errorbeacon/.env.example) to\n"
        "test the AI-unavailable leg the same way. Always revert immediately after -- this is a\n"
        "shared production ErrorBeacon instance."
    )

    print_summary()
    return 1 if any(s == FAIL for _, s, _ in results) else 0


def expect(step: str, condition: bool, ok_detail: str, fail_detail: str) -> bool:
    record(step, PASS if condition else FAIL, ok_detail if condition else fail_detail)
    return condition


def record_env_check(step: str, condition: bool, detail: str) -> bool:
    record(step, PASS if condition else FAIL, detail)
    return condition


def print_summary() -> None:
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    warned = sum(1 for _, s, _ in results if s == WARN)
    print(f"\n{passed} passed, {failed} failed, {warned} informational")
    if failed:
        print("\nFailures:")
        for step, status, detail in results:
            if status == FAIL:
                print(f"  - {step}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
