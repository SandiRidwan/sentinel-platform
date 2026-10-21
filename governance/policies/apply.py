"""Apply the column-security policy and the retention rules.

Two things this does that a YAML file alone does not:

  1. Validates the policy against the actual warehouse. A policy that names a column which
     no longer exists is worse than no policy: it looks enforced and is not.
  2. Produces the dev-target enforcement — a DuckDB view per table that projects only the
     columns a consumer is allowed to see, so the rule is exercised locally.

    python -m governance.policies.apply
    python -m governance.policies.apply --validate-only
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import yaml

from tools.common.config import ROOT, get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("governance.policies")

POLICY = ROOT / "governance" / "policies" / "column_security.yml"

# Columns a consumer role may see. Masked and hashed columns are excluded from the view
# rather than returned as NULL, because a NULL looks like missing data and invites someone
# to "fix" it.
CONSUMER_ALLOWED = {"allow"}


def load_policy() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def validate(policy: dict, connection) -> list[dict]:
    results: list[dict] = []
    for entry in policy.get("columns", []):
        table, column = entry["table"], entry["column"]
        try:
            found = connection.execute(
                "select count(*) from information_schema.columns "
                "where lower(table_name) = lower(?) and lower(column_name) = lower(?)",
                [table, column],
            ).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            results.append({"table": table, "column": column, "status": "error", "error": str(exc)})
            continue
        results.append({
            "table": table,
            "column": column,
            "action": entry.get("action"),
            "status": "ok" if found else "column_missing",
        })
    return results


def build_consumer_views(policy: dict, connection) -> list[str]:
    """Create one `consumer_<table>` view per table, exposing only allowed columns."""
    by_table: dict[str, list[str]] = {}
    for entry in policy.get("columns", []):
        if entry.get("action") in CONSUMER_ALLOWED:
            by_table.setdefault(entry["table"], []).append(entry["column"])

    created: list[str] = []
    for table, columns in by_table.items():
        if not columns:
            continue
        # Find the schema the table actually lives in.
        row = connection.execute(
            "select table_schema from information_schema.columns "
            "where lower(table_name) = lower(?) limit 1",
            [table],
        ).fetchone()
        if not row:
            continue
        schema = row[0]
        projection = ", ".join(f'"{c}"' for c in columns)
        view = f"consumer_{table}"
        connection.execute(
            f"create or replace view {schema}.{view} as select {projection} from {schema}.{table}"
        )
        created.append(f"{schema}.{view}")
    return created


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Validate and apply the column-security policy.")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)

    policy = load_policy()
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=False)

    try:
        print(f"Policy {POLICY.name} (reviewed {policy.get('reviewed_at')})")
        results = validate(policy, connection)
        missing = [r for r in results if r["status"] != "ok"]
        for result in results:
            mark = "OK  " if result["status"] == "ok" else "MISS"
            print(f"  {mark} {result['table']}.{result['column']:<18} {result.get('action', '')}")

        created: list[str] = []
        if not args.validate_only and not missing:
            created = build_consumer_views(policy, connection)
            for view in created:
                print(f"  view {view}")
    finally:
        connection.close()

    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "columns": results,
        "missing": [f"{r['table']}.{r['column']}" for r in missing],
        "views_created": created,
        "separation": policy.get("separation", {}),
        "retention": policy.get("retention", {}),
    }

    path = Path("quality/reports/column_security.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    if missing:
        # A policy naming a column that does not exist is a policy that is not enforced.
        print(f"FAILED: {len(missing)} policy entries reference columns that do not exist")
        return 1
    print(f"PASS: {len(results)} policy entries validated, {len(created)} consumer views")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
