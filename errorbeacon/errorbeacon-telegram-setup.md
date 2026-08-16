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
11. [How to verify the fallback models](#11-how-to-verify-the-fallback-models)
    - 11.1 [Provider/model reference](#111-providermodel-reference)
12. [Azure Container Apps](#12-azure-container-apps)
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

### Full-stack

From the repository root:

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

### Standalone

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
ERRORBEACON_GROQ_API_KEY          optional
ERRORBEACON_GEMINI_API_KEY       optional
ERRORBEACON_OPENROUTER_API_KEY   optional
```

### Variables

```text
ERRORBEACON_TELEGRAM_CHAT_ID
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
Azure Files storage.

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

### ErrorBeacon appears healthy but backend incidents are missing

Check the backend configuration:

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
