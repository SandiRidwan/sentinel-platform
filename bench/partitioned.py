"""Partitioned write at scale, and the measurement that makes partitioning worth doing.

A partitioned table is only better if the engine can skip files. So this writes the same
rows three ways and times the same query against each:

    partitioned/   Hive layout, dt=NNN, one file per day
    flat/          one directory, no partition key
    Iceberg        partition spec identity(day), written through pyiceberg

Then each layout is queried twice: once filtered to a single day, once with no filter. The
filtered number is the one a dashboard produces; the unfiltered number is the cost of not
being able to prune. The report records how many files the engine actually read, because a
faster query with the same file count is a coincidence, not pruning.

Why the flat copy is still here at full scale: it is the negative control. The same query on
a layout that cannot prune must show no speed-up. If it did, the comparison would be
measuring cache warmth rather than file skipping.

The first version of this file was wrong in a way worth keeping in the record. It wrote both
layouts, then read the partitioned side through a glob (`partitioned/**/*.parquet`) and found
it *slower* (149 ms vs 21 ms at 2M rows). The cause was not the layout: DuckDB expands the
glob and opens every file before the `WHERE day = ...` filter is applied. The measurement now
names the partition files explicitly — which is what a partition-aware reader does — and also
reports the glob number, so the difference between "the layout is wrong" and "the reader is
wrong" is visible instead of guessed at.

Memory: rows are generated one day at a time and written straight to Parquet. A 100M-row
Arrow table would be ~8 GB; a day of 1M rows is ~80 MB. DuckDB gets a memory limit and a
spill directory, because the full scan does not fit either.

    python -m bench.partitioned                          # 100,000,000 rows
    python -m bench.partitioned --rows 2000000 --days 20 # quick check
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("bench.partitioned")

REPORT = Path("quality/reports/bench_partitioned.json")

DEFAULT_ROWS = 100_000_000
DEFAULT_DAYS = 100
BATCH = 1_000_000
QUERY_RUNS = 5

CATEGORIES = ("books", "electronics", "garden", "kitchen", "office", "toys")

# Declared once. Inferring a schema per batch is how an all-null column in batch 3 changes
# type and makes the whole dataset unreadable.
SCHEMA = pa.schema(
    [
        pa.field("sale_id", pa.int64(), nullable=False),
        pa.field("day", pa.int32(), nullable=False),
        pa.field("category", pa.string(), nullable=False),
        pa.field("amount", pa.float64(), nullable=False),
        pa.field("quantity", pa.int32(), nullable=False),
    ]
)

ICEBERG_SCHEMA = None  # built lazily; importing pyiceberg types at module level is not free

def build_table(start: int, size: int, boundaries: np.ndarray, shuffle_days: int | None = None) -> pa.Table:
    """Rows [start, start+size), with `day` derived from the same row index as everything else.

    Rows are ordered by day rather than `index % days`. That is what lets one append write
    one partition file instead of one file per partition per append: a modulo layout
    scatters every batch across all 100 partitions.

    `shuffle_days` scrambles the day assignment with a multiplier coprime to the day count.
    The result still contains exactly the same number of rows per day, but every Parquet row
    group now spans the whole range of days. That is the control for the claim that the
    flat layout's speed-up comes from row-group statistics: with the days scrambled, the
    statistics cannot exclude anything.
    """
    indices = np.arange(start, start + size)
    if shuffle_days:
        # 37 is coprime with 100, so the map is a permutation of the day values.
        days = (indices * 37 + 11) % shuffle_days
    else:
        days = np.searchsorted(boundaries, indices, side="right")

    return pa.Table.from_pydict(
        {
            "sale_id": indices.tolist(),
            "day": days.astype("int32").tolist(),
            "category": [CATEGORIES[index % len(CATEGORIES)] for index in range(start, start + size)],
            "amount": [round(1.0 + (index % 5000) * 0.01, 2) for index in range(start, start + size)],
            "quantity": [1 + index % 7 for index in range(start, start + size)],
        },
        schema=SCHEMA,
    )

def day_sizes(rows: int, days: int) -> list[int]:
    """Split `rows` across `days` exactly, remainder on the last day."""
    per_day = rows // days
    sizes = [per_day] * days
    sizes[-1] += rows - per_day * days
    return sizes

def boundaries(sizes: list[int]) -> np.ndarray:
    """Cumulative row counts, so a row index maps to its day without a loop."""
    return np.cumsum(np.array(sizes, dtype="int64"))

def _bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*.parquet"))

def write_parquet_partitioned(rows: int, days: int, base: Path) -> dict:
    directory = base / "partitioned"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    sizes = day_sizes(rows, days)
    edges = boundaries(sizes)
    started = time.perf_counter()
    written = 0
    files = 0
    cursor = 0

    for day, size in enumerate(sizes):
        target = directory / f"dt={day:03d}"
        target.mkdir(parents=True, exist_ok=True)
        offset = 0
        while offset < size:
            chunk = min(BATCH, size - offset)
            table = build_table(cursor + offset, chunk, edges)
            pq.write_table(table, target / f"part-{offset // BATCH:05d}.parquet", compression="zstd")
            files += 1
            offset += chunk
            del table
        cursor += size
        written += size

    elapsed = time.perf_counter() - started
    return {
        "rows": written,
        "seconds": round(elapsed, 2),
        "rows_per_second": int(written / elapsed) if elapsed else 0,
        "files": files,
        "partitions": days,
        "bytes": _bytes(directory),
    }

def write_parquet_flat(
    rows: int,
    days: int,
    base: Path,
    name: str = "flat",
    files_target: int = 10,
    shuffle_days: bool = False,
) -> dict:
    """One directory, no partition key, few large files."""
    directory = base / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    per_file = max(BATCH, rows // max(1, files_target))
    sizes = day_sizes(rows, max(1, rows // per_file))
    edges = boundaries(day_sizes(rows, days))
    scramble = days if shuffle_days else None
    started = time.perf_counter()
    written = 0
    files = 0
    cursor = 0

    for size in sizes:
        # One writer per file, not one file per chunk. A flat layout with 100 small files
        # is not the thing being contrasted: the point is that it holds the same rows with
        # no partition key, so the contrast has to be about the key, not the file size.
        writer = pq.ParquetWriter(
            directory / f"part-{files:05d}.parquet", SCHEMA, compression="zstd"
        )
        try:
            offset = 0
            while offset < size:
                chunk = min(BATCH, size - offset)
                table = build_table(cursor + offset, chunk, edges, scramble)
                writer.write_table(table)
                offset += chunk
                del table
        finally:
            writer.close()
        files += 1
        cursor += size
        written += size

    elapsed = time.perf_counter() - started
    return {
        "rows": written,
        "seconds": round(elapsed, 2),
        "rows_per_second": int(written / elapsed) if elapsed else 0,
        "files": files,
        "partitions": 0,
        "bytes": _bytes(directory),
    }

def write_iceberg(rows: int, days: int, batch: int) -> dict:
    """Write the same rows into an Iceberg table partitioned by day.

    pyiceberg 0.9 has no `rewrite_data_files` and no partition-aware append plan, so the
    rows are handed over one day at a time. That is also the honest shape of a partitioned
    load: you write a partition, not a scatter of every partition.
    """
    from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import DoubleType, IntegerType, LongType, NestedField, StringType

    from cdc.catalog import build_catalog

    schema = Schema(
        NestedField(1, "sale_id", LongType(), required=False),
        NestedField(2, "day", IntegerType(), required=False),
        NestedField(3, "category", StringType(), required=False),
        NestedField(4, "amount", DoubleType(), required=False),
        NestedField(5, "quantity", IntegerType(), required=False),
    )
    spec = PartitionSpec(PartitionField(source_id=2, field_id=1000, transform=IdentityTransform(), name="day"))

    catalog = build_catalog()
    try:
        catalog.create_namespace("bench")
    except NamespaceAlreadyExistsError:
        pass

    identifier = "bench.sales_partitioned"
    try:
        catalog.drop_table(identifier)
    except NoSuchTableError:
        pass
    table = catalog.create_table(identifier, schema=schema, partition_spec=spec)

    sizes = day_sizes(rows, days)
    edges = boundaries(sizes)
    started = time.perf_counter()
    written = 0
    cursor = 0
    for day, size in enumerate(sizes):
        offset = 0
        while offset < size:
            chunk = min(batch, size - offset)
            table.append(build_table(cursor + offset, chunk, edges))
            offset += chunk
        cursor += size
        written += size
    elapsed = time.perf_counter() - started

    table = catalog.load_table(identifier)
    all_files = [item.file for item in table.scan().plan_files()]
    total_rows = sum(item.record_count for item in all_files)
    data_bytes = sum(item.file_size_in_bytes for item in all_files)

    return {
        "identifier": identifier,
        "rows": written,
        "rows_verified": total_rows,
        "seconds": round(elapsed, 2),
        "rows_per_second": int(written / elapsed) if elapsed else 0,
        "files": len(all_files),
        "partitions": days,
        "bytes": data_bytes,
        "snapshots": len(table.metadata.snapshots),
        "partition_spec": str(table.spec()).replace("\n", " "),
        "_table": table,
        "_all_files": all_files,
    }

def _absolute(paths: list[str]) -> list[str]:
    root = get_settings().root
    resolved = []
    for path in paths:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved.append(candidate.as_posix())
    return resolved

def _connect(spill: Path) -> duckdb.DuckDBPyConnection:
    """A fresh connection per measurement, with a memory limit and a spill directory.

    Fresh, because DuckDB caches Parquet metadata per connection: measuring the second
    layout on a warm connection that already read those files would report a speed-up that
    is really a cache hit.
    """
    connection = duckdb.connect()
    connection.execute("SET memory_limit='2GB'")
    connection.execute(f"SET temp_directory='{spill.as_posix()}'")
    return connection

def time_files(files: list[str], probe_day: int | None, spill: Path, runs: int = QUERY_RUNS) -> dict:
    """Time an aggregate over an explicit list of files, optionally filtered to one day."""
    if not files:
        return {"runs": 0, "files": 0, "rows_returned": 0, "p50_ms": 0.0, "cold_ms": 0.0}

    literal = "[" + ", ".join(f"'{path}'" for path in files) + "]"
    where = f" WHERE day = {probe_day}" if probe_day is not None else ""
    sql = f"SELECT count(*), sum(amount) FROM read_parquet({literal}){where}"

    timings: list[float] = []
    rows = 0
    connection = _connect(spill)
    try:
        for _ in range(runs):
            started = time.perf_counter()
            result = connection.execute(sql).fetchone()
            timings.append((time.perf_counter() - started) * 1000)
            rows = int(result[0]) if result else 0
    finally:
        connection.close()

    warm = sorted(timings[1:]) or timings
    return {
        "runs": runs,
        "files": len(files),
        "rows_returned": rows,
        "cold_ms": round(timings[0], 2),
        "p50_ms": round(warm[len(warm) // 2], 2),
        "min_ms": round(min(warm), 2),
        "max_ms": round(max(warm), 2),
    }

def time_glob(pattern: str, probe_day: int | None, spill: Path, runs: int = QUERY_RUNS) -> dict:
    """Time the query as a user would write it: a glob plus a Hive filter.

    Reported next to the explicit-file measurement because the difference between the two is
    the difference between what the layout allows and what the reader actually does.
    """
    where = f" WHERE day = {probe_day}" if probe_day is not None else ""
    sql = f"SELECT count(*), sum(amount) FROM read_parquet('{pattern}', hive_partitioning=true){where}"

    timings: list[float] = []
    rows = 0
    connection = _connect(spill)
    try:
        for _ in range(runs):
            started = time.perf_counter()
            result = connection.execute(sql).fetchone()
            timings.append((time.perf_counter() - started) * 1000)
            rows = int(result[0]) if result else 0
    finally:
        connection.close()

    warm = sorted(timings[1:]) or timings
    return {
        "runs": runs,
        "pattern": pattern,
        "rows_returned": rows,
        "cold_ms": round(timings[0], 2),
        "p50_ms": round(warm[len(warm) // 2], 2),
        "min_ms": round(min(warm), 2),
        "max_ms": round(max(warm), 2),
    }

def partition_files(directory: Path, probe_day: int) -> list[str]:
    """The files a partition-aware reader would open for one day."""
    return [path.as_posix() for path in sorted((directory / f"dt={probe_day:03d}").glob("*.parquet"))]

def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Write partitioned and flat, then measure pruning.")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--runs", type=int, default=QUERY_RUNS)
    parser.add_argument("--skip-flat", action="store_true", help="skip the negative-control flat copy")
    parser.add_argument("--keep", action="store_true", help="keep the generated data directories")
    parser.add_argument("--skip-iceberg", action="store_true")
    args = parser.parse_args(argv)

    if args.days < 2:
        raise SystemExit("--days must be at least 2, otherwise there is nothing to prune")
    if args.rows < args.days:
        raise SystemExit("--rows must be at least --days")

    settings = get_settings()
    base = settings.root / "data" / "bench_partitioned"
    spill = settings.root / "data" / "bench_spill"
    spill.mkdir(parents=True, exist_ok=True)
    base.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    probe_day = args.days // 2

    print(f"rows={args.rows:,} days={args.days} batch={args.batch:,} probe_day={probe_day}")

    write_partitioned = write_parquet_partitioned(args.rows, args.days, base)
    print(
        f"  parquet partitioned : {write_partitioned['seconds']}s "
        f"({write_partitioned['rows_per_second']:,}/s) files={write_partitioned['files']} "
        f"bytes={write_partitioned['bytes']:,}"
    )

    write_flat = None
    write_flat_unsorted = None
    if not args.skip_flat:
        write_flat = write_parquet_flat(args.rows, args.days, base)
        print(
            f"  parquet flat       : {write_flat['seconds']}s "
            f"({write_flat['rows_per_second']:,}/s) files={write_flat['files']} "
            f"bytes={write_flat['bytes']:,}"
        )
        if args.rows % args.days == 0:
            write_flat_unsorted = write_parquet_flat(
                args.rows, args.days, base, name="flat_unsorted", shuffle_days=True
            )
            print(
                f"  parquet flat mixed : {write_flat_unsorted['seconds']}s "
                f"({write_flat_unsorted['rows_per_second']:,}/s) files={write_flat_unsorted['files']} "
                f"bytes={write_flat_unsorted['bytes']:,}"
            )

    iceberg_write = None
    iceberg_queries = None
    iceberg_all_files: list[str] = []
    if not args.skip_iceberg:
        iceberg_write = write_iceberg(args.rows, args.days, min(args.batch, 2_000_000))
        iceberg_all_files = _absolute([item.file_path for item in iceberg_write.pop("_all_files")])
        iceberg_write.pop("_table", None)
        print(
            f"  iceberg partitioned : {iceberg_write['seconds']}s "
            f"({iceberg_write['rows_per_second']:,}/s) files={iceberg_write['files']} "
            f"bytes={iceberg_write['bytes']:,} snapshots={iceberg_write['snapshots']}"
        )

    # --- queries ---

    partitioned_dir = base / "partitioned"
    partitioned_day_files = partition_files(partitioned_dir, probe_day)
    partitioned_all = sorted(partitioned_dir.rglob("*.parquet"))
    partitioned_all_paths = [path.as_posix() for path in partitioned_all]

    print("  measuring partitioned parquet ...")
    parquet_partitioned = {
        "pruned": time_files(partitioned_day_files, probe_day, spill, args.runs),
        "glob": time_glob(f"{partitioned_dir.as_posix()}/**/*.parquet", probe_day, spill, args.runs),
        "full_scan": time_files(partitioned_all_paths, None, spill, args.runs),
    }

    parquet_flat = None
    if write_flat is not None:
        print("  measuring flat parquet ...")
        flat_all = sorted((base / "flat").glob("*.parquet"))
        flat_paths = [path.as_posix() for path in flat_all]
        parquet_flat = {
            "filtered": time_files(flat_paths, probe_day, spill, args.runs),
            "full_scan": time_files(flat_paths, None, spill, args.runs),
        }

    parquet_flat_unsorted = None
    if write_flat_unsorted is not None:
        print("  measuring flat parquet with mixed days ...")
        mixed_all = sorted((base / "flat_unsorted").glob("*.parquet"))
        mixed_paths = [path.as_posix() for path in mixed_all]
        parquet_flat_unsorted = {
            "filtered": time_files(mixed_paths, probe_day, spill, args.runs),
            "full_scan": time_files(mixed_paths, None, spill, args.runs),
        }

    if iceberg_write is not None:
        print("  measuring iceberg ...")
        from cdc.catalog import build_catalog

        table = build_catalog().load_table(iceberg_write["identifier"])
        probe_files = _absolute(
            [item.file.file_path for item in table.scan(row_filter=f"day = {probe_day}").plan_files()]
        )
        iceberg_queries = {
            "pruned": time_files(probe_files, probe_day, spill, args.runs),
            "full_scan": time_files(iceberg_all_files, None, spill, args.runs),
        }

    # --- comparison ---

    def speedup(slower: dict, faster: dict) -> float:
        if not faster.get("p50_ms"):
            return 0.0
        return round(slower["p50_ms"] / faster["p50_ms"], 2)

    pruning = {
        "parquet_partitioned": {
            "files_filtered": len(partitioned_day_files),
            "files_total": len(partitioned_all_paths),
            "bytes_filtered": sum(Path(p).stat().st_size for p in partitioned_day_files),
            "bytes_total": sum(path.stat().st_size for path in partitioned_all),
            "speedup_explicit_files": speedup(parquet_partitioned["full_scan"], parquet_partitioned["pruned"]),
            "speedup_via_glob": speedup(parquet_partitioned["full_scan"], parquet_partitioned["glob"]),
        },
        "iceberg": None
        if iceberg_queries is None
        else {
            "files_filtered": iceberg_queries["pruned"]["files"],
            "files_total": iceberg_queries["full_scan"]["files"],
            "speedup_explicit_files": speedup(iceberg_queries["full_scan"], iceberg_queries["pruned"]),
        },
        "flat": None
        if parquet_flat is None
        else {
            "files_filtered": parquet_flat["filtered"]["files"],
            "files_total": parquet_flat["full_scan"]["files"],
            # Not 1.0. The rows are ordered by day, so Parquet row-group statistics can
            # exclude almost every row group even though no partition key exists. That is
            # row-group pruning, and it is why the mixed-day copy below is the real control.
            "speedup": speedup(parquet_flat["full_scan"], parquet_flat["filtered"]),
        },
        "flat_unsorted": None
        if parquet_flat_unsorted is None
        else {
            "files_filtered": parquet_flat_unsorted["filtered"]["files"],
            "files_total": parquet_flat_unsorted["full_scan"]["files"],
            # Must be ~1.0: every row group spans all days, so statistics exclude nothing
            # and the only way to answer is to read the column and filter.
            "speedup": speedup(parquet_flat_unsorted["full_scan"], parquet_flat_unsorted["filtered"]),
        },
    }

    answers = {
        "partitioned": parquet_partitioned["pruned"]["rows_returned"],
        "flat": parquet_flat["filtered"]["rows_returned"] if parquet_flat else None,
        "flat_unsorted": parquet_flat_unsorted["filtered"]["rows_returned"] if parquet_flat_unsorted else None,
        "iceberg": iceberg_queries["pruned"]["rows_returned"] if iceberg_queries else None,
    }
    expected_per_day = args.rows // args.days
    same_answer = (
        len({value for value in answers.values() if value is not None}) == 1
        and answers["partitioned"] == expected_per_day
    )

    checks = {
        "same_answer": same_answer,
        "parquet_files_pruned": len(partitioned_day_files) < len(partitioned_all_paths),
        "parquet_filtered_faster": parquet_partitioned["pruned"]["p50_ms"] < parquet_partitioned["full_scan"]["p50_ms"],
    }
    if iceberg_queries is not None:
        checks["iceberg_files_pruned"] = (
            iceberg_queries["pruned"]["files"] < iceberg_queries["full_scan"]["files"]
        )
        checks["iceberg_filtered_faster"] = (
            iceberg_queries["pruned"]["p50_ms"] < iceberg_queries["full_scan"]["p50_ms"]
        )
    if parquet_flat is not None and args.rows >= 10_000_000:
        # Sorted data inside a flat layout is not "no pruning": Parquet statistics skip row
        # groups. Asserted only at a scale where a file holds many row groups — at 200k rows
        # there is one row group per file and there is nothing for the statistics to exclude.
        checks["sorted_flat_prunes_by_row_group_stats"] = pruning["flat"]["speedup"] > 1.5
    if parquet_flat_unsorted is not None:
        # The real negative control: same flat layout, same rows, days mixed inside every
        # row group. If this were still fast, the earlier speed-up was cache, not pruning.
        checks["mixed_flat_prunes_nothing"] = pruning["flat_unsorted"]["speedup"] < 1.3
    if parquet_flat is not None:
        checks["partitioned_beats_sorted_flat"] = (
            parquet_partitioned["pruned"]["p50_ms"] < parquet_flat["filtered"]["p50_ms"]
        )

    report = {
        "ran_at": started.isoformat(),
        "rows": args.rows,
        "days": args.days,
        "rows_per_day": expected_per_day,
        "probe_day": probe_day,
        "memory_limit": "2GB",
        "write": {
            "parquet_partitioned": write_partitioned,
            "parquet_flat": write_flat,
            "parquet_flat_unsorted": write_flat_unsorted,
            "iceberg": iceberg_write,
        },
        "queries": {
            "parquet_partitioned": parquet_partitioned,
            "parquet_flat": parquet_flat,
            "parquet_flat_unsorted": parquet_flat_unsorted,
            "iceberg": iceberg_queries,
        },
        "pruning": pruning,
        "answers": answers,
        "checks": checks,
        "result": "PASS" if all(checks.values()) else "FAIL",
        "notes": [
            "The partitioned read is timed twice: once naming the partition files (what a "
            "partition-aware reader does) and once through a glob, because a glob makes DuckDB "
            "expand every file before the filter is applied.",
            "Two flat copies are written with identical rows: one ordered by day and one with "
            "the days mixed inside every row group. The ordered copy is fast because Parquet "
            "row-group statistics prune it; the mixed copy is the negative control and cannot "
            "prune at all. Partitioning wins at the file level regardless of row order.",
            "Iceberg pruning is reported from plan_files(), which is the table format's own "
            "statement about which files a scan must read, not an inference from timing.",
            "The row-group-statistics check is asserted only at 10M rows and above: below that "
            "a flat file is a single row group and there is nothing for the statistics to skip.",
            "The mixed-day flat copy is slower filtered than unfiltered, and that is expected: "
            "the unfiltered aggregate never reads the `day` column at all, while the filtered "
            "query must read it for every row and then throw almost all of it away. Reading a "
            "column you cannot prune from is pure cost.",
        ],
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print(f"  partitioned pruned  p50 {parquet_partitioned['pruned']['p50_ms']:>9} ms  files={len(partitioned_day_files)}")
    print(f"  partitioned glob    p50 {parquet_partitioned['glob']['p50_ms']:>9} ms  files={len(partitioned_all_paths)}")
    print(f"  partitioned full    p50 {parquet_partitioned['full_scan']['p50_ms']:>9} ms  files={len(partitioned_all_paths)}")
    if parquet_flat:
        print(f"  flat filtered       p50 {parquet_flat['filtered']['p50_ms']:>9} ms  files={parquet_flat['filtered']['files']}")
        print(f"  flat full           p50 {parquet_flat['full_scan']['p50_ms']:>9} ms  files={parquet_flat['full_scan']['files']}")
    if parquet_flat_unsorted:
        print(f"  flat mixed filtered p50 {parquet_flat_unsorted['filtered']['p50_ms']:>9} ms  files={parquet_flat_unsorted['filtered']['files']}")
        print(f"  flat mixed full     p50 {parquet_flat_unsorted['full_scan']['p50_ms']:>9} ms  files={parquet_flat_unsorted['full_scan']['files']}")
    if iceberg_queries:
        print(f"  iceberg pruned      p50 {iceberg_queries['pruned']['p50_ms']:>9} ms  files={iceberg_queries['pruned']['files']}")
        print(f"  iceberg full        p50 {iceberg_queries['full_scan']['p50_ms']:>9} ms  files={iceberg_queries['full_scan']['files']}")
    print()
    print(f"  answer (day {probe_day})     {answers['partitioned']:,} rows, expected {expected_per_day:,}")
    for name, ok in checks.items():
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"RESULT: {report['result']}")

    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)

    return 0 if report["result"] == "PASS" else 1

if __name__ == "__main__":
    raise SystemExit(main())
