terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
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

# No root `provider "google"` block, and no BigQuery resources, on purpose.
#
# The Google provider resolves credentials during configuration, so declaring it here made
# `terraform plan` reach for Application Default Credentials even with
# enable_cloud_resources = false and every resource at count = 0. The plan therefore only
# worked on a machine that had run `gcloud auth application-default login` — which is not
# "no credentials needed", it is "credentials needed but already present".
#
# The BigQuery dataset and its policy tags now live in their own stack at
# infra/terraform/bigquery, the same way Snowflake does. The root plan is a pure
# configuration check: no provider in it authenticates.
#
# No root `provider "snowflake"` block either, for the identical reason. Terraform
# configures a declared provider even when every resource using it has count = 0, and the
# Snowflake provider authenticates during configuration; with the block present, the
# default plan tried to log in to sentinel-unset.snowflakecomputing.com and failed.

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
#
# The BigQuery dataset moved to infra/terraform/bigquery. See the note above about the
# Google provider authenticating at configuration time.


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
