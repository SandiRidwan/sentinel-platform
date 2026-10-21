"""raw -> bronze -> silver, with Spark.

Bronze: typed, no business rules. One column added, `_ingested_from`, so a row can always
be traced to the object it came from.
Silver: deduplicated, conformed, and joinable. This is where the platform decides what a
"book" or a "country" is, and that decision is written down as a schema rather than implied
by whatever the spider happened to produce.

    python -m transform.spark.bronze_to_silver
    python -m transform.spark.bronze_to_silver --source books
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

from transform.spark import staging
from transform.spark.session import build_session

log = get_logger("spark.bronze_to_silver")

# The silver contract, per table. Anything not listed is dropped rather than stored as a
# surprise column that dbt then has to guess about.
SILVER_SCHEMAS: dict[str, dict[str, str]] = {
    "books": {
        "title": "string",
        "price": "double",
        "currency": "string",
        "availability": "string",
        "stock_count": "int",
        "rating": "int",
        "category": "string",
        "upc": "string",
        "url": "string",
    },
    "book_categories": {"name": "string", "slug": "string", "url": "string"},
    "quotes": {"text": "string", "author": "string", "author_url": "string", "tags": "array<string>"},
    "countries": {"name": "string", "capital": "string", "population": "long", "area_km2": "double"},
    "hockey_teams": {
        "year": "int",
        "team": "string",
        "wins": "int",
        "losses": "int",
        "ot_losses": "int",
        "win_pct": "double",
        "goals_for": "int",
        "goals_against": "int",
    },
}

# Column(s) that make a row unique within its table. Used for the dedup step.
DEDUP_KEYS: dict[str, list[str]] = {
    "books": ["upc"],
    "book_categories": ["slug"],
    "quotes": ["author", "text"],
    "countries": ["name"],
    "hockey_teams": ["year", "team"],
}


def table_for_source(source: str) -> str:
    return source


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Build bronze and silver tables with Spark.")
    parser.add_argument("--source", action="append", help="limit to one source (repeatable)")
    parser.add_argument("--no-upload", action="store_true", help="skip pushing results to S3")
    args = parser.parse_args(argv)

    settings = get_settings()
    sources = args.source or list(SILVER_SCHEMAS)
    started = datetime.now(timezone.utc)

    spark = build_session("sentinel-bronze-to-silver")

    bronze_dir = staging.local_zone("bronze")
    silver_dir = staging.local_zone("silver")

    report: dict[str, dict] = {}

    try:
        for source in sources:
            table = table_for_source(source)
            print(f"\n--- {source} -> {table} ---")

            local_files = staging.download_prefix(settings.s3.bucket_raw, prefix=f"{source}/")
            if not local_files:
                print(f"  no raw objects under {source}/ — skipped")
                report[source] = {"raw_objects": 0, "bronze_rows": 0, "silver_rows": 0, "dropped": 0}
                continue

            # Spark wants a directory of parquet, not a list of files.
            raw_root = staging.staging_root() / settings.s3.bucket_raw / source
            df = spark.read.option("recursiveFileLookup", "true").parquet(str(raw_root))
            raw_count = df.count()

            bronze = df.withColumn("_ingested_from", _source_file_udf())
            _write(bronze, bronze_dir / table)

            schema = SILVER_SCHEMAS[table]
            silver = _conform(bronze, schema)
            keys = DEDUP_KEYS.get(table, [])
            if keys:
                silver = silver.dropDuplicates(keys)

            silver_count = silver.count()
            _write(silver, silver_dir / table)

            report[table] = {
                "raw_objects": len(local_files),
                "raw_rows": raw_count,
                "bronze_rows": bronze.count(),
                "silver_rows": silver_count,
                "dropped": max(0, raw_count - silver_count),
                "dedup_keys": keys,
            }
            print(
                f"  raw={raw_count} silver={silver_count} "
                f"dropped={report[table]['dropped']} keys={keys}"
            )
    finally:
        spark.stop()

    if not args.no_upload:
        settings_bronze = settings.s3.bucket_bronze
        settings_silver = settings.s3.bucket_silver
        uploaded_bronze = staging.upload_directory(bronze_dir, settings_bronze, "bronze")
        uploaded_silver = staging.upload_directory(silver_dir, settings_silver, "silver")
        report["_uploads"] = {"bronze": uploaded_bronze, "silver": uploaded_silver}

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print()
    print(json.dumps({"elapsed_s": round(elapsed, 2), "tables": report}, indent=2))

    out = Path("quality/reports/bronze_to_silver.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"ran_at": started.isoformat(), "elapsed_s": elapsed, "tables": report}, indent=2),
        encoding="utf-8",
    )
    return 0


def _conform(df, schema: dict[str, str]):
    """Select declared columns, cast to the declared type, add nothing else.

    A source that produced an undeclared column is not rejected here (the spider already
    validated it); the column is simply not carried forward, so silver has exactly the
    shape dbt models were written against.
    """
    from pyspark.sql import functions as F
    from pyspark.sql import types as T

    type_map = {
        "string": T.StringType(),
        "double": T.DoubleType(),
        "int": T.IntegerType(),
        "long": T.LongType(),
        "array<string>": T.ArrayType(T.StringType()),
    }

    selectors = []
    for column, kind in schema.items():
        if column not in df.columns:
            # Missing column: emit a typed null rather than failing, so one absent field
            # does not sink an otherwise good run. The quality suite reports the null rate.
            selectors.append(F.lit(None).cast(type_map[kind]).alias(column))
        else:
            selectors.append(F.col(column).cast(type_map[kind]).alias(column))

    # Lineage column, added by both engines. The Spark and pandas paths must produce the
    # same columns: they write into the same directory, and a reader takes the schema from
    # whichever file it opens first. A mismatch there is a runtime Binder Error in dbt,
    # not a warning.
    #
    # It has to be part of the select list, not a withColumn afterwards: select() drops the
    # column, and a withColumn then references a column that no longer exists.
    if "_ingested_from" in df.columns:
        selectors.append(F.col("_ingested_from").cast(T.StringType()))
    else:
        selectors.append(F.lit(None).cast(T.StringType()).alias("_ingested_from"))

    return df.select(*selectors)


def _write(df, path: Path) -> None:
    (
        df.write.mode("overwrite")
        .option("compression", "zstd")
        .parquet(str(path))
    )


def _source_file_udf():
    """Record the origin object for lineage. Derived from the input file name Spark sees."""
    from pyspark.sql import functions as F

    return F.element_at(F.split(F.input_file_name(), "/"), -1)


if __name__ == "__main__":
    raise SystemExit(main())
