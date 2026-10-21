# ADR 0011: CDC reads the write-ahead log, not the table

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

The batch path answers "what does the warehouse contain after tonight's run". It cannot
answer "what changed, and when", and polling the source table on a schedule cannot answer
it either: between two polls, an update that is later reverted is invisible, and a delete
leaves no trace at all.

## Decision

Debezium reads the Postgres write-ahead log through a logical replication slot and
publishes row-level changes to Kafka. A consumer folds those events into two Iceberg
tables: `cdc.product_price_current` (one row per key, the state after the last event) and
`cdc.product_price_changes` (the ordered change log).

Three implementation choices follow from that:

1. **`wal_level=logical` is a hard prerequisite.** The default `replica` level writes only
   enough WAL to replay whole pages, so logical decoding has nothing to read. This is set
   in `docker-compose.yml` and is the first thing to check when the connector fails.

2. **The sink overwrites, it does not append.** Replaying the topic from offset zero must
   produce the same result as replaying it once. An append-only sink doubles the row count
   on every restart, and the bug looks like "the agent is returning duplicates" three
   layers away. Overwrite makes idempotency structural.

3. **Parity is proven, not assumed.** `cdc/parity.py` compares the folded state against
   the source table column by column, and runs a negative control first: it corrupts one
   value and drops one key, and requires the comparator to report both. A comparator that
   cannot fail is indistinguishable from a passing test.

## Consequences

- The platform now has two writers describing the same data, so it must have a proof that
  they agree. That proof is `make cdc`, which is part of the CI gate.
- Money is stored as integer minor units (`price_cents`) rather than a decimal column.
  This was forced by a pyiceberg 0.9 limitation (it writes `decimal(12,2)` as a
  fixed-length byte array while its statistics reader expects int64) but it is the better
  storage format regardless: no float, no rounding, no dialect-dependent decimal
  semantics.
- The consumer keeps the whole topic in memory. At 638 events that is nothing; at 10M
  events it would need a streaming fold with a state store. This is documented rather than
  solved, because the demo's job is to be correct at its scale, not to pretend to be
  production-sized.
