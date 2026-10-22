# ADR 0018: Trace portability is proven by reading the spans back

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** data-platform

## Context

Every observability vendor says it "supports OpenTelemetry". The useful question is not
whether an exporter returns 200 — it is whether the trace that arrives is the trace that was
sent, and whether the format is one you could move tomorrow.

The first version of `agents/trace_parity.py` answered a weaker question than it appeared to.
It ran the agent twice, once with OTLP enabled and once without, and compared the two local
traces. That proves the exporter does not break the local trace. It says nothing about what
the backend stored, and it made durations look comparable when they are not: two separate
runs on a shared machine differed by 20 ms vs 57 ms and the check failed on a machine-noise
difference, not on an export bug.

## Decision

One run, three backends, and the spans are **read back from each backend's own store**:

| Backend | Written by | Read back through |
|---|---|---|
| local JSONL | `agents/tracing.py` | the file |
| Phoenix | OTLP/HTTP (`agents/export/phoenix.py`) | Phoenix REST `/v1/projects/<name>/spans` |
| LangSmith | LangSmith's own SDK (`agents/export/langsmith.py`) | local receiver `/runs/count` |

Compared: span count, span names, and the attribute keys on each span. Durations are carried
and explicitly not compared — all three receive the same span list, so a duration comparison
would only measure this machine's clock.

Two implementation details that were forced by the first attempt:

1. **A unique project per run.** Phoenix routes spans by the `openinference.project.name`
   resource attribute. Without it every run lands in `default` and a parity check cannot tell
   this run's nine spans from the previous run's.
2. **Polling for the expected count.** Export is asynchronous on both sides. Reading
   immediately after the call reports a partial trace; a backend that never reaches the count
   is the failure being detected, so the wait is part of the check rather than a workaround.

## The LangSmith limit, stated plainly

No LangSmith account is used. The payload is produced by LangSmith's SDK and posted to
`agents/export/receiver.py`, a local implementation of the ingest contract that validates the
fields the API requires (`id`, `name`, `run_type`, `start_time`, `inputs`, parseable
timestamps).

What that proves: the trace serialises into LangSmith's format and arrives intact, with the
same span count, names and attributes. What it does **not** prove: that the hosted service
would accept it. A receiver that accepted anything would let a malformed export pass, which
is why it rejects missing fields and records the rejections.

## Consequences

- Adding a backend means writing an exporter and a reader. An exporter alone cannot be
  verified, which is the point.
- The same reasoning applies to the BI layer (see `analytics/parity.py`): a rendered
  definition that was never imported is reported as `compiled, not executed`, per target,
  never folded into a single pass.
- If a LangSmith trial becomes available, `LANGSMITH_ENDPOINT` plus a real key replaces the
  receiver without changing the comparison.

## Evidence

`quality/reports/trace_parity.json`: 9 spans (`plan` ×3, `act` ×3, `verify` ×3) with identical
names and attribute keys in the local JSONL, Phoenix and the LangSmith receiver.
