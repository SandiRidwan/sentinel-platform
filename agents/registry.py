"""Load the agent registry and build SQL from validated parts.

Two rules, both enforced here rather than trusted to a caller:

  1. A table that is not in governance/agent_registry.yml does not exist, whatever the
     database says. `resolve_table` raises `Refused` before any SQL is built.
  2. A column that is not declared for that table is refused the same way. This is what
     stops `select *` from becoming an accidental data export: the column list comes
     from the registry, not from the caller.

Because the SQL is assembled from an allow-listed table, allow-listed columns, and a
fixed set of operators and metrics, there is no free-text SQL path to inject into.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "governance" / "agent_registry.yml"

METRICS = {"count", "sum", "avg", "min", "max"}
OPERATORS = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "like": "LIKE"}
MAX_LIMIT = 50


class Refused(Exception):
    """Raised when a request falls outside the registry.

    A refusal is a normal outcome, not a crash: the agent reports it as the answer.
    Keeping it a distinct exception type is what lets the eval tell "refused on purpose"
    apart from "failed unexpectedly".
    """


@dataclass(frozen=True)
class TableSpec:
    name: str
    description: str
    columns: dict[str, str]

    def require_column(self, column: str) -> str:
        if column not in self.columns:
            raise Refused(
                f"column {column!r} is not declared for {self.name}. "
                f"Available: {', '.join(sorted(self.columns))}"
            )
        return column


@dataclass(frozen=True)
class Registry:
    tables: dict[str, TableSpec]
    denied: dict[str, str]

    def resolve_table(self, name: str) -> TableSpec:
        if name in self.denied:
            raise Refused(f"{name} is deliberately out of reach: {self.denied[name]}")
        if name not in self.tables:
            raise Refused(
                f"{name!r} is not a registered table. "
                f"Registered: {', '.join(sorted(self.tables))}"
            )
        return self.tables[name]


def load_registry(path: Path | None = None) -> Registry:
    source = path or REGISTRY_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))

    tables = {
        name: TableSpec(name=name, description=spec.get("description", ""), columns=spec.get("columns", {}))
        for name, spec in (raw.get("tables") or {}).items()
    }
    denied = {entry["table"]: entry.get("reason", "denied") for entry in (raw.get("denied") or [])}
    return Registry(tables=tables, denied=denied)


def _quote(value: Any) -> str:
    """Render a literal safely.

    Numbers and booleans are rendered as-is; everything else is a single-quoted string
    with embedded quotes doubled. This is the only place a value becomes SQL text, which
    is why it is a function with a docstring rather than an f-string inline.
    """
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def build_count(spec: TableSpec, filters: list[dict] | None = None) -> tuple[str, list]:
    where, params = _where(spec, filters)
    return f"SELECT COUNT(*) AS n FROM {spec.name}{where}", params


def build_aggregate(
    spec: TableSpec,
    metric: str,
    column: str | None = None,
    group_by: str | None = None,
    filters: list[dict] | None = None,
    order: str = "desc",
    limit: int = 10,
) -> tuple[str, list]:
    if metric not in METRICS:
        raise Refused(f"metric {metric!r} is not one of {sorted(METRICS)}")
    if metric != "count" and column is None:
        raise Refused(f"metric {metric!r} needs a column")

    if metric == "count":
        expression = "COUNT(*)"
    else:
        expression = f"{metric.upper()}({spec.require_column(column)})"

    alias = f"{metric}_{column}" if column else "n"
    if group_by is not None:
        spec.require_column(group_by)
        select = f"SELECT {group_by} AS group_key, {expression} AS {alias}"
        grouping = f" GROUP BY {group_by}"
        order_by = f" ORDER BY {alias} {'DESC' if order == 'desc' else 'ASC'}"
    else:
        select = f"SELECT {expression} AS {alias}"
        grouping = ""
        order_by = ""

    where, params = _where(spec, filters)
    limit = max(1, min(int(limit), MAX_LIMIT))
    return f"{select} FROM {spec.name}{where}{grouping}{order_by} LIMIT {limit}", params


def build_lookup(
    spec: TableSpec,
    columns: list[str],
    filters: list[dict] | None = None,
    order_by: str | None = None,
    order: str = "desc",
    limit: int = 10,
) -> tuple[str, list]:
    if not columns:
        raise Refused("lookup needs at least one column")
    for column in columns:
        spec.require_column(column)

    selected = ", ".join(columns)
    where, params = _where(spec, filters)
    clause = ""
    if order_by:
        spec.require_column(order_by)
        clause = f" ORDER BY {order_by} {'DESC' if order == 'desc' else 'ASC'}"
    limit = max(1, min(int(limit), MAX_LIMIT))
    return f"SELECT {selected} FROM {spec.name}{where}{clause} LIMIT {limit}", params


def _where(spec: TableSpec, filters: list[dict] | None) -> tuple[str, list]:
    if not filters:
        return "", []

    clauses: list[str] = []
    params: list = []
    for item in filters:
        column = item.get("column")
        operator = item.get("op", "eq")
        value = item.get("value")

        if operator not in OPERATORS:
            raise Refused(f"operator {operator!r} is not one of {sorted(OPERATORS)}")
        if operator == "like":
            spec.require_column(column)
            clauses.append(f"{column} LIKE {_quote(value)}")
            continue

        spec.require_column(column)
        clauses.append(f"{column} {OPERATORS[operator]} {_quote(value)}")

    return " WHERE " + " AND ".join(clauses), params
