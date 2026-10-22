"""The agent's three phases, with no orchestration library in them.

`graph.py` wires these into a LangGraph state machine, and `portability.py` wires the same
three methods into LangChain, LlamaIndex and CrewAI. The split exists for the same reason
`orchestration/shared/pipeline.py` exists: if each framework had its own copy of "how to
plan" and "when to refuse", a difference between frameworks would prove nothing about
portability — it would prove that four copies of the logic had drifted.

What lives here: the scope rules, the deterministic planner, the registry-backed tool call,
and the verify step that refuses an empty result set while accepting a count of zero.

What does not live here: any import of langgraph, langchain, llama_index or crewai. That is
enforced by the imports, and it is what lets the CrewAI worker run in its own virtualenv,
where those packages cannot be installed together.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

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

    def as_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "reason": self.reason, "rationale": self.rationale}

def _mentions(question: str, terms: set[str]) -> str | None:
    """The longest matching term, with ties broken alphabetically.

    The tie-break is not cosmetic. `sorted(terms, key=len, reverse=True)` keeps the input
    order for equal lengths, and the input is a set — whose iteration order depends on string
    hashing, which Python randomises per process. "Which book has the highest profit margin?"
    matches both `profit` and `margin`, so the refusal reason alternated between runs and the
    four-framework comparison caught it as a disagreement. Sorting by (-length, term) makes
    the choice a property of the words, not of the interpreter's hash seed.
    """
    lowered = question.lower()
    for term in sorted(terms, key=lambda item: (-len(item), item)):
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

class AgentCore:
    """plan -> act -> verify, with no orchestration library involved.

    Each framework wrapper calls these three methods in whatever shape it prefers. The
    methods are the contract; the wrapper is the variable being tested.

    Spans are emitted here, not in the wrappers, so a trace has the same span names no
    matter which framework ran the question.
    """

    def __init__(
        self,
        warehouse: Warehouse | None = None,
        planner=None,
        trace: Trace | None = None,
    ) -> None:
        self.warehouse = warehouse or Warehouse()
        self.trace = trace or Trace()
        self.planner = planner or self._default_planner()

    @staticmethod
    def _default_planner():
        if os.environ.get("SENTINEL_AGENT_PLANNER", "deterministic").lower() == "llm":
            from pathlib import Path

            registry_yaml = (
                Path(__file__).resolve().parents[1] / "governance" / "agent_registry.yml"
            ).read_text(encoding="utf-8")
            return lambda question: llm_plan(question, registry_yaml)
        return deterministic_plan

    def plan(self, question: str) -> dict:
        with self.trace.span("plan", question=question):
            plan = self.planner(question)
        return plan.as_dict()

    def refuse(self, plan: dict) -> dict:
        reason = plan.get("reason") or "out of scope"
        with self.trace.span("refuse", reason=reason):
            pass
        return {"refused": True, "refusal_reason": reason, "answer": f"I cannot answer that: {reason}."}

    def act(self, plan: dict) -> dict:
        """Run the planned tool call, or return a refusal.

        Returns either `{"tool", "sql", "rows"}` or the refusal dict. The caller decides
        what to do with it; no exception escapes for a refusal, because a refusal is a
        normal outcome and an exception would make each framework handle it differently.
        """
        args = dict(plan.get("args") or {})
        with self.trace.span("act", tool=plan["tool"]):
            try:
                if plan["tool"] == "list_tables":
                    result = self.warehouse.list_tables()
                elif plan["tool"] == "describe":
                    result = self.warehouse.describe(args["table"])
                elif plan["tool"] == "count":
                    result = self.warehouse.count(args["table"], args.get("filters"))
                elif plan["tool"] == "aggregate":
                    result = self.warehouse.aggregate(
                        args["table"],
                        args["metric"],
                        args.get("column"),
                        args.get("group_by"),
                        args.get("filters"),
                        args.get("order", "desc"),
                        args.get("limit", MAX_ROWS),
                    )
                elif plan["tool"] == "lookup":
                    result = self.warehouse.lookup(
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

    def verify(self, plan: dict, rows: list[dict]) -> dict:
        """Decide whether the result set answers the question.

        A count of zero is an answer: "no rows match" is a fact about the data. An empty
        result set from a lookup or an aggregate is not — it means the question could not
        be satisfied, and reporting "0" there would invent a finding.
        """
        with self.trace.span("verify", rows=len(rows)):
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

    def ask(self, question: str) -> dict:
        """The three phases in order, for wrappers that do not need a graph.

        LangGraph expresses this as a state machine; LangChain as a runnable chain;
        LlamaIndex as workflow steps; CrewAI as flow listeners. They all end up here.
        """
        plan = self.plan(question)
        state: dict = {"question": question, "plan": plan}

        if plan["tool"] == "refuse":
            state.update(self.refuse(plan))
        else:
            outcome = self.act(plan)
            state.update(outcome)
            if not outcome.get("refused"):
                state.update(self.verify(plan, outcome.get("rows") or []))

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
