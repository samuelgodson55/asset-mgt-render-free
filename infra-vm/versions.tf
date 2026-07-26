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
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.116"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      # Cloudflare renames Zero Trust resources fairly often between major
      # versions (e.g. cloudflare_argo_tunnel -> cloudflare_tunnel ->
      # cloudflare_zero_trust_tunnel_cloudflared). This stack targets the
      # v4 resource/data-source names below -- v5 is a from-scratch,
      # OpenAPI-generated rewrite that renames/removes several of them
      # (notably: as of this writing v5 has no working way to read a
      # tunnel's token at all -- see
      # https://github.com/cloudflare/terraform-provider-cloudflare/issues/5009)
      # so v5 is explicitly excluded below, matching Cloudflare's own
      # "Deploy Tunnels with Terraform" guide.
      #
      # BUG FIX (round 1): this used to be pinned to "~> 4.41", which --
      # because that's a two-part version, so Terraform's `~>` only floats
      # the PATCH digit -- locked to the single 4.41.x patch series for no
      # real reason. Widened here to the full v4 line (still excluding
      # v5.x) to track Cloudflare's own recommended range instead of an
      # arbitrarily narrow one. This alone did NOT fix the "Invalid data
      # source ... does not support data source
      # cloudflare_zero_trust_tunnel_cloudflared_token" validate error,
      # though -- see the note in main.tf next to the tunnel resource for
      # round 2, which found the actual cause: that data source has never
      # existed in any v4.x release (confirmed against the v4.52.8
      # provider source directly), only in v5. The tunnel token is read
      # off the resource itself in v4, not a separate data source.
      version = ">= 4.40.0, < 5.0.0"
    }
  }

  # ---------------------------------------------------------------------
  # Remote state -- REQUIRED, not optional. Terraform defaults to a LOCAL
  # state file (infra-vm/terraform.tfstate) if no backend is configured,
  # which is fine for a single person experimenting locally, but every
  # infra-deploy-vm.yml run happens on a fresh, throwaway GitHub Actions
  # runner -- its filesystem (and any local .tfstate written to it) is
  # deleted the moment the job ends, whether that job succeeded, failed
  # partway through, or was cancelled. Without a remote backend, the NEXT
  # run then starts from a totally empty state, sees the resource group
  # (and anything else the previous run managed to create before it
  # failed) already sitting in Azure, and errors with "A resource with
  # the ID ... already exists" -- the exact failure this backend fixes.
  # With a remote backend, Terraform writes state after every single
  # resource it touches (not just at the end of a successful apply), so
  # a run that fails on, say, resource #6 of 10 leaves state for
  # resources #1-5 safely in blob storage for the next run to pick up
  # from -- no more manually deleting the resource group to retry.
  #
  # This is a PARTIAL backend config on purpose -- resource_group_name/
  # storage_account_name/container_name/key are deliberately left out of
  # this file and supplied instead via `terraform init -backend-config=...`
  # flags (see infra-deploy-vm.yml's "terraform init" step), so the same
  # config can point vm-staging and prod at two different state files
  # (different `key`) without editing this file, and so no account-
  # specific storage account name is hardcoded into version control.
  #
  # One-time setup, before the very first real (non-throwaway) deploy,
  # create a small storage account for state manually (NOT via this same
  # Terraform config -- state can't reliably bootstrap its own backend):
  #
  #   az group create -n rg-snipeit-tfstate -l eastus
  #   az storage account create -n snipeitliteterraformstate \
  #     --resource-group rg-snipeit-tfstate --sku Standard_LRS \
  #     --min-tls-version TLS1_2 --allow-blob-public-access false
  #   az storage container create -n vm-state \
  #     --account-name snipeitliteterraformstate --auth-mode login
  #
  # Then set these as repo/environment Variables (not secrets -- none of
  # this is sensitive, it's just where state lives) for each of the
  # vm-staging/prod GitHub Environments: TF_STATE_RESOURCE_GROUP,
  # TF_STATE_STORAGE_ACCOUNT, TF_STATE_CONTAINER. See DEPLOYMENT_VM.md's
  # "One-time Azure setup" section for the full walkthrough, including
  # the one-off `terraform init -migrate-state` if you're moving an
  # existing local state file into this backend rather than starting
  # clean.
  #
  # use_azuread_auth = true below means auth flows through the same
  # `az login`/OIDC session azure/login@v3 already establishes in
  # infra-deploy-vm.yml (and your own `az login` locally) -- no storage
  # account access key is ever generated or stored anywhere.
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

  # `use_cli = true` (the default) authenticates using whatever `az login`
  # session is already active -- either your own (local `terraform apply`)
  # or the one azure/login@v2 establishes in infra-deploy-vm.yml via OIDC.
  # No client secret is ever stored in this repo or in Terraform state.
  subscription_id = var.subscription_id
}

# Cloudflare -- provisions the Tunnel, its DNS records, and the Access
# application/policy guarding SSH (see main.tf's "Cloudflare Tunnel"
# section). api_token needs Zone:DNS:Edit on the zone, plus
# Account:Cloudflare Tunnel:Edit and Account:Access:Apps and Policies:Edit
# -- see DEPLOYMENT_VM.md's "Set up Cloudflare Tunnel" section for the
# exact token permissions to select when creating it.
provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
