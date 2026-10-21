"""Item pipelines: gate -> validate -> scan for PII -> land as Parquet.

The order in settings.ITEM_PIPELINES is load-bearing. An item that fails validation is
dropped and counted; it is never written. That counter is what the quality suite reads.
"""

from __future__ import annotations

import io
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from itemadapter import ItemAdapter
from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.exceptions import DropItem

from tools.common.logging_setup import get_logger
from tools.common.s3 import put_bytes
from tools.common.sources import load_register

log = get_logger("scrapy.pipelines")

# Required fields per item class name. A missing required field is a hard drop.
REQUIRED: dict[str, tuple[str, ...]] = {
    "BookItem": ("source", "url", "title", "price", "currency", "availability", "rating"),
    "QuoteItem": ("source", "url", "text", "author"),
    "CountryItem": ("source", "url", "name"),
    "HockeyTeamItem": ("source", "url", "team"),
    "CategoryItem": ("source", "url", "name"),
}

# Numeric fields that must parse; unparseable values become None, not a wrong number.
NUMERIC: dict[str, tuple[str, ...]] = {
    "BookItem": ("price", "stock_count", "rating"),
    "CountryItem": ("population", "area_km2"),
    "HockeyTeamItem": ("year", "wins", "losses", "ot_losses", "win_pct", "goals_for", "goals_against"),

}

PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    "phone_intl": re.compile(r"\+\d[\d\s().-]{7,}\d"),
    "ipv4": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    "credit_card_like": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
}


class SourceGatePipeline:
    """Second gate. The middleware blocks the request; this blocks the item.

    Both exist because an item can be produced by a spider callback whose request came
    from somewhere the middleware never saw (a cached page, a file:// fixture in a test).
    """

    def open_spider(self, spider: Spider) -> None:
        self.register = load_register()
        self.dropped = 0

    def process_item(self, item, spider: Spider):
        source_key = ItemAdapter(item).get("source")
        entry = self.register.sources.get(source_key)
        if entry is None or not entry.approved:
            self.dropped += 1
            raise DropItem(f"item source '{source_key}' is not an approved source")
        return item

    def close_spider(self, spider: Spider) -> None:
        if self.dropped:
            log.warning(
                "gate dropped items",
                extra={"event": "gate_dropped", "spider": spider.name, "rows": self.dropped},
            )


class ValidationPipeline:
    """Enforce required fields and coerce numerics. Count rejections with a reason."""

    def open_spider(self, spider: Spider) -> None:
        self.accepted = 0
        self.rejected = 0
        self.reasons: dict[str, int] = defaultdict(int)

    def process_item(self, item, spider: Spider):
        adapter = ItemAdapter(item)
        cls_name = type(item).__name__
        required = REQUIRED.get(cls_name, ("source", "url"))

        for field in required:
            if adapter.get(field) in (None, "", []):
                self._reject(f"missing:{field}")
                raise DropItem(f"missing required field '{field}'")

        for field in NUMERIC.get(cls_name, ()):
            value = adapter.get(field)
            if value is None:
                continue
            coerced = _coerce_number(value)
            if coerced is None:
                self._reject(f"unparseable:{field}")
                raise DropItem(f"field '{field}' is not numeric: {value!r}")
            adapter[field] = coerced

        self.accepted += 1
        return item

    def _reject(self, reason: str) -> None:
        self.rejected += 1
        self.reasons[reason] += 1

    def close_spider(self, spider: Spider) -> None:
        log.info(
            "validation summary",
            extra={"event": "validation", "spider": spider.name, "rows": self.accepted},
        )
        if self.rejected:
            top = ", ".join(f"{k}={v}" for k, v in sorted(self.reasons.items(), key=lambda kv: -kv[1]))
            log.warning(
                "validation rejections",
                extra={"event": "validation_rejected", "spider": spider.name, "rows": self.rejected, "stage": top},
            )


class PiiScanPipeline:
    """Detect PII in free-text fields and mask it before it is written.

    Masking is on by default. When PII_MASK_ENABLED=false the values are preserved but
    still counted, so a run can be audited without changing what is stored.
    """

    TEXT_FIELDS = ("text", "title", "notes", "author")

    def open_spider(self, spider: Spider) -> None:
        self.mask = os.environ.get("PII_MASK_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
        self.detections: dict[str, int] = defaultdict(int)

    def process_item(self, item, spider: Spider):
        adapter = ItemAdapter(item)
        for field in self.TEXT_FIELDS:
            value = adapter.get(field)
            if not isinstance(value, str) or not value:
                continue
            masked = value
            for label, pattern in PII_PATTERNS.items():
                found = pattern.findall(masked)
                if not found:
                    continue
                self.detections[label] += len(found)
                if self.mask:
                    masked = pattern.sub(f"[REDACTED_{label.upper()}]", masked)
            if masked != value:
                adapter[field] = masked
        return item

    def close_spider(self, spider: Spider) -> None:
        if self.detections:
            total = sum(self.detections.values())
            detail = ", ".join(f"{k}={v}" for k, v in self.detections.items())
            log.info(
                "pii scan",
                extra={"event": "pii_scan", "spider": spider.name, "rows": total, "stage": detail},
            )
        else:
            log.info("pii scan clean", extra={"event": "pii_scan", "spider": spider.name, "rows": 0})


class ParquetSinkPipeline:
    """Buffer items per item-type and write partitioned Parquet to the raw bucket.

    One file per flush, never one file per item: a small-files lake is unusable and the
    mistake is expensive to undo later.
    """

    FLUSH_EVERY = 200

    def __init__(self, bucket: str, prefix: str) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.item_class_by_table: dict[str, str] = {}
        self.written_files: list[str] = []
        self.written_rows = 0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "ParquetSinkPipeline":
        from tools.common.config import get_settings

        settings = get_settings()
        return cls(bucket=settings.s3.bucket_raw, prefix="")

    def open_spider(self, spider: Spider) -> None:
        log.info(
            "sink ready",
            extra={"event": "sink_open", "spider": spider.name, "source": self.bucket},
        )

    def process_item(self, item, spider: Spider):
        adapter = ItemAdapter(item)
        record = dict(adapter)
        record.setdefault("scraped_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        record["_spider"] = spider.name
        item_class = type(item).__name__
        table = _table_name(item_class)
        self.item_class_by_table[table] = item_class
        self.buffers[table].append(record)
        if len(self.buffers[table]) >= self.FLUSH_EVERY:
            self._flush(table, spider)
        return item

    def _flush(self, table: str, spider: Spider) -> None:
        rows = self.buffers.get(table) or []
        if not rows:
            return
        now = datetime.now(timezone.utc)
        key = (
            f"{self.prefix}{table}/dt={now:%Y-%m-%d}/hour={now:%H}/"
            f"{spider.name}-{now:%Y%m%dT%H%M%S}-{len(self.written_files):04d}.parquet"
        )
        payload = _rows_to_parquet(rows, self.item_class_by_table.get(table))
        uri = put_bytes(key, payload, bucket=self.bucket, content_type="application/octet-stream")
        self.written_files.append(uri)
        self.written_rows += len(rows)
        self.buffers[table] = []
        log.info(
            "flushed parquet",
            extra={"event": "parquet_flush", "spider": spider.name, "stage": table, "rows": len(rows)},
        )

    def close_spider(self, spider: Spider) -> None:
        for table in list(self.buffers):
            self._flush(table, spider)
        log.info(
            "sink summary",
            extra={
                "event": "sink_close",
                "spider": spider.name,
                "rows": self.written_rows,
                "stage": f"files={len(self.written_files)}",
            },
        )
        # Surface totals to the runner through the spider's stats.
        spider.crawler.stats.set_value("sentinel/rows_landed", self.written_rows)
        spider.crawler.stats.set_value("sentinel/files_written", len(self.written_files))


# Explicit table names. String-mangling "Country" into a plural produces "countrys",
# which is exactly the kind of quiet wrongness that then propagates into dbt model names.
TABLE_BY_ITEM = {
    "BookItem": "books",
    "QuoteItem": "quotes",
    "CountryItem": "countries",
    "HockeyTeamItem": "hockey_teams",
    "CategoryItem": "book_categories",
}


def _table_name(item_class_name: str) -> str:
    return TABLE_BY_ITEM.get(item_class_name, re.sub(r"(?<!^)(?=[A-Z])", "_", item_class_name).lower())


def _coerce_number(value: Any) -> float | int | None:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
        if cleaned in {"", "-", "."}:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


# Explicit Parquet schemas, per item type.
#
# Inferring the schema per flush is how a lake rots: a run where a column happened to be
# all-null writes it as string, the next run writes it as int64, and Spark then refuses to
# read the table at all ("Parquet column cannot be converted ... [ot_losses]"). Declaring
# the type once means every file is readable by every reader.
PARQUET_SCHEMAS: dict[str, pa.Schema] = {
    "BookItem": pa.schema([
        ("source", pa.string()), ("url", pa.string()), ("title", pa.string()),
        ("price", pa.float64()), ("currency", pa.string()), ("availability", pa.string()),
        ("stock_count", pa.int64()), ("rating", pa.int64()), ("category", pa.string()),
        ("upc", pa.string()), ("scraped_at", pa.string()), ("_spider", pa.string()),
    ]),
    "CategoryItem": pa.schema([
        ("source", pa.string()), ("url", pa.string()), ("name", pa.string()),
        ("slug", pa.string()), ("scraped_at", pa.string()), ("_spider", pa.string()),
    ]),
    "QuoteItem": pa.schema([
        ("source", pa.string()), ("url", pa.string()), ("text", pa.string()),
        ("author", pa.string()), ("author_url", pa.string()),
        ("tags", pa.list_(pa.string())), ("scraped_at", pa.string()), ("_spider", pa.string()),
    ]),
    "CountryItem": pa.schema([
        ("source", pa.string()), ("url", pa.string()), ("name", pa.string()),
        ("capital", pa.string()), ("population", pa.int64()), ("area_km2", pa.float64()),
        ("scraped_at", pa.string()), ("_spider", pa.string()),
    ]),
    "HockeyTeamItem": pa.schema([
        ("source", pa.string()), ("url", pa.string()), ("year", pa.int64()),
        ("team", pa.string()), ("wins", pa.int64()), ("losses", pa.int64()),
        ("ot_losses", pa.int64()), ("win_pct", pa.float64()), ("goals_for", pa.int64()),
        ("goals_against", pa.int64()), ("scraped_at", pa.string()), ("_spider", pa.string()),
    ]),
}


def _rows_to_parquet(rows: list[dict[str, Any]], item_class_name: str | None = None) -> bytes:
    """Write rows with a declared schema when one exists, inferring only as a fallback."""
    schema = PARQUET_SCHEMAS.get(item_class_name or "")

    if schema is not None:
        columns: dict[str, list[Any]] = {name: [] for name in schema.names}
        for row in rows:
            for name in schema.names:
                columns[name].append(_coerce_for_field(row.get(name), schema.field(name).type))
        arrays = [pa.array(columns[name], type=schema.field(name).type) for name in schema.names]
        table = pa.Table.from_arrays(arrays, schema=schema)
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="zstd")
        return buffer.getvalue()

    # No declared schema: fall back to inference, still guarding against the
    # all-null-becomes-string trap by widening to string only for that case.
    columns = {}
    for row in rows:
        for key, value in row.items():
            columns.setdefault(key, []).append(value)

    arrays: dict[str, pa.Array] = {}
    for name, values in columns.items():
        if all(v is None for v in values):
            arrays[name] = pa.array([None] * len(values), type=pa.string())
            continue
        try:
            arrays[name] = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arrays[name] = pa.array([None if v is None else str(v) for v in values], type=pa.string())

    table = pa.table(arrays)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="zstd")
    return buffer.getvalue()


def _coerce_for_field(value: Any, arrow_type: pa.DataType) -> Any:
    """Fit a python value to the declared Arrow type, or None. Never guesses a wrong number."""
    if value is None:
        return None
    if pa.types.is_integer(arrow_type):
        number = _coerce_number(value)
        return int(number) if number is not None else None
    if pa.types.is_floating(arrow_type):
        number = _coerce_number(value)
        return float(number) if number is not None else None
    if pa.types.is_list(arrow_type):
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [str(value)] if value else []
    return str(value)
