"""Run the same pipeline through three orchestrators and compare what landed.

The claim being tested is narrow and worth stating exactly: the three orchestrators produce
the same number of rows in every gold table. It is not "the orchestrators are equivalent" and
not "the pipeline is correct" — that is what the four quality levels are for.

Airflow runs inside its own container, because that is how it actually runs. Prefect and
Dagster run on the host. Each wrapper writes its result to the same file path, so the
comparison reads a value instead of scraping a scheduler log.

    python -m orchestration.parity
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from orchestration.shared.pipeline import GOLD_TABLES

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path("quality/reports/orchestration_parity.json")


def _run_airflow(out_file: Path) -> dict:
    """Airflow's task test, inside the scheduler container.

    The result is written under `data/`, which compose mounts at /opt/sentinel/data. A host
    temp path would not exist inside the container, and the task would fail for a reason that
    has nothing to do with the pipeline.
    """
    container_path = "/opt/sentinel/data/_parity/airflow.json"
    host_path = ROOT / "data" / "_parity" / "airflow.json"
    host_path.parent.mkdir(parents=True, exist_ok=True)
    if host_path.exists():
        host_path.unlink()

    command = [
        "docker", "compose", "exec", "-T",
        "-e", f"SENTINEL_PARITY_OUT={container_path}",
        "airflow-scheduler",
        "airflow", "tasks", "test", "pipeline_parity", "run_pipeline", "2026-10-22",
    ]
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
    if result.returncode != 0 or not host_path.exists():
        tail = "\n".join(((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-10:])
        raise RuntimeError(f"airflow failed (exit {result.returncode}):\n{tail}")
    return json.loads(host_path.read_text(encoding="utf-8"))


def _run_host(module: str, out_file: Path, extra_env: dict[str, str] | None = None) -> dict:
    env = {**os.environ, "SENTINEL_PARITY_OUT": str(out_file), **(extra_env or {})}
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        tail = "\n".join(((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-8:])
        raise RuntimeError(f"{module} failed:\n{tail}")
    return json.loads(out_file.read_text(encoding="utf-8"))


def _compare(results: dict[str, dict], expected: list[str]) -> tuple[dict, list[str]]:
    """Compare row counts across orchestrators. Separated so the self-test can call it."""
    comparison: dict[str, dict] = {}
    mismatched: list[str] = []

    for table in GOLD_TABLES:
        values = {name: results[name]["counts"].get(table) for name in results}
        same = len(set(values.values())) == 1
        comparison[table] = {"values": values, "identical": same}
        if not same:
            mismatched.append(table)

    if sorted(results) != sorted(expected):
        mismatched.append("incomplete: not every orchestrator produced a result")

    return comparison, mismatched


def _self_test() -> int:
    """A comparison that cannot report a difference is decoration.

    Feed it two results that differ by one row and require it to notice, then feed it
    identical results and require it to pass. Both directions, or the check is untested.
    """
    base = {"counts": {table: 10 for table in GOLD_TABLES}}
    same = {"a": dict(base), "b": dict(base), "c": dict(base)}
    _, mismatched_same = _compare(same, ["a", "b", "c"])

    differs = {"a": dict(base), "b": dict(base), "c": {"counts": {**base["counts"], "dim_book": 11}}}
    comparison, mismatched_differs = _compare(differs, ["a", "b", "c"])

    ok = (
        not mismatched_same
        and mismatched_differs == ["dim_book"]
        and comparison["dim_book"]["identical"] is False
    )
    print(f"self-test: identical -> {mismatched_same or 'no mismatch'}")
    print(f"self-test: one row apart -> {mismatched_differs or 'NOT DETECTED'}")
    print(f"self-test: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    started = datetime.now(timezone.utc)
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-parity-"))

    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    runners = {
        "airflow": lambda out: _run_airflow(out),
        "prefect": lambda out: _run_host(
            "orchestration.prefect.flows", out, {"PREFECT_HOME": str(tmp / "prefect")}
        ),
        "dagster": lambda out: _run_host("orchestration.dagster.assets", out),
    }

    for name, runner in runners.items():
        try:
            results[name] = runner(tmp / f"{name}.json")
            print(f"{name:10s} ok")
        except Exception as exc:  # noqa: BLE001 - the message is the result
            errors[name] = str(exc)
            print(f"{name:10s} FAILED")
            print(f"  {str(exc).splitlines()[0][:160]}")

    # Compare only the tables all three reported. A missing table is a failure, not a skip.
    comparison: dict[str, dict] = {}
    mismatched: list[str] = []
    if len(results) == len(runners):
        for table in GOLD_TABLES:
            values = {name: results[name]["counts"].get(table) for name in results}
            same = len(set(values.values())) == 1
            comparison[table] = {"values": values, "identical": same}
            if not same:
                mismatched.append(table)
    else:
        mismatched.append("incomplete: not every orchestrator produced a result")

    identical = bool(results) and not mismatched and len(results) == len(runners)

    report = {
        "ran_at": started.isoformat(),
        "orchestrators": sorted(runners),
        "tables_compared": list(GOLD_TABLES),
        "comparison": comparison,
        "mismatched": mismatched,
        "errors": errors,
        "counts_by_orchestrator": {name: r.get("counts", {}) for name, r in results.items()},
        "result": "PASS" if identical else "FAIL",
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    for table, item in comparison.items():
        marks = " ".join(f"{name}={value}" for name, value in item["values"].items())
        print(f"  {'OK  ' if item['identical'] else 'DIFF'} {table:20s} {marks}")
    print()
    print(f"RESULT: {report['result']}")
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
