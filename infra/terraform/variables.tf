variable "project" {
  description = "Short project name used as a prefix on every resource."
  type        = string
  default     = "sentinel"
}

variable "environment" {
  description = "Environment name: dev, staging, prod."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, prod."
  }
}

variable "aws_region" {
  description = "AWS region for the lake buckets."
  type        = string
  default     = "us-east-1"
}

variable "gcp_project" {
  description = "GCP project for BigQuery. Empty means the BigQuery resources are not created."
  type        = string
  default     = ""
}

variable "snowflake_account" {
  description = "Snowflake account identifier. Empty means the Snowflake resources are not created."
  type        = string
  default     = ""
}

variable "enable_cloud_resources" {
  description = <<-EOT
    Create real cloud resources. false makes the plan a no-op that still validates the
    configuration and the variable wiring, which is what CI runs: a plan that needs
    credentials cannot gate a pull request from a fork.
  EOT
  type        = bool
  default     = false
}

variable "raw_retention_days" {
  description = "Days to keep the raw zone. Must match governance/policies/column_security.yml."
  type        = number
  default     = 90

  validation {
    condition     = var.raw_retention_days >= 7 && var.raw_retention_days <= 3650
    error_message = "raw_retention_days must be between 7 and 3650."
  }
}

variable "gold_retention_days" {
  description = "Days to keep the gold zone. Must match governance/policies/column_security.yml."
  type        = number
  default     = 730
}

variable "budget_usd" {
  description = "Monthly budget ceiling in USD. Exceeding it should page someone, not be discovered on the invoice."
  type        = number
  default     = 50
}

variable "tags" {
  description = "Tags applied to every resource that supports them."
  type        = map(string)
  default = {
    project = "sentinel"
    managed = "terraform"
    owner   = "data-platform"
  }
}
