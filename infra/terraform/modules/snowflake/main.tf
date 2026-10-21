terraform {
  required_providers {
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 0.95"
    }
  }
}
# SENTINEL Snowflake target — the second warehouse, proving the dbt models are portable.
#
# Deliberately minimal: database, schemas, and a small warehouse with an auto-suspend so a
# forgotten session does not bill for an idle cluster. Everything above the schemas is dbt's.

resource "snowflake_database" "sentinel" {
  name                        = var.database
  comment                     = "SENTINEL second target. Models are shared with BigQuery."
  data_retention_time_in_days = 1
}

resource "snowflake_schema" "zone" {
  for_each = toset(var.schemas)

  database = snowflake_database.sentinel.name
  name     = each.value
  comment  = "Medallion zone ${each.value}."
}

resource "snowflake_warehouse" "compute" {
  name           = var.warehouse
  warehouse_size = "XSMALL"
  comment        = "Smallest size that runs the dbt build; suspend quickly so idle costs nothing."

  auto_suspend        = 60
  auto_resume         = true
  initially_suspended = true
}

output "database" {
  value = snowflake_database.sentinel.name
}

output "schemas" {
  value = [for schema in snowflake_schema.zone : schema.name]
}
