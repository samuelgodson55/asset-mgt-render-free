# Fresh VM deployment: exact current procedure




I'll assume you're creating **production**. For staging, replace `prod` with `vm-staging` where indicated.

---

## Table of Contents

- [Fresh VM deployment: exact current procedure](#fresh-vm-deployment-exact-current-procedure)
- [Phase 1: Prepare your local machine](#phase-1-prepare-your-local-machine)
- [Phase 2: Create the GitHub Environment](#phase-2-create-the-github-environment)
- [Phase 3: Generate the VM SSH key](#phase-3-generate-the-vm-ssh-key)
- [Phase 4: Prepare Cloudflare](#phase-4-prepare-cloudflare)
  - [4A. Cloudflare API token](#4a-cloudflare-api-token)
  - [4B. Cloudflare account and zone IDs](#4b-cloudflare-account-and-zone-ids)
- [Phase 5: Cloudflare Origin Certificate](#phase-5-cloudflare-origin-certificate)
- [Phase 6: Set the application domain](#phase-6-set-the-application-domain)
- [Phase 7: Tell the VM which people can SSH](#phase-7-tell-the-vm-which-people-can-ssh)
- [Phase 8: Generate the database/application secrets](#phase-8-generate-the-databaseapplication-secrets)
- [Phase 9: Docker Hub configuration](#phase-9-docker-hub-configuration)
- [Important](#important)
- [Phase 10: One-time Azure/GitHub bootstrap](#phase-10-one-time-azuregithub-bootstrap)
- [You do NOT manually create the Terraform state storage account.](#you-do-not-manually-create-the-terraform-state-storage-account)
- [Phase 11: Create `GH_ADMIN_TOKEN`](#phase-11-create-gh_admin_token)
- [Phase 12: Check the production Environment](#phase-12-check-the-production-environment)
- [Secrets](#secrets)
- [Variables](#variables)
- [Phase 13: First Terraform deployment](#phase-13-first-terraform-deployment)
- [Phase 14: Review the plan](#phase-14-review-the-plan)
- [Phase 15: Apply](#phase-15-apply)
- [Phase 16: Automatic GitHub synchronization](#phase-16-automatic-github-synchronization)
- [Phase 17: Find your SSH command](#phase-17-find-your-ssh-command)
- [Phase 18: Configure your local SSH client](#phase-18-configure-your-local-ssh-client)
- [Phase 19: Verify the VM manually](#phase-19-verify-the-vm-manually)
- [Phase 20: Deploy the actual current application](#phase-20-deploy-the-actual-current-application)
- [Phase 21: Review the website](#phase-21-review-the-website)
- [1. Website loads](#1-website-loads)
- [2. Login works](#2-login-works)
- [3. Backend API responds](#3-backend-api-responds)
- [4. Database works](#4-database-works)
- [5. Redis/worker work](#5-redisworker-work)
- [6. Email](#6-email)
- [7. SSH](#7-ssh)
- [8. Deployment dashboard](#8-deployment-dashboard)
- [The entire fresh deployment in one flow](#the-entire-fresh-deployment-in-one-flow)
  - [The most important changes from the old procedure](#the-most-important-changes-from-the-old-procedure)

# Phase 1: Prepare your local machine

You need:

```text
Azure CLI
Git
GitHub CLI (gh)
OpenSSH / ssh-keygen
OpenSSL
```

You're already using Git Bash, so you're good.

Clone the repository and enter it:

```bash
git clone https://github.com/samuelgodson55/asset-mgt-render-free.git
cd asset-mgt-render-free
git checkout develop
```

Make sure you're authenticated:

```bash
az login
gh auth login
```

Confirm Azure:

```bash
az account show --query "{subscription:id,tenant:tenantId,name:name}" -o table
```

Set the correct subscription if necessary:

```bash
az account set --subscription "<YOUR_SUBSCRIPTION_ID>"
```

Confirm GitHub:

```bash
gh repo view --json nameWithOwner
```

It should show:

```text
samuelgodson55/asset-mgt-render-free
```

---

# Phase 2: Create the GitHub Environment

For production, the GitHub Environment must be:

```text
prod
```

Create/reconcile it:

```bash
gh api --method PUT "repos/samuelgodson55/asset-mgt-render-free/environments/prod"
```

For staging:

```bash
gh api --method PUT "repos/samuelgodson55/asset-mgt-render-free/environments/vm-staging"
```

**Do not call the VM staging environment `staging`.**

The code deliberately distinguishes:

```text
GitHub Environment:
vm-staging

Terraform environment_name:
staging
```

For production:

```text
GitHub Environment:
prod

Terraform environment_name:
prod
```

The workflow performs that mapping automatically. 

---

# Phase 3: Generate the VM SSH key

This is the key you'll use to access the VM.

Generate an RSA key pair:

```bash
ssh-keygen -t rsa -b 4096 -C "snipeit-lite-vm-deploy" -f ./snipeit_vm_deploy_key -N ""
```

You now have:

```text
snipeit_vm_deploy_key
snipeit_vm_deploy_key.pub
```

**Never commit either file.**

The private key becomes a GitHub Environment secret:

```bash
gh secret set VM_SSH_PRIVATE_KEY --env prod --body-file ./snipeit_vm_deploy_key
```

The public key becomes:

```bash
gh secret set VM_SSH_PUBLIC_KEY --env prod --body-file ./snipeit_vm_deploy_key.pub
```

The Terraform VM resource disables password authentication and uses this SSH public key. 

---

# Phase 4: Prepare Cloudflare

Your domain must already be inside the Cloudflare zone.

For your current production setup, this is essentially:

```text
Cloudflare zone:
multione.online

Application:
assets.multione.online

SSH:
ssh-assets.multione.online
```

The Terraform code creates the DNS records itself. **Do not manually create the application or SSH CNAME records.**

Terraform creates:

```text
assets.multione.online
        ↓
Cloudflare Tunnel
        ↓
Caddy
        ↓
VM

ssh-assets.multione.online
        ↓
Cloudflare Tunnel
        ↓
sshd
        ↓
VM
```

The Tunnel handles both HTTP and SSH through outbound connections from the VM. No inbound 22/80/443 NSG rule is required. 

---

## 4A. Cloudflare API token

Your Cloudflare API token needs the permissions required by the Terraform resources:

```text
Account:
  Cloudflare Tunnel → Edit
  Access: Apps and Policies → Edit

Zone:
  DNS → Edit
```

Set it:

```bash
gh secret set CLOUDFLARE_API_TOKEN --env prod
```

It will prompt you for the value.

Or:

```bash
gh secret set CLOUDFLARE_API_TOKEN --env prod --body "<TOKEN>"
```

---

## 4B. Cloudflare account and zone IDs

Set:

```bash
gh secret set CLOUDFLARE_ACCOUNT_ID --env prod --body "<CLOUDFLARE_ACCOUNT_ID>"
```

```bash
gh secret set CLOUDFLARE_ZONE_ID --env prod --body "<CLOUDFLARE_ZONE_ID>"
```

And the zone name is a **GitHub Environment variable**, not a secret:

```bash
gh variable set CLOUDFLARE_ZONE_NAME --env prod --body "multione.online"
```

---

# Phase 5: Cloudflare Origin Certificate

The VM's Caddy uses a Cloudflare Origin CA certificate.

Create the Origin CA certificate in Cloudflare for:

```text
assets.multione.online
*.multione.online
```

Then save the certificate and private key somewhere temporarily, for example:

```text
origin-cert.pem
origin-cert-key.pem
```

Upload them directly to GitHub Environment secrets:

```bash
gh secret set CLOUDFLARE_ORIGIN_CERT --env prod --body-file ./origin-cert.pem
```

```bash
gh secret set CLOUDFLARE_ORIGIN_CERT_KEY --env prod --body-file ./origin-cert-key.pem
```

Then remove your local copies when you're finished if you don't need them.

---

# Phase 6: Set the application domain

This is a **GitHub Environment variable**:

```bash
gh variable set CUSTOM_DOMAIN --env prod --body "assets.multione.online"
```

This is important because Terraform validates that the custom domain belongs to the configured Cloudflare zone. 

---

# Phase 7: Tell the VM which people can SSH

Set the Cloudflare Access allowed email addresses.

For example:

```bash
gh secret set SSH_ACCESS_ALLOWED_EMAILS --env prod --body '["your-email@example.com"]'
```

If you have multiple authorized people:

```bash
gh secret set SSH_ACCESS_ALLOWED_EMAILS --env prod --body '["you@example.com","admin@example.com"]'
```

This is what protects:

```text
ssh-assets.multione.online
```

Cloudflare Access authenticates humans using these addresses. CI uses a separate Cloudflare service token generated by Terraform. 

---

# Phase 8: Generate the database/application secrets

Generate the PostgreSQL password:

```bash
POSTGRES_PASSWORD="$(openssl rand -base64 24)"
```

Store it:

```bash
gh secret set POSTGRES_PASSWORD --env prod --body "$POSTGRES_PASSWORD"
```

Generate JWT secret:

```bash
JWT_SECRET_KEY="$(openssl rand -hex 32)"
```

Store it:

```bash
gh secret set JWT_SECRET_KEY --env prod --body "$JWT_SECRET_KEY"
```

You can set your initial super-admin password explicitly:

```bash
gh secret set ROOT_ADMIN_BOOTSTRAP_PASSWORD --env prod --body "<STRONG_INITIAL_PASSWORD>"
```

Or leave it unset and the application can generate one during first initialization.

The current Terraform variables make PostgreSQL password and JWT secret mandatory, while the root-admin bootstrap password is optional. 

---

# Phase 9: Docker Hub configuration

This is important for a **fresh VM**.

The Terraform infrastructure workflow verifies that the initial images actually exist on Docker Hub **before it applies**.

For production with the current default React frontend, the initial images are:

```text
<DOCKERHUB_USERNAME>/snipeit-lite-backend:latest
<DOCKERHUB_USERNAME>/snipeit-lite-frontend-react:latest
```

The workflow explicitly defaults the frontend build target to `react`. 

Set:

```bash
gh secret set DOCKERHUB_USERNAME --env prod --body "<YOUR_DOCKERHUB_USERNAME>"
```

And:

```bash
gh secret set DOCKERHUB_TOKEN --env prod --body "<YOUR_DOCKERHUB_TOKEN>"
```

If your repositories are public, the VM itself doesn't need the Docker Hub credentials, but the deployment workflow uses them for building/pushing images.

### Important

Before the **first Terraform apply**, these images must already exist:

```text
snipeit-lite-backend:latest
snipeit-lite-frontend-react:latest
```

If they don't exist yet, build/push an initial image first. The infrastructure workflow deliberately refuses to provision a VM with dangling application image references. 

After the VM exists, the normal **Deploy to VM** workflow can build fresh images itself.

---

# Phase 10: One-time Azure/GitHub bootstrap

Now we're finally at the part you specifically asked about: the initial CLI bootstrap.

From the repository root:

```bash
./scripts/bootstrap-azure-github.sh prod
```

That's the **one-time privileged bootstrap**.

It does several things automatically:

```text
Azure
 │
 ├── Registers required Azure providers
 ├── Creates/reuses Entra application
 ├── Creates/reuses service principal
 ├── Grants subscription Contributor
 ├── Creates GitHub OIDC federation
 ├── Creates/reuses Terraform state resource group
 ├── Creates/reuses Terraform state storage account
 ├── Creates vm-state container
 └── Grants Storage Blob Data Contributor
       │
       ↓
GitHub prod Environment
 ├── AZURE_CLIENT_ID
 ├── AZURE_TENANT_ID
 └── AZURE_SUBSCRIPTION_ID
```

This is exactly what the current script does. 

And importantly:

### You do NOT manually create the Terraform state storage account.

The current bootstrap handles that.

This fixes the earlier mess where we had to deal with:

```text
ContainerNotFound
403 AuthorizationPermissionMismatch
```

The state bootstrap now explicitly reconciles the container before Terraform initialization. 

---

# Phase 11: Create `GH_ADMIN_TOKEN`

You've already done this part.

It belongs here:

```text
Repository secret
GH_ADMIN_TOKEN
```

**Not**:

```text
prod → GH_ADMIN_TOKEN
```

It needs:

```text
Repository:
asset-mgt-render-free

Permission:
Environments → Read and write
```

This is what lets the Terraform workflow automatically write the generated connection information back into GitHub.

---

# Phase 12: Check the production Environment

Before Terraform, I would verify the critical values:

```bash
gh secret list --env prod
```

And:

```bash
gh variable list --env prod
```

You should have at least:

### Secrets

```text
AZURE_CLIENT_ID
AZURE_TENANT_ID
AZURE_SUBSCRIPTION_ID

VM_SSH_PUBLIC_KEY
VM_SSH_PRIVATE_KEY

CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_ZONE_ID
CLOUDFLARE_ORIGIN_CERT
CLOUDFLARE_ORIGIN_CERT_KEY
SSH_ACCESS_ALLOWED_EMAILS

POSTGRES_PASSWORD
JWT_SECRET_KEY
ROOT_ADMIN_BOOTSTRAP_PASSWORD

DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

### Variables

```text
AZURE_LOCATION
CLOUDFLARE_ZONE_NAME
CUSTOM_DOMAIN
FRONTEND_BUILD_TARGET
```

For your current production deployment:

```text
AZURE_LOCATION=e.g. southafricanorth
CLOUDFLARE_ZONE_NAME=multione.online
CUSTOM_DOMAIN=assets.multione.online
FRONTEND_BUILD_TARGET=react
```

You can omit most other optional variables because the Terraform workflow supplies their defaults.

---

# Phase 13: First Terraform deployment

Now go to:

**GitHub → Actions → Deploy VM Infrastructure (Terraform)**

Run:

```text
environment: prod
action: plan
```

The workflow automatically:

```text
GitHub OIDC
     ↓
Azure login
     ↓
Register Azure providers
     ↓
Bootstrap/reuse Terraform state
     ↓
terraform init
     ↓
Check whether VM already exists
     ↓
Determine initial images
     ↓
Verify Docker Hub images
     ↓
Validate Cloudflare
     ↓
terraform plan
```

The state is:

```text
rg-snipeit-tfstate
    │
    └── snipeittfstate...
          │
          └── vm-state
                ├── prod.tfstate
                └── vm-staging.tfstate
```

The two environments therefore share the backend but have separate state files. 

---

# Phase 14: Review the plan

For a genuinely fresh production VM you should expect Terraform to create things such as:

```text
Resource Group
VNet
Subnet
NSG
Public IP
NIC
Linux VM
OS disk
Data disk
Disk attachment
Cloudflare Tunnel
Cloudflare Tunnel configuration
Cloudflare DNS records
Cloudflare Access application
Cloudflare Access policies
Cloudflare CI service token
Backup/snapshot resources
```

The VM gets:

```text
Ubuntu
Docker
Docker Compose
PostgreSQL
Redis
Backend
Worker
Beat
Frontend
Caddy
cloudflared
```

through first-boot cloud-init.

The VM's Docker data root is placed on:

```text
/mnt/docker-data/docker
```

and the separate managed data disk is mounted there during first boot. 

---

# Phase 15: Apply

If the plan is correct:

**GitHub Actions → Re-run / Run workflow**

```text
environment: prod
action: apply
```

Terraform creates the VM.

Cloud-init then automatically:

```text
Format data disk
        ↓
Mount /mnt/docker-data
        ↓
Install Docker
        ↓
Configure Docker data-root
        ↓
Install fail2ban
        ↓
Configure UFW
        ↓
Create /opt/snipeit
        ↓
Pull initial images
        ↓
Start systemd snipeit.service
        ↓
Run Alembic migrations
        ↓
Seed root admin
```

The first boot is therefore not an empty VM waiting for you to SSH in. The code is explicitly designed to bring up the application stack during cloud-init. 

---

# Phase 16: Automatic GitHub synchronization

This is the new important part.

After successful Terraform Apply, the workflow reads the **actual Terraform outputs from the state** and automatically writes them to the selected GitHub Environment. 

It automatically creates/updates these **secrets**:

```text
VM_HOST
VM_SSH_USER
CF_ACCESS_CLIENT_ID
CF_ACCESS_CLIENT_SECRET
CLOUDFLARE_TUNNEL_TOKEN
```

And these **Environment variables**:

```text
VM_HOST
VM_SSH_COMMAND
VM_SSH_COMMAND_BREAK_GLASS
VM_PUBLIC_IP
VM_AZURE_FQDN
APP_URL
```

So you no longer need to hunt through Terraform logs for:

```text
cloudflare_ci_service_token_id
cloudflare_ci_service_token_secret
```

or:

```text
ssh_command
ssh_hostname
```

The workflow gets them directly from Terraform state and writes them to GitHub.

---

# Phase 17: Find your SSH command

After Apply finishes:

Go to:

**GitHub → Settings → Environments → prod → Variables**

You'll see:

```text
VM_SSH_COMMAND
```

It will look like:

```bash
ssh -i ./snipeit_vm_deploy_key azureuser@ssh-assets.multione.online
```

The Terraform output itself is generated from:

```text
admin_username
+
ssh.<custom_domain>
```

The current output is explicitly non-sensitive. 

---

# Phase 18: Configure your local SSH client

You need `cloudflared` installed locally because normal SSH goes through Cloudflare Access.

Then add this to:

```text
~/.ssh/config
```

For your current production example:

```sshconfig
Host ssh-assets.multione.online
    StrictHostKeyChecking accept-new
    ProxyCommand cloudflared access ssh --hostname %h
```

However, because **human SSH is Cloudflare Access authenticated**, your first connection should trigger the Cloudflare Access identity flow.

Then:

```bash
ssh -i ./snipeit_vm_deploy_key azureuser@ssh-assets.multione.online
```

The important point is:

**Do not SSH directly to the public IP during normal operation.**

The public IP is a break-glass path only. The default NSG has no inbound SSH rule. 

---

# Phase 19: Verify the VM manually

Once SSH works:

```bash
ssh -i ./snipeit_vm_deploy_key azureuser@ssh-assets.multione.online
```

Then:

```bash
cd /opt/snipeit
```

Check containers:

```bash
docker compose -f docker-compose.vm.yml ps
```

You should see the active stack running.

Check Docker:

```bash
docker info | grep "Docker Root Dir"
```

It should show:

```text
Docker Root Dir: /mnt/docker-data/docker
```

Check the application configuration:

```bash
grep -E '^(DOMAIN|ENVIRONMENT|ACTIVE_SLOT|COMPOSE_PROFILES|IMAGE_TAG)=' /opt/snipeit/.env
```

Check cloudflared:

```bash
docker compose -f docker-compose.vm.yml logs --tail 100 cloudflared
```

You want the Tunnel connector to be healthy.

---

# Phase 20: Deploy the actual current application

This is an important distinction.

**Terraform creates the infrastructure and bootstraps a working initial stack.**

The actual application CI/CD deployment is:

**GitHub → Actions → Deploy to VM**

For your first real application deployment:

```text
environment:
prod

image_tag:
<leave blank>

frontend_type:
(environment default)

skip_migrate:
false
```

Leaving `image_tag` blank means the workflow builds fresh images from the selected branch rather than deliberately reusing an old Docker image. The current workflow is explicitly designed this way. 

It then:

```text
Build backend
Build React frontend
        ↓
Push Docker images
        ↓
SSH through Cloudflare
        ↓
Sync docker-compose.vm.yml
Sync Caddyfile
Sync deployment scripts
        ↓
Pull images
        ↓
Run migration
        ↓
Deploy blue slot
        ↓
Health check
        ↓
Gradually move traffic
        ↓
Retire old slot
        ↓
External smoke test
```

That is the application deployment, separate from Terraform.

---

# Phase 21: Review the website

Terraform gives you:

```text
APP_URL
```

The automatic GitHub Environment variable will contain:

```text
APP_URL=https://assets.multione.online
```

Open:

**[https://assets.multione.online](https://assets.multione.online)**

Then verify:

### 1. Website loads

```text
https://assets.multione.online
```

### 2. Login works

Use the root admin credentials you configured/generated during bootstrap.

### 3. Backend API responds

The application should be able to communicate with the backend.

### 4. Database works

Create/login to an account and confirm data persists.

### 5. Redis/worker work

Trigger functionality that uses background processing.

### 6. Email

If notifications are enabled, test a notification/password-reset flow.

### 7. SSH

From your local machine:

```bash
ssh -i ./snipeit_vm_deploy_key azureuser@ssh-assets.multione.online
```

### 8. Deployment dashboard

The VM also has:

```text
/_deploy/
```

protected by the configured Basic Auth credentials.

---

# The entire fresh deployment in one flow

This is the sequence I would actually follow for a **brand-new production VM today**:

```text
1. Clone repo
       ↓
2. az login
       ↓
3. gh auth login
       ↓
4. Create GitHub Environment: prod
       ↓
5. Generate RSA SSH key
       ↓
6. Configure Cloudflare API/domain/origin certificate
       ↓
7. Configure required application secrets
       ↓
8. Configure Docker Hub credentials
       ↓
9. Ensure initial Docker images exist
       ↓
10. Run:
       ./scripts/bootstrap-azure-github.sh prod
       ↓
11. Add GH_ADMIN_TOKEN as repository secret
       ↓
12. GitHub Actions:
       Deploy VM Infrastructure
       environment = prod
       action = plan
       ↓
13. Review Terraform plan
       ↓
14. action = apply
       ↓
15. Azure creates VM
       ↓
16. cloud-init installs Docker + application
       ↓
17. Terraform creates Cloudflare Tunnel + Access
       ↓
18. GitHub automatically receives:
       VM_SSH_COMMAND
       VM_HOST
       CF_ACCESS_CLIENT_ID
       CF_ACCESS_CLIENT_SECRET
       CLOUDFLARE_TUNNEL_TOKEN
       ↓
19. Configure local Cloudflare SSH access
       ↓
20. SSH into VM
       ↓
21. Verify Docker/services/Tunnel
       ↓
22. GitHub Actions:
       Deploy to VM
       environment = prod
       image_tag = blank
       frontend_type = environment default
       skip_migrate = false
       ↓
23. Blue-green deployment
       ↓
24. Open:
       https://assets.multione.online
       ↓
25. Test login/application/database/email
```

## The most important changes from the old procedure

You **do not** need to:

* manually create `rg-snipeit-tfstate`
* manually create the Storage Account
* manually create `vm-state`
* manually configure Terraform backend variables
* manually copy Terraform's Cloudflare service-token values
* manually copy `ssh_hostname`
* manually copy `ssh_command`
* upload a VM connection artifact
* expose port 22 to the internet
* install Tailscale
* manually install Docker on the VM
* manually copy `docker-compose.vm.yml` for the first boot
* manually run the initial database migration

The current code automates those pieces. The one genuinely manual bootstrap credential is **`GH_ADMIN_TOKEN`**, and the one credential that intentionally cannot be generated by Terraform is your **SSH private key**, because you need to retain the private half locally.

Also, **protect the Terraform state**. The AzureRM provider stores VM credentials and other sensitive infrastructure values in state, and HashiCorp explicitly warns that state can contain sensitive values. ([Terraform Registry][1])
