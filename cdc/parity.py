"""Prove that the batch path and the CDC path agree.

Two independent paths now claim to describe the same table:

    batch   SELECT ... FROM ops.product_price        (the source of truth)
    stream  cdc.product_price_current in Iceberg     (folded from the WAL)

This compares them column by column. It is the only reason the CDC path is allowed to
exist: a second writer that silently disagrees is worse than no second writer.

Two details that make the result trustworthy:

  * A negative control. Before reporting a pass, the comparator is run against a
    deliberately corrupted copy of the stream state and must report a mismatch. A
    comparator that always says "equal" would otherwise look exactly like a passing
    test — the failure mode recorded as E93.

  * `verified` counts rows actually compared, and the command fails when it is zero.
    "0 rows compared, 0 differences" is a pass-shaped result that means nothing.

    python -m cdc.parity
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2

from cdc.catalog import build_catalog
from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("cdc.parity")

COMPARED_COLUMNS = ("product_name", "category", "price", "currency", "in_stock", "updated_at")

# updated_at is compared to the second. Debezium sends microseconds and Postgres
# returns microseconds, but the two round-trip through different parsers; comparing
# below the second would turn a formatting difference into a false failure, and the
# claim being tested is "the same row", not "the same clock resolution".
TIMESTAMP_PRECISION = "second"


def normalise(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    if isinstance(value, str) and value[:1].isdigit() and "T" in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return value


def read_source() -> dict[str, dict]:
    settings = get_settings()
    conn = psycopg2.connect(settings.pg.psycopg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, product_name, category, price, currency, in_stock, updated_at "
                "FROM ops.product_price ORDER BY product_id"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        row[0]: {
            "product_name": row[1],
            "category": row[2],
            "price": normalise(row[3]),
            "currency": row[4],
            "in_stock": row[5],
            "updated_at": normalise(row[6]),
        }
        for row in rows
    }


def read_stream() -> dict[str, dict]:
    catalog = build_catalog()
    table = catalog.load_table("cdc.product_price_current")
    rows = table.scan().to_arrow().to_pylist()
    return {
        row["product_id"]: {
            "product_name": row["product_name"],
            "category": row["category"],
            # The stream stores integer cents; the source stores NUMERIC. Convert to
            # one representation before comparing, otherwise this would test the
            # storage format instead of the value.
            "price": normalise(Decimal(row["price_cents"]) / 100) if row["price_cents"] is not None else None,
            "currency": row["currency"],
            "in_stock": row["in_stock"],
            "updated_at": normalise(row["updated_at"]),
        }
        for row in rows
    }


def compare(source: dict[str, dict], stream: dict[str, dict]) -> dict:
    """Return a comparison result. Never raises on a difference; differences are data."""
    missing_in_stream = sorted(set(source) - set(stream))
    extra_in_stream = sorted(set(stream) - set(source))

    field_mismatches: list[dict] = []
    compared = 0
    for product_id in sorted(set(source) & set(stream)):
        compared += 1
        left, right = source[product_id], stream[product_id]
        for column in COMPARED_COLUMNS:
            if left[column] != right[column]:
                field_mismatches.append(
                    {
                        "product_id": product_id,
                        "column": column,
                        "source": str(left[column]),
                        "stream": str(right[column]),
                    }
                )

    return {
        "source_rows": len(source),
        "stream_rows": len(stream),
        "verified": compared,
        "missing_in_stream": missing_in_stream[:20],
        "extra_in_stream": extra_in_stream[:20],
        "missing_count": len(missing_in_stream),
        "extra_count": len(extra_in_stream),
        "field_mismatches": field_mismatches[:20],
        "field_mismatch_count": len(field_mismatches),
        "equal": not missing_in_stream and not extra_in_stream and not field_mismatches,
    }


def negative_control(source: dict[str, dict]) -> dict:
    """Corrupt one row and one key, then require the comparator to notice both.

    Two separate corruptions on purpose: a comparator that only diffs keys would pass
    a value corruption, and one that only diffs values would pass a missing row.
    """
    if not source:
        return {"ran": False, "reason": "source table is empty"}

    target = sorted(source)[0]
    corrupted = {key: dict(value) for key, value in source.items()}
    corrupted[target]["price"] = (corrupted[target]["price"] or Decimal("0")) + Decimal("1.00")
    dropped = sorted(source)[-1]
    corrupted.pop(dropped, None)

    result = compare(source, corrupted)
    detected_value = any(m["product_id"] == target and m["column"] == "price" for m in result["field_mismatches"])
    detected_key = dropped in result["missing_in_stream"]
    return {
        "ran": True,
        "corrupted_row": target,
        "dropped_row": dropped,
        "detected_value_change": detected_value,
        "detected_missing_row": detected_key,
        "passed": detected_value and detected_key,
    }


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Compare the batch and CDC paths.")
    parser.add_argument("--report", default="quality/reports/cdc_parity.json")
    args = parser.parse_args(argv)

    started = time.time()
    source = read_source()
    stream = read_stream()
    result = compare(source, stream)
    control = negative_control(source)

    passed = bool(result["equal"]) and bool(control.get("passed")) and result["verified"] > 0

    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(time.time() - started, 2),
        "timestamp_precision": TIMESTAMP_PRECISION,
        "compared_columns": list(COMPARED_COLUMNS),
        **result,
        "negative_control": control,
        "result": "PASS" if passed else "FAIL",
    }

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"source rows        : {result['source_rows']}")
    print(f"stream rows        : {result['stream_rows']}")
    print(f"rows verified      : {result['verified']}")
    print(f"missing in stream  : {result['missing_count']}")
    print(f"extra in stream    : {result['extra_count']}")
    print(f"field mismatches   : {result['field_mismatch_count']}")
    print(f"negative control   : {'PASS' if control.get('passed') else 'FAIL'}")
    print(f"RESULT             : {report['result']}")

    if not passed:
        if result["verified"] == 0:
            print("  -> nothing was compared. An empty comparison is not a pass.")
        for item in result["field_mismatches"][:5]:
            print(f"  -> {item['product_id']}.{item['column']}: source={item['source']} stream={item['stream']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
