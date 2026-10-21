# ADR 0007: Plain Kubernetes manifests, not Helm

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

The stack has four workloads and four Services. Helm is the default answer for
"deploy to Kubernetes", and it brings templating, values files and a release model.

## Decision

Write plain manifests under `infra/k8s/` and apply them with `kubectl`. Deploy to a
local `kind` cluster.

## Consequences

A reader can see the whole object without rendering it first, and there is no values
file to keep in sync with `docker-compose.yml`.

Helm earns its place when there are many environments with real variation. Here there are
two (compose and kind) and both are read more often than they are changed. If a third
environment appears with genuinely different configuration, this decision should be
revisited rather than defended.
