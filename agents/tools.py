"""Read-only query tools over the dev warehouse, bounded by the agent registry.

Every function takes a table *name* and lets `agents.registry` decide whether that name
exists. Nothing here accepts SQL text from a caller.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from agents.registry import Registry, Refused, build_aggregate, build_count, build_lookup, load_registry
from tools.common.config import get_settings


@dataclass
class ToolResult:
    name: str
    sql: str
    rows: list[dict]
    columns: list[str]

    @property
    def empty(self) -> bool:
        return not self.rows


class Warehouse:
    """A read-only handle on the dev warehouse.

    `read_only=True` is not cosmetic: an agent that can write is an agent that can be
    talked into writing, and the guard against that should be the connection, not a
    regex over the SQL.
    """

    def __init__(self, registry: Registry | None = None, database: str | None = None) -> None:
        self.registry = registry or load_registry()
        path = database or str(get_settings().duckdb_path)
        self.connection = duckdb.connect(path, read_only=True)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _run(self, name: str, sql: str, params: list) -> ToolResult:
        cursor = self.connection.execute(sql, params) if params else self.connection.execute(sql)
        columns = [description[0] for description in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return ToolResult(name=name, sql=sql, rows=rows, columns=columns)

    def list_tables(self) -> ToolResult:
        rows = [
            {"table": spec.name, "description": spec.description, "columns": ", ".join(sorted(spec.columns))}
            for spec in sorted(self.registry.tables.values(), key=lambda s: s.name)
        ]
        return ToolResult(name="list_tables", sql="", rows=rows, columns=["table", "description", "columns"])

    def describe(self, table: str) -> ToolResult:
        spec = self.registry.resolve_table(table)
        rows = [{"column": name, "type": kind} for name, kind in sorted(spec.columns.items())]
        return ToolResult(name="describe_table", sql="", rows=rows, columns=["column", "type"])

    def count(self, table: str, filters: list[dict] | None = None) -> ToolResult:
        spec = self.registry.resolve_table(table)
        sql, params = build_count(spec, filters)
        return self._run("count_rows", sql, params)

    def aggregate(
        self,
        table: str,
        metric: str,
        column: str | None = None,
        group_by: str | None = None,
        filters: list[dict] | None = None,
        order: str = "desc",
        limit: int = 10,
    ) -> ToolResult:
        spec = self.registry.resolve_table(table)
        sql, params = build_aggregate(spec, metric, column, group_by, filters, order, limit)
        return self._run("aggregate", sql, params)

    def lookup(
        self,
        table: str,
        columns: list[str],
        filters: list[dict] | None = None,
        order_by: str | None = None,
        order: str = "desc",
        limit: int = 10,
    ) -> ToolResult:
        spec = self.registry.resolve_table(table)
        sql, params = build_lookup(spec, columns, filters, order_by, order, limit)
        return self._run("lookup", sql, params)


__all__ = ["Warehouse", "ToolResult", "Refused"]
