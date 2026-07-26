# Deploying Snipe-IT Lite To Azure — Single VM (Terraform + Docker Compose)

This is the exact-steps companion for the **VM deployment target**: one
Azure Virtual Machine running the app's full six-container stack (`db`,
`redis`, `backend`, `worker`, `beat`, `frontend`) plus a seventh container,
`caddy`, for free automatic HTTPS. Infrastructure is provisioned with
Terraform (`infra-vm/`); every code deploy after that goes out over SSH via
`.github/workflows/deploy-azure-vm.yml`.

**This is a different, parallel path from `DEPLOYMENT.md`'s Azure Container
Apps guide** (`infra/main.bicep` + `deploy-azure-production.yml`/
`deploy-azure-staging.yml`). Pick one:

| | This guide (VM) | `DEPLOYMENT.md` (Container Apps) |
|---|---|---|
| Compute | 1 fixed-size VM, always on | Serverless containers, scale-to-zero |
| Database | Postgres in a container, on the VM's own disk | Managed Azure Database for PostgreSQL |
| Cost shape | Fixed monthly cost regardless of traffic | Pay only while handling requests |
| Ops | You own OS patching (mostly automated), backups, container health | Azure owns almost all of it |
| Good for | Predictable low/steady traffic, tightest possible fixed budget, wanting a single box you can SSH into | Bursty/unknown traffic, least ops burden |

Everything below assumes you're starting from a fresh clone of this repo
with no Azure resources yet.

---

## Table of Contents

- [0. Prerequisites](#0-prerequisites)
- [1. One-time Azure setup](#1-one-time-azure-setup)
- [2. Set up Cloudflare Tunnel (no open ports, no Bastion)](#2-set-up-cloudflare-tunnel-no-open-ports-no-bastion)
- [3. Generate the deploy SSH key pair](#3-generate-the-deploy-ssh-key-pair)
- [4. Generate application secrets](#4-generate-application-secrets)
- [5. Configure GitHub OIDC federation (for Terraform + no client secrets)](#5-configure-github-oidc-federation-for-terraform--no-client-secrets)
- [6. Set GitHub Environment secrets/variables](#6-set-github-environment-secretsvariables)
- [7. Review the Terraform plan locally (optional but recommended first time)](#7-review-the-terraform-plan-locally-optional-but-recommended-first-time)
- [8. Provision the VM (`infra-deploy-vm.yml`)](#8-provision-the-vm-infra-deploy-vmyml)
- [9. Point `deploy-azure-vm.yml` at the new VM](#9-point-deploy-azure-vmyml-at-the-new-vm)
- [10. Deploy the application (`deploy-azure-vm.yml`)](#10-deploy-the-application-deploy-azure-vmyml)
- [11. Verify](#11-verify)
- [Tagging & Versioning](#tagging--versioning)
- [Free domain + HTTPS](#free-domain--https)
- [Updating secrets on an already-running VM](#updating-secrets-on-an-already-running-vm)
- [Google Drive backup uploads](#google-drive-backup-uploads)
- [Per-service memory limits](#per-service-memory-limits)
- [Backups + restore](#backups--restore)
- [Growing the data disk](#growing-the-data-disk)
- [Cost](#cost)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

---

## 0. Prerequisites

Install locally (only needed for step 7's optional local plan — steps 8-10
run entirely inside GitHub Actions):

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`)
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.7
- An SSH client (`ssh-keygen`, already on macOS/Linux; use WSL or Git Bash on Windows)
- A Docker Hub account (free tier is fine — same requirement `DEPLOYMENT.md` already has)

You'll also need an Azure subscription with permission to create resource
groups, and (for step 5) permission to create an Azure AD App Registration —
ask a subscription/tenant admin for that if you don't have it yourself.

---

## 1. One-time Azure setup

```bash
az login
az account list --output table          # confirm you're on the right subscription
az account set --subscription "<subscription-id-or-name>"
az account show --query id -o tsv        # copy this — it's TF_VAR_subscription_id / AZURE_SUBSCRIPTION_ID later
```

---

## 2. Set up Cloudflare Tunnel (no open ports, no Bastion)

This is what lets the VM have **zero inbound ports open at all** — no
Azure Bastion, no jump box, no public port 22, 80, or 443 — while the app
is reachable over HTTPS (with free, automatic SSL) and `ssh`/`scp` still
work completely normally for you and for the deploy workflows. The VM
only ever makes one *outbound* connection to Cloudflare's edge (via the
`cloudflared` container in `docker-compose.vm.yml`); Cloudflare proxies
both the app and SSH back over that single connection.

Unlike the direct-IP + Let's Encrypt path this replaces, this setup
**requires a real domain living in a Cloudflare-managed zone** — Cloudflare
can only route a hostname to your Tunnel if it already controls DNS for
it. If you don't have one, register something cheap (many TLDs run
$1–12/year) and move its nameservers to Cloudflare (free) before
continuing. If you just want to confirm the app itself works behind
Cloudflare before committing to a domain, see the "Testing without a
domain yet" box at the end of this section.

**2a. Create a free Cloudflare account** at
[dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up), then
**Add a domain** and follow its nameserver-migration steps for your
domain if it isn't already on Cloudflare. From that domain's **Overview**
page, copy the **Account ID** and **Zone ID** shown in the right sidebar —
these are `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_ZONE_ID` in step 6's
secrets table. Also note the domain's apex (e.g. `example.com`) — this is
`CLOUDFLARE_ZONE_NAME`, a repo/environment **Variable**, not a secret.

**2b. Create an API token for Terraform** — this is what
`infra-deploy-vm.yml` uses to create the Tunnel, its DNS records, and the
Access application/policy that gates SSH:

- Go to [My Profile → API
  Tokens](https://dash.cloudflare.com/profile/api-tokens) → **Create
  Token** → **Create Custom Token**.
- Permissions: **Account → Cloudflare Tunnel → Edit**, **Account →
  Access: Apps and Policies → Edit**, **Zone → DNS → Edit** (scoped to
  your zone from 2a).
- Copy the generated token — this is `CLOUDFLARE_API_TOKEN` in step 6's
  secrets table. (Shown only once — if you lose it, create a new one.)

**2c. Generate an Origin CA certificate** — this is what Caddy presents
for the inner hop from `cloudflared` to itself, instead of requesting one
from Let's Encrypt (nothing but Cloudflare's own edge ever validates this
one, since the Tunnel is the only path in):

- Go to your domain's **SSL/TLS → Origin Server** tab → **Create
  Certificate**.
- Leave "Let Cloudflare generate a private key and a CSR for me" selected.
- Under hostnames, list your `custom_domain` (e.g. `assets.example.com`)
  and, if you'd like room to add more subdomains later without
  regenerating, `*.example.com` too.
- Certificate Validity: 15 years is fine (only Cloudflare validates it;
  there's no ACME-style 90-day renewal treadmill here).
- Copy the **Origin Certificate** block — this is `CLOUDFLARE_ORIGIN_CERT`
  in step 6's secrets table.
- Copy the **Private Key** block (shown once, on this same screen) — this
  is `CLOUDFLARE_ORIGIN_CERT_KEY`.

**2d. Decide who can SSH in** — list the email address(es) allowed through
Cloudflare Access's browser-SSO login as a JSON array, e.g.
`["you@example.com"]` — this is `SSH_ACCESS_ALLOWED_EMAILS` in step 6's
secrets table. CI/CD authenticates separately via a service token
Terraform creates automatically (see step 9's `CF_ACCESS_CLIENT_ID`/
`CF_ACCESS_CLIENT_SECRET`) and doesn't need to be listed here.

You now have everything steps 6-9 need: `CLOUDFLARE_API_TOKEN`,
`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_ZONE_NAME`,
`SSH_ACCESS_ALLOWED_EMAILS`, `CLOUDFLARE_ORIGIN_CERT`,
`CLOUDFLARE_ORIGIN_CERT_KEY`, and `CUSTOM_DOMAIN`.

> **Testing without a domain yet.** This is a **local-machine smoke test,
> not a VM/Terraform step** — nothing here needs a VM provisioned, a
> Cloudflare account, or any of steps 2a-2d's credentials. Cloudflare's
> free "Quick Tunnel" needs no account or zone at all; it just needs the
> app already running via the *root* `docker-compose.yml` (not
> `docker-compose.vm.yml`) on your own machine:
> ```bash
> docker compose up --build -d      # from the repo root, if it isn't already running
> docker compose ps                 # confirm "frontend" is Up
> ```
> `<project>_default` in the command below is a placeholder for Compose's
> auto-generated network name, which depends on the name of the folder
> you cloned this repo into — it is **not** literally the word
> `<project>`. Find the real value with:
> ```bash
> docker network ls | grep default
> ```
> (typically `<folder-name>_default`, e.g. `snipe-it-lite_default` if you
> cloned into a folder called `snipe-it-lite` — Compose lowercases the
> folder name and strips characters outside `[a-z0-9_-]`). Then run:
> ```bash
> docker run --rm --network <the-network-name-from-above> cloudflare/cloudflared:2025.6.1 \
>   tunnel --url http://frontend:80
> ```
> This prints a random `https://<random-words>.trycloudflare.com` URL you
> can open immediately — real HTTPS, zero setup. It's genuinely useful for
> confirming the app itself works behind Cloudflare, but it can't replace
> the setup above: the hostname changes every restart, and Cloudflare
> Access (the SSH gate in step 2d) can't attach to a hostname outside a
> zone you control. Treat it as a smoke test while you register/migrate a
> real domain, not as the deployed configuration — it doesn't provision
> anything, doesn't touch Terraform state, and the tunnel disappears the
> moment you Ctrl-C it (`--rm`).

**Once you've applied Terraform (step 8), reaching the VM looks like
this** — install
[`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
locally, then add to `~/.ssh/config`:

```
Host ssh.example.com
  ProxyCommand cloudflared access ssh --hostname %h
```

The first `ssh azureuser@ssh.example.com` opens a browser for Access SSO
login (must be one of `SSH_ACCESS_ALLOWED_EMAILS`); after that it's cached
for 24h. This is exactly the `ssh_command` Terraform output from step 8.

---

## 3. Generate the deploy SSH key pair

This ONE key pair is used both for your own manual SSH access and for
`deploy-azure-vm.yml`'s automated deploys. Generate it fresh — don't reuse
a personal key. It's presented only ever through the Cloudflare Tunnel/Access
path from step 2 (or Azure's Serial Console as a last resort) — never over the
public internet, since there's no inbound rule for port 22 on the NSG by
default.

```bash
ssh-keygen -t rsa -b 4096 -C "snipeit-lite-vm-deploy" -f ./snipeit_vm_deploy_key -N ""
```

> **Why RSA, not ed25519:** Azure's `admin_ssh_key` (the field
> `azurerm_linux_virtual_machine` writes this into) only accepts RSA
> public keys — the Azure Compute API rejects ed25519 outright with
> `the provided ssh-ed25519 SSH key is not supported. Only RSA SSH keys
> are supported by Azure`, even though ed25519 works everywhere else in
> this stack (Cloudflare Access, GitHub, etc.). If you already generated
> an ed25519 key by mistake, just re-run the command above and re-paste
> the resulting `.pub` into `VM_SSH_PUBLIC_KEY` before your next
> `terraform apply`.


This produces two files in your current directory:

- `snipeit_vm_deploy_key` — the **private** key. This goes into the
  `VM_SSH_PRIVATE_KEY` GitHub secret (step 6) and nowhere else. Never
  commit it.
- `snipeit_vm_deploy_key.pub` — the **public** key. This goes into
  Terraform's `ssh_public_key` variable (`TF_VAR_ssh_public_key` / the
  `VM_SSH_PUBLIC_KEY` secret in step 6).

```bash
cat snipeit_vm_deploy_key.pub     # copy this whole line
cat snipeit_vm_deploy_key         # copy this whole file, including the BEGIN/END lines
```

---

## 4. Generate application secrets

```bash
openssl rand -base64 24    # -> POSTGRES_PASSWORD
openssl rand -hex 32       # -> JWT_SECRET_KEY
```

Leave `ROOT_ADMIN_BOOTSTRAP_PASSWORD` blank (recommended) — the first
`alembic upgrade head` run generates and logs a random one instead (see
step 11's "find the generated root admin password" note). Set it yourself
only if you want a specific known password on first login.

---

## 5. Configure GitHub OIDC federation (for Terraform + no client secrets)

Same mechanism `DEPLOYMENT.md`'s Container Apps path already uses — a
federated Azure AD App Registration lets GitHub Actions authenticate to
Azure with a short-lived OIDC token instead of a long-lived client secret
sitting in a GitHub secret. If you already have one set up for this repo
(from following `DEPLOYMENT.md`), you can reuse it and skip to step 6 —
just make sure its federated credential's subject matches the branches/
environments this VM path's workflows actually run from.

```bash
az ad app create --display-name "snipeit-lite-vm-deploy" \
  --query appId -o tsv
# -> save this as AZURE_CLIENT_ID

az ad sp create --id <appId-from-above>

az account show --query tenantId -o tsv
# -> save this as AZURE_TENANT_ID
```

Grant it Contributor on the subscription (or scope it tighter to a
specific resource group if you'd rather pre-create one):

```bash
az role assignment create \
  --assignee <appId> \
  --role Contributor \
  --scope /subscriptions/<subscription-id>
```

Add federated credentials — one per environment/workflow combination that
needs to authenticate. At minimum, for `infra-deploy-vm.yml` (triggered by
`workflow_dispatch`, GitHub Environment `prod` or `vm-staging` — see the
callout right after this code block for why it's `vm-staging` and not
plain `staging`):

```bash
az ad app federated-credential create --id <appId> --parameters '{
  "name": "snipeit-lite-vm-prod",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<your-org>/<your-repo>:environment:prod",
  "audiences": ["api://AzureADTokenExchange"]
}'

az ad app federated-credential create --id <appId> --parameters '{
  "name": "snipeit-lite-vm-staging",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<your-org>/<your-repo>:environment:vm-staging",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

> **Why `vm-staging` and not `staging`?** If you've also set up
> `DEPLOYMENT.md`'s Container Apps path against this same repo, it already
> owns a GitHub Environment literally called `staging` — with its own
> `AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID` (federated to
> a subject of `environment:staging`) and its own `POSTGRES_PASSWORD`/
> `JWT_SECRET_KEY`/etc. Every workflow on this VM path
> (`infra-deploy-vm.yml`, `deploy-azure-vm.yml`, `sync-secrets-vm.yml`)
> uses `vm-staging` as its second environment's name specifically so it
> never reads from or overwrites that Environment — the two paths' second
> environments are fully independent, same as `prod` (VM) already is from
> `production` (Container Apps). See [Using both deployment
> targets](#using-both-deployment-targets-optional) below for the full
> picture, including what happens if you rename this back to `staging`
> yourself.

`deploy-azure-vm.yml`'s `deploy` job also declares `environment:`, but it
never calls `azure/login` (it only SSHes to the VM) — it doesn't need a
federated credential of its own.

---

## 6. Set GitHub Environment secrets/variables

In GitHub: **Settings → Environments** → create `prod` (and `vm-staging`
if you want a second, cheaper/smaller environment for testing changes
first — see step 5's callout above for why it's not just `staging`). For
`prod`, consider adding a **required reviewer** protection rule — this
is what makes `infra-deploy-vm.yml`'s `destroy` action require a second
person's approval before it can run (see that workflow's comment on the
`terraform destroy` step).

Add these to each Environment (Secrets unless marked **Variable**):

| Name | Value | Used by |
|---|---|---|
| `AZURE_CLIENT_ID` | App Registration's appId (step 5) | `infra-deploy-vm.yml` |
| `AZURE_TENANT_ID` | Tenant ID (step 5) | `infra-deploy-vm.yml` |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID (step 1) | `infra-deploy-vm.yml` |
| `VM_SSH_PUBLIC_KEY` | Contents of `snipeit_vm_deploy_key.pub` (step 3) | `infra-deploy-vm.yml` |
| `VM_SSH_PRIVATE_KEY` | Contents of `snipeit_vm_deploy_key` (step 3) | `deploy-azure-vm.yml`, `sync-secrets-vm.yml` |
| `VM_SSH_USER` | `azureuser` (or whatever you set `admin_username` to) | `deploy-azure-vm.yml`, `sync-secrets-vm.yml` |
| `SSH_ALLOWED_SOURCE_IPS` | **Leave unset** (the default, `[]`, means no inbound port 22 at all — reached via the Cloudflare Tunnel instead). Only set this, e.g. `["203.0.113.4/32"]`, as a temporary break-glass measure — see [Set up Cloudflare Tunnel](#2-set-up-cloudflare-tunnel-no-open-ports-no-bastion) and Troubleshooting | `infra-deploy-vm.yml` |
| `CLOUDFLARE_API_TOKEN` | API token from step 2b | `infra-deploy-vm.yml` |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID from step 2a | `infra-deploy-vm.yml` |
| `CLOUDFLARE_ZONE_ID` | Zone ID from step 2a | `infra-deploy-vm.yml` |
| `CLOUDFLARE_ZONE_NAME` (**Variable**, not secret) | Your zone's apex domain from step 2a, e.g. `example.com` | `infra-deploy-vm.yml` |
| `SSH_ACCESS_ALLOWED_EMAILS` | JSON list of emails from step 2d, e.g. `["you@example.com"]` | `infra-deploy-vm.yml` |
| `CLOUDFLARE_ORIGIN_CERT` | Origin Certificate PEM from step 2c | `infra-deploy-vm.yml` |
| `CLOUDFLARE_ORIGIN_CERT_KEY` | Origin private key PEM from step 2c | `infra-deploy-vm.yml` |
| `CF_ACCESS_CLIENT_ID` | Filled in AFTER step 8 (see step 9) — the CI service token Terraform creates automatically | `deploy-azure-vm.yml`, `sync-secrets-vm.yml` |
| `CF_ACCESS_CLIENT_SECRET` | Filled in AFTER step 8 (see step 9) — pairs with the above | `deploy-azure-vm.yml`, `sync-secrets-vm.yml` |
| `POSTGRES_PASSWORD` | From step 4 | `infra-deploy-vm.yml` |
| `JWT_SECRET_KEY` | From step 4 | `infra-deploy-vm.yml` |
| `ROOT_ADMIN_BOOTSTRAP_PASSWORD` | Leave empty, or a specific password | `infra-deploy-vm.yml` |
| `DOCKERHUB_USERNAME` | Your Docker Hub username | both workflows |
| `DOCKERHUB_TOKEN` | A Docker Hub [Personal Access Token](https://app.docker.com/settings/personal-access-tokens) (not your password) | both workflows |
| `CUSTOM_DOMAIN` (**Variable**, not secret) | REQUIRED — a hostname in the `CLOUDFLARE_ZONE_ID` zone, e.g. `assets.example.com` | `infra-deploy-vm.yml` |
| `VM_HOST` | Filled in AFTER step 8 (see step 9) — this is the VM's **Cloudflare Tunnel SSH hostname** (`ssh.<CUSTOM_DOMAIN>`), not its public IP; nothing listens on port 22 at the public IP by default | `deploy-azure-vm.yml`, `sync-secrets-vm.yml` |

Optional (leave unset if you don't use them yet):
`NOTIFICATIONS_ENABLED` (**Variable**), `SMTP_HOST`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `ADMIN_NOTIFICATION_EMAILS`,
`BACKUP_GDRIVE_ENABLED` (**Variable**), `BACKUP_GDRIVE_OAUTH_CLIENT_ID`,
`BACKUP_GDRIVE_OAUTH_CLIENT_SECRET`, `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN`,
`BACKUP_GDRIVE_FOLDER_ID` — see [Google Drive backup uploads](#google-drive-backup-uploads)
below for where these five come from.

Also optional — distributed tracing (OpenTelemetry, off by default; see
`README.md`'s **Distributed Tracing** section for the full walkthrough,
which covers this VM path via `docker-compose.vm.yml`'s opt-in `jaeger`
service the same way it covers local Docker Compose):
`OTEL_ENABLED` (**Variable**), `OTEL_SERVICE_NAME` (**Variable**),
`OTEL_EXPORTER_OTLP_ENDPOINT` (**Variable** — defaults to this VM's own
`jaeger` service, no need to set it yourself for that path),
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_TRACES_SAMPLE_RATIO` (**Variable**),
`OTEL_CONSOLE_EXPORTER` (**Variable**), `APPLICATIONINSIGHTS_CONNECTION_STRING`
(only if pointing this VM at an Application Insights resource you
provisioned some other way — this VM path doesn't provision one itself,
unlike the Container Apps path's `infra/main.bicep`).

---

## 7. Review the Terraform plan locally (optional but recommended first time)

```bash
cd infra-vm
az login   # if you haven't already in this shell
terraform init

export TF_VAR_subscription_id="<from step 1>"
export TF_VAR_ssh_public_key="$(cat ../snipeit_vm_deploy_key.pub)"
export TF_VAR_postgres_password="<from step 4>"
export TF_VAR_jwt_secret_key="<from step 4>"
export TF_VAR_cloudflare_api_token="<from step 2b>"
export TF_VAR_cloudflare_account_id="<from step 2a>"
export TF_VAR_cloudflare_zone_id="<from step 2a>"
export TF_VAR_cloudflare_zone_name="example.com"
export TF_VAR_ssh_access_allowed_emails='["you@example.com"]'
export TF_VAR_cloudflare_origin_cert="$(cat /path/to/origin-cert.pem)"
export TF_VAR_cloudflare_origin_cert_key="$(cat /path/to/origin-key.pem)"
export TF_VAR_custom_domain="assets.example.com"
export TF_VAR_dockerhub_backend_image="yourusername/snipeit-lite-backend"
export TF_VAR_dockerhub_frontend_image="yourusername/snipeit-lite-frontend"

terraform plan
```

Read the plan. It should show ~10 Azure resources to add (resource group,
vnet, subnet, NSG, public IP, NIC, managed disk, VM, disk attachment, plus
the recovery vault/backup policy/protected-VM trio if
`enable_data_disk_snapshots` is left at its default `true`) plus 8
Cloudflare resources (the Tunnel, its ingress config, the two DNS records,
the Access application, its two Access policies, and the CI service
token), and nothing to change or destroy. Don't `terraform apply` locally
for a real deployment — let `infra-deploy-vm.yml` do it (step 8), so the
state and the run history both live in one place your whole team can see.

---

## 8. Provision the VM (`infra-deploy-vm.yml`)

In GitHub: **Actions → Deploy VM Infrastructure (Terraform) → Run workflow**.

- `environment`: `prod` (or `vm-staging`)
- `action`: `plan` first — read the output, confirm it matches step 7's
  local plan. Re-run the workflow with `action: apply` once you're happy.

`apply` takes 3-5 minutes. When it finishes, open the run's **Summary**
tab — it prints every Terraform output, including:

- `public_ip_address` — the VM's static IP (break-glass only — no SSH or app traffic here by default)
- `azure_fqdn` — `<label>.<region>.cloudapp.azure.com` (break-glass reference only)
- `app_domain` — the domain Caddy actually serves on (your `CUSTOM_DOMAIN`)
- `app_url` — `https://<app_domain>` — not reachable yet, the app isn't deployed until step 10
- `ssh_hostname` — `ssh.<app_domain>`, the Cloudflare Access-gated hostname — this is what `VM_HOST` becomes in step 9
- `ssh_command` — exact command to SSH in through the Tunnel/Access (see step 2's last box)
- `ssh_command_break_glass` — direct SSH over the public IP; only works if you've temporarily set `ssh_allowed_source_ips` (see step 2 / Troubleshooting)
- `cloudflare_ci_service_token_id` / `cloudflare_ci_service_token_secret` — needed for step 9's `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET`

The VM is already running the six-container stack + Caddy at this point
(cloud-init brings it up on first boot using `initial_image_tag`, default
`latest`) — but `latest` may not exist yet on your Docker Hub repos if
this is a brand new project. That's fine; step 10 fixes that with a real
build.

---

## 9. Point `deploy-azure-vm.yml` at the new VM

From step 8's Summary tab, copy the `ssh_hostname`,
`cloudflare_ci_service_token_id`, and `cloudflare_ci_service_token_secret`
outputs (the last one only shows once `terraform apply` created it — if
you've lost it, `terraform taint cloudflare_zero_trust_access_service_token.ci`
and re-apply to get a fresh one). In GitHub: **Settings → Environments →
prod → Secrets** → add:

| Name | Value |
|---|---|
| `VM_HOST` | the `ssh_hostname` output value (e.g. `ssh.assets.example.com`) |
| `CF_ACCESS_CLIENT_ID` | the `cloudflare_ci_service_token_id` output value |
| `CF_ACCESS_CLIENT_SECRET` | the `cloudflare_ci_service_token_secret` output value |

Don't use `public_ip_address` here — nothing listens on port 22 there by
default (see step 2).

---

## 10. Deploy the application (`deploy-azure-vm.yml`)

In GitHub: **Actions → Deploy to Azure VM → Run workflow**, `environment:
prod`, leave `image_tag` blank (build fresh). Or just `git push` to `main`
— the workflow also triggers on that automatically.

This runs: `ci.yml` (full test suite) → build + push both images to Docker
Hub → SSH in, sync `docker-compose.vm.yml`/`Caddyfile`, update `IMAGE_TAG`
→ `docker compose up -d` → `alembic upgrade head` → prune old image layers
→ smoke test `https://<domain>/` and `https://<domain>/api/auth/me`.

First run takes the longest (full CI + two Docker builds + `cloudflared`
establishing the Tunnel for the first time) — expect 5-10 minutes.

---

## 11. Verify

Open `app_url` from step 8's output in a browser. You should see a valid
padlock (Cloudflare's own edge certificate, issued automatically — free,
no ACME step of any kind on this VM) and the login page.

**Find the generated root admin password** (if you left
`ROOT_ADMIN_BOOTSTRAP_PASSWORD` blank in step 4). This requires
[`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
installed locally and the `~/.ssh/config` entry from step 2's last box —
then:

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> \
  "docker compose -f /opt/snipeit/docker-compose.vm.yml logs backend | grep -A2 'root admin'"
```

(`<VM_HOST>` here is the `ssh_hostname` output from step 8, e.g.
`ssh.assets.example.com` — same value everywhere else in this doc that
shows `<VM_HOST>`.)

Log in as `superadmin` with that password, then change it immediately
(Settings → your account).

---

## Tagging & Versioning

**This is the important one to actually read** — it's how you always know
exactly what's running in production, and how you get back to a known-good
version fast if a deploy goes bad.

### How a real, named release is created

```bash
git tag v1.4.2
git push origin v1.4.2
```

Pushing a tag matching `v*.*.*` is what `deploy-azure-vm.yml`'s `on.push.
tags` trigger listens for (mirrors `release.yml`'s identical trigger for
the Container Apps path — see [Using both deployment
targets](#using-both-deployment-targets-optional) below if you have both
set up). This single push does all of the following, automatically:

1. Runs the full `ci.yml` gate (lint, tests, Trivy scan) — same as any push.
2. Builds and pushes **both** images to Docker Hub, tagged **three ways**:
   `:v1.4.2` (the real, pullable release artifact), `:<commit-sha>`
   (exact-source traceability), and `:latest` (convenience). Each image
   also carries OCI labels (`org.opencontainers.image.version`,
   `.revision`, `.created`) — visible via `docker inspect`.
3. SSHes to the VM (through the Cloudflare Tunnel/Access — see step 2)
   and deploys `v1.4.2` — pull, `docker compose up -d`, `alembic upgrade
   head`, smoke test.
4. **Only if the smoke test passes**, writes `/opt/snipeit/CURRENT_RELEASE`
   on the VM and a summary table in the GitHub Actions run — see
   [Checking the current running version](#checking-the-current-running-version)
   below. A failed smoke test means this step never runs, so the marker
   keeps pointing at whatever the last *confirmed-healthy* version was —
   deliberately, not a bug.

Use [Semantic Versioning](https://semver.org) for the tag itself:
`MAJOR.MINOR.PATCH` — bump `MAJOR` for breaking changes (e.g. a migration
that isn't safely reversible, see below), `MINOR` for new features,
`PATCH` for fixes. `CHANGELOG.md` (maintained by `release.yml`, not this
workflow — see the caveat below) is the running human-readable history of
what changed in each one.

A plain `git push` to `main` (no tag) or a `workflow_dispatch` run without
`image_tag` still deploys fine — it's just recorded as `VERSION=unversioned`
rather than a named release (real SHA-tagged image, fully functional, just
not something `git tag`/Docker Hub/`CHANGELOG.md` will ever show you as a
release you can refer back to by name). Good for iterating on `vm-staging`;
tag a real version once you're ready to call something stable.

### Checking the current running version

Three ways, in order of convenience:

**1. SSH to the VM and read the marker file** (always accurate, works
even if GitHub Actions/Docker Hub are both unreachable):

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> "cat /opt/snipeit/CURRENT_RELEASE"
```

```
VERSION=v1.4.2
IMAGE_TAG=v1.4.2
GIT_SHA=8f3c1e9a2b4d5f6e7a8b9c0d1e2f3a4b5c6d7e8f
BACKEND_IMAGE=yourusername/snipeit-lite-backend
FRONTEND_IMAGE=yourusername/snipeit-lite-frontend
DEPLOYED_AT=2026-07-25T04:12:33Z
DEPLOYED_BY=your-github-username
DEPLOYED_VIA=Deploy to Azure VM (run 123456789)
RUN_URL=https://github.com/<org>/<repo>/actions/runs/123456789
```

**2. GitHub Actions, no SSH needed** — **Actions → Deploy to Azure VM** →
latest successful run → **Summary** tab shows the same version/image
tag/commit/deployer table.

**3. `docker inspect` on the VM** (corroborates #1 directly against the
actual running container, useful if you suspect `CURRENT_RELEASE` itself
might be stale for some reason):

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> \
  "docker inspect snipeit_lite_backend --format '{{index .Config.Labels \"org.opencontainers.image.version\"}}'"
```

### Rolling back to a previous stable version

**1. Find available versions** — three equivalent sources:

```bash
# Locally, newest first:
git fetch --tags && git tag --sort=-creatordate

# Or browse releases/changelog on GitHub, or read CHANGELOG.md directly
# (each "## [vX.Y.Z] - <date>" section lists what changed in that release)
```

**2. Check what changed between your current version and the one you're
rolling back to** — read the relevant `CHANGELOG.md` sections, especially
for any migration/breaking-change notes. This matters most for step 4
below.

**3. Roll out the older version**: **Actions → Deploy to Azure VM → Run
workflow**:

- `environment`: `prod`
- `image_tag`: the older version, e.g. `v1.4.1` (must already exist on
  Docker Hub — any version that was ever successfully released has it,
  since step 2 of "How a real, named release is created" always pushes it)
- `skip_migrate`: `true` **only** if you're certain no database migration
  shipped between `v1.4.1` and `v1.4.2` (check `backend/alembic/versions/`
  in the diff between those two tags). Leave `false` (the default) if
  unsure — running `alembic upgrade head` against a backend that's
  already at the head revision is always a safe no-op, so there's no
  downside to leaving it on.

This skips `ci` and `build-push` entirely (the image already exists) and
goes straight to the SSH deploy — fast, typically under a minute.

**4. If the NEWER version's migration needs to be undone** (rare — only
when the migration itself is genuinely incompatible with the older code,
not just "newer than it needs to be"): SSH in and downgrade explicitly
*before* step 3's redeploy, since `alembic upgrade head` in step 3 would
otherwise immediately re-apply it:

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST>
cd /opt/snipeit
docker compose -f docker-compose.vm.yml run --rm backend alembic downgrade -1
```

Then run step 3 with `skip_migrate: true` (the schema is now already at
the older version's expected revision).

**5. Verify** — same three methods as
[Checking the current running version](#checking-the-current-running-version)
above; `CURRENT_RELEASE` should now show the older `VERSION`.

### Using both deployment targets (optional)

**GitHub Environments never collide between the two paths.** Each path
uses two independent GitHub Environments with independent secrets:

| | Container Apps path (`DEPLOYMENT.md`) | VM path (this doc) |
|---|---|---|
| "production" environment | `production` | `prod` |
| "staging" environment | `staging` | `vm-staging` |

Every name in that table is deliberately distinct — `AZURE_CLIENT_ID`,
`POSTGRES_PASSWORD`, `JWT_SECRET_KEY`, and everything else scoped to one
Environment can never be read by, or silently overwritten by, the other
path's setup steps, even if you follow both docs against the same repo
and paste secrets into whichever Environment each doc tells you to at the
time. (Earlier drafts of this doc used plain `staging` here too, which
collided with the Container Apps path's `staging` Environment — if you
set that up before this note existed, see the callout in [step
5](#5-configure-github-oidc-federation-for-terraform--no-client-secrets)
for how to migrate to `vm-staging` instead.)

If you also have the Container Apps path (`DEPLOYMENT.md`) set up
against the *same* repo, pushing a version tag triggers **both**
`release.yml` (changelog PR + GitHub Release + Container Apps deploy) and
`deploy-azure-vm.yml` (VM deploy) — both independently build+push the
same images under the same tag, which is redundant but harmless (same
content, just built twice). This is fine if you genuinely run both
targets in parallel (e.g. VM for a cost-capped primary environment,
Container Apps for a burst-capacity secondary).

If you're **only** using the VM path, `release.yml`'s own `deploy` job
(which calls `deploy-azure-production.yml`) will still attempt to run on
every version tag and fail, since it targets Container Apps
infrastructure that was never provisioned — harmless (no destructive
action, just a red X in Actions), but avoid the noise by removing
`release.yml`'s trigger:

```yaml
# .github/workflows/release.yml
on:
  push:
    tags:
      - 'v*.*.*'   # <- comment out or remove this whole `on:` block's
                   #    push trigger if you don't use the Container Apps
                   #    path at all; deploy-azure-vm.yml's own identical
                   #    trigger is unaffected either way.
```

`CHANGELOG.md` is currently only maintained by `release.yml`'s
`changelog` job — if you disable that trigger, `CHANGELOG.md` stops
updating automatically too. Keep it enabled (its `changelog` job doesn't
depend on Container Apps existing, only `deploy` does) if you still want
automatic changelog entries + GitHub Releases while skipping just the
Container Apps deploy attempt — remove only the `deploy:` job from
`release.yml`, not the whole trigger, to get that combination.

### Redeploying without a version bump

**Normal redeploy** (new code, not yet a named release): `git push` to
`main`, or run `deploy-azure-vm.yml` manually with `environment: vm-staging`
(or `prod`, with no `image_tag`) — see [How a real, named release is
created](#how-a-real-named-release-is-created) above for how this differs
from a tagged release.

---

## Free HTTPS (and the domain it now requires)

Unlike the old direct-IP + Let's Encrypt path, this setup has no bare-IP
fallback left to offer a free `*.sslip.io`-style domain for — Cloudflare
can only route a hostname to your Tunnel if it already controls DNS for
it, so `custom_domain` is required (see step 2). The HTTPS itself is
still genuinely free and automatic: Cloudflare issues and rotates the
edge certificate visitors see with no ACME step on this VM at all, and
the Origin CA certificate for the inner Caddy hop is free and 15-year-
valid (step 2c).

If you don't already own a domain, register something cheap (many TLDs
run $1–12/year at registrars like Namecheap or Porkbun) and move its
nameservers to Cloudflare (free) — see step 2a. Want to try the app
behind Cloudflare before committing to that? See the "Testing without a
domain yet" box in step 2 for the free, account-less Quick Tunnel option.

**Changing `custom_domain` later** (e.g. moving from a throwaway
subdomain to your real one): update `CUSTOM_DOMAIN` (GitHub Environment
Variable) and re-run `infra-deploy-vm.yml` with `action: apply` —
Terraform updates the Tunnel's ingress rules and DNS records for the new
hostname (it does NOT replace the VM — `custom_data` changes are ignored
by the `lifecycle` block in `main.tf`), but you'll also need to SSH in
once and re-run `docker compose up -d` after updating `/opt/snipeit/.env`'s
`DOMAIN` line by hand (cloud-init only runs on first boot). If the
domain's Origin CA cert needs updating too (different hostname coverage),
regenerate it per step 2c and update `CLOUDFLARE_ORIGIN_CERT`/`_KEY`
first, then run `sync-secrets-vm.yml` before the VM-side restart.

---

## Updating secrets on an already-running VM

Changing a secret's value in a GitHub Environment does **not**, by
itself, reach a VM that's already running — `infra-vm/cloud-init.yaml`
only executes on a VM's first boot (see `main.tf`'s `lifecycle.
ignore_changes = [custom_data]`), and re-running `infra-deploy-vm.yml`'s
`apply` only updates Terraform's own record of the value, not the VM.
GitHub also has no "a secret changed" trigger to hook into automatically
— there's no way to make this truly automatic. `.github/workflows/
sync-secrets-vm.yml` is the next best thing: a one-click, repeatable
button you re-run any time you've changed a secret, as often as you like.

**What it does:** reads every relevant GitHub Environment secret/variable
(the same ones `infra-deploy-vm.yml` uses — one source of truth),
SSHes into the VM, reads back the two values it deliberately does **not**
touch (`DOMAIN` and `IMAGE_TAG` — see the workflow's top comment for why:
those are owned by Terraform/cloud-init and `deploy-azure-vm.yml`
respectively, never by this workflow), writes a fresh `/opt/snipeit/.env`
with everything else refreshed, backs up the previous `.env` first
(`/opt/snipeit/.env.bak.<timestamp>`, keeping the 5 most recent), then
restarts only the containers whose resolved configuration actually
changed.

**To use it:** update whichever secret(s)/variable(s) changed in
**Settings → Environments → prod** (or `vm-staging`), then **Actions → Sync
secrets to Azure VM → Run workflow**:

- `environment`: `prod` or `vm-staging`
- `restart_services`:
  - `auto` (default, recommended) — `docker compose up -d` only recreates
    a container whose config actually differs from what's currently
    running. Changing `SMTP_HOST` alone, for instance, bounces `backend`/
    `worker`/`beat` (they read it) but never touches `db`, `frontend`, or
    `caddy`.
  - `all` — force-recreates every container regardless of whether its
    config changed. Brief full downtime (a few seconds); use this if
    you're not sure something applied correctly and want a clean slate.
  - `none` — writes the new `.env` to the VM but restarts nothing, so you
    can review it (`ssh ... cat /opt/snipeit/.env`) before applying it
    yourself with `docker compose -f docker-compose.vm.yml up -d`.

The run's log includes a redacted diff (key names only, never values) of
exactly what changed, and finishes with a smoke test against `/api/auth/me`
— if that fails, the previous `.env` is still sitting at
`/opt/snipeit/.env.bak.<timestamp>` on the VM for a quick manual rollback:

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST>
cd /opt/snipeit
ls -1t .env.bak.*                        # find the one you want
cp .env.bak.<timestamp> .env
docker compose -f docker-compose.vm.yml up -d
```

**A secret this workflow doesn't cover, or a one-off change you don't
want in GitHub Environments at all?** Editing `/opt/snipeit/.env` by hand
over SSH still works exactly as before — just remember `docker compose -f
docker-compose.vm.yml up -d` afterward to actually apply it; a plain
`.env` edit alone doesn't restart anything.

---

## Google Drive backup uploads

By default, the `pg_dump` backups `ENABLE_AUTO_BACKUP` produces (see
[Backups + restore](#backups--restore)) land only on the VM's own data
disk. `BACKUP_GDRIVE_*` adds a second, independent, off-Azure copy — the
same feature `POST_DEPLOYMENT.md` documents for the Container Apps path,
wired through Terraform instead of Bicep here. **Yes — set these in the
`prod` GitHub Environment** (and `vm-staging` too, if you want backups from
that environment uploaded somewhere as well; use a *different* Drive
folder for it, so staging and prod backups don't land in the same place).

**Step 1 — generate the three OAuth values, once, on your own machine**
(not in CI, not inside a container):

```bash
pip install google-auth-oauthlib
```

In the [Google Cloud Console](https://console.cloud.google.com/):
- Create (or reuse) a project — free, no billing account required.
- **APIs & Services → Library** → enable the **Google Drive API**.
- **APIs & Services → OAuth consent screen** → **External** → fill in an
  app name/support email → Save. Leave it in "Testing" mode (fine
  indefinitely for this) — under "Test users", add your own Google
  account's email.
- **APIs & Services → Credentials → Create Credentials → OAuth client ID**
  → Application type **Desktop app** → Create → **Download JSON**.

Then, from the repo root:

```bash
python backend/scripts/gdrive_oauth_setup.py /path/to/the_downloaded.json
```

A browser window opens — log in with that same Google account, click
Allow. The script prints:

```
BACKUP_GDRIVE_OAUTH_CLIENT_ID=...
BACKUP_GDRIVE_OAUTH_CLIENT_SECRET=...
BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN=...
```

**Step 2 —** in Google Drive, create (or pick) a normal folder for backups
to land in — no sharing step needed, uploads happen as yourself. Copy its
ID from the folder's URL: `https://drive.google.com/drive/folders/<THIS_PART>`.

**Step 3 — add to the `prod` GitHub Environment** (Settings → Environments
→ prod):

| Name | Value | Type |
|---|---|---|
| `BACKUP_GDRIVE_ENABLED` | `true` | **Variable** |
| `BACKUP_GDRIVE_OAUTH_CLIENT_ID` | printed by the script above | Secret |
| `BACKUP_GDRIVE_OAUTH_CLIENT_SECRET` | printed by the script above | Secret |
| `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN` | printed by the script above | Secret |
| `BACKUP_GDRIVE_FOLDER_ID` | folder ID from step 2 | Secret |

**Step 4 — apply it.** This differs depending on where you are:

- **VM not provisioned yet** — just run `infra-deploy-vm.yml` (`action:
  apply`) per step 8 as normal. cloud-init picks these up automatically on
  first boot along with everything else.
- **VM already provisioned** — run **Actions → Sync secrets to Azure VM →
  Run workflow** (`environment: prod`, `restart_services: auto`) — see
  [Updating secrets on an already-running VM](#updating-secrets-on-an-already-running-vm)
  above for exactly what that does. It picks up the four secrets you just
  set, above, automatically; no manual SSH needed.

**Step 5 — verify:** log in, Admin → Audit & Backups → **Backup Now**. The
resulting entry reports its Google Drive upload state directly. Confirm
the file actually landed in the Drive folder from step 2.

The refresh token doesn't expire from time passing alone — only from a
manual revoke at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions)
or 6 months of the app going completely unused. Re-run
`gdrive_oauth_setup.py` and update the three secrets if that ever happens.

---

## Per-service memory limits

`docker-compose.vm.yml` gives every one of the six app containers **and**
Caddy its own hard memory ceiling (`mem_limit`) and a soft reservation
(`mem_reservation`), enforced by Docker as real cgroup limits — a runaway
container (a huge CSV export, a slow query pinning connections, a memory
leak) can only ever consume up to its own ceiling, never the whole VM's
RAM, and never starve a sibling container of the memory it needs to keep
running.

Defaults, sized for the Terraform default `Standard_B2s` (2 vCPU / 4 GiB):

| Service | Limit | Reservation | Role |
|---|---|---|---|
| `db` | 768m | 256m | Postgres |
| `redis` | 256m | 128m | Celery broker + export result cache |
| `backend` | 768m | 256m | FastAPI/uvicorn |
| `worker` | 640m | 256m | Celery worker (CSV/PDF export jobs) |
| `beat` | 128m | 64m | Celery scheduler (no request handling) |
| `frontend` | 128m | 64m | nginx, static assets + internal proxy |
| `caddy` | 160m | 64m | reverse proxy, TLS re-presentation (Origin CA cert) |
| `cloudflared` | 128m | 32m | outbound-only Cloudflare Tunnel connector |
| **Total** | **~2.98 GiB** | **~1.13 GiB** | leaves ~1 GiB for the OS/Docker daemon |

Every value reads from `/opt/snipeit/.env` first (`DB_MEM_LIMIT`, etc —
see `docker-compose.vm.yml`'s services), so you can retune any single
service without editing the compose file: SSH in, edit `.env`, then
`docker compose -f docker-compose.vm.yml up -d` to apply.

**Resized to `Standard_B2ms` (8 GiB) or larger?** Double every `*_MEM_LIMIT`/
`*_MEM_RESERVATION` value in `.env` (or set `vm_size = "Standard_B2ms"` in
your Terraform vars before first `apply`, and add the doubled values to
`infra-vm/cloud-init.yaml`'s `write_files` section for future VMs built
from the same config).

---

## Backups + restore

Two independent layers:

1. **App-level `pg_dump` backups** (`ENABLE_AUTO_BACKUP=true`, the
   default) — `backend`'s own scheduled job writes to
   `/mnt/docker-data/volumes/backup_data` on the VM (retained per
   `BACKUP_RETENTION_COUNT`), optionally also uploaded to Google Drive if
   you configure `BACKUP_GDRIVE_*` (see `POST_DEPLOYMENT.md`).
2. **Whole-VM daily snapshot** (`enable_data_disk_snapshots = true`, the
   default) via Azure Backup — covers everything on the VM, including
   Redis and any in-flight export files, not just Postgres. Restore from
   the Azure Portal: **Recovery Services vault → Backup items → restore**,
   or:

   ```bash
   az backup restore restore-disks \
     --resource-group <rg-name> \
     --vault-name <rsv-name> \
     --container-name <container-name> \
     --item-name <vm-name> \
     --rp-name <recovery-point-name> \
     --storage-account <staging-storage-account>
   ```

**Manual on-demand backup** (before a risky change):

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> \
  "docker compose -f /opt/snipeit/docker-compose.vm.yml exec -T db pg_dump -U <postgres_user> <postgres_db> | gzip" \
  > backup-$(date +%Y%m%d-%H%M%S).sql.gz
```

---

## Growing the data disk

Azure managed disks can only grow, never shrink. To grow:

1. In `terraform.tfvars` (or the `TF_VAR_data_disk_size_gb` secret/var),
   raise `data_disk_size_gb`, then run `infra-deploy-vm.yml` with `action:
   apply` — this resizes the managed disk, no data loss, no downtime to
   the disk itself.
2. SSH in and grow the filesystem to match (the disk is bigger now, but
   ext4 doesn't auto-expand):

   ```bash
   ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST>
   sudo growpart /dev/disk/azure/scsi1/lun0 1 2>/dev/null || true  # no-op if unpartitioned, which it is here
   sudo resize2fs /dev/disk/azure/scsi1/lun0
   df -h /mnt/docker-data   # confirm the new size shows up
   ```

---

## Cost

Rough Azure retail pricing, `eastus`, US$/month (check the
[Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)
for your region/currency):

| Item | Default | Approx. cost |
|---|---|---|
| VM compute | `Standard_B2s` (2 vCPU, 4 GiB, burstable) | ~$30/mo |
| OS disk | 30 GiB StandardSSD_LRS | ~$2/mo |
| Data disk | 32 GiB StandardSSD_LRS | ~$2.50/mo |
| Static public IP | Standard SKU | ~$3.65/mo |
| Daily VM backup | usage-based (retained snapshot storage) | ~$1-3/mo at this disk size |
| Domain registration | cheap TLD at any registrar (one-time/year, not Azure) | ~$1-12/yr |
| TLS cert + Cloudflare Tunnel + Access | Cloudflare free plan | **$0** |
| **Total** | | **~$40-45/mo** (+ ~$1-12/yr for the domain) |

Sizing up when needed: `Standard_B2ms` (8 GiB) roughly doubles the compute
line to ~$60/mo — bump `vm_size` and double the memory limits (see
[Per-service memory limits](#per-service-memory-limits)). Sizing down:
there's no smaller burstable size with 2 vCPUs; `Standard_B1s` (1 vCPU, 1
GiB) is not enough headroom for six containers plus Caddy and isn't
recommended.

---

## Security

- **SSH: no open inbound port, no Bastion**. `ssh_allowed_source_ips` defaults to `[]`, so `main.tf`'s `AllowSSH` NSG rule doesn't exist at all by default — there is nothing to port-scan or brute-force on port 22 from the public internet. Access is instead through the Cloudflare Tunnel (step 2): the VM only ever makes an outbound connection to Cloudflare's edge, and Cloudflare Access (step 2d) gates who/what can reach `ssh.<domain>` from there — email SSO for humans, a service token for CI, both independent of OpenSSH's own key auth. Auth itself is still key-only underneath that (no password auth at all — `disable_password_authentication = true` in `main.tf`). Only set `ssh_allowed_source_ips` as a deliberate, temporary break-glass measure (see Troubleshooting below).
- **Two independent gates on SSH, not one**: even a leaked deploy private key isn't enough on its own — the connection also has to pass Cloudflare Access first (an allow-listed email via browser SSO, or the CI service token), and vice versa: knowing/guessing the Access service token without the SSH private key still gets you nowhere. Defense-in-depth was the whole point of adding Access here rather than just relying on the Tunnel being obscure.
- **fail2ban**: bans an IP after 5 failed SSH attempts within 10 minutes (`/etc/fail2ban/jail.local`, written by cloud-init) — defense-in-depth for the break-glass path above, since Tunnel-originated connections aren't exposed to internet-wide brute-force attempts to begin with.
- **UFW**: OS-level firewall mirroring the NSG, as a second layer.
- **No exposed ports at all, beyond the break-glass path**: the Cloudflare Tunnel needs no inbound NSG rule of any kind (see step 2) — `caddy` (443), `backend` (8000), and `frontend` (80) are all reachable only over the internal Docker network, never published to the host. This is a step further than `docker-compose.yml`'s own "shrink the public attack surface to one hardened entry point" design: here there's no public entry point on the VM at all, Cloudflare's edge is the entry point.
- **Automatic OS patching**: `patch_mode = "AutomaticByPlatform"` — Azure applies security patches on its own schedule.
- **Secrets**: never committed. `infra-vm/.gitignore` excludes `terraform.tfvars`/`*.tfvars`/SSH keys; every real secret is a GitHub encrypted secret, injected as `TF_VAR_*` at workflow run time. CI's own Cloudflare Access session is likewise never persisted — `deploy-azure-vm.yml`/`sync-secrets-vm.yml` authenticate the service token fresh on every run via `cloudflared access ssh`'s `--service-token-id`/`--service-token-secret` flags, nothing lingers between deploys.
- **Origin CA cert, not a shared wildcard secret**: `cloudflare_origin_cert_key` only ever protects the hop between `cloudflared` and Caddy, both on the same VM, over a network nothing external can reach — its blast radius if leaked is strictly smaller than a cert that also had to be trusted by real browsers.

---

## Troubleshooting

**`terraform apply` fails with "public IP DNS label already exists"** —
`local.dns_label` (derived from `app_base_name`+`environment_name`) must
be globally unique across all of Azure. Change `app_base_name` slightly
(e.g. add your initials) and re-apply.

**Site unreachable after `apply` but before the first `deploy-azure-vm.yml`
run** — expected; `initial_image_tag` defaults to `latest`, which may not
exist on your Docker Hub repos yet for a brand-new project. Run step 10.

**Caddy stuck / no valid certificate** — SSH in and check its logs:
```bash
docker compose -f /opt/snipeit/docker-compose.vm.yml logs caddy
```
Common cause: port 80 isn't actually reachable from the internet yet (NSG
rule not applied — check `main.tf`'s `AllowHTTP` rule specifically uses
`source_address_prefix = "*"`, which it always does regardless of
`ssh_allowed_source_ips`, since the two rules are entirely independent).

**`docker compose run --rm backend alembic upgrade head` hangs** —
`db` probably isn't healthy yet. Check: `docker compose -f
docker-compose.vm.yml ps` — `db` should show `(healthy)`. If not,
`docker compose logs db`.

**Out of memory / a container keeps restarting** — check which one:
`docker compose -f docker-compose.vm.yml ps` (a repeatedly restarting
container is the tell). Then `docker stats --no-stream` to see actual
usage vs. the limits in the [Per-service memory limits](#per-service-memory-limits)
table — if a service is consistently hitting its ceiling, raise that one
service's `*_MEM_LIMIT` in `/opt/snipeit/.env` rather than every service's,
then `docker compose up -d`.

**Tunnel shows as inactive/down in the Cloudflare Zero Trust dashboard**
(Networks → Tunnels) — SSH and the app both depend on this, so check it
early. Options, in order of likelihood:
1. `cloudflared` itself isn't running or is crash-looping on the VM —
   check via the Azure Portal's **Serial console** (VM → Support +
   troubleshooting → Serial console, a root shell with no network path at
   all, immune to anything Tunnel- or NSG-related):
   ```bash
   docker compose -f /opt/snipeit/docker-compose.vm.yml logs cloudflared
   ```
   A `failed to parse tunnel token` or similar auth error usually means
   `CLOUDFLARE_TUNNEL_TOKEN` in `/opt/snipeit/.env` doesn't match the
   Tunnel `cloudflare_zero_trust_tunnel_cloudflared.this` in your current
   Terraform state — this can happen if the VM was provisioned before a
   later `terraform apply` recreated the Tunnel resource. Re-run
   `sync-secrets-vm.yml` to push the current token, or by hand: `docker
   compose -f /opt/snipeit/docker-compose.vm.yml up -d cloudflared` after
   fixing the `.env` line, sourced from `terraform output` in `infra-vm`.
2. Outbound internet from the VM is somehow blocked — `cloudflared` needs
   to reach Cloudflare's edge; this project's NSG never restricts
   outbound traffic, so this would point at something unusual in your
   Azure environment (a custom route table, a firewall appliance) rather
   than anything in this repo. Cloudflare's edge is about as broadly
   reachable as infrastructure gets, so if THIS was also what broke your
   original Tailscale attempt (unreachable coordination/DERP servers),
   that specific class of problem shouldn't recur here — but it's worth
   ruling out network-level blocking generally, e.g. `curl -v
   https://www.cloudflare.com` from the Serial Console.

**Need to reach the VM but the Tunnel itself is unreachable (Cloudflare
outage, `cloudflared` crash-looping with no obvious fix, etc.)** — two
fallbacks, in order of preference:
1. **Azure Serial Console** (VM → Support + troubleshooting → Serial
   console) — a root shell with no network path at all, immune to
   anything Tunnel- or NSG-related. Use it to fix `cloudflared` directly
   (see above), or to inspect/repair the VM in any other way.
2. **Temporary break-glass SSH**: set `SSH_ALLOWED_SOURCE_IPS` to your
   current IP (`curl -4 ifconfig.me`) as a GitHub Environment secret,
   e.g. `["203.0.113.4/32"]`, then run `infra-deploy-vm.yml` with
   `action: apply` — this adds `main.tf`'s `AllowSSH` NSG rule back
   temporarily. **Remove the secret and re-apply once you're done** to
   close it again — it's meant as a short-lived escape hatch, not a
   standing access method.

**Locked out of `ssh.<domain>` by Cloudflare Access itself** (not a
Tunnel problem — the Tunnel's up, but Access rejects you) — either your
email isn't in `SSH_ACCESS_ALLOWED_EMAILS` (add it, re-apply Terraform),
or the CI service token was rotated/deleted and `CF_ACCESS_CLIENT_ID`/
`_SECRET` in GitHub are stale (re-copy the current
`cloudflare_ci_service_token_id`/`_secret` Terraform outputs). Either way,
the two break-glass options above still work regardless of Access's own
state, since they don't go through the Tunnel at all.
