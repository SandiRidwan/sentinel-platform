"""The agent graph: plan -> act -> verify.

Why LangGraph for three nodes: the value is not the graph library, it is that the
control flow is explicit and inspectable. A question either produces a plan, or a
refusal, or a tool call, and every transition is a named edge rather than an if-tree
inside a prompt handler. That makes `verify` impossible to skip — which is the step that
stops an empty result from being reported as zero.

The planner has two implementations:

  deterministic  keyword and scope rules. Default. Runs offline, identical every time,
                 which is what makes the eval meaningful.
  llm            asks a chat model for a JSON plan, then validates it against the same
                 registry. Opt-in via SENTINEL_AGENT_PLANNER=llm and an OpenAI-compatible
                 endpoint. The model chooses a table and a metric; it never writes SQL.

Both produce the same `Plan` object, so the act/verify half of the graph is shared and
tested once.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.registry import Refused
from agents.tools import Warehouse
from agents.tracing import Trace

# --------------------------------------------------------------------------------------
# Scope. A question about data the warehouse does not hold must be refused, not answered
# with the nearest table. "How many books were sold yesterday" has the word "books" in
# it; answering with the catalogue count would be confidently wrong.
# --------------------------------------------------------------------------------------

OUT_OF_SCOPE_TERMS = {
    "sold": "there is no sales table in this warehouse",
    "sales": "there is no sales table in this warehouse",
    "revenue": "revenue is not modelled; prices are, sales are not",
    "order": "there is no orders table",
    "customer": "there is no customer table",
    "employee": "there is no employee table",
    "salary": "there is no employee table",
    "profit": "cost and profit are not modelled",
    "margin": "cost and profit are not modelled",
    "refund": "there is no returns table",
    "churn": "there is no subscription or activity table",
    "click": "there is no clickstream table",
    "session": "there is no clickstream table",
    "user": "there is no user table",
}

TIME_WINDOW_TERMS = {
    "yesterday": "the gold layer has no event timestamps to filter by day",
    "today": "the gold layer has no event timestamps to filter by day",
    "last week": "the gold layer has no event timestamps to filter by week",
    "last month": "the gold layer has no event timestamps to filter by month",
    "this year": "only hockey rows carry a season year",
    "last year": "only hockey rows carry a season year",
}

TABLE_HINTS = {
    "book": "gold.dim_book",
    "title": "gold.dim_book",
    "catalogue": "gold.dim_book",
    "catalog": "gold.dim_book",
    "category": "gold.dim_category",
    "categories": "gold.dim_category",
    "country": "gold.dim_country",
    "countries": "gold.dim_country",
    "capital": "gold.dim_country",
    "population": "gold.dim_country",
    "hockey": "gold.fct_hockey_season",
    "team": "gold.fct_hockey_season",
    "season": "gold.fct_hockey_season",
    "inventory": "gold.fct_books",
    "stock": "gold.fct_books",
}

COLUMN_HINTS = {
    "price": "price",
    "expensive": "price",
    "cost": "price",
    # Multi-word hints must be present as their own keys: hints are matched longest-first,
    # so "population density" wins over "population". Without this, "highest population
    # density" sorted by total population and answered China instead of Monaco.
    "population density": "population_density",
    "population": "population",
    "area": "area_km2",
    "density": "population_density",
    "goal difference": "goal_difference",
    "win": "wins",
    "wins": "wins",
    "goal": "goals_for",
    "goals": "goals_for",
    "rating": "rating",
    "rated": "rating",
    "stock count": "stock_count",
    "stock": "stock_count",
    "inventory value": "inventory_value",
    "inventory": "inventory_value",
}

MAX_ROWS = 10


@dataclass
class Plan:
    tool: Literal["count", "aggregate", "lookup", "list_tables", "describe", "refuse"]
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    rationale: str = ""


class AgentState(TypedDict, total=False):
    question: str
    plan: dict
    tool: str
    sql: str
    rows: list[dict]
    answer: str
    refused: bool
    refusal_reason: str
    error: str


def _mentions(question: str, terms: set[str]) -> str | None:
    lowered = question.lower()
    for term in sorted(terms, key=len, reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return term
    return None


def scope_refusal(question: str) -> str | None:
    """Return a refusal reason when the question asks for data that does not exist."""
    hit = _mentions(question, set(OUT_OF_SCOPE_TERMS))
    if hit:
        return f"{hit}: {OUT_OF_SCOPE_TERMS[hit]}"
    hit = _mentions(question, set(TIME_WINDOW_TERMS))
    if hit:
        return f"{hit}: {TIME_WINDOW_TERMS[hit]}"
    return None


def pick_table(question: str) -> str | None:
    lowered = question.lower()
    # Longest hint first so "inventory value" beats "inventory" and "category" beats "cat".
    for hint in sorted(TABLE_HINTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(hint)}", lowered):
            return TABLE_HINTS[hint]
    return None


def pick_column(question: str) -> str | None:
    lowered = question.lower()
    for hint in sorted(COLUMN_HINTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(hint)}", lowered):
            return COLUMN_HINTS[hint]
    return None


def _has(lowered: str, *words: str) -> bool:
    """Word-boundary match.

    `"count" in "countries"` is True, which turned "which country has the largest
    population" into a COUNT(*) and produced a confident, wrong answer. Intent matching
    on substrings is the bug; matching whole words is the fix.
    """
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


def deterministic_plan(question: str) -> Plan:
    """Map a question to a tool call, or to a refusal.

    Ordered: scope first, then table, then metric, then intent. A question with no
    recognisable table is refused rather than defaulted to books — a default table is how
    an agent starts answering questions about the wrong dataset without ever saying so.
    """
    refusal = scope_refusal(question)
    if refusal:
        return Plan(tool="refuse", reason=refusal, rationale="scope")

    lowered = question.lower()
    if _has(lowered, "tables") and _has(lowered, "what", "which"):
        return Plan(tool="list_tables", rationale="metadata")

    table = pick_table(question)
    if table is None:
        return Plan(
            tool="refuse",
            reason="no registered table matches this question",
            rationale="unmapped table",
        )

    filters = []
    if "out of stock" in lowered or "not in stock" in lowered:
        filters.append({"column": "is_in_stock", "op": "eq", "value": False})
    elif "in stock" in lowered:
        filters.append({"column": "is_in_stock", "op": "eq", "value": True})

    rating = re.search(r"rating (?:of )?([1-5])", lowered)
    if rating:
        filters.append({"column": "rating", "op": "eq", "value": int(rating.group(1))})

    band = re.search(r"(?:band|density band)\s+([a-z_-]+)", lowered)
    if band and table == "gold.dim_country":
        filters.append({"column": "density_band", "op": "eq", "value": band.group(1).upper()})

    column = pick_column(question)
    group_by = None
    if table == "gold.dim_country" and "band" in lowered and column:
        group_by = "density_band"
    if table == "gold.dim_book" and "category" in lowered and "each" in lowered:
        group_by = "category_name"
    if table == "gold.fct_books" and "category" in lowered:
        group_by = "category_key"

    if group_by and column:
        metric = "sum" if _has(lowered, "total", "sum", "value") else "avg"
        return Plan(
            tool="aggregate",
            args={"table": table, "metric": metric, "column": column, "group_by": group_by, "limit": MAX_ROWS},
            rationale=f"{metric} by {group_by}",
        )

    # Metric intent, in order of specificity. "total stock count" is a sum even though it
    # contains the word "count"; checking count first would answer it with 57 rows.
    if _has(lowered, "average", "mean"):
        if column is None:
            return Plan(tool="refuse", reason="average of what column?", rationale="ambiguous metric")
        return Plan(tool="aggregate", args={"table": table, "metric": "avg", "column": column}, rationale="average")

    if _has(lowered, "total", "sum"):
        if column is None:
            return Plan(tool="refuse", reason="total of what column?", rationale="ambiguous metric")
        return Plan(tool="aggregate", args={"table": table, "metric": "sum", "column": column}, rationale="total")

    if _has(lowered, "how", "many") and _has(lowered, "many"):
        return Plan(tool="count", args={"table": table, "filters": filters}, rationale="count")
    if _has(lowered, "count") or _has(lowered, "number") and _has(lowered, "of"):
        return Plan(tool="count", args={"table": table, "filters": filters}, rationale="count")

    if _has(lowered, "most", "highest", "largest", "biggest", "top"):
        if column is None:
            return Plan(tool="refuse", reason="most by which column?", rationale="ambiguous metric")
        return Plan(
            tool="lookup",
            args={
                "table": table,
                "columns": _default_columns(table, column),
                "order_by": column,
                "order": "desc",
                "limit": MAX_ROWS,
            },
            rationale="top by column",
        )

    if _has(lowered, "list", "show", "what", "which"):
        return Plan(
            tool="lookup",
            args={"table": table, "columns": _default_columns(table), "filters": filters, "limit": MAX_ROWS},
            rationale="lookup",
        )

    return Plan(
        tool="refuse",
        reason="could not map the question to a tool",
        rationale="unmapped intent",
    )


def _default_columns(table: str, order_by: str | None = None) -> list[str]:
    defaults = {
        "gold.dim_book": ["book_title", "price", "is_in_stock"],
        "gold.dim_category": ["category_name", "category_slug"],
        "gold.dim_country": ["country_name", "capital_name", "population"],
        "gold.fct_books": ["upc", "price", "stock_count", "inventory_value"],
        "gold.fct_hockey_season": ["team_name", "season_year", "wins", "losses"],
    }[table]
    columns = list(defaults)
    if order_by and order_by not in columns:
        columns.insert(0, order_by)
    return columns


def llm_plan(question: str, registry_yaml: str) -> Plan:
    """Ask a chat model for a plan, then validate it against the registry.

    The model is not trusted with SQL, and it is not trusted with table names either:
    the returned table goes through `Warehouse`, which refuses anything not registered.
    A malformed or hallucinated plan therefore degrades to a refusal, never to a query.
    """
    import httpx

    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if not base_url:
        raise RuntimeError("SENTINEL_AGENT_PLANNER=llm requires OPENAI_BASE_URL")

    prompt = (
        "You plan database questions. Reply with JSON only.\n"
        'Schema: {"tool": "count|aggregate|lookup|list_tables|refuse", "args": {...}, "reason": "..."}\n'
        "aggregate args: table, metric(count|sum|avg|min|max), column?, group_by?, filters?, limit?\n"
        "lookup args: table, columns[], filters?, order_by?, order?, limit?\n"
        "filters: [{\"column\": str, \"op\": \"eq|ne|gt|lt|gte|lte|like\", \"value\": any}]\n"
        "If the question needs data not in the tables below, use tool=refuse and explain.\n\n"
        f"TABLES:\n{registry_yaml}\n\nQUESTION: {question}\n"
    )
    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(content)
    return Plan(
        tool=payload.get("tool", "refuse"),
        args=payload.get("args", {}),
        reason=payload.get("reason", ""),
        rationale="llm",
    )


def build_graph(warehouse: Warehouse, trace: Trace, planner=None):
    planner = planner or deterministic_plan

    def plan_node(state: AgentState) -> AgentState:
        with trace.span("plan", question=state["question"]):
            plan = planner(state["question"])
        return {
            "plan": {"tool": plan.tool, "args": plan.args, "reason": plan.reason, "rationale": plan.rationale},
        }

    def route(state: AgentState) -> str:
        return "refuse" if state["plan"]["tool"] == "refuse" else "act"

    def refuse_node(state: AgentState) -> AgentState:
        reason = state["plan"].get("reason") or "out of scope"
        with trace.span("refuse", reason=reason):
            pass
        return {"refused": True, "refusal_reason": reason, "answer": f"I cannot answer that: {reason}."}

    def act_node(state: AgentState) -> AgentState:
        plan = state["plan"]
        args = dict(plan.get("args") or {})
        with trace.span("act", tool=plan["tool"]):
            try:
                if plan["tool"] == "list_tables":
                    result = warehouse.list_tables()
                elif plan["tool"] == "describe":
                    result = warehouse.describe(args["table"])
                elif plan["tool"] == "count":
                    result = warehouse.count(args["table"], args.get("filters"))
                elif plan["tool"] == "aggregate":
                    result = warehouse.aggregate(
                        args["table"],
                        args["metric"],
                        args.get("column"),
                        args.get("group_by"),
                        args.get("filters"),
                        args.get("order", "desc"),
                        args.get("limit", MAX_ROWS),
                    )
                elif plan["tool"] == "lookup":
                    result = warehouse.lookup(
                        args["table"],
                        args["columns"],
                        args.get("filters"),
                        args.get("order_by"),
                        args.get("order", "desc"),
                        args.get("limit", MAX_ROWS),
                    )
                else:
                    raise Refused(f"unknown tool {plan['tool']!r}")
            except Refused as exc:
                return {"refused": True, "refusal_reason": str(exc), "answer": f"I cannot answer that: {exc}."}

        return {"tool": result.name, "sql": result.sql, "rows": result.rows}

    def verify_node(state: AgentState) -> AgentState:
        rows = state.get("rows") or []
        plan = state.get("plan") or {}
        with trace.span("verify", rows=len(rows)):
            # A count of zero is an answer: "no rows match" is a fact about the data.
            # An empty *result set* from a lookup or an aggregate is not — it means the
            # question could not be satisfied, and reporting "0" there would invent a
            # finding. The distinction is the whole point of this node.
            if plan.get("tool") == "count" and rows:
                value = rows[0].get("n")
                if value == 0:
                    table = (plan.get("args") or {}).get("table")
                    return {"answer": f"0 rows in {table} match those conditions."}
            if not rows:
                return {
                    "refused": True,
                    "refusal_reason": "the query returned no rows",
                    "answer": "I cannot answer that: the query matched no rows, so there is nothing to report.",
                }
        return {"answer": summarise(plan, rows)}

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("refuse", refuse_node)
    graph.add_node("act", act_node)
    graph.add_node("verify", verify_node)

    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route, {"refuse": "refuse", "act": "act"})
    graph.add_edge("act", "verify")
    graph.add_edge("refuse", END)
    graph.add_edge("verify", END)
    return graph.compile()


def summarise(plan: dict, rows: list[dict]) -> str:
    """Turn a result set into one sentence.

    Deliberately literal: it reads the column names and values that came back rather
    than inventing a narrative. A summary that paraphrases is a summary that can be
    wrong in ways the rows are not.
    """
    tool = plan.get("tool")
    args = plan.get("args") or {}
    if tool == "count":
        value = rows[0].get("n")
        return f"{value} rows in {args.get('table')}."
    if tool == "aggregate":
        key = next((name for name in rows[0] if name not in {"group_key"}), None)
        value = rows[0].get(key) if key else None
        if "group_key" in rows[0]:
            pairs = ", ".join(f"{row['group_key']}={row.get(key)}" for row in rows[:5])
            return f"{args.get('metric')} of {args.get('column')} by {args.get('group_by')}: {pairs}."
        return f"{args.get('metric')} of {args.get('column')} in {args.get('table')} is {value}."
    if tool == "list_tables":
        return f"{len(rows)} tables are available: " + ", ".join(row["table"] for row in rows) + "."
    first = rows[0]
    return "; ".join(f"{key}={value}" for key, value in first.items())


class Agent:
    """Convenience wrapper: one question in, one answer out, with a trace."""

    def __init__(self, warehouse: Warehouse | None = None, planner=None, trace: Trace | None = None) -> None:
        self.warehouse = warehouse or Warehouse()
        self.trace = trace or Trace()
        if planner is None and os.environ.get("SENTINEL_AGENT_PLANNER", "deterministic").lower() == "llm":
            from pathlib import Path

            registry_yaml = (Path(__file__).resolve().parents[1] / "governance" / "agent_registry.yml").read_text(
                encoding="utf-8"
            )
            planner = lambda question: llm_plan(question, registry_yaml)  # noqa: E731
        self.graph = build_graph(self.warehouse, self.trace, planner)

    def ask(self, question: str) -> dict:
        state = self.graph.invoke({"question": question})
        return {
            "question": question,
            "answer": state.get("answer", ""),
            "refused": bool(state.get("refused")),
            "refusal_reason": state.get("refusal_reason", ""),
            "tool": state.get("tool", ""),
            "sql": state.get("sql", ""),
            "rows": state.get("rows", []),
            "plan": state.get("plan", {}),
        }

    def close(self) -> None:
        self.warehouse.close()
        self.trace.flush()
