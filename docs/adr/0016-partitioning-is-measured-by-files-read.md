# ADR 0016: Partitioning is measured by files read, not by a stopwatch

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** data-platform

## Context

The first version of `bench/partitioned.py` wrote the same 2 million rows twice — one Hive
layout (`dt=NNN`), one flat directory — and timed `WHERE day = 50` against both. The
partitioned side was **seven times slower**: 149 ms against 21 ms. That result is the reason
this ADR exists.

The cause was not the layout. DuckDB expands `partitioned/**/*.parquet`, opens every file,
and only then applies the filter. A partition-aware reader does not do that: it resolves the
partition value first and opens the files for that partition. The benchmark was measuring the
reader, and blaming the layout.

Two wrong conclusions were available and both were tempting:

- "Partitioning does not help" — false, and it would have been recorded as a finding.
- "DuckDB cannot prune" — true of the glob form, but not of the layout.

## Decision

The measurement names the files.

1. **Partitioned, pruned:** the files under `dt=<probe>` are passed explicitly, which is what
   a partition-aware reader does.
2. **Partitioned, glob:** the naive form is still reported, with the explanation, because it
   is what a reader of the code will write first.
3. **Iceberg:** pruning is reported from `plan_files()`, which is the table format's own
   statement about which files a scan must read — not an inference from timing.

The 100M-row run writes four layouts with identical rows:

| Layout | What it is for |
|---|---|
| partitioned parquet | 100 files, one per day |
| flat, ordered by day | a flat directory, rows sorted by the filter column |
| flat, days mixed | the negative control: every row group spans all days |
| Iceberg, `identity(day)` | the table format's own partitioning |

The second flat copy exists because the first one is **not** a negative control, and the run
proved it: sorted data in a flat file is fast (10.9 ms vs 83.4 ms full scan) because Parquet
row-group statistics skip row groups. That is real pruning, just at a different level. The
mixed copy cannot prune at all (138 ms filtered vs 78 ms unfiltered — filtering costs more
than it saves when the column must be read and thrown away), which is what a control has to
look like.

## Consequences

- `partitioned_beats_sorted_flat` is asserted as well as `partitioned_filtered_faster`. The
  first says partitioning is worth doing; the second only says pruning happened.
- The mixed-day copy is slower than its own full scan, and that is recorded rather than
  hidden: the unfiltered aggregate never reads the `day` column, so the comparison is not
  apples to apples and pretending otherwise would be a mistake in the write-up.
- Writing 100M rows four times takes about seven minutes on this machine. That is the price
  of having a control for each claim.

## Evidence

`quality/reports/bench_partitioned.json` at 100,000,000 rows, 100 days:

| read | p50 | files |
|---|---|---|
| partitioned, pruned | 5.7 ms | 1 |
| partitioned, glob | 26.1 ms | 100 |
| partitioned, full scan | 94.8 ms | 100 |
| flat sorted, filtered | 11.0 ms | 10 |
| flat mixed, filtered | 146.2 ms | 10 |
| Iceberg, pruned | 7.0 ms | 1 |
| Iceberg, full scan | 131.3 ms | 100 |

Write side: 101.3 s for the partitioned Parquet copy (987,588 rows/s), 116.4 s for Iceberg
(859,218 rows/s, 100 snapshots), 132 MB of Parquet against 168 MB of Iceberg data files.
