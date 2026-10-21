"""Apply the Debezium change stream to Iceberg.

Reads `sentinel.ops.product_price` from the beginning of the topic, applies every
create/update/delete in offset order, and writes two Iceberg tables:

    cdc.product_price_current   one row per product, the state after the last event
    cdc.product_price_changes   the ordered change log, for lineage and replay

Both are written with a full overwrite on every run. That is deliberate: a sink that
appends would double-count when the consumer is restarted at the same offset, and the
one property a CDC sink must have is that replaying the same input produces the same
output. Overwrite makes idempotency structural instead of something the code has to
remember to check.

    python -m cdc.consumer
    python -m cdc.consumer --timeout 60 --verbose
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
from confluent_kafka import Consumer, TopicPartition
from confluent_kafka.admin import AdminClient
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.schema import Schema
from pyiceberg.types import BooleanType, LongType, NestedField, StringType

from cdc.catalog import build_catalog
from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("cdc.consumer")

TOPIC = "sentinel.ops.product_price"
GROUP_ID = "sentinel-cdc-iceberg"
NAMESPACE = "cdc"

CURRENT_SCHEMA = Schema(
    NestedField(1, "product_id", StringType(), required=True),
    NestedField(2, "product_name", StringType()),
    NestedField(3, "category", StringType()),
    NestedField(4, "price_cents", LongType()),
    NestedField(5, "currency", StringType()),
    NestedField(6, "in_stock", BooleanType()),
    NestedField(7, "updated_at", StringType()),
    # Provenance columns: which operation produced this row, and when the source
    # committed it. Excluded from the parity comparison, but they are what lets a
    # reader tell a CDC row from a batch row without asking anyone.
    NestedField(8, "_cdc_op", StringType()),
    NestedField(9, "_cdc_ts_ms", LongType()),
)

CHANGES_SCHEMA = Schema(
    NestedField(1, "offset", LongType(), required=True),
    NestedField(2, "op", StringType()),
    NestedField(3, "ts_ms", LongType()),
    NestedField(4, "product_id", StringType()),
    NestedField(5, "product_name", StringType()),
    NestedField(6, "category", StringType()),
    NestedField(7, "price_cents", LongType()),
    NestedField(8, "currency", StringType()),
    NestedField(9, "in_stock", BooleanType()),
    NestedField(10, "updated_at", StringType()),
)

CURRENT_ARROW = pa.schema(
    [
        # nullable=False matters: Iceberg declares product_id as required, and a
        # nullable Arrow column is rejected at write time with a field-by-field
        # "Mismatch in fields" table. The schema is the contract, not a hint.
        pa.field("product_id", pa.string(), nullable=False),
        pa.field("product_name", pa.string()),
        pa.field("category", pa.string()),
        pa.field("price_cents", pa.int64()),
        pa.field("currency", pa.string()),
        pa.field("in_stock", pa.bool_()),
        pa.field("updated_at", pa.string()),
        pa.field("_cdc_op", pa.string()),
        pa.field("_cdc_ts_ms", pa.int64()),
    ]
)

CHANGES_ARROW = pa.schema(
    [
        pa.field("offset", pa.int64(), nullable=False),
        pa.field("op", pa.string()),
        pa.field("ts_ms", pa.int64()),
        pa.field("product_id", pa.string()),
        pa.field("product_name", pa.string()),
        pa.field("category", pa.string()),
        pa.field("price_cents", pa.int64()),
        pa.field("currency", pa.string()),
        pa.field("in_stock", pa.bool_()),
        pa.field("updated_at", pa.string()),
    ]
)


def _cents(value) -> int | None:
    """Money as integer minor units.

    Storing 12.34 as 1234 is not a workaround for a library quirk; it is the storage
    format that cannot drift. pyiceberg 0.9 writes a decimal(12, 2) column as a
    fixed-length byte array while its statistics reader expects an int64, which is how
    this surfaced — but an integer column is the design that should have been there
    first: no float, no rounding, no dialect-dependent decimal semantics.
    """
    if value is None:
        return None
    exact = Decimal(str(value)).quantize(Decimal("0.01"))
    return int(exact * 100)


def _timestamp(value) -> str | None:
    """Normalise the three shapes a timestamp arrives in.

    Debezium sends TIMESTAMPTZ as an ISO-8601 string with an offset. The same column
    read from Postgres is a `datetime`. A hand-written test fixture might be an int.
    Handling all three here means the parity check compares timestamps, not formats.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        # Connect mode gives epoch millis; adaptive modes give micros. Anything past
        # 1e14 is micros, past 1e17 is nanos.
        raw = float(value)
        if raw > 1e17:
            parsed = datetime.fromtimestamp(raw / 1e9, tz=timezone.utc)
        elif raw > 1e14:
            parsed = datetime.fromtimestamp(raw / 1e6, tz=timezone.utc)
        else:
            parsed = datetime.fromtimestamp(raw / 1e3, tz=timezone.utc)
    else:
        raise TypeError(f"unsupported timestamp type: {type(value)!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_envelope(raw: bytes) -> dict | None:
    """Return the Debezium payload, or None for a tombstone.

    Tombstones are the `null` value Debezium writes after a delete. They are not
    changes; treating one as a delete would delete the row twice, which is harmless,
    but treating one as a create would resurrect it.
    """
    if raw is None:
        return None
    message = json.loads(raw.decode("utf-8"))
    payload = message.get("payload")
    if payload is None:
        return None
    return payload


def apply_event(state: dict[str, dict], payload: dict, offset: int) -> dict | None:
    """Fold one change into the running state. Returns the change-log row."""
    op = payload.get("op")
    ts_ms = payload.get("ts_ms")
    after = payload.get("after")
    before = payload.get("before")

    if op in ("c", "r", "u") and after:
        key = after["product_id"]
        state[key] = {
            "product_id": key,
            "product_name": after.get("product_name"),
            "category": after.get("category"),
            "price_cents": _cents(after.get("price")),
            "currency": after.get("currency"),
            "in_stock": after.get("in_stock"),
            "updated_at": _timestamp(after.get("updated_at")),
            "_cdc_op": op,
            "_cdc_ts_ms": ts_ms,
        }
        row = dict(state[key])
    elif op == "d":
        key = (before or {}).get("product_id")
        if key is None:
            raise ValueError(f"delete event at offset {offset} carries no key")
        state.pop(key, None)
        row = {
            "product_id": key,
            "product_name": None,
            "category": None,
            "price_cents": None,
            "currency": None,
            "in_stock": None,
            "updated_at": None,
        }
    else:
        return None

    return {
        "offset": offset,
        "op": op,
        "ts_ms": ts_ms,
        **{k: row[k] for k in ("product_id", "product_name", "category", "price_cents", "currency", "in_stock", "updated_at")},
    }


def wait_for_topic(bootstrap: str, topic: str, timeout_s: int = 120) -> list[int]:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    deadline = time.time() + timeout_s
    last = "topic not listed"
    while time.time() < deadline:
        try:
            metadata = admin.list_topics(timeout=5)
            if topic in metadata.topics and metadata.topics[topic].error is None:
                return sorted(metadata.topics[topic].partitions)
            last = str(metadata.topics[topic].error) if topic in metadata.topics else last
        except Exception as exc:  # noqa: BLE001 - the broker may still be electing
            last = str(exc)
        time.sleep(1)
    raise SystemExit(f"topic {topic} never appeared on {bootstrap}: {last}")


def consume(bootstrap: str, timeout_s: int, verbose: bool) -> tuple[dict[str, dict], list[dict]]:
    partitions = wait_for_topic(bootstrap, TOPIC)

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": GROUP_ID,
            "enable.auto.commit": False,
            # Replay from the start every time: the sink is an overwrite, so the whole
            # topic is the input, not the part since the last commit. Committing offsets
            # here would make the output depend on run history.
            "auto.offset.reset": "earliest",
            "enable.partition.eof": True,
        }
    )

    targets: dict[int, int] = {}
    for partition in partitions:
        tp = TopicPartition(TOPIC, partition)
        low, high = consumer.get_watermark_offsets(tp, timeout=10)
        consumer.assign([TopicPartition(TOPIC, partition, low)])
        targets[partition] = high

    state: dict[str, dict] = {}
    changes: list[dict] = []
    eof_seen: set[int] = set()
    deadline = time.time() + timeout_s
    skipped = 0

    try:
        while time.time() < deadline:
            if len(eof_seen) == len(partitions):
                break
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                from confluent_kafka import KafkaError

                if message.error().code() == KafkaError._PARTITION_EOF:
                    if message.offset() >= targets.get(message.partition(), 0) - 1:
                        eof_seen.add(message.partition())
                    continue
                raise SystemExit(f"consume error: {message.error()}")

            payload = parse_envelope(message.value())
            if payload is None:
                skipped += 1
                continue
            change = apply_event(state, payload, message.offset())
            if change is not None:
                changes.append(change)
            if verbose:
                print(f"  offset={message.offset()} op={payload.get('op')} key={(payload.get('after') or payload.get('before') or {}).get('product_id')}")
    finally:
        consumer.close()

    if skipped:
        log.info("tombstones skipped", extra={"count": skipped})
    return state, changes


def write_iceberg(state: dict[str, dict], changes: list[dict], reset: bool = False) -> dict:
    catalog = build_catalog()
    try:
        catalog.create_namespace(NAMESPACE)
    except NamespaceAlreadyExistsError:
        pass

    if reset:
        # Dropping and recreating is the only honest way to change a column type in
        # Iceberg: the format allows schema evolution for adds and renames, but a
        # decimal->bigint change is a type change, and silently coercing it would
        # rewrite history. The demo can afford to rebuild; production would add a
        # new column and backfill.
        for name in ("product_price_current", "product_price_changes"):
            try:
                catalog.drop_table(f"{NAMESPACE}.{name}")
            except NoSuchTableError:
                pass

    current_rows = [state[key] for key in sorted(state)]
    current_arrow = pa.Table.from_pylist(current_rows, schema=CURRENT_ARROW) if current_rows else CURRENT_ARROW.empty_table()

    identifier = f"{NAMESPACE}.product_price_current"
    try:
        table = catalog.load_table(identifier)
        table.overwrite(current_arrow)
    except NoSuchTableError:
        table = catalog.create_table(identifier, schema=CURRENT_SCHEMA)
        if current_arrow.num_rows:
            table.append(current_arrow)

    changes_arrow = pa.Table.from_pylist(changes, schema=CHANGES_ARROW) if changes else CHANGES_ARROW.empty_table()
    changes_identifier = f"{NAMESPACE}.product_price_changes"
    try:
        changes_table = catalog.load_table(changes_identifier)
        changes_table.overwrite(changes_arrow)
    except NoSuchTableError:
        changes_table = catalog.create_table(changes_identifier, schema=CHANGES_SCHEMA)
        if changes_arrow.num_rows:
            changes_table.append(changes_arrow)

    return {
        "current_rows": table.scan().to_arrow().num_rows,
        "change_events": changes_table.scan().to_arrow().num_rows,
        "current_snapshots": len(table.metadata.snapshots),
        "changes_snapshots": len(changes_table.metadata.snapshots),
    }


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Apply the Debezium stream to Iceberg.")
    parser.add_argument("--timeout", type=int, default=90, help="seconds to wait for the topic to drain")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the Iceberg tables first")
    args = parser.parse_args(argv)

    started = time.time()
    bootstrap = get_settings().kafka.bootstrap
    state, changes = consume(bootstrap, args.timeout, args.verbose)
    if not changes:
        raise SystemExit(
            "no change events were consumed. The connector may not be running, or "
            "cdc/seed.py has not been run. Check: curl localhost:8083/connectors/sentinel-product-price/status"
        )

    written = write_iceberg(state, changes, reset=args.reset)
    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(time.time() - started, 2),
        "topic": TOPIC,
        "change_events": len(changes),
        "distinct_products": len(state),
        "ops": {op: sum(1 for c in changes if c["op"] == op) for op in ("r", "c", "u", "d")},
        **written,
    }
    print(json.dumps(report, indent=2))

    out = Path("quality/reports/cdc_consume.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
