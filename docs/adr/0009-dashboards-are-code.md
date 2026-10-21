# ADR 0009: Dashboards are code

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

Metabase dashboards are normally built by clicking. A dashboard built that way lives in
one person's browser session, is never reviewed, and cannot be rebuilt when the instance is
replaced.

## Decision

Define dashboards in `analytics/metabase/dashboards/*.json` and create them through the
API with an idempotent importer.

## Consequences

The dashboard is reviewable in a pull request and reproducible from an empty instance.

Verified by querying the cards after import: inventory by category returns 15 rows, price by
rating 5, goals by season 10 — so the cards are backed by data rather than merely existing.
