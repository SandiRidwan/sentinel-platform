variable "database" {
  description = "Snowflake database name."
  type        = string
  default     = "SENTINEL"
}

variable "warehouse" {
  description = "Snowflake compute warehouse name."
  type        = string
  default     = "COMPUTE_WH"
}

variable "schemas" {
  description = "Schemas to create inside the database."
  type        = list(string)
  default     = ["SILVER", "GOLD"]
}
