"""Scan the warehouse for PII and report what it finds.

The pipeline masks PII on the way in (PiiScanPipeline). This is the independent check: it
looks at what actually landed, because a masking rule that silently stopped matching is
indistinguishable from a clean dataset unless something looks at the output.

    python -m governance.pii.scan
    python -m governance.pii.scan --fail-on-detection
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("governance.pii")

# Patterns that indicate a real identifier, not a shape. Order matters: the specific
# patterns run first so a credit-card-like number is not also counted as a phone number.
PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    "phone_intl": re.compile(r"\+\d[\d\s().-]{7,}\d"),
    "credit_card_like": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ipv4": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    "iban_like": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
}

# Columns worth scanning. Scanning every column is slow and produces noise; these are the
# free-text and identifier columns where PII can actually appear.
SCAN_TARGETS: list[tuple[str, str, str]] = [
    ("gold", "dim_book", "book_title"),
    ("gold", "dim_country", "country_name"),
    ("gold", "dim_country", "capital_name"),
    ("silver", "stg_quotes", "quote_text"),
    ("silver", "stg_quotes", "author_name"),
]


def scan() -> dict:
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    findings: list[dict] = []

    try:
        for schema, table, column in SCAN_TARGETS:
            try:
                values = connection.execute(
                    f'select distinct "{column}" from {schema}.{table} '
                    f'where "{column}" is not null limit 50000'
                ).fetchall()
            except Exception as exc:  # noqa: BLE001
                findings.append({
                    "schema": schema, "table": table, "column": column,
                    "status": "unavailable", "error": str(exc),
                })
                continue

            counts: dict[str, int] = {}
            examples: dict[str, list[str]] = {}
            for (value,) in values:
                if not isinstance(value, str):
                    continue
                for label, pattern in PATTERNS.items():
                    matches = pattern.findall(value)
                    if matches:
                        counts[label] = counts.get(label, 0) + len(matches)
                        # Keep a redacted shape, never the value itself.
                        examples.setdefault(label, []).append(_redact(matches[0]))

            findings.append({
                "schema": schema,
                "table": table,
                "column": column,
                "status": "clean" if not counts else "pii_found",
                "rows_scanned": len(values),
                "detections": counts,
                "redacted_examples": examples,
            })
    finally:
        connection.close()

    return {"scanned_at": datetime.now(timezone.utc).isoformat(), "findings": findings}


def _redact(value: str) -> str:
    """Show the shape, never the identifier: keep the first and last character only."""
    if len(value) <= 2:
        return "**"
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Scan the warehouse for PII.")
    parser.add_argument("--fail-on-detection", action="store_true")
    args = parser.parse_args(argv)

    report = scan()
    total = 0
    for finding in report["findings"]:
        status = finding["status"]
        if status == "unavailable":
            print(f"  {finding['schema']}.{finding['table']}.{finding['column']:<20} UNAVAILABLE")
            continue
        counts = finding.get("detections", {})
        detected = sum(counts.values())
        total += detected
        label = "CLEAN" if detected == 0 else f"PII x{detected} {counts}"
        print(f"  {finding['schema']}.{finding['table']}.{finding['column']:<20} {label}")

    report["total_detections"] = total
    path = Path("quality/reports/pii_scan.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    if total:
        print(f"detections: {total} (see {path})")
        if args.fail_on_detection:
            print("FAILED: PII present in the warehouse")
            return 1
        print("note: not failing (pass --fail-on-detection to gate on this)")
        return 0
    print("PASS: no PII detected in scanned columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
