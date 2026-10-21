# Runbook: DAG failure

**Alert:** `NoIngestionActivity`, or an Airflow task failure email
**Severity:** warning / critical depending on the DAG

## What it means

A scheduled run did not complete. The interesting question is not "which task failed" but
"what did it produce before it failed".

## Triage

1. **Which task, which try?**
   ```bash
   airflow tasks states-for-dag-run <dag_id> <run_id>
   ```
   A failure on try 1 of 4 that succeeded on try 2 is noise. A failure on the last try is real.

2. **Did it write anything?**
   The `validate_landed` task exists for this: a scrape that reports success but lands zero
   objects fails the DAG. If the failure is there, the spider ran and produced nothing, which
   is a source problem, not an orchestration problem.

3. **Was it the source or the pipeline?**
   - Source: the spider's log shows a 403/404/timeout. Go to `target-blocked.md`.
   - Pipeline: the task failed before or after the fetch. Check S3 reachability
     (`curl localhost:9090/`) and the warehouse.

4. **Backfill only after the cause is known.**
   ```bash
   airflow dags backfill <dag_id> --start-date <date> --end-date <date>
   ```
   Backfilling a broken DAG produces many more failures and hides the original one.

## Do not

- Do not clear the failed task repeatedly. Two identical failures is a signal to stop and
  read, not to try a third time.
- Do not mark a task successful to unblock downstream. A green DAG over no data is worse than
  a red one.
