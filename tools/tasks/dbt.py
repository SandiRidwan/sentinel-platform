"""dbt runner.

Wraps dbt so every entry point (make, tasks.ps1, the Airflow DAG) invokes it the same way:
the project directory and profiles directory are set here, not repeated at each call site.

    python -m tools.tasks.dbt build
    python -m tools.tasks.dbt test --select gold
    python -m tools.tasks.dbt docs generate
    python -m tools.tasks.dbt debug
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tools.common.config import ROOT, get_settings

PROJECT_DIR = ROOT / "transform" / "dbt"
PROFILES_DIR = PROJECT_DIR


def dbt_env() -> dict[str, str]:
    settings = get_settings()
    env = {
        **os.environ,
        "DBT_PROFILES_DIR": str(PROFILES_DIR),
        "DBT_PROJECT_DIR": str(PROJECT_DIR),
        "DBT_TARGET": settings.dbt_target,
        "DUCKDB_PATH": str(settings.duckdb_path),
    }
    return env


def silver_root() -> str:
    """Absolute path to the silver zone.

    dbt views materialise the path they were compiled with. A relative path is baked into
    the view, so the view only resolves while the process cwd happens to be the dbt project
    directory — which is true for `dbt run` and false for anything reading the warehouse
    afterwards. Absolute is the only version that survives both.
    """
    return (ROOT / "data" / "staging" / "zones" / "silver").as_posix()


def run_dbt(args: list[str]) -> int:
    cmd = [sys.executable, "-m", "dbt.cli.main", *args]
    if "--vars" not in args:
        cmd.extend(["--vars", f'{{"silver_root": "{silver_root()}"}}'])
    print(f">> dbt {' '.join(args)}  (target={dbt_env()['DBT_TARGET']})")

    completed = subprocess.run(
        cmd,
        cwd=str(PROJECT_DIR),
        env=dbt_env(),
        text=True,
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["build"]
    return run_dbt(args)


if __name__ == "__main__":
    raise SystemExit(main())
