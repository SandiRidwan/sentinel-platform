# ADR 0015: Three orchestrators, one implementation, one row count

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** data-platform

## Context

"Portable across orchestrators" is easy to claim and easy to fake. The usual way it is faked
is to write the pipeline three times — an Airflow DAG, a Prefect flow, a Dagster job — and
then point at three green runs. That proves three copies agree on the day they were written.
It says nothing about the orchestrator, and the copies start to drift with the first bug fix.

The claim worth making is narrower and checkable: **the same code, run by three schedulers,
leaves the same number of rows in every table.**

## Decision

The transformation lives once, in `orchestration/shared/pipeline.py`. Each orchestrator is a
thin wrapper that calls `run_pipeline()`:

| Orchestrator | Wrapper | Primitive |
|---|---|---|
| Airflow | `orchestration/dags/pipeline_parity.py` | one `PythonOperator` |
| Prefect | `orchestration/prefect/flows.py` | `@flow` + `@task` |
| Dagster | `orchestration/dagster/assets.py` | one `@asset`, materialised |

`orchestration/parity.py` runs all three, reads each result from a file the wrapper wrote,
and compares the row count per gold table. Airflow runs inside its own container
(`docker compose exec airflow-scheduler airflow tasks test ...`) because that is how Airflow
actually runs; Prefect and Dagster run on the host.

Three details that make the check real rather than decorative:

1. **The comparison has its own test.** `--self-test` feeds the comparator two results that
   differ by one row and requires it to notice, then identical results and requires it to
   pass. A comparator that cannot report a difference returns "equal" for everything.
2. **A missing result is a failure, not a skip.** If one orchestrator fails to produce a
   file, the report says `incomplete` and the exit code is non-zero. Comparing the two that
   did run would be the interesting way to get a false pass.
3. **The wrapper writes a value, not a log line.** Scraping a scheduler's stdout couples the
   check to log formatting, which changes between versions.

## Consequences

- The Dockerfile copies `orchestration/shared/` into the image, because the DAG imports it.
  Without that the Airflow run fails with `ModuleNotFoundError` — which is exactly what
  happened before the parity check was run the first time.
- Dagster's `main()` goes through `materialize()` rather than calling the function. Calling
  it directly would have compared two orchestrators and one plain function call.
- The check compares row counts, not content. Two runs could agree on 57 rows and disagree on
  which 57; catching that needs a checksum per table and is not part of this ADR.

## Evidence

`quality/reports/orchestration_parity.json`, produced by `python -m orchestration.parity`:
all five gold tables identical across airflow, prefect and dagster (57 / 50 / 250 / 57 / 250).
