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
      # v4 resource names below -- if `terraform init` pulls a newer major
      # and `plan` complains about unknown resource types, check
      # https://registry.terraform.io/providers/cloudflare/cloudflare/latest/docs
      # for the current names before bumping this constraint.
      version = "~> 4.41"
    }
  }

  # ---------------------------------------------------------------------
  # Remote state -- deliberately left commented out. Terraform defaults to
  # a LOCAL state file (infra-vm/terraform.tfstate) if you never configure
  # a backend, which is fine for a single person experimenting, but two
  # people (or a human + infra-deploy-vm.yml) applying against local state
  # WILL eventually clobber each other's changes or lose track of what's
  # actually deployed.
  #
  # Before your first real (non-throwaway) deploy, create a small storage
  # account for state (one-time, do this manually, NOT via this same
  # Terraform config -- state can't reliably bootstrap its own backend):
  #
  #   az group create -n rg-snipeit-tfstate -l eastus
  #   az storage account create -n snipeitliteterraformstate \
  #     --resource-group rg-snipeit-tfstate --sku Standard_LRS \
  #     --min-tls-version TLS1_2 --allow-blob-public-access false
  #   az storage container create -n vm-state \
  #     --account-name snipeitliteterraformstate --auth-mode login
  #
  # Then uncomment the block below (storage account name must be globally
  # unique -- change it if the one above is taken), and run:
  #   terraform init -migrate-state
  #
  # backend "azurerm" {
  #   resource_group_name  = "rg-snipeit-tfstate"
  #   storage_account_name = "snipeitliteterraformstate"
  #   container_name       = "vm-state"
  #   key                  = "snipeit-lite-vm.tfstate"
  #   # use_azuread_auth = true   # recommended: auth via `az login`/OIDC,
  #                                # not a storage account access key
  # }
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
