"""One implementation of the pipeline, called by three orchestrators.

The parity check is only meaningful if the three runs execute the same code. Copying the
steps into an Airflow DAG, a Prefect flow and a Dagster job would prove that three copies
agree, which is a much weaker claim and one that decays the first time somebody edits one
of them.

So the work lives here, once, and each orchestrator is a thin wrapper that calls
`run_pipeline()`. If the row counts differ, the orchestrator is the variable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from tools.common.config import get_settings

ROOT = Path(__file__).resolve().parents[2]

# The tables the gold layer is expected to contain. Listed explicitly rather than discovered,
# so a table that silently disappears from the build is a failure instead of a shorter list.
GOLD_TABLES = (
    "dim_book",
    "dim_category",
    "dim_country",
    "fct_books",
    "fct_hockey_season",
)


@dataclass
class PipelineResult:
    counts: dict[str, int]
    silver: dict[str, int]
    dbt_exit_code: int
    transform_exit_code: int

    def as_dict(self) -> dict:
        return {
            "counts": self.counts,
            "silver": self.silver,
            "dbt_exit_code": self.dbt_exit_code,
            "transform_exit_code": self.transform_exit_code,
        }


def _run(module: str) -> int:
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = "\n".join(((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-8:])
        raise RuntimeError(f"{module} failed with {result.returncode}:\n{tail}")
    return result.returncode


def _counts() -> tuple[dict[str, int], dict[str, int]]:
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        gold: dict[str, int] = {}
        for table in GOLD_TABLES:
            try:
                row = connection.execute(f'SELECT COUNT(*) FROM gold."{table}"').fetchone()
                gold[table] = int(row[0]) if row else 0
            except Exception:
                gold[table] = -1

        silver: dict[str, int] = {}
        try:
            rows = connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'silver' ORDER BY 1"
            ).fetchall()
            for (name,) in rows:
                row = connection.execute(f'SELECT COUNT(*) FROM silver."{name}"').fetchone()
                silver[name] = int(row[0]) if row else 0
        except Exception:
            pass
        return gold, silver
    finally:
        connection.close()


def run_pipeline() -> dict:
    """Transform from raw to silver, build the gold layer, and report what landed.

    Idempotent: the transform overwrites its zone and dbt rebuilds its models, so running it
    twice leaves the same row counts. That property is what makes the three-way comparison
    possible at all.
    """
    transform_exit = _run("transform.spark.local_fallback")
    dbt_exit = _run("tools.tasks.dbt")
    gold, silver = _counts()
    return PipelineResult(
        counts=gold, silver=silver, dbt_exit_code=dbt_exit, transform_exit_code=transform_exit
    ).as_dict()


def main() -> int:
    print(json.dumps(run_pipeline(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
