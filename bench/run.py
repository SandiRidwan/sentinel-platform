"""Run the capacity benchmark and write down what it measured.

Four numbers, each from a different layer, because they fail for different reasons:

    generate   rows/s into Parquet          — the writer, not the engine
    build      seconds for a real dbt model — query planning + shuffle
    query      p50 / p95 over 20 runs       — what a dashboard actually feels
    iceberg    append size vs compacted     — file layout, not row count

Iceberg is measured on a subset (`--iceberg-rows`, default 1M) and the report says so.
pyiceberg 0.9 has no `rewrite_data_files`, so "compaction" here is an explicit
single-file rewrite via `overwrite`; calling it compaction without saying that would be
the kind of claim this repository exists to avoid.

    python -m bench.run
    python -m bench.run --rows 1000000 --skip-generate
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bench.gen import generate
from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("bench.run")

REPORT = Path("quality/reports/bench_10m.json")
DBT_DIR = Path(__file__).resolve().parents[1] / "transform" / "dbt"

# The query a dashboard would run: a group-by over the whole table with an aggregate.
DASHBOARD_QUERY = """
select
    category_name,
    count(*)              as products,
    avg(price)            as avg_price,
    sum(inventory_value)  as inventory_value,
    avg(stock_count)      as avg_stock
from gold.bench_books
group by category_name
order by inventory_value desc
"""


def run_dbt_build(rows: int) -> dict:
    import subprocess
    import sys

    executable = Path(sys.executable).parent / "dbt.exe"
    command = [
        str(executable),
        "build",
        "--select",
        "bench_books",
        "--vars",
        "{bench: true}",
        "--target",
        "dev",
    ]
    env = {
        **__import__("os").environ,
        "DBT_PROFILES_DIR": str(DBT_DIR),
        "DBT_TARGET": "dev",
        # Absolute on purpose. dbt runs with cwd=transform/dbt, so a relative DUCKDB_PATH
        # from .env resolves to transform/dbt/data/... and dbt fails with "cannot open
        # file". The path has to be resolved by the caller that knows the repo root.
        "DUCKDB_PATH": str(get_settings().duckdb_path),
    }
    started = time.perf_counter()
    proc = subprocess.run(command, cwd=str(DBT_DIR), env=env, capture_output=True, text=True, timeout=7200)
    elapsed = time.perf_counter() - started
    tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-5:])
    return {
        "exit_code": proc.returncode,
        "seconds": round(elapsed, 2),
        "rows_per_second": int(rows / elapsed) if elapsed else 0,
        "tail": tail,
    }


def measure_queries(runs: int) -> dict:
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        connection.execute("select 1").fetchone()
        timings: list[float] = []
        row_count = 0
        for index in range(runs):
            started = time.perf_counter()
            result = connection.execute(DASHBOARD_QUERY).fetchall()
            elapsed = (time.perf_counter() - started) * 1000
            # The first run pays for reading parquet metadata; it is recorded but a
            # separate warm-up number is also reported so p50 is not inflated by it.
            timings.append(elapsed)
            row_count = len(result)
            if index == 0:
                log.info("first query (cold)", extra={"ms": round(elapsed, 1)})

        warm = sorted(timings[1:]) if len(timings) > 1 else timings

        def percentile(values: list[float], fraction: float) -> float:
            if not values:
                return 0.0
            position = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
            return round(sorted(values)[position], 2)

        return {
            "runs": runs,
            "result_groups": row_count,
            "cold_ms": round(timings[0], 2),
            "p50_ms": round(statistics.median(warm), 2),
            "p95_ms": percentile(warm, 0.95),
            "min_ms": round(min(warm), 2),
            "max_ms": round(max(warm), 2),
        }
    finally:
        connection.close()


def measure_iceberg(rows: int, batch: int) -> dict:
    """Append in batches, then rewrite as one file, and report both layouts."""
    from cdc.catalog import build_catalog
    from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
    from pyiceberg.schema import Schema
    from pyiceberg.types import DoubleType, LongType, NestedField, StringType

    schema = Schema(
        NestedField(1, "upc", StringType(), required=True),
        NestedField(2, "category", StringType()),
        NestedField(3, "price", DoubleType()),
        NestedField(4, "stock_count", LongType()),
        NestedField(5, "inventory_value", DoubleType()),
    )
    arrow_schema = pa.schema(
        [
            pa.field("upc", pa.string(), nullable=False),
            pa.field("category", pa.string()),
            pa.field("price", pa.float64()),
            pa.field("stock_count", pa.int64()),
            pa.field("inventory_value", pa.float64()),
        ]
    )

    catalog = build_catalog()
    try:
        catalog.create_namespace("bench")
    except NamespaceAlreadyExistsError:
        pass

    identifier = "bench.books"
    try:
        catalog.drop_table(identifier)
    except NoSuchTableError:
        pass
    table = catalog.create_table(identifier, schema=schema)

    categories = ("books", "electronics", "garden", "kitchen", "office", "toys")
    started = time.perf_counter()
    written = 0
    while written < rows:
        size = min(batch, rows - written)
        indices = range(written, written + size)
        batch_table = pa.Table.from_pydict(
            {
                "upc": [f"{index:012d}" for index in indices],
                "category": [categories[index % len(categories)] for index in indices],
                "price": [round(5.0 + (index % 9973) * 0.01, 2) for index in indices],
                "stock_count": [index % 500 for index in indices],
                "inventory_value": [round((5.0 + (index % 9973) * 0.01) * (index % 500), 2) for index in indices],
            },
            schema=arrow_schema,
        )
        table.append(batch_table)
        written += size
        del batch_table
    append_seconds = time.perf_counter() - started

    def warehouse_files() -> tuple[int, int, int]:
        """(data files, metadata files, total bytes) under the Iceberg warehouse."""
        data = metadata = 0
        total = 0
        for path in (get_settings().root / "data" / "iceberg" / "warehouse").rglob("*"):
            if not path.is_file():
                continue
            total += path.stat().st_size
            if path.suffix == ".parquet":
                data += 1
            else:
                metadata += 1
        return data, metadata, total

    data_files_append, meta_files_append, size_after_append = warehouse_files()

    # Single-file rewrite. `overwrite` writes one data file for the new snapshot, but the
    # old snapshot's files stay on disk: Iceberg keeps history by design, and pyiceberg
    # 0.9 exposes no expire_snapshots/remove_files. So this does NOT reclaim space, and
    # the report says so instead of presenting the rewrite as compaction.
    scan = table.scan().to_arrow()
    started = time.perf_counter()
    table.overwrite(scan)
    rewrite_seconds = time.perf_counter() - started
    data_files_rewrite, meta_files_rewrite, size_after_rewrite = warehouse_files()
    rows_verified = table.scan().to_arrow().num_rows
    snapshots = len(table.metadata.snapshots)

    return {
        "rows": rows,
        "batch_rows": batch,
        "append_seconds": round(append_seconds, 2),
        "append_rows_per_second": int(rows / append_seconds) if append_seconds else 0,
        "data_files_after_append": data_files_append,
        "data_files_after_rewrite": data_files_rewrite,
        "metadata_files_after_append": meta_files_append,
        "metadata_files_after_rewrite": meta_files_rewrite,
        "bytes_after_append": size_after_append,
        "bytes_after_rewrite": size_after_rewrite,
        "rewrite_seconds": round(rewrite_seconds, 2),
        "rows_verified_after_rewrite": rows_verified,
        "snapshots": snapshots,
        "space_reclaimed": size_after_rewrite < size_after_append,
        "note": (
            "pyiceberg 0.9 exposes no rewrite_data_files or expire_snapshots, so the "
            "rewrite adds a snapshot and keeps the previous files. Disk usage is "
            "expected to grow; space reclamation needs a maintenance job this version "
            "cannot run."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Measure the platform at scale.")
    parser.add_argument("--rows", type=int, default=10_000_000)
    parser.add_argument("--batch", type=int, default=250_000)
    parser.add_argument("--iceberg-rows", type=int, default=1_000_000)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--skip-generate", action="store_true")
    parser.add_argument("--skip-iceberg", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    out_dir = get_settings().root / "data" / "staging" / "zones" / "silver" / "books_bench"

    report: dict = {
        "ran_at": started.isoformat(),
        "target_rows": args.rows,
        "scales": {},
    }

    if args.skip_generate:
        files = list(out_dir.glob("*.parquet"))
        rows = sum(pq.read_metadata(path).num_rows for path in files)
        generate_result = {"rows": rows, "files": len(files), "skipped": True}
    else:
        generate_result = generate(args.rows, args.batch, out_dir)
    report["generate"] = generate_result
    report["scales"]["generate"] = generate_result["rows"]
    print(f"generated  : {generate_result['rows']:,} rows in {generate_result.get('seconds', '?')}s")

    build = run_dbt_build(generate_result["rows"])
    report["build"] = build
    report["scales"]["build"] = generate_result["rows"]
    print(f"dbt build  : exit={build['exit_code']} {build['seconds']}s ({build['rows_per_second']:,} rows/s)")
    if build["exit_code"] != 0:
        print(build["tail"])
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    queries = measure_queries(args.runs)
    report["query"] = queries
    print(f"query p50  : {queries['p50_ms']} ms (p95 {queries['p95_ms']} ms, cold {queries['cold_ms']} ms)")

    if not args.skip_iceberg:
        iceberg = measure_iceberg(args.iceberg_rows, min(args.batch, 100_000))
        report["iceberg"] = iceberg
        report["scales"]["iceberg"] = args.iceberg_rows
        print(
            f"iceberg    : {iceberg['append_rows_per_second']:,} rows/s append, "
            f"data files {iceberg['data_files_after_append']} -> {iceberg['data_files_after_rewrite']}, "
            f"disk {iceberg['bytes_after_append']:,} -> {iceberg['bytes_after_rewrite']:,} bytes "
            f"(reclaimed: {iceberg['space_reclaimed']})"
        )

    report["elapsed_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
    report["result"] = "PASS" if build["exit_code"] == 0 and queries["p50_ms"] > 0 else "FAIL"

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"RESULT     : {report['result']}  -> {REPORT.as_posix()}")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
