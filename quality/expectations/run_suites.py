"""Quality level 4 — statistical expectations on the gold tables.

The first three levels check things that are true or false. This level checks things that
are plausible or not: the row count is in the right band, a column has no unexpected null
rate, a value distribution has not collapsed. That is what catches a pipeline that is
technically correct and quietly wrong.

    python -m quality.expectations.run_suites
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("quality.expectations")

# name -> (loader, expectations)
# Each expectation is (expectation_method, kwargs, description).


def _load(table: str) -> pd.DataFrame:
    import duckdb

    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        return connection.execute(f"select * from {GOLD_SCHEMA}.{table}").fetchdf()
    finally:
        connection.close()


GOLD_SCHEMA = "gold"


SUITES: dict[str, callable] = {
    "dim_book": lambda v: [
        v.expect_table_row_count_to_be_between(min_value=1, max_value=100_000),
        v.expect_column_values_to_not_be_null("book_key"),
        v.expect_column_values_to_be_unique("book_key"),
        v.expect_column_values_to_not_be_null("book_title"),
        v.expect_column_values_to_be_between("price", min_value=0, max_value=10_000, mostly=1.0),
        v.expect_column_values_to_be_in_set("currency", ["GBP", "USD", "EUR"], mostly=0.9),
        v.expect_column_values_to_be_between("rating", min_value=1, max_value=5, mostly=0.9),
    ],
    "fct_books": lambda v: [
        v.expect_table_row_count_to_be_between(min_value=1, max_value=100_000),
        v.expect_column_values_to_not_be_null("book_key"),
        v.expect_column_values_to_be_unique("book_key"),
        v.expect_column_values_to_be_between("inventory_value", min_value=0, mostly=1.0),
    ],
    "dim_country": lambda v: [
        v.expect_table_row_count_to_be_between(min_value=100, max_value=500),
        v.expect_column_values_to_be_unique("country_name"),
        v.expect_column_values_to_be_in_set(
            "density_band", ["unknown", "low", "medium", "high", "very_high"], mostly=1.0
        ),
    ],
    "fct_hockey_season": lambda v: [
        v.expect_table_row_count_to_be_between(min_value=1, max_value=100_000),
        v.expect_column_values_to_be_between("season_year", min_value=1900, max_value=2100),
        v.expect_column_values_to_be_between("games_played", min_value=0, max_value=200, mostly=1.0),
    ],
}


def build_validator(df: pd.DataFrame):
    """Return a validator for a dataframe, across great_expectations API generations.

    gx.from_pandas is the shortest path and has survived several releases; the newer
    datasource API is used if it is absent. Being explicit about the fallback beats
    pinning a version and hoping.
    """
    import great_expectations as gx

    if hasattr(gx, "from_pandas"):
        return gx.from_pandas(df)

    context = gx.get_context()
    datasource = context.sources.pandas_default
    asset = datasource.read_dataframe(df)
    batch = asset.get_batch_list_from_batch_request(asset.batch_request)[0]
    return context.get_validator(batch_request=asset.batch_request, batch=batch)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Run expectation suites.")
    parser.add_argument("--table", action="append")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    tables = args.table or list(SUITES)
    print(f"Expectation suites ({len(tables)} tables)")

    report: dict = {"level": "L4_expectations", "tables": {}}
    failures: list[str] = []

    for table in tables:
        try:
            df = _load(table)
        except Exception as exc:  # noqa: BLE001
            print(f"  {table:<22} SKIP (cannot read: {exc})")
            report["tables"][table] = {"status": "skip", "error": str(exc)}
            continue

        validator = build_validator(df)
        for expectation in SUITES[table](validator):
            expectation  # executed by the call above

        results = validator.validate()
        statistics = results.get("statistics", {})
        evaluated = statistics.get("evaluated_expectations", 0)
        successful = statistics.get("successful_expectations", 0)
        failed = statistics.get("unsuccessful_expectations", 0)

        status = "pass" if results.get("success") else "fail"
        print(f"  {table:<22} {status.upper():<5} {successful}/{evaluated} expectations")
        if status == "fail":
            failures.append(table)
            for result in results.get("results", []):
                if not result.get("success"):
                    config = result.get("expectation_config", {})
                    unexpected = result.get("result", {}).get("unexpected_percent")
                    print(
                        f"      {config.get('expectation_type')} "
                        f"{config.get('kwargs', {}).get('column', '')} "
                        f"unexpected={unexpected}%"
                    )

        report["tables"][table] = {
            "status": status,
            "rows": len(df),
            "evaluated": evaluated,
            "successful": successful,
            "failed": failed,
        }

    report["failed"] = failures
    report["ran_at"] = started.isoformat()
    report["elapsed_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)

    path = Path("quality/reports/expectations.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print()
    verified = [t for t in tables if report["tables"].get(t, {}).get("status") == "pass"]
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    if not verified:
        # Every table was skipped. Reporting PASS here would be the exact lie this level
        # exists to prevent: a suite that ran nothing has verified nothing.
        print("FAILED: nothing was verified (every table was skipped)")
        return 1
    print(f"PASS: {len(verified)}/{len(tables)} expectation suites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
