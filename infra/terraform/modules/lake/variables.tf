variable "buckets" {
  description = "Map of zone name to bucket name."
  type        = map(string)
}

variable "raw_days" {
  description = "Days before raw objects expire."
  type        = number
}

variable "gold_days" {
  description = "Days before gold objects transition to cold storage."
  type        = number
}

variable "tags" {
  description = "Tags for every bucket."
  type        = map(string)
}

variable "force_destroy" {
  description = "Allow destroying a non-empty bucket. Only acceptable in dev."
  type        = bool
  default     = false
}
