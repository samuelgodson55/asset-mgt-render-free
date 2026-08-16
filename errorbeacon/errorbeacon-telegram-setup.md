# ErrorBeacon + Telegram — Complete Setup Guide

This guide configures ErrorBeacon as the application's isolated incident monitor.
It covers Telegram delivery, the optional AI provider chain, local Docker Compose,
Azure Container Apps, the VM deployment path, testing, recovery behavior, and
the configuration that must exist in GitHub for automated deployments.

## Table of contents

1. [How ErrorBeacon works](#1-how-errorbeacon-works)
2. [Configuration locations](#2-configuration-locations)
3. [Create the Telegram bot](#3-create-the-telegram-bot)
4. [Get the Telegram chat ID](#4-get-the-telegram-chat-id)
5. [Generate the ErrorBeacon API key](#5-generate-the-errorbeacon-api-key)
6. [Configure the AI fallback chain](#6-configure-the-ai-fallback-chain)
   - 6.1 [Recommended: configure more than one provider](#61-recommended-configure-more-than-one-provider)
   - 6.2 [Groq](#62-groq) — includes how to get a Groq API key
   - 6.3 [Gemini primary and fallback](#63-gemini-primary-and-fallback) — includes how to get a Gemini API key
   - 6.4 [OpenRouter](#64-openrouter) — includes how to get an OpenRouter API key
   - 6.5 [Run without AI](#65-run-without-ai)
7. [AI retries and durable recovery](#7-ai-retries-and-durable-recovery)
8. [Full-stack local configuration](#8-full-stack-local-configuration)
9. [Standalone ErrorBeacon configuration](#9-standalone-errorbeacon-configuration)
10. [Test Telegram end to end](#10-test-telegram-end-to-end)
    - 10.1 Full-stack (local Docker Compose)
    - 10.2 Standalone (e.g. Render)
    - 10.3 Azure Container Apps (incl. the Windows/Git Bash `exec` gotcha)
    - 10.4 Azure VM
11. [How to verify the fallback models](#11-how-to-verify-the-fallback-models)
    - 11.1 [Provider/model reference](#111-providermodel-reference)
12. [Azure Container Apps](#12-azure-container-apps)
    - 12.1 [Why ErrorBeacon's SQLite database needs special handling on ACA](#121-why-errorbeacons-sqlite-database-needs-special-handling-on-aca)
    - 12.2 [Stale SQLite files left behind by a crashed container](#122-stale-sqlite-files-left-behind-by-a-crashed-container)
13. [Azure VM](#13-azure-vm)
14. [Request correlation and redaction](#14-request-correlation-and-redaction)
15. [Important operational settings](#15-important-operational-settings)
16. [Chaos and test incidents](#16-chaos-and-test-incidents)
17. [Troubleshooting](#17-troubleshooting)
18. [Security rules](#18-security-rules)
19. [Quick reference](#19-quick-reference)

---

## 1. How ErrorBeacon works

ErrorBeacon is deliberately separate from the backend/frontend traffic lifecycle.

```text
Application
    |
    | POST /v1/events + X-API-Key
    v
ErrorBeacon
    |
    +-- SQLite incident history
    +-- fingerprint / deduplication
    +-- spike + deployment-regression detection
    +-- immediate Telegram alert
    |
    +-- optional AI enrichment queue
          |
          +-- 1. Groq
          +-- 2. Gemini primary model
          +-- 3. Gemini fallback model
          +-- 4. OpenRouter
```

Important: **AI is enrichment, not the alert path.** Telegram is attempted immediately.
If every AI provider is unavailable, the incident remains stored and the Telegram
alert can still be delivered.

The AI chain is tried in exactly this order:

1. **Groq** — only when `GROQ_API_KEY` is configured.
2. **Gemini primary** — `GEMINI_MODEL`.
3. **Gemini fallback** — `GEMINI_FALLBACK_MODEL`, using the same Gemini API key.
4. **OpenRouter** — only when `OPENROUTER_API_KEY` is configured.

A provider failure, empty response, invalid response, or unusable response causes
ErrorBeacon to move to the next configured provider. A rate-limited provider is
not immediately hammered again; the durable retry system handles later attempts.

If no AI provider key is configured, ErrorBeacon simply runs without AI analysis.

---

## 2. Configuration locations

There are two normal operating modes.

| Mode | Command | Environment file |
|---|---|---|
| Full application stack | `docker compose up --build` from the repository root | Repository root `.env` |
| Standalone ErrorBeacon | `docker compose up -d --build` from `errorbeacon/` | `errorbeacon/.env` |

In full-stack mode, the root `docker-compose.yml` explicitly passes the ErrorBeacon
variables into the `errorbeacon` container. `errorbeacon/.env` is **not** used by
that stack.

Do not maintain two different local configurations unless you intentionally run both
modes.

---

## 3. Create the Telegram bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`.
3. Follow the prompts.
4. Save the token returned by BotFather.

Set:

```env
TELEGRAM_BOT_TOKEN=<BotFather token>
```

Treat this token as a secret.

---

## 4. Get the Telegram chat ID

### Personal chat

1. Open the new bot.
2. Send it a message.
3. Request updates using:

```text
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

Find:

```json
"chat": {
  "id": 123456789
}
```

Use that number for:

```env
TELEGRAM_CHAT_ID=123456789
```

### Group

1. Add the bot to the group.
2. Send a message in the group.
3. Request `getUpdates` again.
4. Use the group's `chat.id`, commonly a negative number such as:

```env
TELEGRAM_CHAT_ID=-1001234567890
```

If Telegram privacy mode prevents the bot from seeing the group message, disable
group privacy for the bot through BotFather.

### Channel

Add the bot as a channel administrator, publish a message, and inspect
`getUpdates` for the channel's `chat.id`.

### Forum topic

For a Telegram forum topic, configure:

```env
TELEGRAM_THREAD_ID=<topic thread id>
```

Leave it blank when you do not use topics.

---

## 5. Generate the ErrorBeacon API key

This is a separate shared secret between the monitored application and ErrorBeacon.

Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Configure the exact same value as:

```env
ERRORBEACON_API_KEY=<generated value>
```

The backend sends it as:

```text
X-API-Key: <generated value>
```

Never reuse the JWT secret, database password, Telegram token, or an unrelated
cloud credential.

---

## 6. Configure the AI fallback chain

AI analysis is optional. Telegram alerting does not require any AI provider.

### 6.1 Recommended: configure more than one provider

For the strongest fallback behavior, configure at least two independent providers.

Example:

```env
AI_ENABLED=true

# 1. First provider
GROQ_API_KEY=<Groq API key>
GROQ_MODEL=llama-3.1-8b-instant

# 2. Primary Gemini
GEMINI_API_KEY=<Gemini API key>
GEMINI_MODEL=gemini-2.5-flash-lite

# 3. Same Gemini key, different model
GEMINI_FALLBACK_MODEL=gemini-2.5-flash

# 4. Independent fallback
OPENROUTER_API_KEY=<OpenRouter API key>
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local
```

The keys are independent:

- `GROQ_API_KEY` is only for Groq.
- `GEMINI_API_KEY` is used for both Gemini models.
- `OPENROUTER_API_KEY` is only for OpenRouter.

### 6.2 Groq

#### How to get a Groq API key

1. Open [console.groq.com](https://console.groq.com) and sign up with email, Google, or GitHub.
2. Verify your email address if prompted.
3. Open the **API Keys** section of the console (or go directly to
   [console.groq.com/keys](https://console.groq.com/keys)).
4. Click **Create API Key**, give it a descriptive name such as `errorbeacon`,
   and click **Submit**.
5. Copy the key immediately. Groq shows the full value only once — once you
   navigate away it is masked, and a lost key means creating a new one.

No credit card is required. Groq's free tier is enough for development and
light production use.

Set:

```env
GROQ_API_KEY=<your Groq key>
GROQ_MODEL=llama-3.1-8b-instant
```

Groq is attempted first whenever its key is present.

If Groq is not configured, ErrorBeacon starts directly with Gemini.

### 6.3 Gemini primary and fallback

#### How to get a Gemini API key

1. Open [aistudio.google.com](https://aistudio.google.com) and sign in with a
   Google account.
2. Accept the Generative AI terms if this is your first visit, and confirm
   your region — the Gemini API is not available in every country.
3. In the left sidebar, click **Get API key** (or go directly to
   [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)).
4. Click **Create API key**. Choose *Create key in new project* unless you
   already have a Google Cloud project you want to attach it to — no existing
   Cloud setup is required.
5. Copy the generated key. It starts with `AIza`. Unlike Groq and OpenRouter,
   AI Studio lets you view the key again later, but it should still be stored
   securely right away.

No billing setup is required to use the free tier.

Set:

```env
GEMINI_API_KEY=<your Gemini key>
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
```

The fallback model is not a second API provider. It is a second Gemini model using
the same `GEMINI_API_KEY`.

The fallback is only added when it differs from `GEMINI_MODEL`.

### 6.4 OpenRouter

#### How to get an OpenRouter API key

1. Open [openrouter.ai](https://openrouter.ai) and sign up with email, Google,
   or GitHub.
2. Once signed in, open the **Keys** page from the account menu (or go
   directly to [openrouter.ai/keys](https://openrouter.ai/keys)).
3. Click **Create Key**, give it a descriptive name, and optionally set a
   credit limit on it.
4. Copy the key immediately — like Groq, OpenRouter only displays the full
   value once.
5. Add credits on the **Credits** page before sending production traffic.
   Some models are served on free endpoints, but most require a balance;
   check current per-model pricing on OpenRouter's models page.

Set:

```env
OPENROUTER_API_KEY=<your OpenRouter key>
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local
```

OpenRouter is the last provider in the configured chain.

`OPENROUTER_SITE_URL` is sent as the `HTTP-Referer` header. Change it to the real
public application URL when you have one.

### 6.5 Run without AI

To disable AI entirely:

```env
AI_ENABLED=false
```

Or leave every provider key empty. In either case, Telegram incident delivery
continues normally.

---

## 7. AI retries and durable recovery

AI work is not performed synchronously inside `/v1/events`.

The service uses a bounded AI queue and worker pool. Relevant settings are:

```env
AI_WORKERS=1
AI_RETRIES=1
AI_RETRY_BASE_SECONDS=30
AI_MAX_INCIDENT_RETRIES=3
AI_QUEUE_SIZE=500
```

There are two levels of retry:

1. `AI_RETRIES` retries the configured provider chain during one queue attempt.
2. `AI_MAX_INCIDENT_RETRIES` controls durable incident-level retries stored in SQLite.

A provider can therefore fail without losing the incident.

The database tracks AI state such as:

- `ai_status`
- `ai_attempts`
- `ai_next_retry_at`
- `ai_last_error`

This is important for restarts: pending AI work can be recovered from SQLite rather
than depending only on an in-memory queue.

Telegram delivery also has durable retry state. A failed Telegram delivery is not
silently marked successful.

---

## 8. Full-stack local configuration

Copy the repository example:

```bash
cp .env.example .env
```

Add or update:

```env
ERRORBEACON_API_KEY=<generated key>

TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_CHAT_ID=<chat id>
TELEGRAM_THREAD_ID=

AI_ENABLED=true

GROQ_API_KEY=
GROQ_MODEL=llama-3.1-8b-instant

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash

OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local

ERRORBEACON_URL=http://errorbeacon:8000
ERRORBEACON_APP=asset-inventory-quotes
ERRORBEACON_TIMEOUT=0.75

# Included in every incident and used for deployment-regression detection
# (see section 6): the same fingerprint reappearing under a new release
# within 7 days is flagged as a possible regression.
APP_RELEASE=local
ENVIRONMENT=development
```

Then:

```bash
docker compose up --build
```

ErrorBeacon is intentionally not published to the host in the full stack.

From the backend container, check it with Python:

```bash
docker compose exec backend python3 -c "
import requests
r = requests.get('http://errorbeacon:8000/healthz', timeout=5)
print(r.status_code, r.text)
"
```

Do not expect `curl` to exist inside the backend or ErrorBeacon containers.

---

## 9. Standalone ErrorBeacon configuration

From the ErrorBeacon directory:

```bash
cd errorbeacon
cp .env.example .env
```

Configure:

```env
ERRORBEACON_API_KEY=<generated key>

TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_CHAT_ID=<chat id>
TELEGRAM_THREAD_ID=

AI_ENABLED=true

GROQ_API_KEY=
GROQ_MODEL=llama-3.1-8b-instant

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash

OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local
```

Start it:

```bash
docker compose up -d --build
```

The standalone compose file publishes:

```text
127.0.0.1:8787 -> container:8000
```

Check health:

```bash
curl http://127.0.0.1:8787/healthz
```

---

## 10. Test Telegram end to end

Every deploy path exposes the same `/v1/test` endpoint (`POST`, requires the
`X-API-Key` header). What differs between paths is **how you reach ErrorBeacon
at all** — some paths let you `curl` it directly, others only let you reach it
from inside the Docker/container network.

> **Beginner note:** if you're not sure which of these applies to you, look at
> where you're running commands *from*. If you can `curl 127.0.0.1:<port>` and
> get a response, you have direct network access (Standalone/VM-via-SSH). If
> you get "connection refused" or "could not resolve host", you need to run
> the test *from inside* the same Docker network or container app environment
> — that's what `docker compose exec` / `az containerapp exec` are for below.

### Full-stack (local Docker Compose)

From the repository root, on your own machine:

```bash
docker compose exec backend python3 -c "
import requests
r = requests.post(
    'http://errorbeacon:8000/v1/test',
    headers={'X-API-Key': '<your key>'},
    timeout=10,
)
print(r.status_code, r.text)
"
```

This runs the test *from inside* the `backend` container, which is why it uses
the Docker Compose service name `errorbeacon` instead of `localhost` — Docker
Compose gives every service a DNS name equal to its service name, but only
reachable from other containers on the same Compose network, not from your
host machine directly.

### Standalone (`errorbeacon/docker-compose.yml` only, e.g. Render)

The standalone Compose file publishes ErrorBeacon on your host at
`127.0.0.1:8787`, so you can `curl` it directly without exec'ing into
anything:

```bash
curl -X POST   http://127.0.0.1:8787/v1/test   -H "X-API-Key: <your key>"
```

A successful response resembles:

```json
{
  "ok": true,
  "incident_id": "daea0ae11ad1",
  "queued": true,
  "request_id": "b2b9e6f0-2f1a-4c3e-9c2d-6e6a9a2b7e10",
  "silenced": false
}
```

The test incident should arrive in Telegram.

The `/v1/test` event also exercises the AI enrichment path when a provider is
configured.

### Azure Container Apps

The `errorbeacon` Container App is deployed with `ingress.external: false` —
this is **intentional**, it keeps ErrorBeacon reachable only from other apps
inside the same Container Apps Environment (like `backend`), not from the
public internet. That means `curl http://errorbeacon.<region>.azurecontainerapps.io/...`
from your laptop will not work, and that's expected, not a bug.

To test it, exec straight into the running container and talk to
`localhost:8000` from inside. The container's base image (`python:3.12-slim`)
does not have `curl` installed, so use `python3`, which is already there:

```bash
az containerapp exec --name errorbeacon --resource-group <your-resource-group> --command "/bin/sh"
```

Once you have a shell inside the container:

```bash
# 1. Confirm Telegram is actually configured (no auth needed for /healthz)
python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/healthz').read())"
# Look for "telegram_configured": true in the output. If it's false, the
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets never made it into this
# revision -- see section 12 below.

# 2. Fire the test alert
python3 -c "
import os, urllib.request
req = urllib.request.Request(
    'http://localhost:8000/v1/test',
    method='POST',
    headers={'x-api-key': os.environ['ERRORBEACON_API_KEY']},
)
print(urllib.request.urlopen(req).read())
"

# 3. If 2 doesn't work
python3 -c "import os, urllib.request; req = urllib.request.Request('http://localhost:8000/v1/test', method='POST', headers={'x-api-key': os.environ['ERRORBEACON_API_KEY']}); print(urllib.request.urlopen(req).read())"
```

Then check Telegram — the message should arrive within a couple of seconds
(alerts are processed by a small worker pool, see `ALERT_WORKERS`).

**Windows / Git Bash gotcha:** if `az containerapp exec` fails immediately with
something like:

```text
ERROR: {"Error":{"Code":"ClusterExecFailure","Message":"...websocket: close 1011 (internal server error)...
```

this is almost always **not** an Azure-side problem — it's Git Bash (MINGW64)
failing to allocate a proper terminal (PTY) for the interactive session. Two
fixes, either works:

- Prefix the command with `winpty` (ships with Git for Windows):
  `winpty az containerapp exec --name errorbeacon ...`
- Or just run the exact same command from PowerShell or `cmd.exe` instead of
  Git Bash — both allocate a PTY correctly with no extra flags needed.

If you'd rather not fight the terminal at all, you can temporarily flip
`errorbeacon`'s ingress to `external: true` (via the Portal, `az containerapp
ingress enable`, or a one-off bicep change) to `curl` it directly from your
machine — but remember it's then reachable from the public internet, protected
only by the API key, so flip it back to `external: false` once you're done
testing.

### Azure VM

SSH into the VM first, then run the same style of test as the local Compose
paths, since the VM runs `docker-compose.vm.yml`:

```bash
ssh <user>@<vm-ip>
cd /path/to/deployed/compose   # wherever deploy-azure-vm.yml placed it
docker compose -f docker-compose.vm.yml exec backend python3 -c "
import requests
r = requests.post(
    'http://errorbeacon:8000/v1/test',
    headers={'X-API-Key': '<your key>'},
    timeout=10,
)
print(r.status_code, r.text)
"
```

Same reasoning as Full-stack above: `errorbeacon` here is a Docker Compose
service name, only resolvable from another container on the same VM's Compose
network — not from your own laptop.

---

## 11. How to verify the fallback models

Do not test the fallback by disabling Telegram. Telegram and AI are separate.

For a real fallback test:

1. Configure at least two providers.
2. Send `/v1/test`.
3. Inspect the ErrorBeacon logs.
4. The successful provider is logged by name.
5. If the first provider fails, the next configured provider should be attempted.

For example, with all four configured:

```text
groq
gemini
gemini-fallback
openrouter
```

The expected order is always:

```text
Groq
  -> Gemini primary
  -> Gemini fallback
  -> OpenRouter
```

If Groq succeeds, Gemini is not called for that incident.

If Groq fails but Gemini succeeds, Gemini fallback is not called.

If both Gemini models fail, OpenRouter is attempted when configured.

If every provider fails, the incident remains persisted and the durable retry
mechanism will try the chain again according to the AI retry settings.

### 11.1 Provider/model reference

| Order | Log name | Env var(s) | Default model | Requires |
|---|---|---|---|---|
| 1 | `groq` | `GROQ_MODEL` | `llama-3.1-8b-instant` | `GROQ_API_KEY` |
| 2 | `gemini` | `GEMINI_MODEL` | `gemini-2.5-flash-lite` | `GEMINI_API_KEY` |
| 3 | `gemini-fallback` | `GEMINI_FALLBACK_MODEL` | `gemini-2.5-flash` | `GEMINI_API_KEY` (same key as #2; only added when this model differs from `GEMINI_MODEL`) |
| 4 | `openrouter` | `OPENROUTER_MODEL` | `openrouter/free` | `OPENROUTER_API_KEY` |

Any row whose key is unset is skipped entirely — it is never counted as a "failed"
attempt, it simply isn't part of the chain for that deployment.

---

## 12. Azure Container Apps

The ACA infrastructure supports the same provider chain.

Configure these GitHub secrets/variables when you want AI in ACA:

### Secrets

```text
ERRORBEACON_API_KEY
ERRORBEACON_TELEGRAM_BOT_TOKEN
ERRORBEACON_TELEGRAM_CHAT_ID
ERRORBEACON_GROQ_API_KEY          optional
ERRORBEACON_GEMINI_API_KEY       optional
ERRORBEACON_OPENROUTER_API_KEY   optional
```

### Variables

```text
ERRORBEACON_TELEGRAM_THREAD_ID   optional

ERRORBEACON_GROQ_MODEL            optional
ERRORBEACON_GEMINI_MODEL          optional
ERRORBEACON_GEMINI_FALLBACK_MODEL optional
ERRORBEACON_OPENROUTER_MODEL     optional
ERRORBEACON_OPENROUTER_SITE_URL  optional
```

Defaults are provided by the deployment code:

```text
Groq:             llama-3.1-8b-instant
Gemini primary:   gemini-2.5-flash-lite
Gemini fallback:  gemini-2.5-flash
OpenRouter:       openrouter/free
```

The ACA deployment passes these values into the isolated ErrorBeacon Container App.
They are not part of backend/frontend blue-green traffic switching.

Keep ErrorBeacon at one active replica when Telegram polling is enabled.

There are two different ways these values reach ACA, and they matter for different
situations:

- **`infra-deploy.yml`** (Bicep) sets every provider/model when the ErrorBeacon
  Container App is first provisioned, or when you re-run the full infra deploy.
- **`sync-secrets-aca.yml`** updates an *already-running* ErrorBeacon Container App
  in place, without a full infra deploy — this is the workflow to run after only
  changing a secret or variable (e.g. rotating `ERRORBEACON_GROQ_API_KEY` or
  switching `ERRORBEACON_GEMINI_FALLBACK_MODEL`). It syncs all four providers
  (Groq, Gemini primary, Gemini fallback, OpenRouter), so a single run is enough
  to pick up any provider/model change.

### 12.1 Why ErrorBeacon's SQLite database needs special handling on ACA

`errorbeacon`'s `/data` directory (where its SQLite database lives) is mounted
from an Azure Files (SMB) share, not local disk — that's what makes incident
history survive a redeploy or a container restart. But SQLite was designed
assuming a local filesystem, and Azure Files' SMB implementation has two
specific incompatibilities with it that showed up as
`sqlite3.OperationalError: database is locked` on startup, even with exactly
one replica and one Uvicorn worker (i.e. genuinely no concurrent writer):

1. **WAL journal mode needs shared memory.** SQLite's default `WAL` journal
   mode relies on an `mmap`-based shared-memory file (`-shm`) that network
   filesystems don't support reliably. Fix: `errorbeacon` is deployed with
   `SQLITE_JOURNAL_MODE=DELETE` (a plain rollback journal, no shared memory
   required) instead of the app's normal `WAL` default. `app/main.py`'s
   `init_db()` also has a built-in fallback: if the configured journal mode
   can't actually be set, it automatically retries with `DELETE` instead of
   failing forever — a safety net in case this ever runs on another
   network-backed mount in the future.
2. **SMB doesn't reliably grant POSIX byte-range locks.** Even with
   `DELETE` mode, SQLite still needs to take an internal write lock before
   `CREATE TABLE` / `INSERT` / `UPDATE`, and Azure Files' SMB client was
   rejecting that lock request outright — this is a
   [documented Azure Files limitation](https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/storage/mountoptions-settings-azure-files),
   not something specific to this app. Fix: the `errorbeacon-data` volume in
   `infra/main.bicep` is mounted with `mountOptions: 'nobrl'`, which tells the
   SMB client not to forward byte-range lock requests to the server at all.
   This is safe here specifically *because* there's only ever one writer
   (`minReplicas`/`maxReplicas` are both `1`, one Uvicorn worker) — `nobrl`
   would be the wrong call for a genuinely multi-writer workload sharing the
   same file.

**You should not need to touch either of these settings.** They're documented
here so that if `errorbeacon` ever starts crash-looping with a `database is
locked` traceback pointing at `init_db()` in the logs again, you know
immediately that it's this class of problem, not a code regression.

### 12.2 Stale SQLite files left behind by a crashed container

If `errorbeacon` crash-looped before you applied the fixes in 12.1, it may
have left a **zero-byte `errorbeacon.db`** and/or a leftover
**`errorbeacon.init.lock`** file sitting on the `errorbeacon-data` file share
(from `init_db()`'s own `fcntl.flock()` never getting a clean chance to
release). These are harmless to delete — the app recreates both on next
startup — but worth clearing so you're testing against a clean slate:

```bash
# Find the storage account (its name is randomized, e.g. snipeitliteprod<random>)
az storage account list --resource-group <your-resource-group> -o table

# List what's actually in the share (OAuth/--auth-mode login often lacks the
# right role by default -- key-based auth is simpler for a one-off check)
az storage account keys list --account-name <storage-account-name> --resource-group <your-resource-group> --query "[0].value" -o tsv
az storage file list --account-name <storage-account-name> --share-name errorbeacon-data --account-key "<key-from-above>" -o table

# Delete any stale errorbeacon.db / errorbeacon.init.lock found
az storage file delete --account-name <storage-account-name> --share-name errorbeacon-data --path errorbeacon.db --account-key "<key>"
az storage file delete --account-name <storage-account-name> --share-name errorbeacon-data --path errorbeacon.init.lock --account-key "<key>"
```

Rotate the storage key afterward if you shared it anywhere outside a secure
terminal (`az storage account keys renew --account-name <storage-account-name>
--resource-group <your-resource-group> --key primary`) — it's a live
credential with full read/write access to the share.

---

## 13. Azure VM

The VM deployment path uses the same provider variables.

Configure the same GitHub secrets/variables listed above. Two workflows write the
AI configuration into the VM's ErrorBeacon `.env`:

- **`deploy-azure-vm.yml`** writes it as part of every image deploy.
- **`sync-secrets-vm.yml`** writes it to an already-running VM without deploying a
  new image — this is the workflow to run after only changing a secret or variable.

The resulting variables are:

```env
GEMINI_API_KEY=...
GEMINI_MODEL=...
GEMINI_FALLBACK_MODEL=...

GROQ_API_KEY=...
GROQ_MODEL=...

OPENROUTER_API_KEY=...
OPENROUTER_MODEL=...
OPENROUTER_SITE_URL=...
```

This keeps the VM and ACA deployments aligned instead of having two different
ErrorBeacon AI implementations.

---

## 14. Request correlation and redaction

Every incoming ErrorBeacon event can carry a request ID.

The backend integration forwards:

```text
X-Request-ID
```

and ErrorBeacon includes the request ID in the persisted incident and Telegram
message.

ErrorBeacon also sanitizes sensitive values before persistence, AI analysis, or
Telegram delivery. Redaction covers common forms including:

- Authorization/Bearer tokens
- JWTs
- API keys
- passwords
- cookies
- session/reset/access/refresh tokens
- database connection URLs
- sensitive query parameters

Do not intentionally put secrets into exception messages or context. Redaction is
defense in depth, not permission to log credentials.

---

## 15. Important operational settings

The main configuration is:

```env
MAX_ALERTS_PER_MINUTE=30
DEDUP_SECONDS=60

SPIKE_THRESHOLD=10
SPIKE_WINDOW_SECONDS=300

ALERT_QUEUE_SIZE=1000
ALERT_WORKERS=3

TELEGRAM_POLLING=true
TELEGRAM_POLL_SECONDS=20

SQLITE_JOURNAL_MODE=WAL
```

For ACA Azure Files, the infrastructure sets:

```env
SQLITE_JOURNAL_MODE=DELETE
```

Do not casually change that in ACA. The setting is intentional for the mounted
Azure Files storage — see [12.1](#121-why-errorbeacons-sqlite-database-needs-special-handling-on-aca)
for why, along with the accompanying `mountOptions: 'nobrl'` on the
`errorbeacon-data` volume in `infra/main.bicep`. Both exist together to work
around real SQLite-on-network-filesystem limitations, not personal preference
— don't remove either without re-reading that section first.

`DATA_DIR` defaults to `/data`. The SQLite database lives there.

---

## 16. Chaos and test incidents

Controlled chaos events are persisted so they remain auditable.

Use:

```env
CHAOS_TEST_ALERTS=true
```

when you intentionally want them delivered to Telegram.

For production, the default is false:

```env
CHAOS_TEST_ALERTS=false
```

A chaos-test event is classified as informational rather than a real outage, and
health-check/dependency-degradation events are deliberately prevented from being
misrepresented as ordinary HTTP outages.

---

## 17. Troubleshooting

### `/v1/test` returns 401/403

The `X-API-Key` does not match `ERRORBEACON_API_KEY`.

Check both sides:

```env
ERRORBEACON_API_KEY=<same exact value>
```

### `/v1/test` succeeds but Telegram does not receive anything

Check:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_THREAD_ID=
```

For a personal DM, send the bot a message first. Bots cannot initiate a new
personal conversation.

### Telegram works but AI analysis is missing

Check:

```env
AI_ENABLED=true
```

and at least one provider key.

Then inspect:

```bash
docker compose logs errorbeacon
```

Look for provider-specific failure messages.

### Groq fails and Gemini does not run

Confirm:

```env
GEMINI_API_KEY=<valid key>
GEMINI_MODEL=gemini-2.5-flash-lite
```

The chain only includes configured providers.

### Gemini primary fails but fallback does not run

Confirm the fallback model differs from the primary:

```env
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
```

### I changed a fallback model or provider key but the running deployment didn't pick it up

Changing a GitHub secret/variable never reaches an already-running deployment by
itself — one of these also needs to run:

- Full-stack local: re-run `docker compose up --build` (or just restart the
  `errorbeacon` container so it re-reads `.env`).
- ACA: run `sync-secrets-aca.yml` (or `infra-deploy.yml` if you also changed
  infrastructure).
- VM: run `sync-secrets-vm.yml` (or let the next `deploy-azure-vm.yml` run pick it
  up).

If you're still not seeing the new model/provider after that, check the
ErrorBeacon logs for which providers it started with — `/healthz` also reports
`ai_providers`, which is the definitive list of what's actually configured right
now.

### All AI providers fail

This does not mean the incident is lost.

The incident remains in SQLite and the AI retry/recovery mechanism records the
failure and schedules another attempt until `AI_MAX_INCIDENT_RETRIES` is reached.

Telegram delivery is independent of this failure.

### Full-stack `curl` fails inside a container

That is expected. Use Python from the backend container:

```bash
docker compose exec backend python3 -c "
import requests
print(requests.get('http://errorbeacon:8000/healthz', timeout=5).text)
"
```

### `getUpdates` returns an empty result

Send a fresh message to the bot/group/channel and request updates again.

### ACA: `sqlite3.OperationalError: database is locked` in the logs, container crash-loops on startup

The traceback points at `init_db()` in `app/main.py`, and the container never
gets past "Application startup failed. Exiting." This is the Azure
Files/SQLite locking issue described in
[12.1](#121-why-errorbeacons-sqlite-database-needs-special-handling-on-aca),
not a code bug or a real concurrency conflict (it happens even with exactly
one replica and one worker). Checklist:

1. Confirm `infra/main.bicep` actually has both `SQLITE_JOURNAL_MODE=DELETE`
   in the `errorbeacon` container's env array **and** `mountOptions: 'nobrl'`
   on the `errorbeacon-data` volume. If either is missing, that's the fix.
2. Clear any stale `errorbeacon.db` / `errorbeacon.init.lock` left behind by
   earlier crashes — see [12.2](#123-stale-sqlite-files-left-behind-by-a-crashed-container).

### ACA: `az containerapp exec` fails with `ClusterExecFailure` / `websocket: close 1011`

This is a terminal problem, not an Azure problem — almost always Git Bash
(MINGW64) on Windows failing to allocate a PTY for the interactive session.
Prefix the command with `winpty`, or run the exact same command from
PowerShell or `cmd.exe` instead. See the Azure Container Apps subsection of
[section 10](#10-test-telegram-end-to-end) for the full testing walkthrough.

### ACA: redeployed but the container still shows the same revision / same crash

Work through the checklist in
[12.2](#122-if-a-redeploy-doesnt-seem-to-change-anything) — in order, the
usual causes are: `action=plan` instead of `apply`, the workflow running
against the wrong branch, or the image not actually being rebuilt.

### ErrorBeacon appears healthy but backend incidents are missing

Check the backend configuration. For Full-stack Docker Compose and the VM:

```env
ERRORBEACON_URL=http://errorbeacon:8000
ERRORBEACON_API_KEY=<same key as ErrorBeacon>
```

Then verify the backend can resolve the Docker service name:

```bash
docker compose exec backend python3 -c "
import requests
r = requests.get('http://errorbeacon:8000/healthz', timeout=5)
print(r.status_code)
"
```

For ACA, `errorbeacon:8000` is the wrong form -- ACA apps don't expose their
raw container port to other apps in the environment the way a Docker Compose
bridge network does; only the ingress proxy's port (80/443) is reachable, and
only via the app's FQDN. The Bicep template already sets this correctly for
you (`ERRORBEACON_URL=http://errorbeacon.internal.<environment-default-domain>`,
no port), so if you see this misconfigured, check that `infra/main.bicep`'s
`errorBeaconInternalFqdn`/`sharedEnv` haven't been hand-edited back to the
Compose-style `errorbeacon:8000` form.

---

## 18. Security rules

- Never commit `.env`, credentials, or provider API keys.
- Use separate credentials for ErrorBeacon, Telegram, Groq, Gemini, and OpenRouter.
- Keep ErrorBeacon private where possible.
- Do not expose `/v1/events` publicly unless there is a deliberate security design
  around it.
- Use HTTPS whenever ErrorBeacon and the monitored application communicate across
  a public network.
- Remember that AI enrichment sends incident information to the selected external
  AI provider.
- Keep Telegram polling to one ErrorBeacon replica unless the architecture is
  explicitly changed to coordinate polling across replicas.

---

## 19. Quick reference

### Minimum Telegram-only setup

```env
ERRORBEACON_API_KEY=<random key>
TELEGRAM_BOT_TOKEN=<telegram token>
TELEGRAM_CHAT_ID=<chat id>
AI_ENABLED=false
```

### Gemini-only setup

```env
AI_ENABLED=true
GEMINI_API_KEY=<gemini key>
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
```

### Recommended multi-provider setup

```env
AI_ENABLED=true

GROQ_API_KEY=<groq key>
GROQ_MODEL=llama-3.1-8b-instant

GEMINI_API_KEY=<gemini key>
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash

OPENROUTER_API_KEY=<openrouter key>
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local
```

The resulting fallback chain is:

```text
Groq
  ↓ failure
Gemini primary
  ↓ failure
Gemini fallback
  ↓ failure
OpenRouter
```
