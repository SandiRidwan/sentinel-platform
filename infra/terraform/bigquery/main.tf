# SENTINEL BigQuery target — a separate stack, for the same reason as Snowflake.
#
# Terraform configures a declared provider even when every resource using it has
# `count = 0`, and the Google provider resolves credentials during configuration. With the
# provider block in the root stack, `terraform plan` with `enable_cloud_resources = false`
# still reached for Application Default Credentials and failed on any machine without a
# `gcloud auth application-default login`. That made the root plan's "no credentials
# needed" claim true only on a developer machine that happened to be authenticated, which
# is the kind of claim this repository exists to avoid.
#
# Splitting the stack makes the root plan genuinely credential-free:
#
#   cd infra/terraform            && terraform plan      # no credentials, no network auth
#   cd infra/terraform/bigquery   && terraform plan -var="project=<gcp-project>"
#
# ADR 0010 records the reasoning; the Snowflake stack follows the identical pattern.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project" {
  description = "GCP project that will hold the dataset."
  type        = string
}

variable "dataset" {
  description = "BigQuery dataset name."
  type        = string
  default     = "sentinel"
}

variable "location" {
  description = "BigQuery dataset location."
  type        = string
  default     = "US"
}

variable "tags" {
  description = "Labels applied to every resource."
  type        = map(string)
  default = {
    project     = "sentinel"
    environment = "dev"
  }
}

provider "google" {
  project = var.project
  region  = "US"
}

module "warehouse" {
  source = "../modules/warehouse"

  project  = var.project
  dataset  = var.dataset
  location = var.location
  tags     = var.tags
}

output "dataset_id" {
  value = module.warehouse.dataset_id
}

output "policy_tags" {
  value = module.warehouse.policy_tags
}
