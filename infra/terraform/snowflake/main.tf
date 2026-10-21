# SENTINEL Snowflake target — a separate stack.
#
# It is separate because it has its own credentials and its own lifecycle: the lake and
# BigQuery dataset exist for the platform, while the Snowflake database exists to prove the
# dbt models are portable. Coupling them meant 	erraform plan in the main stack tried to
# authenticate to Snowflake even when nothing Snowflake-related was being created.
#
#   cd infra/terraform/snowflake
#   terraform init
#   terraform plan  -var="account=<org>-<account>" -var="user=<user>" -var="password=<pass>"
#   terraform apply -var="account=..." -var="user=..." -var="password=..."

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 0.95"
    }
  }
}

variable "account" {
  description = "Snowflake account identifier, e.g. myorg-myaccount."
  type        = string
}

variable "user" {
  description = "Snowflake user with CREATE DATABASE."
  type        = string
}

variable "password" {
  description = "Snowflake password. Pass via TF_VAR_password, never in a file."
  type        = string
  sensitive   = true
}

variable "role" {
  description = "Role to assume."
  type        = string
  default     = "SYSADMIN"
}

variable "database" {
  description = "Database name."
  type        = string
  default     = "SENTINEL"
}

variable "warehouse" {
  description = "Compute warehouse name."
  type        = string
  default     = "COMPUTE_WH"
}

variable "schemas" {
  description = "Schemas to create."
  type        = list(string)
  default     = ["SILVER", "GOLD", "GOVERNANCE"]
}

provider "snowflake" {
  account  = var.account
  user     = var.user
  password = var.password
  role     = var.role
}

module "snowflake_target" {
  source = "../modules/snowflake"

  database  = var.database
  warehouse = var.warehouse
  schemas   = var.schemas
}

output "database" {
  value = module.snowflake_target.database
}

output "schemas" {
  value = module.snowflake_target.schemas
}