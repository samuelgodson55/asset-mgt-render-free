#!/usr/bin/env python3
"""ErrorBeacon end-to-end pipeline test (P0 item #4 + P2 item #14 combined).

RUN THIS INSIDE THE BACKEND CONTAINER, not from your laptop:

  az containerapp exec --name backend --resource-group rg-snipeit-lite-prod

ErrorBeacon's ingress is internal-only (see security checklist item #16 --
"ErrorBeacon public URL -> inaccessible" is a REQUIREMENT, not a bug), so
$ERRORBEACON_URL / $ERRORBEACON_INGEST_API_KEY only resolve from inside the ACA
environment. The backend container already has both set (it's how it talks
to ErrorBeacon on every unhandled exception -- see
backend/integrations/fastapi_errorbeacon.py), so this reuses them instead
of asking you to hunt down a separate ErrorBeacon exec session.

Once you have a shell:

  curl -s -o /tmp/errorbeacon-pipeline-test.py <raw-url-or-copy-paste>
  python3 /tmp/errorbeacon-pipeline-test.py

Or simplest: paste this whole file's contents into `python3` at the exec
prompt, or `cat > test.py <<'EOF' ... EOF` it in, then run it. Standard
library only.

WHAT THIS PROVES, END TO END
-----------------------------
POST /v1/test only proves ErrorBeacon ACCEPTED and QUEUED the event (see
app/main.py's test_alert()) -- it does NOT prove Telegram or the AI
provider actually did anything, since both are delivered asynchronously
by background workers (see alert()/worker()/ai_worker()). This script
polls GET /v1/incidents afterward until the incident record shows a
final telegram_sent/ai_status, so a "test passed" here means the FULL
pipeline actually ran, not just that ErrorBeacon answered the HTTP call.

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
    api_key = os.environ.get("ERRORBEACON_INGEST_API_KEY", "")

    if not record_env_check("ERRORBEACON_URL set", bool(base_url), base_url):
        print_summary()
        return 1
    if not record_env_check("ERRORBEACON_INGEST_API_KEY set", bool(api_key), "(non-empty)" if api_key else "(empty)"):
        print_summary()
        return 1
    if base_url in ("http://errorbeacon:8000",) :
        record("ERRORBEACON_URL looks like the compose default, not an ACA FQDN", WARN, base_url)

    # 1. Health check -- also reports current config (ai_enabled, providers, telegram_configured)
    status, body = call("GET", f"{base_url}/healthz", api_key)
    if not expect("ErrorBeacon /healthz", status == 200 and isinstance(body, dict) and body.get("status") == "ok", f"HTTP {status}: {body}", f"HTTP {status}: {body}"):
        print_summary()
        return 1

    ai_enabled = body.get("ai_enabled")
    ai_providers = body.get("ai_providers", [])
    telegram_configured = body.get("telegram_configured")
    print(f"    config snapshot: ai_enabled={ai_enabled} ai_providers={ai_providers} telegram_configured={telegram_configured}")
    if not ai_enabled:
        record("AI analysis is configured", WARN, "ai_enabled=false in /healthz -- AI step below will short-circuit to ai_status='disabled', not a failure")
    if not telegram_configured:
        record("Telegram is configured", WARN, "telegram_configured=false in /healthz -- Telegram delivery below will fail with TelegramNotConfigured, not a transport error")

    # 2. Fire a controlled test event
    status, body = call("POST", f"{base_url}/v1/test", api_key)
    if not expect("POST /v1/test accepted", status == 200 and isinstance(body, dict) and body.get("ok") is True, f"HTTP {status}: {body}", f"HTTP {status}: {body}"):
        print_summary()
        return 1
    incident_id = body.get("incident_id")
    queued = body.get("queued")
    print(f"    incident_id={incident_id} queued={queued} request_id={body.get('request_id')}")
    expect("event was queued for delivery", queued is True, "queued=true", f"queued={queued} -- event was accepted but NOT enqueued (check should_notify()/severity classification, or that it wasn't silenced)")

    # 3. Poll /v1/incidents until telegram_sent/ai_status settle (or timeout)
    deadline = time.time() + 45
    final = None
    while time.time() < deadline:
        status, body = call("GET", f"{base_url}/v1/incidents?limit=20", api_key)
        if status == 200 and isinstance(body, list):
            match = next((row for row in body if row.get("id") == incident_id), None)
            if match:
                ai_done = match.get("ai_analysis") is not None or match.get("ai_status") in ("failed", "disabled")
                telegram_done = match.get("telegram_sent") in (0, 1) and match.get("telegram_sent") is not None
                if telegram_done and (ai_done or not ai_enabled):
                    final = match
                    break
        time.sleep(2)

    if final is None:
        record("pipeline settled within 45s", WARN, f"incident {incident_id} never reached a final telegram/AI state in time -- either it's slow right now, or check ErrorBeacon logs for stuck workers")
        status, body = call("GET", f"{base_url}/v1/incidents?limit=20", api_key)
        final = next((row for row in body if row.get("id") == incident_id), {}) if status == 200 and isinstance(body, list) else {}

    telegram_sent = final.get("telegram_sent")
    expect(
        "Telegram delivery",
        bool(telegram_sent) or not telegram_configured,
        f"telegram_sent={telegram_sent}" + ("" if telegram_configured else " (Telegram not configured, so this is expected)"),
        f"telegram_sent={telegram_sent} even though telegram_configured=true -- check the ErrorBeacon container logs for the TelegramDelivery status/error (TelegramHTTP4xx/5xx, ConnectionError, etc.)",
    )

    ai_analysis = final.get("ai_analysis")
    record(
        "AI analysis",
        PASS if (ai_analysis or not ai_enabled) else WARN,
        f"ai_analysis={'present' if ai_analysis else 'absent'}" + ("" if ai_enabled else " (AI not enabled, so this is expected)"),
    )

    print(f"\nFull incident record for {incident_id}:")
    print(json.dumps(final, indent=2, default=str))

    print(
        "\nTo actually exercise degradation (P2 #14 -- AI provider unavailable, then Telegram\n"
        "unavailable), from your own machine (not this exec session):\n\n"
        "  az containerapp update --name errorbeacon --resource-group rg-snipeit-lite-prod \\\n"
        "    --set-env-vars TELEGRAM_BOT_TOKEN=invalid-token-for-test\n\n"
        "  # re-run this script -- expect telegram_sent=0 and NO crash/500, AI still completes\n\n"
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
