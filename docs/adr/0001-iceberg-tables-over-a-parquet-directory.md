# ADR 0001: Iceberg tables over a Parquet directory

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

The silver and gold layers are written as Parquet. A Parquet directory answers
"what is in this table now" and nothing else. It cannot answer "what did this look like
before the last run", two writers cannot safely rewrite the same partition, and a column
rename breaks every reader at once.

## Decision

Use Apache Iceberg as the table format for silver and gold, with a SQL catalog for
local work and Glue or a REST catalog in production.

## Consequences

Three things become possible that were not: a query can read a previous snapshot
(`table.scan(snapshot_id=...)`), a commit is atomic, and a schema change is a metadata
operation rather than a rewrite.

The cost is a second artefact to operate and a catalog to keep available. For a project
whose whole claim is "the numbers are trustworthy", history is not optional.

Verified: `python -m transform.spark.iceberg_publish --time-travel books` reports two
snapshots and shows the earlier one holding 57 rows against the latest 114.
