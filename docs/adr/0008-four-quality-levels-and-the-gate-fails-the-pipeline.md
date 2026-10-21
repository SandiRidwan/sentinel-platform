# ADR 0008: Four quality levels, and the gate fails the pipeline

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

Tests that run and report are decoration. The failure mode that matters is a pipeline
that reports success while producing wrong data: the site changed, the spider returned
fewer rows, nothing raised, and a dashboard kept showing yesterday's numbers.

## Decision

Four levels, each catching a different class of failure: L1 unit (logic), L2 spider
contracts (the site's shape), L3 warehouse (declared contracts, column types, row volume,
presence), L4 expectations (statistical plausibility). The gate DAG fails the run if any
level fails, and CI blocks the pull request.

## Consequences

A regression stops the pipeline instead of propagating.

One rule earned its place during construction: L4 originally reported `PASS` when every
table had been skipped. A suite that verified nothing now fails, because "all checks passed"
over an empty set is exactly the lie this level exists to prevent.
