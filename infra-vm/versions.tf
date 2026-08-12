# =============================================================================
# infra-vm/versions.tf
# -----------------------------------------------------------------------------
# Pins Terraform itself and every provider used by this stack. Keeping this
# pinned (not just "latest") is what makes `terraform init` reproducible
# across your machine, a teammate's machine, and the infra-deploy-vm.yml
# GitHub Actions runner -- an unpinned provider can silently pick up a new
# major version between runs and change resource schemas underneath you.
# =============================================================================

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    azurerm    = {
      source  = "hashicorp/azurerm"
      version = "~> 3.116"
    }
    tls        = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random     = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time       = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      # Cloudflare renames Zero Trust resources between major versions
      # (e.g. cloudflare_argo_tunnel -> cloudflare_tunnel ->
      # cloudflare_zero_trust_tunnel_cloudflared). This stack targets v4 --
      # v5 is a from-scratch rewrite that, as of this writing, has no
      # working way to read a tunnel's token at all (see
      # https://github.com/cloudflare/terraform-provider-cloudflare/issues/5009),
      # so v5 is explicitly excluded, matching Cloudflare's own "Deploy
      # Tunnels with Terraform" guide. The tunnel token is read directly
      # off the resource itself (see main.tf), not a separate data source.
      version = ">= 4.40.0, < 5.0.0"
    }
  }

  # ---------------------------------------------------------------------
  # Remote state -- REQUIRED. The GitHub Actions workflow creates the
  # dedicated state resource group/storage account/container automatically
  # before `terraform init` (scripts/bootstrap-terraform-state.sh). Nothing
  # needs to be provisioned manually and the state backend intentionally
  # lives outside the VM resource group so `terraform destroy` cannot delete
  # the state it needs to perform the destroy.
  #
  # The backend is partial so the workflow can supply the resource group,
  # storage account, container and per-environment key at init time. This
  # keeps vm-staging and prod in separate state files while sharing one small
  # state account per subscription.
  backend "azurerm" {
    use_azuread_auth = true
  }
}

provider "azurerm" {
  features {
    resource_group {
      # Lets `terraform destroy` remove the resource group even if it still
      # contains a resource Terraform doesn't know about (e.g. something
      # created manually in the Portal for debugging). Safe here because
      # this resource group is dedicated to this app, not shared.
      prevent_deletion_if_contains_resources = false
    }
    virtual_machine {
      # Also delete the OS disk when the VM is destroyed -- otherwise
      # `terraform destroy` leaves an orphaned, still-billed managed disk
      # behind.
      delete_os_disk_on_deletion = true
    }
  }

  # No explicit auth attributes here on purpose -- the provider picks up
  # ARM_CLIENT_ID/ARM_TENANT_ID/ARM_SUBSCRIPTION_ID/ARM_USE_OIDC from the
  # environment automatically. In infra-deploy-vm.yml those come from
  # direct GitHub Actions OIDC federation (see that workflow's auth
  # env-var comment); locally, run `az login` first and it falls back to
  # that CLI session instead. No client secret is ever stored in this
  # repo or in Terraform state either way.
  subscription_id = var.subscription_id
}

# Cloudflare -- provisions the Tunnel, its DNS records, and the Access
# application/policy guarding SSH (see main.tf's "Cloudflare Tunnel"
# section). api_token needs Zone:DNS:Edit on the zone, plus
# Account:Cloudflare Tunnel:Edit and Account:Access:Apps and Policies:Edit
# -- see DEPLOYMENT_VM.md's "Set up Cloudflare Tunnel" section for the
# exact token permissions to select when creating it.
provider "cloudflare" {
  api_token   = var.cloudflare_api_token

  # Cloudflare's edge intermittently returns transient errors (most often
  # "HTTP 520, please try again later") when Terraform reads back
  # cloudflare_zero_trust_tunnel_cloudflared_config during plan/refresh --
  # a known flaky spot in Cloudflare's own API, not a config problem (see
  # e.g. github.com/cloudflare/terraform-provider-cloudflare/issues/2435).
  # The v4 provider line (only -- these three arguments were removed in
  # v5, see issue #5092) will automatically retry failed API calls with
  # backoff; the defaults (retries = 3, max_backoff = 30s) just aren't
  # generous enough to reliably ride out a 520. Bumped here instead of
  # relying on a manual "just re-run the job" every time this resource
  # gets touched.
  retries     = 12
  min_backoff = 2
  max_backoff = 60
}
