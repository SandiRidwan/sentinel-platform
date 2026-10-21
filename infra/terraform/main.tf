terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # No backend block: local state is Terraform's default, and declaring
  # `backend "local" {}` explicitly makes `init -backend=false` and `plan` disagree about
  # whether a backend change happened. Production adds a remote backend here.
}

locals {
  name = "${var.project}-${var.environment}"

  # One switch for the whole cloud surface. Everything below is counted on this, so
  # `terraform plan` with enable_cloud_resources=false exercises the configuration without
  # creating anything and without needing credentials.
  create = var.enable_cloud_resources ? 1 : 0

  create_bigquery  = var.enable_cloud_resources && var.gcp_project != "" ? 1 : 0
  create_snowflake = var.enable_cloud_resources && var.snowflake_account != "" ? 1 : 0

  buckets = {
    raw    = "${local.name}-raw"
    bronze = "${local.name}-bronze"
    silver = "${local.name}-silver"
    gold   = "${local.name}-gold"
  }

  common_tags = merge(var.tags, {
    environment = var.environment
    cost_centre = "data-platform"
  })
}

provider "aws" {
  region = var.aws_region

  # Offline plan support. With enable_cloud_resources = false nothing is created, but the
  # provider still validates credentials at plan time by default and would reach for the EC2
  # instance metadata endpoint. These flags make the plan a pure configuration check, which
  # is what CI needs: a pull request from a fork has no credentials and must still be able
  # to prove the configuration is sound.
  skip_credentials_validation = !var.enable_cloud_resources
  skip_requesting_account_id  = !var.enable_cloud_resources
  skip_metadata_api_check     = !var.enable_cloud_resources
  skip_region_validation      = !var.enable_cloud_resources
  access_key                  = var.enable_cloud_resources ? null : "offline-plan"
  secret_key                  = var.enable_cloud_resources ? null : "offline-plan"

  default_tags {
    tags = local.common_tags
  }
}

provider "google" {
  project = var.gcp_project != "" ? var.gcp_project : "sentinel-unset"
  region  = "US"
}

# No root `provider "snowflake"` block on purpose.
#
# Terraform configures a declared provider even when every resource using it has count = 0,
# and the Snowflake provider authenticates during configuration. With the block present,
# `terraform plan` with enable_cloud_resources = false still tried to log in to
# sentinel-unset.snowflakecomputing.com and failed. Leaving it out means the default plan
# needs no Snowflake account; enabling the Snowflake target means adding this block with
# real credentials, which is a deliberate act rather than an accident.

# ---------------------------------------------------------------- lake ----

module "lake" {
  source = "./modules/lake"
  count  = local.create

  buckets       = local.buckets
  raw_days      = var.raw_retention_days
  gold_days     = var.gold_retention_days
  tags          = local.common_tags
  force_destroy = var.environment == "dev"
}

# ------------------------------------------------------------ warehouse ----

module "warehouse" {
  source = "./modules/warehouse"
  count  = local.create_bigquery

  project  = var.gcp_project
  dataset  = "sentinel"
  location = "US"
  tags     = local.common_tags
}


# ---------------------------------------------------------------- cost ----
#
# A budget without an alert is a number nobody reads. This is the alert.

resource "aws_budgets_budget" "monthly" {
  count = local.create

  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["data-platform@example.com"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = ["data-platform@example.com"]
  }
}
