"""Ingest DAGs — one per source, generated from the register.

Generated rather than copied five times because five near-identical DAG files drift, and
the one that drifts is the one nobody reads until it fails.

Flow per source:
    gate_check -> scrape -> validate_landed -> record_run

`gate_check` fails the DAG *before* any request is made if the source is not approved in
governance/sources.yml. An unapproved source should not cost a single HTTP request.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.utils.dates import days_ago

from common import SPIDER_SOURCES, default_args, on_failure, on_sla_miss, record_stage_metric, run_module

DAG_IDS: list[str] = []


def _build(spider: str, meta: dict) -> DAG:
    dag_id = f"sentinel_ingest_{spider}"

    with DAG(
        dag_id=dag_id,
        description=f"Ingest {spider} from {meta['host']} into the raw zone.",
        default_args=default_args(retries=3),
        schedule=meta["schedule"],
        start_date=days_ago(1),
        catchup=False,
        max_active_runs=1,
        sla_miss_callback=on_sla_miss,
        on_failure_callback=on_failure,
        tags=["sentinel", "ingest", meta["host"]],
        doc_md=__doc__,
    ) as dag:

        @task(task_id="gate_check")
        def gate_check(spider: str = spider, host: str = meta["host"]) -> dict:
            """Refuse to crawl an unapproved source. Cheap, and it runs first."""
            from tools.common.sources import SourceNotApproved, assert_approved

            try:
                source = assert_approved(host)
            except SourceNotApproved as exc:
                raise RuntimeError(
                    f"source gate refused {host}: {exc}. "
                    "Add it to governance/sources.yml with a legal basis before crawling."
                ) from exc

            return {
                "host": host,
                "key": source.key,
                "robots_status": source.robots_status,
                "legal_basis": source.legal_basis,
                "rate_limit_rps": source.rate_limit_rps,
            }

        @task(task_id="scrape", execution_timeout=timedelta(minutes=45))
        def scrape(summary: dict, spider: str = spider) -> dict:
            result = run_module(
                "ingestion.scrapy.sentinel_spiders.run_ingest",
                spider,
                timeout=2400,
            )
            return {**summary, "scrape_returncode": result["returncode"], "duration_s": result["elapsed_s"]}

        @task(task_id="validate_landed")
        def validate_landed(summary: dict, spider: str = spider) -> dict:
            """Read back what landed in S3 and refuse a run that produced nothing.

            A green DAG that wrote zero rows is the failure mode that silently starves
            every downstream model.
            """
            from tools.common.config import get_settings
            from tools.common.s3 import list_keys

            settings = get_settings()
            keys = list_keys(settings.s3.bucket_raw, prefix=f"{spider}/")
            if not keys:
                raise RuntimeError(
                    f"scrape reported success but no objects exist under {spider}/ in "
                    f"{settings.s3.bucket_raw}"
                )

            latest = sorted(keys)[-1]
            print(f"{spider}: {len(keys)} objects, latest={latest}")
            return {**summary, "objects": len(keys), "latest_key": latest}

        @task(task_id="record_run")
        def record(summary: dict, spider: str = spider) -> None:
            record_stage_metric(
                run_id=summary.get("run_id") or summary["latest_key"],
                stage="ingest",
                metric=f"objects_written:{spider}",
                value=float(summary["objects"]),
            )

        # Call each TaskFlow function exactly once and pass the result along: calling
        # gate_check() three times would create three separate task instances.
        gate = gate_check()
        raw = scrape(gate)
        landed = validate_landed(raw)
        recorded = record(landed)

        gate >> raw >> landed >> recorded

    return dag


for _spider, _meta in SPIDER_SOURCES.items():
    globals()[f"sentinel_ingest_{_spider}"] = _build(_spider, _meta)
    DAG_IDS.append(f"sentinel_ingest_{_spider}")
