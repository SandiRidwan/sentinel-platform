variable "project" {
  description = "GCP project id."
  type        = string
}

variable "dataset" {
  description = "BigQuery dataset name for the gold layer."
  type        = string
  default     = "sentinel"
}

variable "location" {
  description = "BigQuery location."
  type        = string
  default     = "US"
}

variable "tags" {
  description = "Labels applied to the dataset."
  type        = map(string)
  default     = {}
}
