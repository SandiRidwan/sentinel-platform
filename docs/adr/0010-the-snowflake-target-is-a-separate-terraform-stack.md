# ADR 0010: The Snowflake target is a separate Terraform stack

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

The Snowflake database exists to prove the dbt models are portable. It was first a
module inside the main Terraform stack, guarded by `count = 0` when no account was
configured.

That did not work: Terraform configures a declared provider even when every resource using it
has `count = 0`, and the Snowflake provider authenticates during configuration. `terraform
plan` tried to log in to `sentinel-unset.snowflakecomputing.com` and failed, on a machine
that was not trying to create anything Snowflake-related.

## Decision

Move it to `infra/terraform/snowflake/` — its own root module, its own credentials, its
own lifecycle.

## Consequences

The main stack plans cleanly with no credentials, which is what CI needs.

The second warehouse is now a deliberate act (`cd infra/terraform/snowflake && terraform
apply`) rather than a side effect of planning the lake.
