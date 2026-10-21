# ADR 0002: The source register is an enforcement gate, not a config file

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

Scrapers need a list of targets. The obvious implementation is a constant in each
spider or a URL in a settings file. That list then drifts from what anyone reviewed, and
nobody can answer "what does this platform crawl, and on what basis" without reading code.

## Decision

Every host must be declared in `governance/sources.yml` with its robots.txt status,
legal basis and allowed paths. A middleware refuses any request whose host is not approved,
and a pipeline stage refuses any item whose source is not approved. An undeclared host costs
zero HTTP requests.

## Consequences

Adding a source becomes a reviewable change with a written justification, and the
`rejected` list records what was considered and why it was declined.

This caught a real mistake: `en.wikipedia.org` was first marked `api_permitted`, but its
robots.txt sets `Disallow: /w/` for `User-agent: *`, which covers `/w/api.php`. The source
was removed rather than given an exception, because the point of obeying robots.txt is that
it is not carved up when a source is convenient.
