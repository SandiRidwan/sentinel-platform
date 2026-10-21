terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
# SENTINEL warehouse — BigQuery dataset plus the policy tags that enforce column security.
#
# The taxonomies mirror governance/policies/column_security.yml: identity columns and
# content columns sit on separate taxonomies so a role can be granted one and not the other.

resource "google_bigquery_dataset" "gold" {
  dataset_id                 = var.dataset
  friendly_name              = "SENTINEL gold"
  description                = "Analytics-ready layer. Written by dbt, read by the serving layer."
  location                   = var.location
  delete_contents_on_destroy = false

  labels = var.tags
}

resource "google_data_catalog_taxonomy" "classification" {
  provider     = google
  display_name = "sentinel-classification"
  description  = "Column classes from governance/policies/column_security.yml."
  region       = var.location

  activated_policy_types = ["FINE_GRAINED_ACCESS_CONTROL"]
}

resource "google_data_catalog_policy_tag" "identity" {
  taxonomy     = google_data_catalog_taxonomy.classification.id
  display_name = "identity"
  description  = "Direct identifier. Hashed for consumers outside the pipeline."
}

resource "google_data_catalog_policy_tag" "free_text" {
  taxonomy     = google_data_catalog_taxonomy.classification.id
  display_name = "free_text"
  description  = "Unstructured text. Masked before export."
}

resource "google_data_catalog_policy_tag" "public" {
  taxonomy     = google_data_catalog_taxonomy.classification.id
  display_name = "public"
  description  = "Published by the source. No restriction."
}

# The tables are created by dbt, not by Terraform: the schema belongs to the transformation
# layer, and duplicating it here would create two sources of truth that drift. Terraform owns
# the dataset, the taxonomy and the access policy; dbt owns the tables.
output "dataset_id" {
  value = google_bigquery_dataset.gold.dataset_id
}

output "policy_tags" {
  value = {
    identity  = google_data_catalog_policy_tag.identity.id
    free_text = google_data_catalog_policy_tag.free_text.id
    public    = google_data_catalog_policy_tag.public.id
  }
}
