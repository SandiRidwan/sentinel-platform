# ADR 0012: The agent selects from a registry; it never writes SQL

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

The obvious way to build a text-to-SQL agent is to let the model write a query, run it, and
return the rows. It demos well and fails in three specific ways:

- **Scope.** "How many books were sold yesterday" contains the word *books*, so a model
  will happily answer with the catalogue count. The answer is wrong and looks right.
- **Surface.** Every free-text SQL path is an injection surface, and sanitising it is a
  parser arms race against a model that is optimised for producing plausible SQL.
- **Auditability.** "The agent said 57" cannot be checked. "The agent ran
  `SELECT COUNT(*) FROM gold.dim_book WHERE is_in_stock = FALSE`" can.

## Decision

The agent selects a **table, metric, columns and filters** from
`governance/agent_registry.yml`. `agents/registry.py` builds the SQL from those validated
parts. A table that is not registered, a column that is not declared for that table, an
unknown operator or an unknown metric raises `Refused` *before* any SQL exists.

The graph is `plan -> act -> verify`, with `verify` as a mandatory node rather than a
convenience. It enforces one distinction that is easy to get wrong:

- a **count** of zero is an answer ("no rows match those conditions");
- an **empty result set** from a lookup or an aggregate is not an answer, and is reported
  as a refusal instead of as "0".

## Consequences

- The refusal set in `agents/eval.py` is ten questions the agent must decline. Without it,
  an agent that answers everything scores 20/20 on the valid half and is still unusable,
  because its wrong answers are indistinguishable from its right ones.
- The registry is the second gate in the platform, after `governance/sources.yml`. Both are
  written allow-lists enforced by code, and both record what was deliberately excluded
  (`denied:` in the registry, `rejected:` in the source register) so the decision is
  reviewable rather than implicit.
- The planner is deterministic by default and an LLM is opt-in
  (`SENTINEL_AGENT_PLANNER=llm`). A model-backed planner goes through the same registry, so
  a hallucinated table name degrades to a refusal rather than a query. Determinism by
  default is what makes the eval meaningful: the same 30 questions produce the same verdict
  on every run and in CI.
