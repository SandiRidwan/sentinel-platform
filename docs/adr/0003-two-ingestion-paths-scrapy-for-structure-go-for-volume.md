# ADR 0003: Two ingestion paths — Scrapy for structure, Go for volume

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

One crawler would be simpler. Python is the right tool for five sources with paginated HTML
and per-page callbacks. It is the wrong tool for a million requests: connection reuse, a hard
concurrency ceiling and a real TLS fingerprint are easier to hold steady in Go.

## Decision

Keep both, with a clear split.

- **Scrapy** owns structured crawls, contract tests and the source-register gate. It is where
  "does this page still look like the page I wrote the spider against" is answered.
- **The Go service** owns the high-volume fetch path. It exposes `/fetch` and `/fetch/batch`,
  a token-bucket limiter, a bounded worker pool, and a utls ClientHello so the TLS
  fingerprint matches the User-Agent it sends.

## Consequences

Two codebases to maintain, and one more language in the repository.

The benefit is that each path is good at its job, and the Go service doubles as the honest
answer to "do you know Go" — it is not a tutorial, it is the path that carries throughput.

The cost is real and recorded here rather than discovered later: a change to fetch policy has
to be made twice. If that becomes a frequent source of drift, the right fix is to move the
policy into a shared config that both read, not to delete one path.
