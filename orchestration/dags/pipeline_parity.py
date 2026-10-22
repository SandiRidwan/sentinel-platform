"""Airflow wrapper.

The DAG is one task. Everything that matters is in orchestration.shared.pipeline, which the
Prefect flow and the Dagster asset call as well, so a difference in row counts points at the
orchestrator rather than at a divergent copy of the logic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.shared.pipeline import run_pipeline


def _run(**context) -> str:
    result = run_pipeline()
    payload = {"orchestrator": "airflow", **result}

    # Written to a file as well as the log, so the parity check reads a value rather than
    # scraping stdout from a scheduler log.
    out = os.environ.get("SENTINEL_PARITY_OUT")
    if out:
        Path(out).write_text(json.dumps(payload), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    # Pushed to XCom so the row counts are inspectable after the run, not only in the log.
    return json.dumps(result)


with DAG(
    dag_id="pipeline_parity",
    description="Transform + dbt build through the shared pipeline module, for orchestration parity",
    start_date=datetime(2026, 10, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=10)},
    tags=["parity", "sentinel"],
) as dag:
    run_pipeline_task = PythonOperator(
        task_id="run_pipeline",
        python_callable=_run,
    )
