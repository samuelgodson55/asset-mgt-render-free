# Deploying Snipe-IT Lite To Azure — Single VM (Terraform + Docker Compose)

This is the exact-steps companion for the **VM deployment target**: one
Azure Virtual Machine running the app's full six-container stack (`db`,
`redis`, `backend`, `worker`, `beat`, `frontend`) plus a seventh container,
`caddy`, for free automatic HTTPS. Infrastructure is provisioned with
Terraform (`infra-vm/`); every code deploy after that goes out over SSH via
`.github/workflows/deploy-azure-vm.yml`.

**This is a different, parallel path from `DEPLOYMENT.md`'s Azure Container
Apps guide** (`infra/main.bicep` + `deploy-azure-aca.yml`). Pick one:

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
- [Zero-Downtime Blue-Green Deployments](#zero-downtime-blue-green-deployments)
- [Tagging & Versioning](#tagging--versioning)
- [Free HTTPS (and the domain it now requires)](#free-https-and-the-domain-it-now-requires)
- [Updating secrets on an already-running VM](#updating-secrets-on-an-already-running-vm)
- [Google Drive backup uploads](#google-drive-backup-uploads)
- [Per-service memory limits](#per-service-memory-limits)
- [Backups + restore](#backups--restore)
- [Growing the data disk](#growing-the-data-disk)
- [Rebuilding just the VM (recovering from a broken first boot)](#rebuilding-just-the-vm-recovering-from-a-broken-first-boot)
- [Cost](#cost)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

---

## 0. Prerequisites

Install locally (the VM infrastructure workflow itself runs in GitHub Actions):

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`)
- [GitHub CLI](https://cli.github.com/) (`gh`) — used only for the one-time Azure/GitHub bootstrap
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.7 — only needed if you want the optional local plan
- An SSH client (`ssh-keygen`, already on macOS/Linux; use WSL or Git Bash on Windows)
- A Docker Hub account (free tier is fine — same requirement `DEPLOYMENT.md` already has)

You need an Azure subscription and enough tenant/subscription permissions for
the one-time bootstrap to create the GitHub Actions Entra application/service
principal and grant its subscription role. After that bootstrap, the workflow
creates and manages the VM resource group and Terraform state backend itself.

---

## 1. One-time Azure setup

The VM path is designed so you do **not** manually create the VM resource
group, Terraform state resource group, Storage Account, blob container, NIC,
network, public IP, VM, disks, or other Azure infrastructure. The only
unavoidable bootstrap is establishing the Azure identity that GitHub Actions
will use.

Run this once from your local CLI:

```bash
az login
az account set --subscription "<subscription-id-or-name>"
gh auth login
./scripts/bootstrap-azure-github.sh
```

The bootstrap helper is idempotent. It:

1. Registers the Azure providers used by the Bicep and VM paths.
2. Creates or reuses the Microsoft Entra application and service principal.
3. Grants the CI identity subscription-level `Contributor`.
4. Creates or reuses the exact GitHub OIDC federated credentials for
   `production`, `staging`, `prod`, and `vm-staging`.
5. Writes `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and
   `AZURE_SUBSCRIPTION_ID` into those GitHub Environments.

You therefore **do not** manually create an App Registration, service
principal, federated credential, Azure resource group, or Terraform state
storage. The VM state bootstrap also uses the ARM Authorization REST API for
`Storage Blob Data Contributor`, with a deterministic role-assignment ID, so
it does not depend on `az role assignment` working in the local tenant.

### Terraform state is bootstrapped by the workflow

Before `terraform init`, `.github/workflows/infra-deploy-vm.yml` runs
`scripts/bootstrap-terraform-state.sh`. That script automatically creates or
reuses:

```text
rg-snipeit-tfstate
└── <subscription-specific Storage Account>
    └── vm-state
```

The Storage Account name is deterministically derived from the Azure
subscription ID (`snipeittfstate<12-char-sha256-prefix>`). The workflow also
grants the GitHub OIDC identity `Storage Blob Data Contributor` on that
Storage Account. No `TF_STATE_*` GitHub Environment variables are required.
The bootstrap script accepts `TF_STATE_RESOURCE_GROUP`,
`TF_STATE_STORAGE_ACCOUNT`, and `TF_STATE_CONTAINER` only as optional
advanced overrides; the normal configuration should leave them unset.

The state backend is deliberately **outside** the VM resource group. This is
what makes the destroy path safe: Terraform can destroy the VM stack while
retaining the state it needs to record that destruction. The environments use
separate backend keys:

```text
vm-staging.tfstate
prod.tfstate
```

The lifecycle is therefore:

```text
workflow
  ├─ register providers
  ├─ create/reuse Terraform state RG + Storage Account + container
  ├─ grant Blob Data Contributor to CI identity via ARM Authorization API
  ├─ terraform init
  └─ terraform plan/apply/destroy
```

If the state backend already exists, the bootstrap step simply reuses it.
There is no recurring scheduler and no manual storage maintenance.

> **Important:** `terraform destroy` intentionally does **not** destroy the
> Terraform state resource group or Storage Account. Those are bootstrap
> infrastructure for the lifecycle itself.

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

**2a-0. Enable Zero Trust / Access on the account** — this is a one-time,
manual dashboard step that Terraform genuinely cannot do for you (it's
account-level onboarding, not a resource): in the Cloudflare dashboard,
open the **Zero Trust** section from the left sidebar, and follow its
one-time setup (choose a team name — the free plan covers the small
number of Access applications/policies this stack creates). Do this
*before* running `infra-deploy-vm.yml`'s `apply` for the first time —
otherwise `terraform apply` fails on both
`cloudflare_zero_trust_access_application.ssh` and
`cloudflare_zero_trust_access_service_token.ci` with
`access.api.error.not_enabled: Access is not enabled`, since the
Access API has nothing to attach an application/policy/service token to
until Zero Trust has been switched on at least once for the account.

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
  Access: Apps and Policies → Edit**, **Account → Access: Service
  Tokens → Edit**, **Zone → DNS → Edit** (scoped to
  your zone from 2a). The Service Tokens permission is easy to miss
  since it's a separate scope from Access: Apps and Policies — leaving
  it out doesn't block `cloudflare_zero_trust_access_application` or
  `cloudflare_zero_trust_access_policy` (those only need Apps and
  Policies), it only breaks `cloudflare_zero_trust_access_service_token`
  specifically, with a generic `error creating access service token:
  Authentication error (10000)` rather than anything mentioning
  permissions directly.
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
Host ssh-assets.example.com
  ProxyCommand cloudflared access ssh --hostname %h
```

The first `ssh azureuser@ssh-assets.example.com` opens a browser for Access SSO
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

## 5. Configure GitHub OIDC federation (automated)

You do not manually create the Azure App Registration, service principal,
subscription role assignment, or federated credentials.

Those are created/reused by:

```bash
./scripts/bootstrap-azure-github.sh
```

The script uses your current `az` subscription and `gh` repository context,
then configures the four GitHub Environment subjects used by this repository.
It is safe to run again; existing identities, role assignments, and federated
credentials are reused.

The only values you should expect to see in GitHub as Azure bootstrap values
are:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

The VM Terraform workflow authenticates to both Azure Resource Manager and the
Terraform `azurerm` backend using GitHub Actions OIDC. No Azure client secret
is stored in the repository or required by the VM infrastructure workflow.

> **Bootstrap boundary:** GitHub Actions cannot create the first Azure
> identity it needs to authenticate. That is why the `az login` +
> `gh auth login` + bootstrap command is the one-time local step. Once it has
> run, Azure infrastructure provisioning and Terraform state provisioning are
> owned by GitHub Actions.

## 6. Set GitHub Environment secrets/variables

In GitHub: **Settings → Environments** → create `prod` (and `vm-staging`
if you want a second, cheaper/smaller environment for testing changes
first — see the OIDC bootstrap step's callout above for why it's not just `staging`). For
`prod`, consider adding a **required reviewer** protection rule — this
is what makes `infra-deploy-vm.yml`'s `destroy` action require a second
person's approval before it can run (see that workflow's comment on the
`terraform destroy` step).

Opening an Environment shows two separate sections, **Environment
secrets** and **Environment variables** — each row in the table below is
tagged **Variable** or left as a (default) **Secret**; add it under the
matching section, both live on the same Environment page, just click
**Add secret** or **Add variable** as appropriate. A **Variable** isn't
sensitive (it's plain text, visible to anyone with repo read access) —
that's exactly why non-secret settings like `VM_SIZE`/`AZURE_LOCATION` below
use it instead of a secret.

**Deploy target vs. frontend build mode -- two separate settings, don't
confuse them:**

- **Deploy target** (`vm-staging` or `prod`): picked explicitly every run,
  via `deploy-azure-vm.yml`'s `environment` dropdown on the Actions tab
  (`workflow_dispatch`). There is no repo-level fallback for this anymore --
  the dropdown defaults to `prod` if you don't touch it, so an
  unattended/default run always targets production; pick `vm-staging`
  explicitly if that's what you want. A version-tag push (`git tag vX.Y.Z
  && git push origin vX.Y.Z`) never deploys by itself -- it only builds and
  publishes the release images (see `release.yml`); run this workflow by
  hand afterward, targeting `prod`, with that version in `image_tag`, when
  you're ready to actually deploy it.
- **Frontend build mode** (minified-only vs. minified+obfuscated, and the
  `ENVIRONMENT` value the backend/worker/beat containers themselves read --
  see `backend/config.py`'s `apply_environment_defaults`): a **Variable**
  named `ENVIRONMENT`, set PER Environment page -- on `prod`'s Environment
  variables section (and again on `vm-staging`'s, if you use it), NOT the
  repo-level `Settings → Secrets and variables → Actions → Variables tab`.
  Set `prod`'s copy to `production` to ship a minified+obfuscated frontend
  build and `ENVIRONMENT=production` in the containers on that Environment;
  `development`/anything else (or leaving it unset) on either Environment
  makes that Environment's build minified-only and
  `ENVIRONMENT=development` in the containers -- the safe default, so an
  Environment nobody deliberately configured never silently ships an
  obfuscated, production-flagged build. This is read by the
  `resolve-target` job (see `deploy-azure-vm.yml`'s own
  `ENVIRONMENT`/`RUNTIME_ENVIRONMENT` comments for the exact resolution
  order) and fed into `frontend/Dockerfile`'s `BUILD_ENV` build arg -- it
  has no effect on which VM/image name this run touches, that's entirely
  the dropdown's job. The run's summary (`GITHUB_STEP_SUMMARY`) prints
  which build mode was actually used, so you can confirm it after the fact
  instead of guessing. This same per-Environment variable is read the same
  way by `deploy-azure-aca.yml` (see `DEPLOYMENT.md`'s equivalent
  callout). **Redeploying an existing `image_tag` instead of building
  fresh skips this entirely** -- it reuses whatever build mode that image
  was originally built with, regardless of what the variable is set to
  now.
- **Frontend type** (which of the two mutually exclusive frontend images
  ships): a **Variable** named `FRONTEND_BUILD_TARGET`, same per-Environment
  scoping as `ENVIRONMENT` above. Leave unset (the default) to ship
  `frontend/Dockerfile`'s `frontend-legacy-only` stage -- the legacy static
  site, served at `/`. Set it to exactly `react` on an Environment that
  should ship the React "Ledger" SPA instead (`frontend-react-only`, also
  served at `/`); anything else resolves to the legacy site, so a typo
  never silently switches which frontend ships. Also read by the
  `resolve-target` job and fed into `frontend/Dockerfile` as `--target`
  instead of `BUILD_ENV`; independent of the `ENVIRONMENT` variable above,
  so you can obfuscate a legacy build or leave a React build unobfuscated
  (though `BUILD_ENV` only affects the legacy target's minify/obfuscate
  pipeline -- Vite's own production build for the React SPA is unaffected
  either way). `infra-deploy-vm.yml` reads the SAME variable and passes it
  to Terraform as `frontend_build_target` purely so `/opt/snipeit/.env` on
  the VM documents which kind of image that environment is supposed to be
  running (see `infra-vm/variables.tf`'s `frontend_build_target`
  description) -- keep the two in sync by hand if you ever change one
  without the other, and make sure `dockerhub_frontend_image` in your
  `terraform.tfvars` points at the matching flavor's own Docker Hub repo
  (there are two separate repos now, one per flavor -- see
  `frontend/Dockerfile`'s own top-of-file comment). See
  `frontend-app/README.md`'s "Choosing which frontend to ship" section for the full
  reasoning, and this same per-Environment variable is read the same way by
  `deploy-azure-aca.yml`. **This variable is the standing default only.**
  For a one-off override on a single run -- ship the React SPA just this
  once without touching Settings, or vice versa -- use the
  **`frontend_type`** dropdown right on the "Run workflow" form instead
  (`(environment default)` / `react` / `legacy`); it beats the variable for
  that run and nothing is persisted, so the very next run goes back to
  whatever `FRONTEND_BUILD_TARGET` says. Ignored, with a note in the run
  summary, whenever `image_tag` reuses an already-built image. The run
  summary's **Frontend type** row also says which of the two actually
  decided it.

Add these to each Environment (Secrets unless marked **Variable**):

| Name | Value | Used by |
|---|---|---|
| `ENVIRONMENT` (**Variable**, not secret) | `production` for a minified+obfuscated frontend build and `ENVIRONMENT=production` in the containers; `development`/unset for minified-only and `ENVIRONMENT=development` -- set this independently on EACH Environment page (`prod`, and `vm-staging` if used) | `deploy-azure-vm.yml` (`resolve-target` job) |
| `FRONTEND_BUILD_TARGET` (**Variable**, not secret) | `react` to ship the React "Ledger" SPA; unset/anything else for the default legacy static site -- the two are mutually exclusive (no combined option) -- set independently on EACH Environment page | `deploy-azure-vm.yml` (`resolve-target` job), `infra-deploy-vm.yml` |
| `AZURE_CLIENT_ID` | App Registration's appId (the OIDC bootstrap step) | `infra-deploy-vm.yml` |
| `AZURE_TENANT_ID` | Tenant ID (the OIDC bootstrap step) | `infra-deploy-vm.yml` |
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
| `DEPLOY_STATUS_USER` (**Variable**, not secret) | Optional — Basic Auth username for the `/_deploy/` dashboard, defaults to `admin` | `infra-deploy-vm.yml`, `sync-secrets-vm.yml` |
| `DEPLOY_STATUS_PASSWORD_HASH` | Optional but recommended — bcrypt hash from `docker run --rm caddy:2-alpine caddy hash-password`; see [Monitoring a rollout](#monitoring-a-rollout). Leave unset and the route stays fail-closed with a random, never-recorded hash | `infra-deploy-vm.yml`, `sync-secrets-vm.yml` |
| `DOCKERHUB_USERNAME` | Your Docker Hub username | both workflows |
| `DOCKERHUB_TOKEN` | A Docker Hub [Personal Access Token](https://app.docker.com/settings/personal-access-tokens) (not your password) | both workflows |
| `CUSTOM_DOMAIN` (**Variable**, not secret) | REQUIRED — a hostname in the `CLOUDFLARE_ZONE_ID` zone, e.g. `assets.example.com` | `infra-deploy-vm.yml` |
| `VM_SIZE` (**Variable**, not secret) | Optional — overrides `variables.tf`'s `Standard_B2s` default. E.g. `Standard_D2s_v3` if `Standard_B2s` isn't available in your region (see Troubleshooting) | `infra-deploy-vm.yml` |
| `AZURE_LOCATION` (**Variable**, not secret) | Optional — overrides `variables.tf`'s `eastus` default region. E.g. `southafricanorth` for South Africa North (region *names* like "South Africa North" shown in the Portal map to lowercase, no-space *slugs* like this for `az`/Terraform — `az account list-locations -o table` shows every region's slug). Named to match the Container Apps path's own `AZURE_LOCATION` Variable (see `DEPLOYMENT.md`) -- one name, one meaning, on both deploy paths. | `infra-deploy-vm.yml` |
| `VM_HOST` | Filled in AFTER step 8 (see step 9) — this is the VM's **Cloudflare Tunnel SSH hostname** (`ssh-<label>.<CLOUDFLARE_ZONE_NAME>`, e.g. `ssh-assets.example.com` for `CUSTOM_DOMAIN=assets.example.com`; `ssh.<CLOUDFLARE_ZONE_NAME>` if `CUSTOM_DOMAIN` is the zone apex), not its public IP; nothing listens on port 22 at the public IP by default | `deploy-azure-vm.yml`, `sync-secrets-vm.yml` |

Optional (leave unset if you don't use them yet):
`NOTIFICATIONS_ENABLED` (**Variable**), `SMTP_HOST`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `ADMIN_NOTIFICATION_EMAILS`,
`BACKUP_GDRIVE_ENABLED` (**Variable**), `BACKUP_GDRIVE_OAUTH_CLIENT_ID`,
`BACKUP_GDRIVE_OAUTH_CLIENT_SECRET`, `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN`,
`BACKUP_GDRIVE_FOLDER_ID` — see [Google Drive backup uploads](#google-drive-backup-uploads)
below for where these five come from.

Also optional — `EMAIL_PROVIDER` (**Variable**, default `smtp`) \|
`brevo` \| `resend` — an alternative to the `SMTP_*` secrets above if
your network blocks outbound SMTP ports (both send over plain HTTPS).
`BREVO_API_KEY` (only read when `EMAIL_PROVIDER=brevo`) and
`RESEND_API_KEY` (only read when `EMAIL_PROVIDER=resend`) pair with it.
The VM itself has no outbound-port restriction the way Render's Free
plan does, so plain `smtp` is fine here too — these exist for parity
with the Container Apps/Render paths, or if your own network/ISP
happens to block outbound SMTP. Wired all the way through: set here as
a GitHub Environment secret/variable → `infra-deploy-vm.yml` passes it
to Terraform (`infra-vm/variables.tf`'s `email_provider`/
`brevo_api_key`/`resend_api_key`) → `infra-vm/cloud-init.yaml` writes it
into a fresh VM's first-boot `.env` → `sync-secrets-vm.yml` keeps it in
sync on an already-running VM too, the same way it does every other
value on this page.

Also optional — **daily digest send time**: `OVERDUE_DIGEST_HOURS_UTC`
and `DUE_SOON_DIGEST_HOURS_UTC` (both **Variables**, default `8` — i.e.
08:00 UTC), the clock time the overdue-checkout digest and the
due-soon-reminder digest actually land in your inbox each day. Same
comma-separated-hours-of-day-UTC syntax as `BACKUP_HOURS_UTC` (e.g.
`8,20` for twice a day), and the same underlying idea: a fixed clock
time you can reason about, not "N hours after whichever moment the
worker container happened to boot." See `backend/celery_app.py`'s
`beat_schedule` for the `crontab` these two drive. Wired through the
same GitHub Variable → Terraform → `cloud-init.yaml` first-boot →
`sync-secrets-vm.yml` ongoing-sync chain as `EMAIL_PROVIDER` just above
(`infra-vm/variables.tf`'s `overdue_digest_hours_utc`/
`due_soon_digest_hours_utc`).

Also optional — **pending-approval SLA nudges**: `EXTENSION_REQUEST_SLA_HOURS`
and `QUOTATION_SLA_HOURS` (both **Variables**, default `24`) — how many
hours a `pending` Extension Request / `submitted` Quotation can go
without a decision before the SLA-nudge digest escalates it;
`APPROVAL_SLA_CHECK_INTERVAL_MINUTES` (**Variable**, default `60`) — how
often (in minutes) the worker checks both queues; and
`APPROVAL_SLA_ESCALATION_REPEAT_HOURS` (**Variable**, default `24`) —
how long an already-escalated, still-undecided row waits before it's
eligible to be re-escalated. See `backend/tasks/sla_tasks.py`'s module
docstring and [Due-Date Extensions & Notifications](README.md#due-date-extensions--notifications)'s
SLA-nudges item for the full rationale. Wired through the same GitHub
Variable → Terraform → `cloud-init.yaml` first-boot →
`sync-secrets-vm.yml` ongoing-sync chain as the two above
(`infra-vm/variables.tf`'s `extension_request_sla_hours`/
`quotation_sla_hours`/`approval_sla_check_interval_minutes`/
`approval_sla_escalation_repeat_hours`).

Also optional — `SEND_QUOTATION_RECIPIENT_EMAILS` (**Variable**, default
`true`) — whether a Quotation's own recipient gets emailed on every
change (line items, notes, discount, assignment, approval, fulfillment),
on top of the in-app bell notification, which is always created
regardless of this setting. See `services/quotation_service.py`'s
`_notify_quotation_recipient()`. Wired through the same GitHub Variable →
Terraform → `cloud-init.yaml` first-boot → `sync-secrets-vm.yml`
ongoing-sync chain as the SLA-nudge settings above
(`infra-vm/variables.tf`'s `send_quotation_recipient_emails`).

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

Also optional — app config overrides (all **Variables**, each with a
working default baked into `sync-secrets-vm.yml` if left unset, so none
of these are required for a working deploy): `POSTGRES_USER` (default
`admin`), `POSTGRES_DB` (default `asset_db`), `JWT_ALGORITHM` (default
`HS256`), `JWT_EXPIRY_HOURS` (default `12`), `SITE_NAME` (default
`Snipe-IT Lite`), `ENABLE_API_DOCS` (default `false`), `SMTP_PORT`
(default `587`), `DISPLAY_TIMEZONE` (default `Africa/Lagos`),
`CURRENCY_CODE` (default `NGN`), `ENABLE_AUTO_BACKUP` (default `true`).
See [Environment Variables Reference](README.md#environment-variables-reference)
in `README.md` for what each one actually does.

Also optional — per-service memory tuning (all **Variables**; see [Per-service
memory limits](#per-service-memory-limits) below for what each defends
against and its default): `DB_MEM_LIMIT`, `DB_MEM_RESERVATION`,
`REDIS_MEM_LIMIT`, `REDIS_MEM_RESERVATION`, `BACKEND_MEM_LIMIT`,
`BACKEND_MEM_RESERVATION`, `WORKER_MEM_LIMIT`, `WORKER_MEM_RESERVATION`,
`BEAT_MEM_LIMIT`, `BEAT_MEM_RESERVATION`, `FRONTEND_MEM_LIMIT`,
`FRONTEND_MEM_RESERVATION`, `CADDY_MEM_LIMIT`, `CADDY_MEM_RESERVATION`.
Setting these here (rather than hand-editing `/opt/snipeit/.env` on the
VM) is the durable way to retune a service's memory ceiling, since
`sync-secrets-vm.yml` rewrites `.env` from these GitHub Environment
values on every run and would silently overwrite a manual edit on its
next invocation.

> **Not yet exposed as GitHub Variables on this VM path** (unlike
> `DEPLOYMENT.md`'s Container Apps path, which wires all of these
> through `infra/main.bicep`): `LOG_LEVEL`, `LOGIN_RATE_LIMIT_MAX`,
> `LOGIN_RATE_LIMIT_WINDOW_SECONDS`, `ACCOUNT_LOCKOUT_MAX_ATTEMPTS`,
> `ACCOUNT_LOCKOUT_DURATION_MINUTES`, `DUE_SOON_REMINDER_DAYS`,
> `SEND_INDIVIDUAL_HOLDER_REMINDERS`, `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER`,
> `BACKUP_HOURS_UTC`, `BACKUP_RETENTION_COUNT`,
> `OTEL_AZURE_MONITOR_ENABLED`. `backend/config.py` reads all of these as
> plain environment variables with sensible defaults (e.g.
> `LOGIN_RATE_LIMIT_MAX=5`) regardless of deployment target, so the app
> works correctly without them — but on this VM path they can currently
> only be changed by hand-adding the line to both `/opt/snipeit/.env`
> *and* the relevant service(s)' `environment:` block in
> `/opt/snipeit/docker-compose.vm.yml` (`sync-secrets-vm.yml` won't
> preserve either edit across its next run, since it rewrites `.env`
> from GitHub Environment secrets/variables only). Wiring these through
> as first-class VM GitHub Variables, the same way the Container Apps
> path already does, is a reasonable follow-up if you need to tune them
> regularly. (`OVERDUE_DIGEST_HOURS_UTC`/`DUE_SOON_DIGEST_HOURS_UTC` — the
> daily digest send times — used to be on this list too; see the
> "Daily digest send time" callout above for where they're documented
> now that they're wired through.)

---

## 7. Review the Terraform plan locally (optional but recommended first time)

```bash
cd infra-vm
az login   # if you haven't already in this shell
# The CI workflow creates the remote backend automatically. For an optional
# local plan, derive the same deterministic state-account name instead of
# manually creating or looking up a storage account.
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
STATE_ACCOUNT="snipeittfstate$(printf '%s' "$SUBSCRIPTION_ID" | sha256sum | cut -c1-12)"
terraform init -input=false \
  -backend-config="resource_group_name=rg-snipeit-tfstate" \
  -backend-config="storage_account_name=$STATE_ACCOUNT" \
  -backend-config="container_name=vm-state" \
  -backend-config="key=prod.tfstate"

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

The workflow also bootstraps Terraform's remote state automatically. There is
nothing to create manually for Terraform state. For `destroy`, the workflow
requires an explicit confirmation string (`DESTROY vm-staging` or `DESTROY
prod`) before Terraform is allowed to remove the tracked VM resources; the
separate state resource group/storage account is retained.


In GitHub: **Actions → Deploy VM Infrastructure (Terraform) → Run workflow**.

- `environment`: `prod` (or `vm-staging`)
- `action`: `plan` first — read the output, confirm it matches step 7's
  local plan. Re-run the workflow with `action: apply` once you're happy.

`apply` takes 3-5 minutes. When it finishes, open the run's **Summary**
tab (not a step's raw log) — the "Print outputs" step writes a markdown
table there with every Terraform output, including:

- `public_ip_address` — the VM's static IP. Not used for normal traffic
  at all: both the app (`app_url`) and SSH (`ssh_command`) go through the
  Cloudflare Tunnel instead, which is an *outbound-only* connection the
  VM itself initiates to Cloudflare — nothing needs to reach this IP
  directly, which is also why `ssh_allowed_source_ips` is empty (no
  inbound ports open) by default. It's kept around for two reasons: (1)
  break-glass access via `ssh_command_break_glass` if Cloudflare's
  network is ever unreachable from where you are (only works once you've
  temporarily set `ssh_allowed_source_ips`, see Troubleshooting), and (2)
  it's what Azure's own tooling (Portal, `az vm show`, diagnostics/
  monitoring) references the VM by, regardless of how you actually route
  traffic to it. Not something to open in a browser or SSH into day to
  day.
- `azure_fqdn` — `<label>.<region>.cloudapp.azure.com` (break-glass reference only)
- `app_domain` — the domain Caddy actually serves on (your `CUSTOM_DOMAIN`)
- `app_url` — `https://<app_domain>` — not reachable yet, the app isn't deployed until step 10
- `ssh_hostname` — the Cloudflare Access-gated SSH hostname, kept to a single DNS label under the zone apex (e.g. `ssh-assets.example.com`, not `ssh.assets.example.com`) so it's covered by Cloudflare's default wildcard cert — this is what `VM_HOST` becomes in step 9
- `ssh_command` — exact command to SSH in through the Tunnel/Access (see step 2's last box)
- `ssh_command_break_glass` — direct SSH over the public IP; only works if you've temporarily set `ssh_allowed_source_ips` (see step 2 / Troubleshooting)
- `cloudflare_ci_service_token_id` / `cloudflare_ci_service_token_secret` — needed for step 9's `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET`

> **If you instead expand a step's raw log** (e.g. "terraform apply"),
> you'll see an `env:` listing at the top with most `TF_VAR_*` lines
> showing `***` and some showing nothing at all — that's normal, not
> anything going wrong. GitHub Actions always shows this env context for
> every step, and always masks any value that matches a registered repo/
> environment secret (which is most of these), while the blank ones are
> just optional variables (`admin_notification_emails`, the OTel/App
> Insights ones, backup GDrive OAuth fields, etc.) nobody set. It's
> unrelated to the actual Terraform outputs above — those live only on
> the Summary tab, not in any step's raw log, and aren't masked (they're
> freshly generated values, not copies of anything already registered as
> a secret).

The VM is already running the six-container stack + Caddy at this point
(cloud-init brings it up on first boot using `initial_image_tag`, default
`latest`) — but `latest` may not exist yet on your Docker Hub repos if
this is a brand new project. That's fine; step 10 fixes that with a real
build.

---

## 9. Point `deploy-azure-vm.yml` at the new VM

From step 8's Summary tab, copy the `ssh_hostname`,
`cloudflare_ci_service_token_id`, and `cloudflare_ci_service_token_secret`
outputs. In GitHub: **Settings → Environments →
prod → Secrets** → add:

| Name | Value |
|---|---|
| `VM_HOST` | the `ssh_hostname` output value (e.g. `ssh-assets.example.com`) |
| `CF_ACCESS_CLIENT_ID` | the `cloudflare_ci_service_token_id` output value |
| `CF_ACCESS_CLIENT_SECRET` | the `cloudflare_ci_service_token_secret` output value |

Don't use `public_ip_address` here — nothing listens on port 22 there by
default (see step 2).

**If you lose `cloudflare_ci_service_token_secret` later** (didn't copy
it in time, or `CF_ACCESS_CLIENT_SECRET` got deleted from GitHub) —
Cloudflare only ever reveals a service token's secret once, at creation,
so there's no "retrieve it again" API call. The fix is to force
Terraform to create a brand new token and rewire GitHub to match:

1. Locally (or via a one-off `workflow_dispatch` step you add
   temporarily), run:
   ```bash
   terraform taint cloudflare_zero_trust_access_service_token.ci
   ```
   `taint` just marks this one resource for recreation on the next
   `apply` — it doesn't touch the VM, the tunnel, DNS, or anything else.
2. Run `infra-deploy-vm.yml` with `action: apply` again. This destroys
   the old service token (instantly invalidating the old
   `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET` pair — `deploy-azure-
   vm.yml`/`sync-secrets-vm.yml` will fail to authenticate until step 3
   below) and creates a new one.
3. Copy the fresh `cloudflare_ci_service_token_id`/
   `cloudflare_ci_service_token_secret` from this new run's Summary tab,
   and update the `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET` repo/
   environment secrets to match — same as the table above, just
   overwriting the existing secrets instead of adding new ones.

---

## 10. Deploy the application (`deploy-azure-vm.yml`)

In GitHub: **Actions → Deploy to Azure VM → Run workflow**, `environment:
prod`, leave `image_tag` blank (build fresh). This workflow only ever runs
via this manual `workflow_dispatch` step — there is no `push` trigger and
no `workflow_call` entry point, so neither a plain `git push` to `main` nor
a `git tag vX.Y.Z && git push origin vX.Y.Z` triggers it automatically
(see "Tagging & Versioning" below for what a tag push *does* do — publish
the release images, nothing more). To deploy an already-published version
instead of building fresh, put it in `image_tag`.

This runs: `ci.yml` (full test suite) → build + push both images to Docker
Hub → SSH in, sync `docker-compose.vm.yml`/`Caddyfile`/`caddy/weights.conf`/
`scripts/*`, update `IMAGE_TAG` → `scripts/blue-green-deploy.sh` (migrate →
start blue (the incoming slot) with zero production traffic → health-check
it directly → gradually ramp traffic onto it → promote green onto the
same image → spin blue back down) → prune old image layers → smoke test
`https://<domain>/` and
`https://<domain>/api/auth/me`. See "Zero-Downtime Blue-Green Deployments"
below for the full mechanics and how to watch it happen live.

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
  'cd /opt/snipeit && docker compose -f docker-compose.vm.yml logs "backend-$(grep ^ACTIVE_SLOT= .env | cut -d= -f2)" | grep -A2 "root admin"'
```

(`backend-blue`/`backend-green` are this VM's two blue-green slots — see
"Zero-Downtime Blue-Green Deployments" below; the command above reads
whichever one is currently active straight out of `.env` so you don't have
to know it in advance.)

(`<VM_HOST>` here is the `ssh_hostname` output from step 8, e.g.
`ssh-assets.example.com` — same value everywhere else in this doc that
shows `<VM_HOST>`.)

Log in as `superadmin` with that password, then change it immediately
(Settings → your account).

---

## Zero-Downtime Blue-Green Deployments

Every deploy on this path (`deploy-azure-vm.yml` → `scripts/blue-green-deploy.sh`)
runs entirely on the idle slot before it ever receives a real request, and
only shifts production traffic onto it gradually, after it proves itself.
**Roles are fixed, not swapped from one deploy to the next:** `green` is
always the active/production slot, `blue` is always the incoming candidate
a rollout deploys into — see `scripts/blue-green-deploy.sh`'s own
top-of-file comment for the full rationale. `docker compose -f
docker-compose.vm.yml ps backend-green` (or the `/_deploy/` dashboard
below) always answers "what's live right now," with no need to also check
which slot most recently won a rollout.

1. **Migrate.** `alembic upgrade head` runs against the incoming image,
   through `backend-blue`, against the one shared Postgres `db` — this is
   why migrations on this path need to stay backward-compatible with the
   still-running `green` slot's code for the duration of a rollout (add
   columns, don't rename or drop them in the same release that also stops
   writing to them).
2. **Start the replica, cold.** Only `backend-blue`/`frontend-blue` start.
   `caddy` is still sending 100% of traffic to `green` — the replica
   exists but is invisible to real users.
3. **Health-check `blue` directly**, bypassing Caddy/production traffic
   entirely (`scripts/health-check.sh --mode internal`): `GET /healthz`
   (liveness), `GET /readyz` (DB reachable + schema matches this build), a
   static-asset fetch through the replica's own nginx, and a full `GET
   /api/auth/me` round trip through that same nginx to that same slot's
   backend — proving the *pairing* works, not just each half alone. This
   is the exact same bar the post-cutover external smoke test already
   holds the live domain to (`--mode external`) — see that script's own
   header comment.
4. **Ramp traffic onto `blue`**, 10% → 25% → 50% → 75% → 100%, re-running
   the health check after each step (now under real, if partial, traffic —
   catches things an empty-load check can't). Each step rewrites
   `caddy/weights.conf` and runs `caddy reload` — a *graceful* reload that
   finishes in-flight requests and drops zero connections, not a restart.
5. **Promote.** Now that `blue` has proven itself under 100% real traffic,
   bring `green` up on the exact same image `blue` is already running
   (both share `docker-compose.vm.yml`'s `${IMAGE_TAG}` reference, so this
   starts the identical build, not a separate deploy), health-check it
   internally, then flip Caddy's weight straight back to 100% `green` /
   0% `blue` — both slots are running the identical, already-proven image
   at this point, so this is a same-code swap, not a second canary.
   Finally stop+remove `blue`'s containers so it's idle again, ready for
   the next incoming image.

**On any failed check before the OIDC bootstrap step**, the rollout stops immediately:
traffic is left at (or restored to) 100% on the still-good `green` slot,
and `blue`'s containers are stopped — `green` is never touched until
`blue` has already proven itself end to end, so a bad deploy never causes
an outage, it just doesn't roll out. **A failure DURING the OIDC bootstrap step itself**
(green's own health check failing after blue had already proven itself at
100% traffic) is handled differently: the already-proven new image is
left serving traffic on `blue` rather than reverting to `green`'s old,
potentially-stale image. That state is flagged loudly (a non-zero exit,
and `status.json`'s phase left as `promotion_failed`, not `done`) since
it's the one case where "`green` = active" doesn't hold until the next
successful deploy re-runs promotion.

Unlike earlier versions of this script, `.env`'s `ACTIVE_SLOT`/
`COMPOSE_PROFILES` are no longer flipped at the end of a rollout — with
roles fixed, `COMPOSE_PROFILES` is simply `green`, permanently, set once
by `infra-vm/cloud-init.yaml` on first boot. A reboot (or a bare `docker
compose up -d`) always comes back up on `green` with no rollout needing to
run first.

`worker`/`beat` aren't behind Caddy at all (they don't serve HTTP), so
they aren't blue-green'd — they're just restarted in place once the new
image has passed migration, right alongside starting the replica. A few
seconds of no background task processing here is invisible to end users,
unlike an HTTP-serving restart would be.

### Monitoring a rollout

**Live, while it's running** — the GitHub Actions run itself streams every
phase (`migrating`, `starting_replica`, `health_checking`, each ramp
percentage, `promoting`, `spinning_down_incoming`, `done`) and every
individual check's pass/fail as it happens, in the "Deploy over SSH +
migrate" step's log.

**The `/_deploy/` dashboard** — a small live-updating page (auto-refreshes
every 3s) showing the current phase, which slot is active (green) and
incoming (blue), the live traffic split as a bar, and the full
health-check log, backed by `status.json`/`checks.log` that
`scripts/blue-green-deploy.sh` writes on every phase transition. Reachable
at `https://<domain>/_deploy/`, gated by HTTP Basic Auth (Caddy's
`basic_auth`, see `Caddyfile`) so it's never just sitting open on the same
origin as the public app. **Set your own credentials before relying on
this** — the values `cloud-init.yaml` seeds on first boot are
random/unknown by design (the route fails *closed*, not open, until you
set your own).

This is fully automated — no SSH required, in either direction:

```bash
# Generates a bcrypt hash of a password you choose (prompted interactively)
docker run --rm -it caddy:2-alpine caddy hash-password
```

- **Before a VM's first boot** — set the `DEPLOY_STATUS_USER` repo/
  environment *Variable* (defaults to `admin` if you skip it) and the
  `DEPLOY_STATUS_PASSWORD_HASH` repo/environment *Secret* (paste the
  bcrypt hash above) on the `vm-staging`/`prod` GitHub Environment(s), the
  same place `JWT_SECRET_KEY`/`POSTGRES_PASSWORD` already live. The next
  `infra-deploy-vm.yml` run (provisioning a new VM) bakes them straight
  into `/opt/snipeit/.env` via `infra-vm/variables.tf` — see that file's
  `deploy_status_user`/`deploy_status_password_hash` comments. Leaving
  `DEPLOY_STATUS_PASSWORD_HASH` unset keeps the same fail-closed random
  hash cloud-init always used.
- **On an already-provisioned VM** (rotating credentials, or setting them
  for the first time after skipping the step above) — update the same two
  values, then run **`sync-secrets-vm.yml`** (`workflow_dispatch`, pick
  `vm-staging`/`prod`, leave `restart_services` on its `auto` default).
  It writes the new `DEPLOY_STATUS_USER`/`DEPLOY_STATUS_PASSWORD_HASH`
  into `/opt/snipeit/.env` over the same Cloudflare-Access-proxied SSH
  connection `deploy-azure-vm.yml` uses (no interactive shell, no manual
  edit), then recreates only the `caddy` container since that's the only
  service whose config actually changed. If you leave
  `DEPLOY_STATUS_PASSWORD_HASH` unset for a given run, the workflow reads
  back and keeps whatever hash is already live on the VM instead of
  blanking it out — so a routine secrets sync (e.g. rotating `SMTP_PASSWORD`)
  can never accidentally lock you out of, or fail open on, this dashboard.

**From the command line** — tail everything at once:

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST> bash -s <<'EOF'
  cd /opt/snipeit
  echo "--- status.json ---"; cat /mnt/docker-data/volumes/deploy_status/status.json
  echo "--- docker compose ps ---"; docker compose -f docker-compose.vm.yml ps
  echo "--- current weights ---"; cat caddy/weights.conf
EOF
```

**A manual/emergency traffic shift** (bypassing the script entirely — e.g.
you want to force traffic back onto the known-good `green` slot right
now) is just editing `caddy/weights.conf` and reloading. The two numbers
are `<blue-weight> <green-weight>`, in that order (see the file's own
top comment):

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST>
cd /opt/snipeit
echo "lb_policy weighted_round_robin 0 100" > caddy/weights.conf   # 100% green (active), 0% blue
docker compose -f docker-compose.vm.yml exec caddy \
  caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

### Running a rollout by hand (outside CI)

Everything `deploy-azure-vm.yml` does over SSH is also just a script you
can run directly, useful for debugging a stuck rollout or deploying
without waiting on CI:

```bash
ssh -i snipeit_vm_deploy_key azureuser@<VM_HOST>
cd /opt/snipeit
# .env's IMAGE_TAG must already point at an image tag that's been pulled
# (docker compose -f docker-compose.vm.yml pull) before running this.
./scripts/blue-green-deploy.sh
```

Exit code 0 means `green` is now active and serving 100% of traffic on the
new image, `blue` is stopped; non-zero means it aborted (see the "On any
failed check" note above for the one case — a failure during promotion —
where `green` may not be what's currently serving) — check
`/mnt/docker-data/volumes/deploy_status/status.json`'s `"phase": "failed"`
(or `"promotion_failed"`) entry and `checks.log` for which check failed.

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

Pushing a tag matching `v*.*.*` triggers `release.yml` — and *only*
`release.yml`; `deploy-azure-vm.yml` has no `push` trigger and no
`workflow_call` entry point, so this push does not deploy anything to the
VM (or to Azure Container Apps — see [Using both deployment
targets](#using-both-deployment-targets-optional) below). What it does do,
automatically:

1. Runs the full `ci.yml` gate (lint, tests, Trivy scan) — same as any push.
2. Builds and pushes **both** images to Docker Hub, tagged **three ways**:
   `:v1.4.2` (the real, pullable release artifact), `:<commit-sha>`
   (exact-source traceability), and `:latest` (convenience). Each image
   also carries OCI labels (`org.opencontainers.image.version`,
   `.revision`, `.created`) — visible via `docker inspect`.
3. Opens a PR against `main` with the new `CHANGELOG.md` section and cuts
   a GitHub Release with the same notes.

That's it — the tag push stops there. **To actually deploy `v1.4.2` to the
VM**, run `deploy-azure-vm.yml` yourself: Actions tab → "Deploy to VM" →
"Run workflow", `environment: prod`, and paste `v1.4.2` into `image_tag`.
It pulls the exact image built above rather than rebuilding, then SSHes to
the VM (through the Cloudflare Tunnel/Access — see step 2), deploys it —
pull, `docker compose up -d`, `alembic upgrade head`, smoke test — and,
**only if the smoke test passes**, writes `/opt/snipeit/CURRENT_RELEASE`
on the VM and a summary table in the GitHub Actions run — see
[Checking the current running version](#checking-the-current-running-version)
below. A failed smoke test means this step never runs, so the marker keeps
pointing at whatever the last *confirmed-healthy* version was —
deliberately, not a bug. Run the same tag against `deploy-azure-aca.yml`
too if you're also shipping to Container Apps — the two are independent
manual runs, on your own schedule.

Use [Semantic Versioning](https://semver.org) for the tag itself:
`MAJOR.MINOR.PATCH` — bump `MAJOR` for breaking changes (e.g. a migration
that isn't safely reversible, see below), `MINOR` for new features,
`PATCH` for fixes. `CHANGELOG.md` (maintained by `release.yml`, not this
workflow — see the caveat below) is the running human-readable history of
what changed in each one.

A manual `workflow_dispatch` run without `image_tag` still deploys fine —
it's just recorded as `VERSION=unversioned`
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
docker compose -f docker-compose.vm.yml run --rm "backend-$(grep ^ACTIVE_SLOT= .env | cut -d= -f2)" alembic downgrade -1
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

If you also have the Container Apps path (`DEPLOYMENT.md`) set up against
the *same* repo, the two paths never conflict or step on each other: a
pushed version tag runs `release.yml` exactly once (builds + Trivy-scans +
publishes the images under that tag, cuts the changelog PR/GitHub Release)
— it doesn't call either deploy workflow. Deploying to the VM
(`deploy-azure-vm.yml`) and deploying to Container Apps
(`deploy-azure-aca.yml`) are both separate, manual `workflow_dispatch`
runs you trigger yourself, whenever you choose, in whatever order you
choose — run one, the other, or both against the same version tag if you
genuinely run both targets in parallel (e.g. VM for a cost-capped primary
environment, Container Apps for a burst-capacity secondary). Neither
workflow has a `push` trigger or a `workflow_call` entry point, so there's
nothing to disable if you only use one path.

### Redeploying without a version bump

**Normal redeploy** (new code, not yet a named release): run
`deploy-azure-vm.yml` manually with `environment: vm-staging` (or `prod`),
leaving `image_tag` blank so it builds fresh from whichever branch/ref you
run it against — see [How a real, named release is
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

> **Mode 2 (Google Workspace service account + Shared Drive) instead of Mode
> 1 above?** Set `BACKUP_GDRIVE_CREDENTIALS_JSON` (the service account's JSON
> key, pasted as one line) as a Secret on the `prod`/`vm-staging` GitHub
> Environment instead of the three OAuth secrets above — see README.md's
> [Backups](README.md#backups) section for how to create that service
> account. `infra-deploy-vm.yml` reads it into
> `TF_VAR_backup_gdrive_credentials_json` the same way it reads every other
> `TF_VAR_*` here.

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

Defaults, sized for the Terraform default `Standard_B2s` (2 vCPU / 4 GiB),
STEADY STATE (only one slot's `backend-*`/`frontend-*` running -- see
"Zero-Downtime Blue-Green Deployments" below):

| Service | Limit | Reservation | Role |
|---|---|---|---|
| `db` | 768m | 256m | Postgres |
| `redis` | 256m | 128m | Celery broker + export result cache |
| `backend-blue` / `backend-green` | 768m | 256m | FastAPI/uvicorn (only ONE of these two runs at a time in steady state) |
| `worker` | 640m | 256m | Celery worker (CSV/PDF export jobs) |
| `beat` | 128m | 64m | Celery scheduler (no request handling) |
| `frontend-blue` / `frontend-green` | 128m | 64m | nginx, static assets + internal proxy (only ONE runs at a time in steady state) |
| `caddy` | 160m | 64m | reverse proxy, TLS re-presentation (Origin CA cert), blue/green traffic split |
| `cloudflared` | 128m | 32m | outbound-only Cloudflare Tunnel connector |
| **Total (steady state)** | **~2.98 GiB** | **~1.13 GiB** | leaves ~1 GiB for the OS/Docker daemon |
| **Total (mid-rollout, both slots' backend+frontend briefly up)** | **~3.87 GiB** | **~1.45 GiB** | see the note below |

A rollout (`scripts/blue-green-deploy.sh`) briefly runs BOTH slots'
`backend-*`+`frontend-*` together — from step [2/6] (starting blue, the
incoming slot) until step [6/6] (blue is spun back down after green is
promoted), typically a couple of minutes
end to end. On the default `Standard_B2s`, that peak leaves only ~130 MiB
of headroom for the OS/Docker daemon, which is tight but has proven fine
for light-to-moderate traffic; if your traffic is heavier, size up to
`Standard_B2ms` (8 GiB) — see below.

Every value reads from `/opt/snipeit/.env` first (`DB_MEM_LIMIT`, etc —
see `docker-compose.vm.yml`'s services). The durable way to retune one is
setting the matching **GitHub Environment Variable** (see [Set GitHub
Environment secrets/variables](#6-set-github-environment-secretsvariables)
above) and re-running `sync-secrets-vm.yml` (or your next
`deploy-azure-vm.yml` run, which calls it too) — that writes the value
into `.env` in a way that survives future syncs. Editing `.env` directly
over SSH works for a quick one-off test, but `sync-secrets-vm.yml`
regenerates that file from GitHub Environment secrets/variables on every
run, so a manual edit is silently lost the next time it runs; either way,
`docker compose -f docker-compose.vm.yml up -d` is what actually applies
the new value.

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

## Rebuilding just the VM (recovering from a broken first boot)

Cloud-init (`infra-vm/cloud-init.yaml`) only ever runs once, on the VM's
very first boot. If something in it was broken at the time (the classic
symptom: `docker`/`docker compose` are missing entirely, nothing in
`/opt/snipeit` came up, and every deploy/SSH step fails with `remote
error: tls: handshake failure` because `cloudflared` — which only starts
as part of the app stack — never got a chance to run), fixing the file
and re-running `infra-deploy-vm.yml`'s normal `apply` **won't help**:
`main.tf`'s VM resource has `lifecycle.ignore_changes = [custom_data]`
specifically so routine applies don't perpetually want to rebuild the VM
just because `IMAGE_TAG` drifted — which also means Terraform won't
notice your cloud-init fix on its own. The VM needs to be destroyed and
recreated so cloud-init gets a genuine first boot again.

**You do NOT need to, and should not, run `terraform destroy` or delete
anything by hand in the Azure/Cloudflare consoles.** Everything else this
stack owns — the data disk (and anything already written to it), the
NIC, the static public IP, the NSG, the Cloudflare Tunnel, its DNS
records, and the Access application/policies — has no dependency
pointing *at* the VM, so it's all left completely alone. Only the VM
resource itself (plus the disk-attachment record, and the backup-vault
registration if `enable_data_disk_snapshots` is on — both cheap,
non-destructive to recreate) gets replaced.

### Where to run this: GitHub Actions, not your PC

Same reasoning as steps 7-8 above: state and run history should live in
one place your whole team can see, not on whoever's laptop happened to
run it. `infra-deploy-vm.yml` has a `replace_target` input for exactly
this case, so the whole thing runs the same way a normal `apply` does —
nothing to install or run locally at all.

1. Make sure the fixed `infra-vm/cloud-init.yaml` is committed on the
   branch this workflow checks out (usually `main`).
2. **Actions → Deploy VM Infrastructure (Terraform) → Run workflow**:
   - `environment`: `prod` (or `vm-staging`) — whichever actually has the broken VM
   - `action`: `plan`
   - `replace_target`: `azurerm_linux_virtual_machine.this`
3. Read the plan. It should show `-/+` (destroy-and-recreate) on exactly:
   - `azurerm_linux_virtual_machine.this`
   - `azurerm_virtual_machine_data_disk_attachment.data` (just re-attaches the *same* disk to the new VM's ID — no data loss)
   - `azurerm_backup_protected_vm.this[0]` (only present if `enable_data_disk_snapshots = true` — re-registers the new VM with the existing backup vault/policy)

   and **nothing else** — no Cloudflare resources, no NSG, no public IP,
   no managed disk itself. If you see anything beyond that trio, stop and
   figure out why before applying.
4. Re-run the same workflow with `action: apply` and the same
   `replace_target`. Takes the usual 3-5 minutes — cloud-init runs fresh
   on the new VM, and this time it should actually install Docker, mount
   the data disk, and bring the stack up.
5. The new VM keeps the same static IP and `ssh_hostname`, so nothing in
   step 9's `VM_HOST` secret needs to change. Do re-run
   `deploy-azure-vm.yml` once (step 10) afterward anyway, so the app is
   running the exact image tag your last real release used, rather than
   whatever `initial_image_tag` cloud-init happened to bring up.
6. Confirm the Cloudflare Zero Trust dashboard (Networks → Tunnels) shows
   `Status: Active` with one connector before considering this done.

### terraform.tfvars — nothing new to fill in

This is a targeted repair of infrastructure that was already provisioned
once, in an environment that already has every secret it needs sitting
in GitHub (steps 2-6). You don't need to add or change a single value in
`terraform.tfvars` or in GitHub's secrets for this — the same
`environment_name`, `ssh_public_key`, Cloudflare token/IDs, domain, and
application secrets already stored there are exactly what the rebuilt VM
needs again.

If you'd rather eyeball the plan locally first (optional, same spirit as
step 7), reuse that exact recipe but point it at the **same remote state**
the CI run uses — otherwise a local plan against a fresh local state file
would show it wanting to create *everything*, not just replace the VM:

```bash
cd infra-vm
az login
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
STATE_ACCOUNT="snipeittfstate$(printf '%s' "$SUBSCRIPTION_ID" | sha256sum | cut -c1-12)"
terraform init -input=false \
  -backend-config="resource_group_name=rg-snipeit-tfstate" \
  -backend-config="storage_account_name=$STATE_ACCOUNT" \
  -backend-config="container_name=vm-state" \
  -backend-config="key=prod.tfstate"   # or vm-staging.tfstate

# Same TF_VAR_* exports as step 7 -- the same values already sitting in
# your GitHub Environment secrets, not new ones:
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

terraform plan -replace="azurerm_linux_virtual_machine.this"
```

Read it, confirm it matches the three-resource list in step 3 above, then
**apply through the workflow (step 4), not locally** — same rule as step
7: let one place (CI) own every real state-changing apply, so there's
never a question of whose local state is authoritative.

(Prefer a `terraform.tfvars` file over exports for this local review
instead? Copy `infra-vm/terraform.tfvars.example` to
`infra-vm/terraform.tfvars` and fill in the exact same values listed
above — nothing in that example file needs to change for this recovery,
it's already a complete match for `variables.tf`. Just remember it's
`.gitignore`'d and must never be committed, and delete it again once
you're done reviewing.)

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

- **SSH: no open inbound port, no Bastion**. `ssh_allowed_source_ips` defaults to `[]`, so `main.tf`'s `AllowSSH` NSG rule doesn't exist at all by default — there is nothing to port-scan or brute-force on port 22 from the public internet. Access is instead through the Cloudflare Tunnel (step 2): the VM only ever makes an outbound connection to Cloudflare's edge, and Cloudflare Access (step 2d) gates who/what can reach the SSH hostname from there — email SSO for humans, a service token for CI, both independent of OpenSSH's own key auth. Auth itself is still key-only underneath that (no password auth at all — `disable_password_authentication = true` in `main.tf`). Only set `ssh_allowed_source_ips` as a deliberate, temporary break-glass measure (see Troubleshooting below).
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

**`terraform apply` fails with `SkuNotAvailable`/
`SkuNotAvailableForLocation`** — Azure has no current capacity for that
VM size in that region; this is regional capacity, not anything wrong
with the config, and it shifts over time (a size unavailable today may
free up later). Set the `VM_SIZE` and/or `AZURE_LOCATION` repo/environment
Variables (step 6) to something else — e.g. `Standard_B2ms`, or a nearby
region — and re-apply. `az vm list-skus --location <region> --size
Standard_B --output table` shows what's actually available/restricted
in a given region before you guess.

**`terraform apply` fails with "already exists"/"already be present"
for a resource Terraform doesn't have in state (resource group, Cloudflare
Tunnel, DNS record, etc.)** — this means something got created in a
*previous* run whose state Terraform lost track of (most commonly:
state genuinely wasn't being persisted yet — see `versions.tf`'s remote
backend requirement above — or a run failed partway and left one
resource behind that a later, unrelated change then collided with).
Once the remote backend (this doc's step 1) is correctly configured,
new failures stop causing this — but any resource created *before* it
was working still needs one-time manual cleanup: either delete the
specific leftover resource in the Portal/Cloudflare dashboard and let
Terraform recreate it fresh on the next apply, or import it into state
instead if you'd rather keep the existing one.

The easiest way to import: run this workflow again with **action: import**
and **import_resources** set to one `address=azure_resource_id` line per
resource named in the error — copy both straight out of the error message
itself (the address is the `resource "..." "..."` line right below "with",
the ID is the quoted string in the `Error:` line). For example, given:
```
Error: A resource with the ID ".../resourceGroups/rg-.../providers/Microsoft.Network/networkSecurityGroups/nsg-..." already exists
  with azurerm_network_security_group.this,
```
set `import_resources` to:
```
azurerm_network_security_group.this=/subscriptions/.../resourceGroups/rg-.../providers/Microsoft.Network/networkSecurityGroups/nsg-...
```
One line per conflicting resource (the error may list more than one in a
single failed apply — include all of them in the same import run). No
`create`, no `delete`, no risk to an existing data disk or database this
way — it just tells Terraform's state "this address IS that ID, go read its
current real config." Follow up with **action: plan** to confirm the diff
is now clean (or only shows the change you actually intended) before
switching back to **action: apply**.

If you'd rather do it from a local machine with Terraform + Azure CLI
already set up instead, the equivalent is:
```bash
terraform import azurerm_network_security_group.this <resource_id>
```

**I need to change `AZURE_LOCATION`/region after already applying, and Azure
won't let me move an existing resource group in place** — resist the
urge to wipe or reset the whole state file to force this through. State
is **one shared file covering every provider in this config** — Azure
*and* Cloudflare together, not scoped per-provider — so wiping it also
un-tracks the tunnel, DNS records, and Access application/policies/
service token, none of which actually depend on region and didn't need
to change. That just recreates this doc's entire "already exists"
problem class, now for Cloudflare resources that were working fine.
Scope the reset to only the Azure resources that actually need
recreating instead:
```bash
terraform state rm azurerm_resource_group.this
terraform state rm azurerm_linux_virtual_machine.this
# ...and any other azurerm_* resource whose location can't change in place
```
then `terraform apply` — Terraform recreates just those, in the new
region, while every `cloudflare_*` resource stays untouched and tracked.
If a full state reset already happened, see the next entry.

**A previous state reset (or a run that failed before this doc's remote
backend existed) left real Cloudflare resources — tunnel, DNS records,
Access application/policy/service token — orphaned from state** — same
underlying pattern as the resource-group case above, just for
Cloudflare. Since none of these are expensive or risky to keep, `import`
them back into the fresh state rather than deleting and recreating them
again:
```bash
terraform import cloudflare_zero_trust_tunnel_cloudflared.this <account_id>/<tunnel_id>
terraform import cloudflare_zero_trust_tunnel_cloudflared_config.this <account_id>/<tunnel_id>
terraform import cloudflare_record.app <zone_id>/<app_record_id>
terraform import cloudflare_record.ssh <zone_id>/<ssh_record_id>
terraform import cloudflare_zero_trust_access_application.ssh <account_id>/<app_id>
terraform import cloudflare_zero_trust_access_policy.ssh_humans <account_id>/<app_id>/<policy_id>
terraform import cloudflare_zero_trust_access_service_token.ci <account_id>/<service_token_id>
terraform import cloudflare_zero_trust_access_policy.ssh_ci <account_id>/<app_id>/<ci_policy_id>
```
(IDs come from the Cloudflare dashboard or `cloudflare` provider's own
API — each resource's Terraform Registry page documents its exact
import ID format if one of these doesn't match what you have.) Once
imported, these stay tracked in the shared state going forward, same as
every Azure resource.

**Lost `cloudflare_ci_service_token_secret` / need to rotate
`CF_ACCESS_CLIENT_SECRET`** — Cloudflare only reveals a service token's
secret once, at creation; there's no API to fetch it again later. See
step 9's "If you lose `cloudflare_ci_service_token_secret` later"
callout for the full `terraform taint` + re-apply + secret-update
procedure.

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

**`docker compose run --rm backend-<slot> alembic upgrade head` hangs**
(`scripts/blue-green-deploy.sh` step [1/6], or the first-boot migration in
`cloud-init.yaml`) — `db` probably isn't healthy yet. Check: `docker
compose -f docker-compose.vm.yml ps` — `db` should show `(healthy)`. If
not, `docker compose logs db`.

**System Backups panel shows "Backup failed: Port could not be cast to
integer value as '\<random-looking fragment\>'"** — fixed (see
`docker-compose.vm.yml`'s `DATABASE_URL` comment on the `backend-blue`/
`backend-green` services for the full root-cause writeup); a pre-fix VM
still needs the corrected `DATABASE_URL` pushed to it once: re-run
**Actions → Sync secrets to Azure VM** (`sync-secrets-vm.yml`) — no secret
values actually need to change, this workflow always recomputes
`DATABASE_URL` from the current `POSTGRES_USER`/`POSTGRES_PASSWORD` on
every run — then confirm `docker compose -f
/opt/snipeit/docker-compose.vm.yml exec "backend-$(grep ^ACTIVE_SLOT=
/opt/snipeit/.env | cut -d= -f2)" env | grep ^DATABASE_URL=` on the VM
shows a `%`-encoded password (e.g. `%2B` for a literal `+`) rather than a
raw one. The Container Apps path (`DEPLOYMENT.md`) was never affected --
`infra/main.bicep` already percent-encodes the password with
`uriComponent()`.

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
   later `terraform apply` recreated the Tunnel resource (cloud-init
   only writes this token in ONCE, at VM creation; it's never refreshed
   automatically after that). The Zero Trust dashboard's **Networks →
   Tunnels → this tunnel → Overview** tab confirms it fast: `Status:
   Inactive` with an empty Connectors table means this is exactly what's
   happening, and every `ssh`/`scp` through the Tunnel will fail
   identically (e.g. as a generic `remote error: tls: handshake
   failure`, regardless of how far downstream the CI step actually is)
   since there's no live connection to route through at all. Fix it by
   re-running `sync-secrets-vm.yml` to push the current token -- **but
   note that workflow also connects over this same Tunnel, so it can't
   help while the Tunnel itself is down.**

   With the Tunnel down, run **`repair-tunnel-token-vm.yml`** instead
   (`workflow_dispatch`, pick the same `environment`) -- it reads the
   current live token straight out of Terraform state and pushes it via
   `az vm run-command invoke`, which goes over Azure's own VM Agent
   control-plane channel rather than SSH/the Tunnel, so it works
   regardless of Tunnel state without you touching the VM by hand at
   all. It restarts just the `cloudflared` container once the token's
   written.

   No CI access, or want to see exactly what it's doing / do it
   yourself? Same repair, by hand, over the Azure Serial Console (VM →
   Support + troubleshooting → Serial console -- no network path, so it
   also works regardless of Tunnel state):
   ```bash
   # 1. On your machine, in infra-vm/, get the current live token:
   terraform output -raw cloudflare_tunnel_token
   # 2. In the Serial Console, on the VM:
   sudo nano /opt/snipeit/.env   # update the CLOUDFLARE_TUNNEL_TOKEN= line
   cd /opt/snipeit && docker compose -f docker-compose.vm.yml up -d cloudflared
   ```
   Either way, confirm the fix in the dashboard -- `Status` should flip
   to `Active` with one connector listed -- before retrying any CI
   deploy.
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

**Same `tls: handshake failure` as below, but `docker`/`docker compose`
turn out to be missing entirely on the VM** (check via Serial Console:
`which docker; dpkg -l | grep -i docker`) — this is a different problem
from the cloudflared-client-version one right below: cloud-init itself
never actually finished on first boot, so `cloudflared` never started at
all (rather than starting and later losing its connection). A common
cause: cloud-init concatenates every `runcmd` entry into one script and
runs it with `#!/bin/sh` (dash on Ubuntu), which doesn't support `set -o
pipefail` — if a `runcmd` block used it inline, dash aborts the *entire*
combined script right there, silently skipping every step after it
(disk mount, Docker install, everything). Confirm with `bash -n
/var/lib/cloud/instance/scripts/runcmd` and `cat
/var/log/cloud-init-output.log` over the Serial Console. Fix: give any
such `runcmd` block its own file with a real `#!/usr/bin/env bash`
shebang (written via `write_files`, same pattern `create-swap.sh` in
`infra-vm/cloud-init.yaml` already uses) and call it by path from
`runcmd` instead of inlining it — then see [Rebuilding just the
VM](#rebuilding-just-the-vm-recovering-from-a-broken-first-boot) above to
get a genuine first boot with the fix in place.

**CI's `deploy-azure-vm.yml`/`sync-secrets-vm.yml` fails at "Sync
docker-compose.vm.yml + Caddyfile to the VM" (or any other `ssh`/`scp`
step) with `remote error: tls: handshake failure`, followed by
`Connection closed by UNKNOWN port 65535`/`scp: Connection closed`** —
this is not a Tunnel, Access, or NSG problem; the Tunnel/Access
config in `main.tf` is correct as-is. It's the *client-side*
`cloudflared` binary these workflows install fresh on every run (the
"Install cloudflared" step): `cloudflared` 2026.6.0 shipped a
regression ([cloudflare/cloudflared#1673](https://github.com/cloudflare/cloudflared/issues/1673))
where `access ssh`/`access tcp` silently ignore
`--service-token-id`/`--service-token-secret` and fall through to an
interactive browser-auth flow on every connection attempt instead — a
non-interactive GitHub Actions runner can never complete that flow, so
the `ProxyCommand` process dies immediately, and `ssh`/`scp` surface
that death as the generic `remote error: tls: handshake failure`
rather than anything mentioning Access or browser auth. Both
"Install cloudflared" steps pin `CLOUDFLARED_VERSION: '2026.5.1'` (the
last release confirmed to honor service-token auth correctly) instead
of pulling `cloudflared/releases/latest` for exactly this reason — if
you've reverted that pin, or the failure comes back on `2026.5.1`
itself, re-check the linked issue for a fixed release, bump
`CLOUDFLARED_VERSION` to it in both workflow files, and confirm a real
CI deploy succeeds before trusting the bump.

**Locked out of the SSH hostname by Cloudflare Access itself** (not a
Tunnel problem — the Tunnel's up, but Access rejects you) — either your
email isn't in `SSH_ACCESS_ALLOWED_EMAILS` (add it, re-apply Terraform),
or the CI service token was rotated/deleted and `CF_ACCESS_CLIENT_ID`/
`_SECRET` in GitHub are stale (re-copy the current
`cloudflare_ci_service_token_id`/`_secret` Terraform outputs). Either way,
the two break-glass options above still work regardless of Access's own
state, since they don't go through the Tunnel at all.
