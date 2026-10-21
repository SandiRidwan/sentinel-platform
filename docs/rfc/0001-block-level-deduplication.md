# RFC 0001: Block-level deduplication for re-crawls

- **Status:** Proposed — NOT implemented
- **Author:** data-platform
- **Reviewers:** one reviewer, notes below
- **Created:** 2026-10-21

## Summary

Replace URL-level deduplication with content-level deduplication for sources that are crawled
more than once, so a re-run does not store the same rows again.

## Motivation

The current `DedupMiddleware` drops a request whose URL was already seen in the same run. That
is correct within one run and useless across runs: the second crawl of
`books.toscrape.com` fetches every page again and writes every row again. Silver then
deduplicates by business key, which works, but the raw zone grows linearly with the number of
runs for data that has not changed.

Observed: `countries` produced 250 rows on the first run and 500 on the second, before the
silver dedup step reduced them back to 250. The raw zone is the layer that is supposed to be
cheap and re-scrapable; it should not be the layer that grows fastest.

## Proposal

Three changes, each independent:

1. **A per-item content hash.** Add `_content_hash` to the Parquet schema: a BLAKE2b digest
   over the item's business fields (not the URL, not `scraped_at`). Cheap, stable, and it
   makes "this row is unchanged" a single comparison.

2. **A raw-zone index.** On startup, the sink reads the existing partition's
   `_content_hash` column and holds the set in memory. An item whose hash is present is
   counted as `unchanged` and not written.

3. **A run summary field.** `run_ingest` reports `new`, `unchanged` and `rejected` per spider
   instead of one `rows_landed` number.

## Alternatives considered

**Fetch-level caching (HTTP cache).** Would avoid the request entirely. Rejected because the
platform's job is to observe change: a cache would hide a price change behind a stale
response, which is the exact thing the pipeline exists to detect.

**Deduplicate only in silver.** Already happens. Rejected as sufficient because raw is the
layer that grows, and the growth is invisible until storage is the problem.

**Bloom filter instead of an exact set.** Lower memory for very large partitions. Rejected for
now: at hundreds of thousands of rows an exact set costs a few megabytes, and a bloom filter
introduces false positives that would silently drop a genuinely new row. Revisit if a source
exceeds roughly ten million rows per partition.

## Drawbacks

- Memory proportional to the partition's row count, per source.
- The hash must cover exactly the business fields. A field added to the item without being
  added to the hash makes every row look changed.
- It does not help the first crawl of a source, which is the expensive one.

## Unresolved questions

- Should `unchanged` rows still be counted in `rows_landed`? Proposal: no, and the run record
  should say `new=0 unchanged=250` rather than `rows_landed=250`. A number that does not
  distinguish "wrote 250" from "found 250 already there" is the kind of metric that makes a
  pipeline look busy while nothing happens.
- Does the hash belong in the Parquet schema or in a sidecar index file? Proposal: in the
  schema, so a reader can verify without the index.

## Review notes

**Reviewer:** "The memory cost is fine at this scale, but say what happens when it is not.
A bloom filter is the obvious answer and you have rejected it — say why, and say at what
point you would change your mind."

*Addressed: see "Alternatives considered", including the threshold.*

**Reviewer:** "What happens if the hash algorithm changes?"

*Not yet addressed. Proposal: version the field as `_content_hash_v1`, and treat an unknown
version as "changed" rather than "unchanged". Fail toward writing, not toward dropping.*

## Decision

**Not yet decided, and nothing in this RFC is built.** The document is here because the
problem is real and observed, not because the solution exists. The raw zone currently grows
linearly with runs and silver deduplicates afterwards; that is the status quo this proposes
to change.

Before implementation, the hash-version question in the review notes needs an answer, and
`_content_hash` needs a schema migration for every existing partition.
