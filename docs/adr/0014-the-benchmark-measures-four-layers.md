# ADR 0014: The benchmark measures four layers, and says what it does not measure

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

"Handles ten million rows" is not a measurement. It hides which layer was tested: a
generator that writes Parquet fast, a query engine that scans fast, and a table format that
commits fast are three different claims, and they fail for different reasons.

## Decision

`bench/run.py` reports four numbers from four layers, each with the row count it was
measured at:

| Layer | What it proves |
|---|---|
| generate | rows/s into Parquet — the writer, not the engine |
| dbt build | seconds for a real model over the full set — planning and shuffle |
| query | p50 / p95 over 20 runs — what a dashboard feels |
| iceberg | append rows/s, and data files before/after a rewrite |

Two honesty constraints are built in:

1. **Iceberg is measured on a subset** (1M rows by default) and the report records
   `scales` per layer. Reporting one number for "the benchmark" would imply the Iceberg
   path ran at 10M when it did not.

2. **The rewrite is not called compaction.** pyiceberg 0.9 exposes no
   `rewrite_data_files` and no `expire_snapshots`. An `overwrite` adds a snapshot and keeps
   the previous snapshot's files, so disk usage *grows*. The first run of this benchmark
   reported `953 KB -> 1.87 MB`, and the report now carries `space_reclaimed: false` with
   the reason. A benchmark that quietly relabels growth as compaction is worse than no
   benchmark, because it produces a number someone will quote.

## Consequences

- The README table is generated from `quality/reports/bench_10m.json`, not typed by hand.
  When the machine or the code changes, the table changes with it.
- The measurement is single-machine and single-process. It says nothing about throughput
  under concurrency, which is a different benchmark and a different claim.
