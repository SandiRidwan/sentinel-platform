# SENTINEL infrastructure

Terraform for the lake and the warehouses, Kubernetes manifests for the stack.

## The three-stack split

| Stack | Path | What it owns |
|---|---|---|
| Platform | `infra/terraform/` | S3 zone buckets, lifecycle, budget |
| BigQuery | `infra/terraform/bigquery/` | The dataset and its policy tags |
| Snowflake | `infra/terraform/snowflake/` | The second warehouse, with its own credentials |

They are separate because they have different credentials and different lifecycles, and
because **Terraform configures a declared provider even when every resource using it has
`count = 0`**. Both the Google and Snowflake providers authenticate during configuration, so
having either block in the platform stack made `terraform plan` reach for credentials even
with `enable_cloud_resources = false`. The BigQuery stack was split out after CI caught
exactly that: `Attempted to load application default credentials`.

A provider block belongs in the stack that has the credentials for it. Anything else turns
"the plan needs no credentials" into "the plan needs no credentials *on my machine*".

## Plan without credentials

`enable_cloud_resources = false` (the default in `dev.tfvars`) makes the platform plan a pure
configuration check: no resources, no credentials, no network, and no provider in the stack
that authenticates.

```bash
cd infra/terraform
terraform init -backend=false
terraform validate
terraform plan -input=false -var-file=dev.tfvars
```

Verified output:

```
Changes to Outputs:
  + bucket_names            = {}
  + budget_usd              = 50
  + cloud_resources_enabled = false
  + environment             = "dev"
  + retention               = { raw_days = 90, gold_days = 730 }
```

This is what CI runs. A pull request from a fork has no credentials and must still be able to
prove the configuration is sound.

The BigQuery and Snowflake stacks are validated with `terraform validate` only, because a
plan there genuinely needs credentials:

```bash
cd infra/terraform/bigquery && terraform init -backend=false && terraform validate
cd infra/terraform/snowflake && terraform init -backend=false && terraform validate
```

Three provider settings make that possible, and each one is there for a reason:

- `skip_credentials_validation`, `skip_requesting_account_id`, `skip_metadata_api_check`,
  `skip_region_validation` — the AWS provider otherwise reaches for the EC2 instance
  metadata endpoint at plan time (`169.254.169.254`) and fails after three attempts.
- no root `provider "snowflake"` block — Terraform configures a declared provider even when
  every resource using it has `count = 0`.
- no `backend "local" {}` block — declaring it explicitly makes `init -backend=false` and
  `plan` disagree about whether a backend change happened.

## Creating real resources

```bash
cd infra/terraform
# fill in gcp_project and/or snowflake_account, then:
terraform plan  -input=false -var-file=prod.tfvars -var="enable_cloud_resources=true"
terraform apply -input=false -var-file=prod.tfvars -var="enable_cloud_resources=true"
terraform destroy -input=false -var-file=prod.tfvars -var="enable_cloud_resources=true"
```

## Cost ceiling

The point of writing this down is that a demo should not be able to surprise anyone.

| Resource | Configuration | Why this size |
|---|---|---|
| S3 | 4 buckets, `STANDARD`, gold → `STANDARD_IA` after 90 days | A few hundred MB of Parquet. Pennies. |
| S3 lifecycle | raw expires at 90 days, gold at 730 | Raw is re-scrapable; gold is the record. |
| BigQuery | one dataset, `US`, no streaming inserts | Queries over <1 GB are inside the free tier. |
| Snowflake | `XSMALL`, `auto_suspend = 60`, `initially_suspended = true` | An idle warehouse costs nothing; the smallest size runs the dbt build. |
| EMR / MWAA | **not created by default** | Both are the expensive part. Bring them up for one demo run, then destroy. |
| Budget alert | 80% actual and 100% forecasted, at `budget_usd` | A budget nobody is alerted about is a number in a document. |

**Hard ceiling for a full demo: `budget_usd` (default 50 USD/month).** The realistic cost of
the described usage is a few dollars; the ceiling exists so a forgotten resource is caught by
an alert rather than by an invoice.

## Kubernetes

`infra/k8s/` deploys the stack to a local `kind` cluster.

```bash
make k8s-up     # create the cluster, apply manifests, wait for rollout
make k8s-down   # delete the cluster
```

Manifests are plain YAML rather than Helm charts on purpose: three Deployments, three
Services and a ConfigMap do not need a templating layer, and a reader can see the whole
object without rendering it first. The Trade-off is recorded in `docs/adr/`.

## What Terraform does not own

The **tables**. dbt creates them, because the schema belongs to the transformation layer and
duplicating it in Terraform creates two sources of truth that drift. Terraform owns the
dataset, the taxonomy, the buckets and the budget.
