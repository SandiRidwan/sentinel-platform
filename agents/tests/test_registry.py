"""Tests for the agent registry: the gate between a question and a table.

The important cases are the refusals. A registry that permits everything passes every
"happy path" test and provides no protection at all.
"""

from __future__ import annotations

import pytest

from agents.registry import Refused, build_aggregate, build_lookup, load_registry


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def test_registered_table_resolves(registry) -> None:
    spec = registry.resolve_table("gold.dim_book")
    assert "price" in spec.columns


def test_denied_table_is_refused_with_its_reason(registry) -> None:
    with pytest.raises(Refused) as excinfo:
        registry.resolve_table("gold.dim_book_snapshot")
    assert "deliberately out of reach" in str(excinfo.value)


def test_staging_layer_is_refused(registry) -> None:
    with pytest.raises(Refused):
        registry.resolve_table("silver.stg_books")


def test_unknown_table_is_refused(registry) -> None:
    with pytest.raises(Refused) as excinfo:
        registry.resolve_table("gold.secret_table")
    assert "not a registered table" in str(excinfo.value)


def test_undeclared_column_is_refused(registry) -> None:
    spec = registry.resolve_table("gold.dim_book")
    with pytest.raises(Refused):
        build_lookup(spec, ["book_title", "internal_notes"])


def test_unknown_operator_is_refused(registry) -> None:
    spec = registry.resolve_table("gold.dim_book")
    with pytest.raises(Refused):
        build_lookup(spec, ["book_title"], filters=[{"column": "price", "op": "drop", "value": 1}])


def test_unknown_metric_is_refused(registry) -> None:
    spec = registry.resolve_table("gold.dim_book")
    with pytest.raises(Refused):
        build_aggregate(spec, "median", "price")


def test_string_literal_is_escaped(registry) -> None:
    """A quote in a value must not end the literal."""
    spec = registry.resolve_table("gold.dim_book")
    sql, _ = build_lookup(spec, ["book_title"], filters=[{"column": "book_title", "op": "eq", "value": "O'Brien"}])
    assert "'O''Brien'" in sql


def test_limit_is_capped(registry) -> None:
    spec = registry.resolve_table("gold.dim_book")
    sql, _ = build_lookup(spec, ["book_title"], limit=10_000)
    assert "LIMIT 50" in sql


def test_boolean_renders_as_sql_keyword(registry) -> None:
    spec = registry.resolve_table("gold.dim_book")
    sql, _ = build_lookup(spec, ["book_title"], filters=[{"column": "is_in_stock", "op": "eq", "value": False}])
    assert "= FALSE" in sql
