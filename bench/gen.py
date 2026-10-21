"""Generate a deterministic 10-million-row dataset, in chunks that fit in RAM.

Why this exists: every capacity claim in the README has to come from a run someone can
repeat. A benchmark that generates its data differently each time measures the generator
as much as the engine.

Determinism: every field is a pure function of the row index. No random seed to lose, no
`uuid4()` to make two runs incomparable, and the row count is exact by construction.

Memory: rows are written in batches of `--batch` (default 250k) and each batch is
released before the next. A 10M-row Arrow table would be ~1.2 GB resident; 40 batches of
250k is ~30 MB. This matters on an 8 GB laptop, which is the machine this runs on.

    python -m bench.gen                     # 10,000,000 rows
    python -m bench.gen --rows 1000000 --batch 100000
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("bench.gen")

CATEGORIES = ("books", "electronics", "garden", "kitchen", "office", "toys")
CURRENCIES = ("USD", "GBP", "EUR")

# Declared once. Inferring a schema per batch is how an all-null column in batch 3
# changes type and makes the whole dataset unreadable — the failure recorded as F295.
SCHEMA = pa.schema(
    [
        pa.field("title", pa.string(), nullable=False),
        pa.field("price", pa.float64(), nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("availability", pa.string(), nullable=False),
        pa.field("stock_count", pa.int64(), nullable=False),
        pa.field("rating", pa.int64(), nullable=False),
        pa.field("category", pa.string(), nullable=False),
        pa.field("upc", pa.string(), nullable=False),
        pa.field("url", pa.string(), nullable=False),
        pa.field("_ingested_from", pa.string(), nullable=False),
    ]
)


def build_batch(start: int, size: int) -> pa.Table:
    """Rows [start, start+size), every value derived from the index."""
    indices = range(start, start + size)
    return pa.Table.from_pydict(
        {
            "title": [f"Product {index:09d}" for index in indices],
            "price": [round(5.0 + (index % 9973) * 0.01, 2) for index in indices],
            "currency": [CURRENCIES[index % 3] for index in indices],
            "availability": ["In stock" if index % 7 else "Out of stock" for index in indices],
            "stock_count": [index % 500 for index in indices],
            "rating": [index % 6 for index in indices],
            "category": [CATEGORIES[index % len(CATEGORIES)] for index in indices],
            "upc": [f"{index:012d}" for index in indices],
            "url": [f"https://bench.invalid/product/{index}" for index in indices],
            "_ingested_from": ["bench.gen"] * size,
        },
        schema=SCHEMA,
    )


def generate(rows: int, batch_size: int, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.parquet"):
        stale.unlink()

    started = time.perf_counter()
    written = 0
    files = 0
    peak_batch_rows = 0

    while written < rows:
        size = min(batch_size, rows - written)
        table = build_batch(written, size)
        peak_batch_rows = max(peak_batch_rows, table.num_rows)
        pq.write_table(table, out_dir / f"part-{files:05d}.parquet", compression="snappy")
        written += size
        files += 1
        # Explicitly drop the reference before the next batch so the previous Arrow
        # buffer is collectable. Without this the loop holds two batches at once.
        del table

    elapsed = time.perf_counter() - started
    total_bytes = sum(path.stat().st_size for path in out_dir.glob("*.parquet"))

    return {
        "rows": written,
        "files": files,
        "batch_rows": peak_batch_rows,
        "seconds": round(elapsed, 2),
        "rows_per_second": int(written / elapsed) if elapsed else 0,
        "bytes": total_bytes,
        "megabytes": round(total_bytes / 1024 / 1024, 1),
        "output": str(out_dir.relative_to(get_settings().root).as_posix()),
    }


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Generate a deterministic bench dataset.")
    parser.add_argument("--rows", type=int, default=10_000_000)
    parser.add_argument("--batch", type=int, default=250_000)
    parser.add_argument("--out", default="data/staging/zones/silver/books_bench")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = get_settings().root / out_dir

    result = generate(args.rows, args.batch, out_dir)
    print(json.dumps(result, indent=2))

    report = Path("quality/reports/bench_generate.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
