project     = "sentinel"
environment = "dev"
aws_region  = "us-east-1"

# No cloud resources in dev by default: 	erraform plan validates the configuration and
# the variable wiring without creating anything and without credentials. Flip to true, and
# fill in gcp_project / snowflake_account, to create the real lake and warehouses.
enable_cloud_resources = false
gcp_project            = ""
snowflake_account      = ""

raw_retention_days  = 90
gold_retention_days = 730
budget_usd          = 50