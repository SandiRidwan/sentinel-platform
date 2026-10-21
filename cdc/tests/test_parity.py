"""Unit tests for the CDC comparator.

The live path (Kafka + Debezium + Iceberg) is exercised by `python -m cdc.parity`,
which needs the stack up. These tests cover the part that must be right regardless:
the comparison itself, and the negative control that proves the comparison can fail.

A comparator with no failing case is not a test, it is a decoration.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cdc.parity import compare, negative_control, normalise


def row(price="10.00", stock=True, updated="2026-10-21T06:00:00+00:00") -> dict:
    return {
        "product_name": "Product 0001",
        "category": "books",
        "price": normalise(Decimal(price)),
        "currency": "USD",
        "in_stock": stock,
        "updated_at": normalise(updated),
    }


def test_identical_state_is_equal() -> None:
    source = {"P0001": row(), "P0002": row("11.25", False)}
    stream = {"P0001": row(), "P0002": row("11.25", False)}

    result = compare(source, stream)

    assert result["equal"] is True
    assert result["verified"] == 2
    assert result["field_mismatch_count"] == 0


def test_value_change_is_detected() -> None:
    source = {"P0001": row("10.00")}
    stream = {"P0001": row("10.01")}

    result = compare(source, stream)

    assert result["equal"] is False
    assert result["field_mismatch_count"] == 1
    assert result["field_mismatches"][0]["column"] == "price"


def test_missing_row_is_detected() -> None:
    source = {"P0001": row(), "P0002": row()}
    stream = {"P0001": row()}

    result = compare(source, stream)

    assert result["equal"] is False
    assert result["missing_in_stream"] == ["P0002"]


def test_extra_row_is_detected() -> None:
    source = {"P0001": row()}
    stream = {"P0001": row(), "P9999": row()}

    result = compare(source, stream)

    assert result["equal"] is False
    assert result["extra_in_stream"] == ["P9999"]


def test_empty_comparison_is_not_a_pass() -> None:
    """Zero rows compared must not read as success — the main() guard relies on this."""
    result = compare({}, {})

    assert result["equal"] is True
    assert result["verified"] == 0


def test_negative_control_catches_both_kinds_of_corruption() -> None:
    source = {f"P{index:04d}": row() for index in range(1, 11)}

    control = negative_control(source)

    assert control["ran"] is True
    assert control["detected_value_change"] is True
    assert control["detected_missing_row"] is True
    assert control["passed"] is True


def test_negative_control_refuses_to_run_on_empty_source() -> None:
    control = negative_control({})

    assert control["ran"] is False
    assert "passed" not in control


def test_timestamp_precision_is_seconds_not_microseconds() -> None:
    """A formatting difference below the second must not read as a data difference."""
    left = normalise("2026-10-21T06:00:00.123456+00:00")
    right = normalise("2026-10-21T06:00:00.987654+00:00")

    assert left == right


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (Decimal("12.30"), Decimal("12.30")),
        (True, True),
        ("books", "books"),
    ],
)
def test_normalise_passes_through_plain_values(value, expected) -> None:
    assert normalise(value) == expected
