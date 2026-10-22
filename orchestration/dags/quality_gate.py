"""Quality gate DAG — four levels, and the DAG fails if any level fails.

This is the difference between having tests and gating on them. A quality suite that runs
and reports is decoration; a quality suite that can stop the pipeline is a gate.

    L1 unit -> L2 contract -> L3 warehouse -> L4 expectations -> publish_report

The report is written as an artifact regardless of outcome, including which level failed
and how many checks passed, so a failure is diagnosable without reading worker logs.
"""

from __future__ import annotations

import json
from datetime import timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.utils.dates import days_ago

from common import default_args, on_failure, record_stage_metric, run_module

DAG_ID = "sentinel_quality_gate"

LEVELS = [
    ("L1_unit", "quality.unit", ["-q"]),
    ("L2_contract", "quality.contract.run_contracts", []),
    ("L3_warehouse", "quality.warehouse.run_warehouse_tests", []),
    ("L4_expectations", "quality.expectations.run_suites", []),
]

with DAG(
    dag_id=DAG_ID,
    description="Run all four quality levels and fail the run when any level regresses.",
    default_args=default_args(retries=1),
    schedule="30 6 * * *",
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    on_failure_callback=on_failure,
    tags=["sentinel", "quality"],
    doc_md=__doc__,
) as dag:

    @task(task_id="run_level", execution_timeout=timedelta(minutes=30))
    def run_level(level: str, module: str, args: list[str]) -> dict:
        """Run one level. check=False so the report captures the failure text."""
        if module == "quality.unit":

            result = run_module("pytest", "quality/unit", *args, check=False, timeout=900)
        else:
            result = run_module(module, *args, check=False, timeout=900)

        passed = result["returncode"] == 0
        print(f"{level}: {'PASS' if passed else 'FAIL'} ({result['elapsed_s']}s)")
        return {
            "level": level,
            "passed": passed,
            "returncode": result["returncode"],
            "elapsed_s": result["elapsed_s"],
            "tail": result["stdout_tail"][-2000:],
        }

    @task(task_id="publish_report", trigger_rule="all_done")
    def publish_report(results: list[dict]) -> dict:
        """Write the report and fail loudly. A gate that passes quietly is not a gate."""
        report = {
            "levels": results,
            "levels_total": len(results),
            "levels_passed": sum(1 for r in results if r["passed"]),
            "levels_failed": [r["level"] for r in results if not r["passed"]],
        }

        from pathlib import Path

        out = Path("quality/reports/quality_gate.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")

        record_stage_metric(
            "quality_gate", "quality", "levels_passed", float(report["levels_passed"])
        )

        print(json.dumps(report, indent=2))

        if report["levels_failed"]:
            raise RuntimeError(
                f"quality gate failed: {', '.join(report['levels_failed'])} "
                f"({report['levels_passed']}/{report['levels_total']} levels passed)"
            )
        return report

    levels = run_level.expand(
        level=[name for name, _, _ in LEVELS],
        module=[module for _, module, _ in LEVELS],
        args=[[*args] for _, _, args in LEVELS],
    )
    publish_report(levels)
