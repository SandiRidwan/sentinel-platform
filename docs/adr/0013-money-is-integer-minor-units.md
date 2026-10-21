# ADR 0013: Money is stored as integer minor units

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** data-platform

## Context

`cdc/consumer.py` first declared `price` as `decimal(12, 2)` in Iceberg. The write failed:

```
ValueError: Unexpected physical type FIXED_LEN_BYTE_ARRAY for decimal(12, 2), expected INT64
```

pyiceberg 0.9 writes a decimal column as a fixed-length byte array while its statistics
reader expects an int64. That is a library inconsistency, and the obvious response is to
work around it — cast to float, or to string.

## Decision

Store the price as `price_cents`, a `long`. Convert at the boundary: the source NUMERIC
becomes `int(Decimal(value).quantize(Decimal("0.01")) * 100)` on the way in, and
`Decimal(cents) / 100` on the way out.

The parity check converts the stream's cents back to a decimal before comparing, so the
comparison tests the value rather than the storage format.

## Consequences

- The library limitation stops mattering, and the column is more portable than a decimal:
  every warehouse, every file format and every JSON encoder has an exact integer type.
- A float price would have been the easy fix and the wrong one. Binary floating point
  cannot represent 0.10 exactly, so a sum of prices drifts, and the drift is invisible
  until a reconciliation fails.
- The conversion lives in exactly two functions (`_cents` in the consumer, `normalise` in
  the parity check), so there is one place to change if the scale ever becomes 3 decimal
  places (some currencies) rather than 2.
- This is recorded as an ADR rather than a comment because the next person will see
  `price_cents` and reasonably ask why it is not a decimal.
