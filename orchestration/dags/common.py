"""Shared Airflow helpers for every SENTINEL DAG.

One place for default_args, the failure callback and the "run a platform module" wrapper,
so a retry policy or an alert route is changed once rather than in four DAGs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from airflow.models import Variable
from airflow.operators.python import get_current_context
from airflow.utils.email import send_email

SENTINEL_ROOT = Path(os.environ.get("SENTINEL_ROOT", "/opt/sentinel"))

OWNER = "data-platform"

SPIDER_SOURCES = {
    "books": {"host": "books.toscrape.com", "schedule": "0 2 * * *"},
    "book_categories": {"host": "books.toscrape.com", "schedule": "0 3 * * *"},
    "quotes": {"host": "quotes.toscrape.com", "schedule": "15 2 * * *"},
    "countries": {"host": "scrapethissite.com", "schedule": "30 2 * * *"},
    "hockey": {"host": "scrapethissite.com", "schedule": "45 2 * * *"},
}


def default_args(retries: int = 3) -> dict:
    """Exponential backoff, not a fixed retry count: the third retry should not hammer.

    A scrape that failed twice because a target was throttling will often succeed on the
    third attempt minutes later, and will almost never succeed 30 seconds later.
    """
    return {
        "owner": OWNER,
        "depends_on_past": False,
        "retries": retries,
        "retry_delay": timedelta(seconds=30),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=15),
        "email_on_failure": False,  # routed through on_failure_callback instead
        "execution_timeout": timedelta(hours=1),
    }


def on_failure(context) -> None:
    """Alert on failure. Never raises: an alert that breaks the DAG is worse than no alert."""
    try:
        task = context.get("task_instance")
        dag_id = context.get("dag").dag_id if context.get("dag") else "unknown"
        run_id = context.get("run_id", "unknown")
        exception = context.get("exception")

        subject = f"[SENTINEL] {dag_id} failed"
        body = (
            f"<b>DAG</b>: {dag_id}<br>"
            f"<b>Task</b>: {getattr(task, 'task_id', 'unknown')}<br>"
            f"<b>Run</b>: {run_id}<br>"
            f"<b>Try</b>: {getattr(task, 'try_number', '?')}<br>"
            f"<b>Log</b>: {getattr(task, 'log_url', 'n/a')}<br>"
            f"<b>Error</b>: <pre>{exception}</pre>"
        )

        recipients = Variable.get("sentinel_alert_emails", default_var="").strip()
        if recipients:
            send_email(to=recipients.split(","), subject=subject, html_content=body)

        print(json.dumps({
            "event": "dag_failure",
            "dag_id": dag_id,
            "task_id": getattr(task, "task_id", None),
            "run_id": run_id,
            "error": str(exception) if exception else None,
        }))
    except Exception as exc:  # noqa: BLE001 - alerting must not mask the real failure
        print(f"on_failure callback itself failed: {type(exc).__name__}: {exc}")


def on_sla_miss(dag, task_list, blocking_task_list, slas, blocking_tis) -> None:
    """SLA misses are recorded distinctly from failures: late is not the same as broken."""
    print(json.dumps({
        "event": "sla_miss",
        "dag_id": dag.dag_id,
        "tasks": [t.task_id for t in task_list],
        "blocking": [t.task_id for t in blocking_task_list],
    }))


def run_module(module: str, *args: str, timeout: int = 3600, check: bool = True) -> dict:
    """Run a platform module as a subprocess and return its JSON summary.

    Subprocess rather than import: these modules have their own reactors (Scrapy), their
    own heaps (Spark) and their own config loading, and none of that belongs inside a
    scheduler worker process.
    """
    cmd = [sys.executable, "-m", module, *args]
    started = datetime.now(timezone.utc)

    completed = subprocess.run(
        cmd,
        cwd=str(SENTINEL_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    result = {
        "module": module,
        "args": list(args),
        "returncode": completed.returncode,
        "elapsed_s": round(elapsed, 2),
        "stdout_tail": "\n".join(completed.stdout.splitlines()[-40:]),
        "stderr_tail": "\n".join(completed.stderr.splitlines()[-40:]),
    }

    if check and completed.returncode != 0:
        raise RuntimeError(
            f"{module} {' '.join(args)} exited {completed.returncode}\n"
            f"--- stderr ---\n{result['stderr_tail']}"
        )
    return result


def new_run_id() -> str:
    return str(uuid.uuid4())


def record_stage_metric(run_id: str, stage: str, metric: str, value: float) -> None:
    """Write a pipeline counter so dashboards do not have to scan fact tables."""
    try:
        import psycopg2

        from tools.common.config import get_settings

        settings = get_settings()
        with psycopg2.connect(settings.pg.psycopg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO governance.stage_metric (run_id, stage, metric, value)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, stage, metric) DO UPDATE SET value = EXCLUDED.value
                """,
                (run_id, stage, metric, value),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - metrics are best-effort
        print(f"stage metric not recorded ({type(exc).__name__}: {exc})")


def task_context() -> dict:
    ctx = get_current_context()
    return {
        "logical_date": ctx["logical_date"].isoformat(),
        "ds": ctx["ds"],
        "run_id": ctx["run_id"],
        "dag_id": ctx["dag"].dag_id,
        "task_id": ctx["task"].task_id,
    }
