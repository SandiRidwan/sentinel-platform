output "environment" {
  value = var.environment
}

output "cloud_resources_enabled" {
  value = var.enable_cloud_resources
}

output "bucket_names" {
  description = "Zone bucket names. Empty when enable_cloud_resources is false."
  value       = var.enable_cloud_resources ? module.lake[0].bucket_names : {}
}

output "bigquery_dataset" {
  description = "BigQuery dataset id. Always null here: the dataset lives in the bigquery stack."
  value       = null
}


output "retention" {
  description = "Effective retention, so it can be diffed against the policy file."
  value = {
    raw_days  = var.raw_retention_days
    gold_days = var.gold_retention_days
  }
}

output "budget_usd" {
  value = var.budget_usd
}