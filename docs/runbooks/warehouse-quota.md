# Runbook: warehouse quota or cost

**Alert:** the Terraform budget alert at 80% actual or 100% forecasted
**Severity:** warning

## What it means

Spend is approaching the ceiling written down in `infra/terraform/README.md`. The ceiling
exists so this is an alert rather than an invoice.

## Triage

1. **Which resource?** The budget is per-account. The usual suspects, in order:
   - Snowflake warehouse left running (`auto_suspend = 60` should prevent this; check for a
     session that bypassed it).
   - EMR cluster not terminated after a demo.
   - MWAA environment, which bills hourly whether or not a DAG runs.
   - BigQuery on-demand queries scanning far more than expected (a `SELECT *` over an
     unpartitioned table).

2. **Stop the bleeding before investigating.**
   ```bash
   terraform destroy -var-file=dev.tfvars -var="enable_cloud_resources=true"
   ```
   The lake and the dataset are rebuildable from the pipeline; the invoice is not refundable.

3. **Then find the cause** in the billing console, filtered to the tags this stack sets
   (`project=sentinel`, `managed=terraform`).

## Do not

- Do not raise `budget_usd` to make the alert stop. The number is the decision; the alert is
  the mechanism.
- Do not leave EMR or MWAA running "just in case". Both are the expensive part and both take
  minutes to recreate.
