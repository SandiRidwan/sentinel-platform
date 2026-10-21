"""Publish silver Parquet as Iceberg tables, with a verifiable time-travel check.

Why Iceberg and not just Parquet on S3: a Parquet directory cannot answer "what did this
table look like yesterday", cannot be safely rewritten by two writers, and breaks readers
when a column is renamed. Iceberg adds a commit log and schema metadata over the same files.

The catalog is a SQLite-backed local catalog and the warehouse is a local directory, so this
runs and is verifiable without a cloud catalog. Swapping in Glue/REST is a config change.

    python -m transform.spark.iceberg_publish
    python -m transform.spark.iceberg_publish --time-travel books
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.schema import Schema
from pyiceberg.types import DoubleType, IntegerType, ListType, LongType, NestedField, StringType

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

from transform.spark import staging

log = get_logger("iceberg")

NAMESPACE = "silver"

# Iceberg schemas, matching SILVER_SCHEMAS in bronze_to_silver. Iceberg requires a
# field-id per column, so this is the explicit declaration that lets a column be renamed
# later without rewriting every file.
ICEBERG_SCHEMAS: dict[str, Schema] = {
    "books": Schema(
        NestedField(1, "title", StringType()),
        NestedField(2, "price", DoubleType()),
        NestedField(3, "currency", StringType()),
        NestedField(4, "availability", StringType()),
        NestedField(5, "stock_count", LongType()),
        NestedField(6, "rating", LongType()),
        NestedField(7, "category", StringType()),
        NestedField(8, "upc", StringType()),
        NestedField(9, "url", StringType()),
        NestedField(10, "_ingested_from", StringType()),
    ),
    "book_categories": Schema(
        NestedField(1, "name", StringType()),
        NestedField(2, "slug", StringType()),
        NestedField(3, "url", StringType()),
        NestedField(4, "_ingested_from", StringType()),
    ),
    "quotes": Schema(
        NestedField(1, "text", StringType()),
        NestedField(2, "author", StringType()),
        NestedField(3, "author_url", StringType()),
        NestedField(4, "tags", ListType(5, StringType(), element_required=False)),
        NestedField(6, "_ingested_from", StringType()),
    ),
    "countries": Schema(
        NestedField(1, "name", StringType()),
        NestedField(2, "capital", StringType()),
        NestedField(3, "population", LongType()),
        NestedField(4, "area_km2", DoubleType()),
        NestedField(5, "_ingested_from", StringType()),
    ),
    "hockey_teams": Schema(
        NestedField(1, "year", IntegerType()),
        NestedField(2, "team", StringType()),
        NestedField(3, "wins", IntegerType()),
        NestedField(4, "losses", IntegerType()),
        NestedField(5, "ot_losses", IntegerType()),
        NestedField(6, "win_pct", DoubleType()),
        NestedField(7, "goals_for", IntegerType()),
        NestedField(8, "goals_against", IntegerType()),
        NestedField(9, "_ingested_from", StringType()),
    ),
}


def catalog_paths() -> tuple[Path, Path]:
    """Return (catalog database file, warehouse directory). Both parents are created."""
    base = get_settings().root / "data" / "iceberg"
    warehouse = base / "warehouse"
    warehouse.mkdir(parents=True, exist_ok=True)
    return base / "catalog.db", warehouse


def build_catalog() -> SqlCatalog:
    """Local SQLite catalog over a filesystem warehouse.

    The warehouse is passed as a path relative to the working directory. Two Windows
    problems are avoided by doing so:
      - "file:///F:/dir" becomes "/F:/dir" inside pyiceberg, which pyarrow rejects
        (WinError 123).
      - a bare "F:/dir" makes urlparse read "f" as the URI scheme
        ("Unrecognized filesystem type in URI: f").
    A relative path has no scheme at all, so the local filesystem is used.
    """
    db_file, warehouse = catalog_paths()
    relative_warehouse = warehouse.relative_to(get_settings().root).as_posix()
    return SqlCatalog(
        "sentinel",
        **{
            "uri": f"sqlite:///{db_file.as_posix()}",
            "warehouse": relative_warehouse,
        },
    )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Publish silver Parquet to Iceberg.")
    parser.add_argument("--time-travel", help="after publishing, show snapshots for one table")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    catalog = build_catalog()

    try:
        catalog.create_namespace(NAMESPACE)
    except NamespaceAlreadyExistsError:
        pass

    silver_dir = staging.local_zone("silver")
    report: dict[str, dict] = {}

    for table_name, schema in ICEBERG_SCHEMAS.items():
        source_dir = silver_dir / table_name
        if not source_dir.exists():
            print(f"  {table_name}: no silver output, skipped")
            report[table_name] = {"status": "skipped", "rows": 0}
            continue

        arrow_table = pq.read_table(source_dir)
        # Iceberg wants the declared column order; Parquet may carry extras.
        declared = [field.name for field in schema.fields]
        keep = [name for name in declared if name in arrow_table.column_names]
        arrow_table = arrow_table.select(keep)

        identifier = f"{NAMESPACE}.{table_name}"
        try:
            table = catalog.load_table(identifier)
            table.append(arrow_table)
        except NoSuchTableError:
            table = catalog.create_table(identifier, schema=schema)
            table.append(arrow_table)

        snapshot = table.current_snapshot()
        rows = table.scan().to_arrow().num_rows

        report[table_name] = {
            "status": "published",
            "rows": rows,
            "appended": arrow_table.num_rows,
            "snapshot_id": snapshot.snapshot_id if snapshot else None,
            "snapshots": len(table.metadata.snapshots),
            "format_version": str(getattr(table.metadata, "format_version", getattr(table, "format_version", "?"))),
        }
        print(
            f"  {table_name}: appended={arrow_table.num_rows} total={rows} "
            f"snapshots={report[table_name]['snapshots']} v{table.metadata.format_version}"
        )

    if args.time_travel:
        show_time_travel(catalog, args.time_travel)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    db, warehouse = catalog_paths()
    out = {
        "ran_at": started.isoformat(),
        "elapsed_s": round(elapsed, 2),
        "catalog": db.as_posix(),
        "warehouse": warehouse.as_posix(),
        "tables": report,
    }
    print()
    print(json.dumps(out, indent=2))

    report_path = Path("quality/reports/iceberg_publish.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


def show_time_travel(catalog: SqlCatalog, table_name: str) -> None:
    """Prove the table has history, which is the whole reason to use Iceberg."""
    identifier = f"{NAMESPACE}.{table_name}"
    try:
        table = catalog.load_table(identifier)
    except NoSuchTableError:
        print(f"  time travel: {identifier} does not exist")
        return

    snapshots = table.metadata.snapshots
    print()
    print(f"  time travel on {identifier}: {len(snapshots)} snapshot(s)")
    for snapshot in snapshots:
        scanned = table.scan(snapshot_id=snapshot.snapshot_id).to_arrow()
        print(f"    snapshot {snapshot.snapshot_id}  rows={scanned.num_rows}  op={snapshot.summary.get('operation')}")

    if len(snapshots) >= 2:
        first, last = snapshots[0], snapshots[-1]
        first_rows = table.scan(snapshot_id=first.snapshot_id).to_arrow().num_rows
        last_rows = table.scan(snapshot_id=last.snapshot_id).to_arrow().num_rows
        print(f"    earlier snapshot sees {first_rows} rows; latest sees {last_rows}")


if __name__ == "__main__":
    raise SystemExit(main())
