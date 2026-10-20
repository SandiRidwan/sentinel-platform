"""Structured logging shared by every component.

JSON on stdout when SENTINEL_LOG_JSON=1 (the default in containers and CI), a compact
human line otherwise. Every run carries a `run_id` so a log line can be traced back to
the ingest_run table.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "source", "stage", "spider", "rows", "duration_ms", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        extra = " ".join(
            f"{k}={getattr(record, k)}"
            for k in ("run_id", "source", "stage", "rows", "duration_ms")
            if getattr(record, k, None) is not None
        )
        base = f"{ts} {record.levelname:<5} {record.name}: {record.getMessage()}"
        return f"{base}  {extra}".rstrip()


def setup_logging(level: str | None = None, force_json: bool | None = None) -> None:
    use_json = (
        force_json
        if force_json is not None
        else os.environ.get("SENTINEL_LOG_JSON", "0").strip().lower() in {"1", "true", "yes"}
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if use_json else HumanFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or os.environ.get("SENTINEL_LOG_LEVEL", "INFO")).upper())

    # Scrapy and boto are chatty at DEBUG; keep them at WARNING unless asked.
    for noisy in ("botocore", "urllib3", "s3transfer", "boto3", "pyiceberg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
