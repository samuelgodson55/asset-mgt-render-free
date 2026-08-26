# Getting The Full Error Story, Fast — Every Deployment Path

One `request_id` ties a browser crash or a backend exception to everything
that happened around it — arrival, every service/SQL log line in between,
the exact stack trace, and (when ErrorBeacon is configured) whether anyone
actually got paged. The shape of that story is identical in every
deployment; only *how you reach the logs* changes. This doc is the
"something broke, give me the ID, tell me what happened" runbook for each
one.

If you want the deeper *why* behind any of this — request-ID propagation,
what gets logged where, ErrorBeacon's internals — see
[`ERRORBEACON_COVERAGE.md`](ERRORBEACON_COVERAGE.md). For a visual
waterfall/trace view instead of log lines (optional, `OTEL_ENABLED=true`
only), see [`TELEMETRY.md`](TELEMETRY.md) — that's a complementary tool,
not a replacement: Jaeger only ever gets a boolean `error=true` marker for
browser errors, never the actual stack trace, by design (see that doc's
"browser telemetry security boundary"). Everything in this guide works with
no extra configuration and no dependency on OpenTelemetry being enabled.

## Contents

- [0. The one thing that's true everywhere: the `request_id`](#0-the-one-thing-thats-true-everywhere-the-request_id)
- [1. Local Docker Compose](#1-local-docker-compose)
- [2. Azure Container Apps (ACA)](#2-azure-container-apps-aca)
- [3. Azure VM (blue-green + Cloudflare Tunnel)](#3-azure-vm-blue-green--cloudflare-tunnel)
- [4. Render Free](#4-render-free)
- [5. Quick reference](#5-quick-reference)
- [6. What you're actually looking for in the output](#6-what-youre-actually-looking-for-in-the-output)

---

## 0. The one thing that's true everywhere: the `request_id`

Every response (success or failure) carries an `X-Request-ID` header, and
every JSON error body includes a `"request_id"` field. A frontend toast/
support ticket shows this as a "Reference" — copy it. That's the only input
every command below needs.

You do **not** need to know in advance whether the failure was a caught
backend 500 or an uncaught JS crash — the workflow, and the ID, are the
same either way.

---

## 1. Local Docker Compose

**From your machine, in the project root (same folder as
`docker-compose.yml`):**

```bash
scripts/trace-request.sh <request_id>
```

That's the whole command. It greps `backend`, `worker`, `beat`, and
`errorbeacon`'s logs in one pass, in chronological order, and pretty-prints
each JSON log line — including the full exception/stack trace, whether it
came from a caught backend exception or an uncaught browser crash.

Useful flags:
```bash
scripts/trace-request.sh <request_id> --since 2h     # search further back (default: 15m)
scripts/trace-request.sh <request_id> --follow        # keep watching live as it happens
```

No `docker compose`/Python required beyond Docker itself; falls back to raw
log lines if `python3` isn't on your `PATH`.

**If ErrorBeacon isn't running or isn't configured** (container stopped, or
`ERRORBEACON_INGEST_API_KEY` unset in `.env`): the exact same command still
gives you the full story — the backend's own log line carries the complete
stack trace regardless of ErrorBeacon's availability. The only thing you
lose is the `errorbeacon` container's ingest/Telegram-delivery lines
(nothing to grep — the event was never queued). Check with:
```bash
docker compose ps errorbeacon
```

---

## 2. Azure Container Apps (ACA)

Four separate Container Apps carry a piece of the story: `backend`,
`worker`, `beat`, `errorbeacon`. Two ways to search them, both from your
own machine (`az login` + access to the resource group is all you need —
no SSH, no exec required just to read logs).

### Fastest: Log Analytics, one query across all four apps

```bash
az monitor log-analytics query \
  --workspace <workspace-id> \
  --analytics-query "
    ContainerAppConsoleLogs_CL
    | where ContainerAppName_s in ('backend','worker','beat','errorbeacon')
    | where Log_s contains '<request_id>'
    | order by TimeGenerated asc
  "
```

Don't have `<workspace-id>` handy? Look it up once:
```bash
az monitor log-analytics workspace list \
  --resource-group rg-snipeit-lite-prod \
  --query "[0].customerId" -o tsv
```

This is the ACA equivalent of `trace-request.sh` — one query, every
container, in order, and `Log_s` is the same JSON line shape the local
containers produce (so the `exception` field is right there in the blob).

### Live tail instead (no Log Analytics query, just watch it happen)

Open one terminal per app you care about:
```bash
az containerapp logs show --name backend --resource-group rg-snipeit-lite-prod --follow
az containerapp logs show --name worker --resource-group rg-snipeit-lite-prod --follow
az containerapp logs show --name beat --resource-group rg-snipeit-lite-prod --follow
az containerapp logs show --name errorbeacon --resource-group rg-snipeit-lite-prod --follow
```
Then reproduce the error and read straight off the stream, or `| grep <request_id>` any of them.

**If ErrorBeacon is down/unconfigured on ACA:** same deal as local — drop
`errorbeacon` from the `in (...)` filter (or just ignore empty results from
it) and the `backend` app's own log line still has the full trace.

---

## 3. Azure VM (blue-green + Cloudflare Tunnel)

This path has one real gotcha: the VM's compose file is
`docker-compose.vm.yml`, **not** `docker-compose.yml`, and the backend
service is split into two blue-green slots — `backend-blue` and
`backend-green` — not a single `backend`. `trace-request.sh`'s hardcoded
service list doesn't match this shape, so use the equivalent command
directly instead (same grep logic, right compose file, right service
names). Run it in one shot over SSH from your own machine — no interactive
shell needed:

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> bash -s <<EOF
  cd /opt/snipeit
  docker compose -f docker-compose.vm.yml logs --no-color --timestamps --since 15m \
    backend-blue backend-green worker beat errorbeacon \
    | grep -F '<request_id>'
EOF
```

(`<VM_HOST>` is the `ssh_hostname` Terraform output, e.g.
`ssh-assets.example.com` — see `DEPLOYMENT_VM.md` steps 2/8/9 if you don't
have this yet. This goes over the Cloudflare Tunnel; there's no open SSH
port to the public IP.)

Both `backend-blue` and `backend-green` are included deliberately — you
generally don't know which slot actually served a given request at the
time it failed, and grepping both costs nothing (the ID only appears in
whichever one actually handled it).

Want the pretty-printed, JSON-formatted version instead of raw lines? Copy
`trace-request.sh` onto the VM once and point it at the right file/services:
```bash
scp -i snipeit_vm_deploy_key scripts/trace-request.sh azureuser@<VM_HOST>:/opt/snipeit/scripts/trace-request-vm.sh
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> \
  "cd /opt/snipeit && sed -i \
    -e 's/docker compose logs/docker compose -f docker-compose.vm.yml logs/' \
    -e 's/SERVICES=(backend worker beat errorbeacon)/SERVICES=(backend-blue backend-green worker beat errorbeacon)/' \
    scripts/trace-request-vm.sh && \
   bash scripts/trace-request-vm.sh <request_id>"
```

**If ErrorBeacon is down/unconfigured on the VM:** same as everywhere else
— drop `errorbeacon` from the service list, the `backend-blue`/
`backend-green` log line still has the full trace.

---

## 4. Render Free

Render's free-tier shape is a single combined web service — `backend`,
`worker`, and `beat` all run inside **one** container (`render-start.sh`
backgrounds Celery worker+beat, then `exec`s uvicorn as PID 1), so their
logs already interleave in **one** log stream. ErrorBeacon, if you deployed
it, is a **separate** Render service (its own Blueprint under
`errorbeacon/render.yaml`) with its own, separate log stream.

**From the Render dashboard:** open the web service → **Logs** tab → use
the search box to filter for the `request_id`. Do the same on the
`errorbeacon` service (if deployed) for the ingest/Telegram-delivery lines.

**From your own machine, via the Render CLI** (`render login` once, then):
```bash
render logs -o <your-web-service-name> --tail | grep '<request_id>'
render logs -o <your-errorbeacon-service-name> --tail | grep '<request_id>'
```

Both services log structured JSON lines (`LOG_FORMAT=json` is the default
for the backend on Render; ErrorBeacon always logs JSON) — same shape as
every other environment, `exception` field included.

**Remember:** on Render Free, `ERRORBEACON_INGEST_API_KEY` is
`sync: false` in `render.yaml` — i.e. it's very possible you haven't set it
at all. If so, you're permanently in "ErrorBeacon not running" mode for
this deployment: the single web service's own log line is still the
complete story (full stack trace, no truncation), you just won't have a
second `errorbeacon` service to check at all.

---

## 5. Quick reference

| Deployment | Access from | Command |
|---|---|---|
| Local Docker Compose | your machine, repo root | `scripts/trace-request.sh <id>` |
| Azure Container Apps | your machine (`az login`) | `az monitor log-analytics query ...` (one query, all 4 apps) or `az containerapp logs show --name <app> --follow` |
| Azure VM | your machine, over Cloudflare Tunnel SSH | `ssh ... "docker compose -f docker-compose.vm.yml logs backend-blue backend-green worker beat errorbeacon \| grep -F '<id>'"` |
| Render Free | Render dashboard, or `render logs` CLI | search/grep the web service's log stream + the separate `errorbeacon` service's, if deployed |

None of these require shelling *into* a container just to read logs — every
path above is reachable from your own machine. You'd only need an actual
in-container shell (`docker compose exec backend sh`, `az containerapp exec`,
Render's Shell tab) for something beyond tracing, like running a one-off
script against the live database.

---

## 6. What you're actually looking for in the output

Regardless of deployment path, a complete story looks like this, in order:

1. **Request arrives** — method, path, and every service/SQL log line the
   backend produced while handling it.
2. **The failure itself** — one of:
   - A caught backend exception: `logger.error(..., exc_info=True)` →
     JSON line with a `"exception"` field holding the full Python
     traceback.
   - An uncaught browser crash: `POST /api/telemetry/client-error` →
     JSON line, same `"exception"` field, holding the full JS stack trace
     — renders identically to the backend case, not a summary or a
     pointer elsewhere.
3. **(If ErrorBeacon is configured and reachable)** — an `errorbeacon`
   ingest line (`"Event ingested: incident=... queued=..."`) followed by
   a delivery outcome line (`"Alert delivered: incident=... telegram_sent=true/false"`),
   both carrying the same `request_id`.

If step 3 is missing entirely, that's not a partial trace — it means
ErrorBeacon wasn't reachable/configured for that event, and step 2 already
has everything you need to know what actually went wrong.
