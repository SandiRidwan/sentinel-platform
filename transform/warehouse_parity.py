"""Prove the dbt models are portable, and say exactly how far the proof goes.

There are three different claims that get conflated whenever someone says "it runs on
Snowflake and BigQuery":

    1. the project parses under that adapter       (no connection needed)
    2. the SQL compiles to that dialect            (no connection needed, via sqlglot)
    3. the models actually built and the row counts match   (needs credentials)

Only (3) is a real execution. This module reports all three separately and never lets
one stand in for another. On a machine with no cloud credentials it exits 0 with
`executed_on: ["duckdb"]` and an explicit list of what was *not* executed — a partial
result that says it is partial.

With credentials, `--with-cloud` runs `dbt build` against the target and compares
per-model row counts to DuckDB. That path uses dbt's own connection handling rather
than a hand-written vendor SDK call, so there is one implementation of "how to talk to
Snowflake" in the repo, and it is the one dbt already tests.

    python -m transform.warehouse_parity
    python -m transform.warehouse_parity --with-cloud
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import sqlglot

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("warehouse_parity")

DBT_DIR = Path(__file__).resolve().parent / "dbt"
COMPILED_DIR = DBT_DIR / "target" / "compiled" / "sentinel" / "models"

TARGETS = {
    "dev": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
}

DIALECTS = {"snowflake": "snowflake", "bigquery": "bigquery"}

# A project name is required by the BigQuery adapter at profile load, but parsing does
# not contact Google. A placeholder keeps the parse offline and honest: the report says
# `executed: false` for this target regardless.
PLACEHOLDER_PROJECT = "sentinel-demo"


def dbt_executable() -> str:
    candidate = Path(sys.executable).parent / "dbt.exe"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("dbt")
    if found:
        return found
    raise SystemExit("dbt executable not found next to the interpreter; activate the venv first")


def dbt_env(target: str) -> dict[str, str]:
    env = dict(os.environ)
    env["DBT_PROFILES_DIR"] = str(DBT_DIR)
    env["DBT_TARGET"] = target
    # Absolute: dbt runs with cwd=transform/dbt, so the relative DUCKDB_PATH from .env
    # would resolve inside the dbt project and fail to open.
    env["DUCKDB_PATH"] = str(get_settings().duckdb_path)
    if target == "bigquery" and not env.get("GCP_PROJECT"):
        env["GCP_PROJECT"] = PLACEHOLDER_PROJECT
    return env


def ensure_warehouse_dir() -> None:
    """dbt-duckdb creates the file, not the directory."""
    Path(get_settings().duckdb_path).parent.mkdir(parents=True, exist_ok=True)


def run_dbt(args: list[str], target: str, timeout: int = 600) -> dict:
    command = [dbt_executable(), *args, "--target", target]
    started = time.time()
    proc = subprocess.run(
        command,
        cwd=str(DBT_DIR),
        env=dbt_env(target),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "command": " ".join(args),
        "target": target,
        "exit_code": proc.returncode,
        "elapsed_s": round(time.time() - started, 2),
        "tail": "\n".join(output.strip().splitlines()[-6:]),
    }


def credentials_present(target: str) -> bool:
    required = {
        "snowflake": ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"),
        "bigquery": ("GCP_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"),
    }.get(target, ())
    return all(os.environ.get(name) for name in required)


def compiled_models() -> dict[str, str]:
    """Return {model_name: sql} for real models, from the last `dbt compile`.

    The manifest is the authority on what is a model: the compiled directory also holds
    generic tests, and counting a test as a model would put `-1` row counts in the report
    and make a passing run look broken.
    """
    manifest_path = DBT_DIR / "target" / "manifest.json"
    if not manifest_path.exists() or not COMPILED_DIR.exists():
        return {}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_names = {
        node["name"]
        for node in manifest.get("nodes", {}).values()
        if node.get("resource_type") == "model"
    }

    models: dict[str, str] = {}
    for path in COMPILED_DIR.rglob("*.sql"):
        if path.stem in model_names:
            models[path.stem] = path.read_text(encoding="utf-8")
    return models


def transpile_check(sql: str, dialect: str) -> dict:
    """Transpile to `dialect` and re-parse the result.

    Re-parsing is the part that catches something real: sqlglot can emit a statement it
    cannot read back when a construct has no equivalent, and that is exactly the case
    that would fail on the warehouse.
    """
    try:
        statements = sqlglot.transpile(sql, read="duckdb", write=dialect)
    except Exception as exc:  # noqa: BLE001 - the exception type is library-specific
        return {"ok": False, "stage": "transpile", "error": f"{type(exc).__name__}: {exc}"}

    if not statements:
        return {"ok": False, "stage": "transpile", "error": "transpile produced no statements"}

    try:
        for statement in statements:
            sqlglot.parse_one(statement, read=dialect)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "parse-back", "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "statements": len(statements), "sample": statements[0][:240].replace("\n", " ")}


def duckdb_counts(models: dict[str, str]) -> dict[str, int]:
    """Row counts for every materialised model, read straight from the dev warehouse."""
    settings = get_settings()
    counts: dict[str, int] = {}
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        for model in sorted(models):
            try:
                row = connection.execute(f'SELECT COUNT(*) FROM gold."{model}"').fetchone()
                counts[model] = int(row[0]) if row else 0
            except Exception:  # noqa: BLE001 - a view may live in another schema
                try:
                    row = connection.execute(f'SELECT COUNT(*) FROM silver."{model}"').fetchone()
                    counts[model] = int(row[0]) if row else 0
                except Exception:
                    counts[model] = -1
    finally:
        connection.close()
    return counts


def dbt_show_count(target: str, model: str) -> dict:
    result = run_dbt(
        ["show", "--inline", f"select count(*) as n from {{{{ ref('{model}') }}}}", "--output", "json"],
        target,
        timeout=300,
    )
    if result["exit_code"] != 0:
        return {"ok": False, "error": result["tail"]}
    match = re.search(r'"n"\s*:\s*(\d+)', result["tail"])
    if not match:
        return {"ok": False, "error": "count not found in dbt show output"}
    return {"ok": True, "rows": int(match.group(1))}


def cloud_parity(target: str, models: dict[str, str], local: dict[str, int]) -> dict:
    build = run_dbt(["build"], target, timeout=1800)
    if build["exit_code"] != 0:
        return {"executed": False, "error": build["tail"], "build": build}

    comparisons = []
    mismatches = 0
    for model in sorted(models):
        remote = dbt_show_count(target, model)
        if not remote.get("ok"):
            comparisons.append({"model": model, "error": remote.get("error")})
            mismatches += 1
            continue
        local_rows = local.get(model, -1)
        same = remote["rows"] == local_rows
        if not same:
            mismatches += 1
        comparisons.append({"model": model, "duckdb_rows": local_rows, "remote_rows": remote["rows"], "same": same})

    return {"executed": True, "build": build, "comparisons": comparisons, "mismatches": mismatches}


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Check dbt portability across warehouse targets.")
    parser.add_argument("--with-cloud", action="store_true", help="also build on cloud targets if credentials exist")
    parser.add_argument("--report", default="quality/reports/warehouse_parity.json")
    args = parser.parse_args(argv)

    started = time.time()
    ensure_warehouse_dir()
    report: dict = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "claims": {
            "parsed": "the dbt project and profile load under the adapter; no connection",
            "transpiled": "compiled SQL converts to the dialect and parses back; no connection",
            "executed": "dbt build ran against the warehouse and row counts were compared",
        },
        "targets": {},
    }

    # 1 + 2: parse under every adapter, then compile once against duckdb.
    parse_results = {}
    for target in TARGETS:
        parse_results[target] = run_dbt(["parse"], target, timeout=300)
        log.info("parsed", extra={"target": target, "exit_code": parse_results[target]["exit_code"]})

    compile_result = run_dbt(["compile"], "dev", timeout=900)
    models = compiled_models()
    if compile_result["exit_code"] != 0:
        print(compile_result["tail"])
        raise SystemExit("dbt compile failed against dev; nothing else can be trusted")

    local_counts = duckdb_counts(models)

    for target, adapter in TARGETS.items():
        parse_ok = parse_results[target]["exit_code"] == 0
        entry: dict = {
            "adapter": adapter,
            "parsed": parse_ok,
            "parse_tail": parse_results[target]["tail"] if not parse_ok else "",
            "credentials_present": credentials_present(target),
            "executed": False,
        }

        if target == "dev":
            entry["executed"] = True
            entry["models"] = len(models)
            entry["row_counts"] = local_counts
        else:
            dialect = DIALECTS[target]
            results = {model: transpile_check(sql, dialect) for model, sql in models.items()}
            failed = {model: result for model, result in results.items() if not result["ok"]}
            entry["transpiled_models"] = len(results)
            entry["transpile_failures"] = failed

            if args.with_cloud and entry["credentials_present"]:
                entry["cloud"] = cloud_parity(target, models, local_counts)
                entry["executed"] = bool(entry["cloud"].get("executed"))
            elif args.with_cloud:
                entry["cloud"] = {"executed": False, "reason": "credentials not set"}
            else:
                entry["cloud"] = {"executed": False, "reason": "not requested (pass --with-cloud)"}

        report["targets"][target] = entry

    failures = 0
    for target, entry in report["targets"].items():
        if not entry["parsed"]:
            failures += 1
        if target != "dev" and entry.get("transpile_failures"):
            failures += 1
        if entry.get("cloud", {}).get("mismatches"):
            failures += 1

    report["elapsed_s"] = round(time.time() - started, 2)
    report["result"] = "PASS" if failures == 0 else "FAIL"
    report["executed_on"] = [t for t, e in report["targets"].items() if e["executed"]]
    report["not_executed"] = [t for t, e in report["targets"].items() if not e["executed"]]

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"models compiled        : {len(models)}")
    for target, entry in report["targets"].items():
        state = "executed" if entry["executed"] else "not executed"
        extra = ""
        if target != "dev":
            extra = f" transpiled={entry['transpiled_models']} failures={len(entry['transpile_failures'])}"
        print(f"  {target:<10} parsed={entry['parsed']!s:<5} {state:<12}{extra}")
    print(f"executed on            : {', '.join(report['executed_on'])}")
    print(f"NOT executed           : {', '.join(report['not_executed']) or 'none'}")
    print(f"RESULT                 : {report['result']}")

    if failures:
        for target, entry in report["targets"].items():
            if not entry["parsed"]:
                print(f"  -> {target}: the adapter is not available or the profile is invalid")
                if entry.get("parse_tail"):
                    for line in entry["parse_tail"].splitlines()[-3:]:
                        print(f"     {line}")
                if target != "dev":
                    print("     hint: pip install -r requirements-extra.txt")
            for model, failure in entry.get("transpile_failures", {}).items():
                print(f"  -> {target}.{model}: {failure['stage']}: {failure['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
