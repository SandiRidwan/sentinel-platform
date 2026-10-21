"""Load the gold layer from DuckDB into ClickHouse.

Why a load step at all: the warehouse is the system of record and ClickHouse is the serving
layer. Copying the aggregated result, rather than pointing the dashboard at the warehouse,
keeps dashboard queries away from the tables the pipeline is writing.

    python -m analytics.clickhouse.load
    python -m analytics.clickhouse.load --table fct_books
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import clickhouse_connect
import duckdb

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("clickhouse.load")

GOLD_SCHEMA = "gold"

# table -> columns to copy, in the order ClickHouse expects
TABLES: dict[str, list[str]] = {
    "dim_category": ["category_key", "category_slug", "category_name", "category_url"],
    "dim_country": [
        "country_key", "country_name", "capital_name", "population",
        "area_km2", "population_density", "density_band",
    ],
    "fct_books": [
        "book_key", "category_key", "upc", "currency", "price", "stock_count",
        "rating", "is_in_stock", "inventory_value", "is_rated",
    ],
    "fct_hockey_season": [
        "season_key", "season_year", "team_name", "wins", "losses", "ot_losses",
        "win_pct", "goals_for", "goals_against", "goal_difference",
        "games_played", "win_rate_recomputed",
    ],
}


def clickhouse_client():
    settings = get_settings()
    return clickhouse_connect.get_client(
        host=settings.ch.host,
        port=settings.ch.port,
        username=settings.ch.user,
        password=settings.ch.password,
        database=settings.ch.db,
    )


def read_gold(table: str, columns: list[str]) -> list[tuple]:
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        select_list = ", ".join(columns)
        rows = connection.execute(
            f"select {select_list} from {GOLD_SCHEMA}.{table}"
        ).fetchall()
        return rows
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Load gold tables into ClickHouse.")
    parser.add_argument("--table", action="append")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    tables = args.table or list(TABLES)
    client = clickhouse_client()
    report: dict[str, dict] = {}

    for table in tables:
        columns = TABLES[table]
        try:
            rows = read_gold(table, columns)
        except Exception as exc:  # noqa: BLE001
            print(f"  {table:<22} SKIP (cannot read gold: {exc})")
            report[table] = {"status": "skip", "error": str(exc)}
            continue

        if not rows:
            print(f"  {table:<22} SKIP (no rows)")
            report[table] = {"status": "skip", "rows": 0}
            continue

        # Truncate then insert: ReplacingMergeTree deduplicates on merge, not on insert, so
        # a reload without a truncate would double the table until a merge happened.
        client.command(f"TRUNCATE TABLE IF EXISTS {settings_table(table)}")
        client.insert(settings_table(table), rows, column_names=columns)

        count = client.command(f"SELECT count() FROM {settings_table(table)}")
        print(f"  {table:<22} loaded rows={len(rows)} stored={count}")
        report[table] = {"status": "loaded", "rows": len(rows), "stored": int(count)}

    # Prove the analytical layer answers a real question.
    try:
        top = client.query(
            "SELECT category_name, products, inventory_value "
            "FROM sentinel.v_inventory_by_category LIMIT 5"
        ).result_rows
        print()
        print("  inventory value by category (top 5):")
        for name, products, value in top:
            print(f"    {str(name or '(none)'):<24} products={products:<4} value={value}")
        report["_view"] = {"rows": len(top)}
    except Exception as exc:  # noqa: BLE001
        print(f"  view query failed: {exc}")
        report["_view"] = {"error": str(exc)}

    client.close()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    out = {"ran_at": started.isoformat(), "elapsed_s": round(elapsed, 2), "tables": report}
    print()
    print(json.dumps(out, indent=2))

    path = Path("quality/reports/clickhouse_load.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


def settings_table(table: str) -> str:
    return f"{get_settings().ch.db}.{table}"


if __name__ == "__main__":
    raise SystemExit(main())
