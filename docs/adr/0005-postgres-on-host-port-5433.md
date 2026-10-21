# ADR 0005: Postgres on host port 5433

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

`docker compose up` published Postgres on 5432. On a developer machine that already
runs a native Postgres, the connection from the host failed with
`password authentication failed for user "sentinel"` — even after the password was correct
inside the container.

The cause: the native server listened on `::` (IPv6) and Docker published on `0.0.0.0`
(IPv4). `localhost` resolved to `::1` first, so the client reached the native server, which
had never heard of that user.

## Decision

Publish Postgres on host port 5433. Inside the compose network it is still 5432, so
Metabase and Airflow are unaffected.

## Consequences

The ambiguity is removed rather than papered over with `127.0.0.1` in every DSN.

Anyone cloning this repository onto a machine with a local Postgres gets a working stack
without diagnosing an IPv4/IPv6 resolution order.
