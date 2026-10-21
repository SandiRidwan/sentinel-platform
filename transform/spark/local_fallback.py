"""raw -> bronze -> silver with pandas, for environments without a usable JDK.

Produces the same tables and the same report as transform.spark.bronze_to_silver, so a DAG
does not have to branch on which one ran. It is slower and single-process, which is the
honest trade: a correct result on a laptop beats no result at all.

    python -m transform.spark.local_fallback
    python -m transform.spark.local_fallback --source books
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

from transform.spark import staging
from transform.spark.bronze_to_silver import DEDUP_KEYS, SILVER_SCHEMAS

log = get_logger("spark.local_fallback")

# pandas dtype per declared silver type, so the output matches the Spark path.
PANDAS_TYPES = {
    "string": "string",
    "double": "float64",
    "int": "Int64",
    "long": "Int64",
    "array<string>": "object",
}


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Build bronze and silver with pandas.")
    parser.add_argument("--source", action="append")
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    sources = args.source or list(SILVER_SCHEMAS)
    started = datetime.now(timezone.utc)

    bronze_dir = staging.local_zone("bronze")
    silver_dir = staging.local_zone("silver")
    report: dict[str, dict] = {}

    for source in sources:
        local_files = staging.download_prefix(settings.s3.bucket_raw, prefix=f"{source}/")
        if not local_files:
            print(f"  {source}: no raw objects, skipped")
            report[source] = {"raw_objects": 0, "silver_rows": 0, "dropped": 0}
            continue

        frames = []
        for path in local_files:
            frame = pq.read_table(path, ignore_prefixes=["_"]).to_pandas()
            frame["_ingested_from"] = path.name
            frames.append(frame)
        raw = pd.concat(frames, ignore_index=True)
        raw_count = len(raw)

        (bronze_dir / source).mkdir(parents=True, exist_ok=True)
        raw.to_parquet(bronze_dir / source / "bronze.parquet", index=False, compression="zstd")

        schema = SILVER_SCHEMAS[source]
        silver = pd.DataFrame()
        for column, kind in schema.items():
            dtype = PANDAS_TYPES[kind]
            if column in raw.columns:
                silver[column] = _cast(raw[column], dtype)
            else:
                silver[column] = pd.Series([pd.NA] * raw_count, dtype=dtype)
        silver["_ingested_from"] = raw["_ingested_from"].values

        keys = DEDUP_KEYS.get(source, [])
        if keys:
            silver = silver.drop_duplicates(subset=keys)

        (silver_dir / source).mkdir(parents=True, exist_ok=True)
        silver.to_parquet(silver_dir / source / "silver.parquet", index=False, compression="zstd")

        report[source] = {
            "raw_objects": len(local_files),
            "raw_rows": raw_count,
            "bronze_rows": raw_count,
            "silver_rows": len(silver),
            "dropped": max(0, raw_count - len(silver)),
            "dedup_keys": keys,
        }
        print(f"  {source}: raw={raw_count} silver={len(silver)} dropped={report[source]['dropped']}")

    if not args.no_upload:
        report["_uploads"] = {
            "bronze": staging.upload_directory(bronze_dir, settings.s3.bucket_bronze, "bronze"),
            "silver": staging.upload_directory(silver_dir, settings.s3.bucket_silver, "silver"),
        }

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    out = {"engine": "pandas", "ran_at": started.isoformat(), "elapsed_s": round(elapsed, 2), "tables": report}
    print()
    print(json.dumps(out, indent=2))

    path = Path("quality/reports/bronze_to_silver.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


def _cast(series: pd.Series, dtype: str) -> pd.Series:
    if dtype == "object":
        return series.apply(_to_string_list)
    if dtype == "string":
        return series.astype("string")
    # to_numeric first: a declared int column that arrived as "  12 " should become 12, and
    # a column that cannot be parsed becomes NA rather than a wrong number.
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.astype(dtype)


def _to_string_list(value) -> list[str]:
    """Normalise a tags-like column to list[str].

    Order matters: pd.isna() on a numpy array returns an array and then raises
    "truth value of an array is ambiguous", so array-likes are handled before the null check.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if hasattr(value, "tolist"):  # numpy array
        return [str(item) for item in value.tolist()]
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    return [str(value)]


if __name__ == "__main__":
    raise SystemExit(main())
