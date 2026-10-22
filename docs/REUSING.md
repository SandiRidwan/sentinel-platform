# Reusing SENTINEL

This repository is one person's platform, but seven of its parts are general. Each section
below says what the part does, what you must change to use it, and what it deliberately
does not do.

Everything here runs without a cloud account. That is a design constraint, not a demo
shortcut: a component you cannot run is a component nobody adapts.

---

## 1. A governed agent over your own warehouse

**What it gives you.** A question-answering agent that cannot read a table you did not
declare, cannot write SQL, and refuses questions your data cannot answer instead of
returning a confident near-miss.

**Files to copy**

```
governance/agent_registry.yml   <- your tables (this is the only file you must write)
agents/registry.py              <- the gate; unchanged
agents/tools.py                 <- read-only query tools; unchanged
agents/graph.py                 <- plan/act/verify; change the planner hints
agents/eval.py                  <- replace the 30 cases with your own
agents/ask.py                   <- CLI; unchanged
agents/tests/test_registry.py   <- the gate's tests; unchanged
```

**What you must change**

1. `governance/agent_registry.yml` — list your tables, their columns and types, and the
   tables that are *deliberately* out of reach with a reason. The `denied:` block matters:
   it is what turns an omission into a decision.
2. `agents/graph.py` — `TABLE_HINTS`, `COLUMN_HINTS` and `OUT_OF_SCOPE_TERMS`. These are
   the words your users actually type. Keep the scope list honest: every term you add
   there is a question the agent will decline rather than guess at.
3. `agents/eval.py` — replace the cases. Each case needs an expectation computed by SQL you
   write yourself, not by the agent.

**What it does not do.** It does not join across tables. A question that needs two tables
gets a refusal, not a wrong answer. Adding joins means extending the registry schema with
declared relationships, and that is a real design task, not a config line.

**The two rules worth copying even if you throw the rest away**

- A count of zero is an answer; an empty result set is not. Conflating them turns "the
  query matched nothing" into "the number is zero".
- Test the refusals. An agent that answers everything passes every happy-path test.

---

## 2. Proving a streaming copy matches the batch copy

**What it gives you.** A comparator that proves two independent paths to the same table
agree, and that fails when it cannot prove it — including when it compared zero rows.

**Files to copy**

```
cdc/parity.py            <- the comparator and its negative control
cdc/tests/test_parity.py <- the comparator's own tests
```

**What you must change**

1. `read_source()` — your SQL for the authoritative side.
2. `read_stream()` — your read from the streaming side, normalised to the same types.
3. `COMPARED_COLUMNS` — exclude provenance columns (`_cdc_op`, `_cdc_ts_ms`) or they will
   differ by construction and the check becomes noise.

**What it does not do.** It does not compare history, only current state. Two paths can
agree on the final row and disagree on how they got there; comparing the change log needs a
second comparator and is not included.

**The part that is easy to get wrong.** The negative control. Before reporting a pass, the
comparator corrupts one value and drops one key, and requires itself to notice both. A
comparator with no failing case is a function that returns "equal".

---

## 3. A control register your reviewer can read

**What it gives you.** `governance/controls.yml` plus `governance/report.py` produce a JSON
and a CSV where every control points at the file that proves it, and the proof is
*evaluated* at report time.

**Files to copy**

```
governance/controls.yml   <- your controls and evidence
governance/report.py      <- the evaluator; unchanged
```

**What you must change.** Only the YAML. Evidence entries are one of four mechanical types:

```yaml
evidence:
  - type: file            # path exists, optional min_bytes
    path: governance/sources.yml
  - type: text_contains   # regex, MULTILINE
    path: tools/common/sources.py
    pattern: "approved"
  - type: json_field      # dotted path into a JSON file
    path: quality/reports/pii_scan.json
    field: total_detections
    equals: 0
  - type: jsonl_min_rows
    path: governance/lineage/events/events.jsonl
    min_rows: 5
```

**What it does not do.** It does not assess whether a control is *adequate* — only whether
the evidence you named is present and says what you claimed. Adequacy is a human judgement;
this tool removes the "we think that's documented somewhere" failure.

**Keep the gaps.** `status: gap` with an owner and a remediation note is a normal outcome.
This register ships with two: encryption at rest, and secret scanning. A register with no
gaps is either a very small system or a register that is lying.

---

## 4. A cache policy that cannot serve stale prices

**What it gives you.** A 90-line Worker that classifies a path as `metadata` or `inventory`
and applies a different cache policy to each, with per-token rate limiting and honest error
propagation.

**Files to copy**

```
edge/worker.ts            <- the handler
edge/test/worker.test.ts  <- 12 tests, no Cloudflare account needed
```

**What you must change**

1. `METADATA_PATHS` — the routes whose response is safe to cache.
2. `ENV` — token, origin URL, rate limit.

**What it does not do.** The rate limiter is in-memory per isolate. That is fine for
stopping a runaway client and wrong for billing anyone: with many isolates each gets its
own counter. Production needs Durable Objects or KV, and that is a different design.

**The rule that generalises.** Cache the schema, never the number. A stale schema costs one
extra request; a stale price is a wrong answer that looks live. The response carries
`x-sentinel-data-class` so a reviewer can see which policy applied without reading code.

---

## 5. Checking dbt portability without cloud credentials

**What it gives you.** `transform/warehouse_parity.py` separates three claims that are
usually conflated: the project **parses** under an adapter, the SQL **transpiles** to that
dialect, and the models **executed** there.

On a machine with no credentials it reports `executed_on: ["dev"]` and lists Snowflake and
BigQuery as not executed. That is the honest result, and it is still useful: a transpile
failure is caught before anyone buys a warehouse.

**What you must change.** `TARGETS` and `DIALECTS`, and `credentials_present()` for your
providers. With `--with-cloud` it runs `dbt build` and compares per-model row counts against
the local target.

---

## 6. One dashboard, four tools, one number

**What it gives you.** `analytics/dashboard_spec.yml` defines each card once — SQL and
display — and `analytics/render.py` produces Metabase JSON, a Superset import bundle, a
Looker Studio community connector and a Sigma workbook mapping. `analytics/parity.py` then
reads the SQL back out of every rendered file and compares it with the spec, runs it against
ClickHouse, and compares the result with what Metabase returns through its own API.

**Files to copy**

```
analytics/dashboard_spec.yml   <- your cards (this is the only file you must write)
analytics/render.py            <- the four renderers
analytics/parity.py            <- the SQL and value comparison
analytics/metabase/            <- importer over the HTTP API
```

**What you must change.** The spec: card SQL, display type, and the dashboard grouping.
Point `METABASE_URL` at your instance. If you have Superset, add its importer next to
Metabase's — the pattern is the same.

**What it does not do.** It does not prove a tool ran a card unless it did. `parity.py`
reports `executed` or `compiled, not executed` per target, with the reason. A rendered
Superset bundle that was never imported is not evidence that Superset works; it is evidence
that the definition is not tool-specific.

**The rule that generalises.** Define the number once. The moment two dashboards carry two
copies of a query, "the dashboard is wrong" becomes unanswerable — there is no single place
that could be right.

---

## 7. Proving an orchestrator migration

**What it gives you.** One pipeline implementation, three thin scheduler wrappers, and a
comparison that fails when the row counts differ or when one orchestrator produced no result.

**Files to copy**

```
orchestration/shared/pipeline.py    <- your pipeline, once
orchestration/prefect/flows.py      <- wrapper: flow + task
orchestration/dagster/assets.py     <- wrapper: asset + materialize
orchestration/dags/pipeline_parity.py  <- wrapper: one PythonOperator
orchestration/parity.py             <- the comparison and its self-test
```

**What you must change.** Only `shared/pipeline.py`. The wrappers should stay empty of logic;
the moment one of them transforms something, the comparison stops testing orchestrators.

**What it does not do.** It compares row counts, not content — two runs could agree on 57
rows and disagree on which 57. Catching that needs a checksum per table, and is a different
check.

**The part that is easy to get wrong.** A missing result is a failure, not a skip. Comparing
the two orchestrators that produced output is the interesting way to get a false pass.

---

## What is intentionally not reusable

- `ingestion/` — the spiders are shaped around three specific sandbox sites.
- `orchestration/dags/` — the DAGs encode this platform's schedule (the shared pipeline
  module and the parity check are reusable; the schedule is not).
- `infra/` — the Terraform is for this account layout.

The seven parts above are the ones written to be lifted. If you adapt one and hit something
the docs did not cover, that is a documentation bug — the ADRs record why each decision was
made, so the reasoning should be checkable even where the code does not fit.
