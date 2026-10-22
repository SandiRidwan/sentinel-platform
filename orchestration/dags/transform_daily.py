"""Transform DAG — raw to bronze to silver to gold, then the warehouse tests.

Runs after every ingest DAG. Uses `ExternalTaskSensor` on each ingest DAG so a transform
never starts against a half-written raw zone.

    wait_for_ingests -> spark_bronze_silver -> dbt_run -> dbt_test -> record

`dbt_test` is a first-class task, not a convenience: a transform that produced the wrong
shape is worse than one that did not run.
"""

from __future__ import annotations

from datetime import timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.sensors.external_task import ExternalTaskSensor
from airflow.utils.dates import days_ago

from common import SPIDER_SOURCES, default_args, on_failure, on_sla_miss, record_stage_metric, run_module

DAG_ID = "sentinel_transform_daily"

with DAG(
    dag_id=DAG_ID,
    description="Land raw into bronze/silver, then build and test the gold layer with dbt.",
    default_args={**default_args(retries=2), "sla": timedelta(hours=3)},
    schedule="0 5 * * *",
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    sla_miss_callback=on_sla_miss,
    on_failure_callback=on_failure,
    tags=["sentinel", "transform"],
    doc_md=__doc__,
) as dag:

    waits = [
        ExternalTaskSensor(
            task_id=f"wait_for_ingest_{spider}",
            external_dag_id=f"sentinel_ingest_{spider}",
            external_task_id="record_run",
            allowed_states=["success"],
            failed_states=["failed", "skipped"],
            execution_delta=timedelta(hours=3),
            timeout=60 * 60,
            mode="reschedule",
            poke_interval=60,
        )
        for spider in SPIDER_SOURCES
    ]

    @task(task_id="spark_bronze_silver", execution_timeout=timedelta(minutes=45))
    def spark_bronze_silver() -> dict:
        """Raw Parquet -> bronze (typed) -> silver (deduplicated, conformed).

        Skipped with an explicit message when Spark is not installed, rather than failing
        a DAG for an optional dependency. The local fallback path is pandas in
        transform/spark/local_fallback.py.
        """
        try:
            import pyspark  # noqa: F401
        except ImportError:
            print(
                "pyspark not installed; running the pandas fallback. "
                "Install requirements-extra.txt for the Spark path."
            )
            result = run_module("transform.spark.local_fallback", timeout=1800)
        else:
            result = run_module("transform.spark.bronze_to_silver", timeout=2400)

        print(result["stdout_tail"])
        return {"returncode": result["returncode"], "duration_s": result["elapsed_s"]}

    @task(task_id="dbt_run", execution_timeout=timedelta(minutes=30))
    def dbt_run() -> dict:
        result = run_module("tools.tasks.dbt", "build", "--select", "silver gold", timeout=1800)
        print(result["stdout_tail"])
        return {"returncode": result["returncode"], "duration_s": result["elapsed_s"]}

    @task(task_id="dbt_test", execution_timeout=timedelta(minutes=20))
    def dbt_test() -> dict:
        result = run_module("tools.tasks.dbt", "test", "--select", "gold", timeout=1200)
        print(result["stdout_tail"])
        return {"returncode": result["returncode"], "duration_s": result["elapsed_s"]}

    @task(task_id="record")
    def record(spark: dict, run_result: dict, test_result: dict) -> None:
        record_stage_metric("transform_daily", "transform", "spark_seconds", float(spark["duration_s"]))
        record_stage_metric("transform_daily", "transform", "dbt_run_seconds", float(run_result["duration_s"]))
        record_stage_metric("transform_daily", "transform", "dbt_test_seconds", float(test_result["duration_s"]))

    spark = spark_bronze_silver()
    build = dbt_run()
    tested = dbt_test()

    for sensor in waits:
        sensor >> spark >> build >> tested

    record(spark, build, tested)
