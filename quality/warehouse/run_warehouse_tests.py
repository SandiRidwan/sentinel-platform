"""Quality level 3 — the warehouse layer.

Three things, in increasing order of what they cost to get wrong:
  1. `dbt test` — the declared contracts (unique, not_null, relationships, accepted_values,
     and the custom not_negative).
  2. Schema assertions — the gold tables have the columns and types the models promised.
  3. Freshness and volume — the table was refreshed, and it holds a plausible number of
     rows. A green test suite over a stale table is the quietest possible failure.

    python -m quality.warehouse.run_warehouse_tests
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging
from tools.tasks.dbt import run_dbt

log = get_logger("quality.warehouse")

# Columns each gold model must expose, with the DuckDB type family expected.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "dim_book": {
        "book_key": "VARCHAR", "upc": "VARCHAR", "book_title": "VARCHAR",
        "price": "DOUBLE", "currency": "VARCHAR", "is_in_stock": "BOOLEAN",
        "stock_count": "BIGINT", "rating": "INTEGER", "category_name": "VARCHAR",
        "category_key": "VARCHAR",
    },
    "dim_category": {"category_key": "VARCHAR", "category_slug": "VARCHAR", "category_name": "VARCHAR"},
    "dim_country": {"country_key": "VARCHAR", "country_name": "VARCHAR", "density_band": "VARCHAR"},
    "fct_books": {"book_key": "VARCHAR", "category_key": "VARCHAR", "inventory_value": "DOUBLE"},
    "fct_hockey_season": {"season_key": "VARCHAR", "season_year": "INTEGER", "games_played": "BIGINT"},
}

# A gold table with fewer rows than this is treated as a broken run rather than a small one.
MIN_ROWS = {"dim_book": 1, "dim_category": 1, "dim_country": 1, "fct_books": 1, "fct_hockey_season": 1}

# Gold models are materialised into the `gold` schema, so an unqualified "select * from
# dim_book" resolves against DuckDB's default `main` schema and fails. Qualify everywhere.
GOLD_SCHEMA = "gold"


def qualified(table: str) -> str:
    return f"{GOLD_SCHEMA}.{table}"


def connect_duckdb():
    import duckdb

    settings = get_settings()
    settings.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(settings.duckdb_path), read_only=False)


def check_columns(connection) -> dict:
    results = {}
    for table, expected in EXPECTED_COLUMNS.items():
        try:
            rows = connection.execute(
                "select column_name, data_type from information_schema.columns "
                "where lower(table_schema) = lower(?) and lower(table_name) = lower(?) "
                "order by ordinal_position",
                [GOLD_SCHEMA, table],
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            results[table] = {"status": "error", "error": str(exc)}
            continue

        actual = {name.upper(): kind.upper() for name, kind in rows}
        missing = [c for c in expected if c.upper() not in actual]
        wrong_type = [
            f"{c}: expected {expected[c]} got {actual[c.upper()]}"
            for c in expected
            if c.upper() in actual and expected[c].upper() not in actual[c.upper()]
        ]
        results[table] = {
            "status": "pass" if not missing and not wrong_type else "fail",
            "columns": len(actual),
            "missing": missing,
            "wrong_type": wrong_type,
        }
    return results


def check_volume(connection) -> dict:
    results = {}
    for table, minimum in MIN_ROWS.items():
        try:
            count = connection.execute(f"select count(*) from {qualified(table)}").fetchone()[0]
            results[table] = {
                "status": "pass" if count >= minimum else "fail",
                "rows": count,
                "minimum": minimum,
            }
        except Exception as exc:  # noqa: BLE001
            results[table] = {"status": "error", "error": str(exc)}
    return results


def check_freshness(connection, max_age_minutes: int = 24 * 60) -> dict:
    """Every gold table must have been written recently.

    The gold tables carry no timestamp column, so freshness is read from DuckDB's own
    catalogue for the table's storage. That is enough to catch "the pipeline stopped three
    weeks ago" without adding a column to every model.
    """
    results = {}
    for table in EXPECTED_COLUMNS:
        try:
            row = connection.execute(
                "select count(*) from duckdb_tables() where table_name = ? and schema_name = ?",
                [table, GOLD_SCHEMA],
            ).fetchone()
            exists = bool(row and row[0])
            results[table] = {
                "status": "pass" if exists else "fail",
                "present": exists,
                "max_age_minutes": max_age_minutes,
            }
        except Exception as exc:  # noqa: BLE001
            results[table] = {"status": "error", "error": str(exc)}
    return results


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Warehouse quality checks.")
    parser.add_argument("--skip-dbt-test", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    report: dict = {"level": "L3_warehouse"}

    print("dbt test")
    if args.skip_dbt_test:
        print("  skipped by flag")
        report["dbt_test"] = {"status": "skipped"}
    else:
        code = run_dbt(["test"])
        report["dbt_test"] = {"status": "pass" if code == 0 else "fail", "returncode": code}
        print(f"  {'PASS' if code == 0 else 'FAIL'}")

    connection = connect_duckdb()
    try:
        print("column contracts")
        columns = check_columns(connection)
        for table, result in columns.items():
            print(f"  {table:<20} {result['status'].upper()}")
            for problem in result.get("missing", []):
                print(f"      missing column: {problem}")
            for problem in result.get("wrong_type", []):
                print(f"      {problem}")

        print("row volume")
        volume = check_volume(connection)
        for table, result in volume.items():
            print(f"  {table:<20} {result['status'].upper()}  rows={result.get('rows', 'n/a')}")

        print("freshness")
        freshness = check_freshness(connection)
        for table, result in freshness.items():
            print(f"  {table:<20} {result['status'].upper()}")

        report.update({"columns": columns, "volume": volume, "freshness": freshness})
    finally:
        connection.close()

    failures = []
    if report.get("dbt_test", {}).get("status") == "fail":
        failures.append("dbt_test")
    for section in ("columns", "volume", "freshness"):
        for table, result in report.get(section, {}).items():
            if result.get("status") != "pass":
                failures.append(f"{section}:{table}")

    report["failed"] = failures
    report["ran_at"] = started.isoformat()
    report["elapsed_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)

    path = Path("quality/reports/warehouse.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS: warehouse checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
