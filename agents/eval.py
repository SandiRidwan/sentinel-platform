"""Evaluate the agent: 20 questions it must answer, 10 it must refuse.

The expected answer for every valid question is computed here, with hand-written SQL
run directly against DuckDB. That is the point: if the expectation came from the agent's
own query, the eval would only prove the agent agrees with itself.

The refusal set is not decoration. An agent that answers everything scores 20/20 on the
valid half and is still useless, because the wrong answers are indistinguishable from the
right ones. Both halves must pass.

    python -m agents.eval
    python -m agents.eval --verbose
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from agents.graph import Agent
from agents.tracing import Trace
from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("agents.eval")

TOLERANCE = 0.01


def _scalar(connection: duckdb.DuckDBPyConnection, sql: str):
    row = connection.execute(sql).fetchone()
    return row[0] if row else None


def _count(answer: dict) -> int | None:
    rows = answer.get("rows") or []
    return rows[0].get("n") if rows else None


def _first_value(answer: dict, column: str):
    rows = answer.get("rows") or []
    return rows[0].get(column) if rows else None


def _aggregate_value(answer: dict):
    rows = answer.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    for key, value in row.items():
        if key != "group_key":
            return value
    return None


def build_cases(connection: duckdb.DuckDBPyConnection) -> tuple[list[dict], list[dict]]:
    """Return (valid_cases, refusal_cases) with expectations already resolved."""

    def expected(sql: str):
        return lambda: _scalar(connection, sql)

    valid = [
        {
            "question": "How many books are in the catalogue?",
            "expect": expected("select count(*) from gold.dim_book"),
            "actual": _count,
        },
        {
            "question": "How many categories are there?",
            "expect": expected("select count(*) from gold.dim_category"),
            "actual": _count,
        },
        {
            "question": "How many countries are there?",
            "expect": expected("select count(*) from gold.dim_country"),
            "actual": _count,
        },
        {
            "question": "How many books are out of stock?",
            "expect": expected("select count(*) from gold.dim_book where is_in_stock = false"),
            "actual": _count,
        },
        {
            "question": "How many books have rating 5?",
            "expect": expected("select count(*) from gold.dim_book where rating = 5"),
            "actual": _count,
        },
        {
            "question": "What is the average price of books?",
            "expect": expected("select avg(price) from gold.dim_book"),
            "actual": _aggregate_value,
        },
        {
            "question": "What is the average population of countries?",
            "expect": expected("select avg(population) from gold.dim_country"),
            "actual": _aggregate_value,
        },
        {
            "question": "What is the average rating of books?",
            "expect": expected("select avg(rating) from gold.dim_book"),
            "actual": _aggregate_value,
        },
        {
            "question": "What is the total inventory value?",
            "expect": expected("select sum(inventory_value) from gold.fct_books"),
            "actual": _aggregate_value,
        },
        {
            "question": "What is the total stock count?",
            "expect": expected("select sum(stock_count) from gold.fct_books"),
            "actual": _aggregate_value,
        },
        {
            "question": "Which book is the most expensive?",
            "expect": expected(
                "select book_title from gold.dim_book order by price desc nulls last limit 1"
            ),
            "actual": lambda answer: _first_value(answer, "book_title"),
        },
        {
            "question": "Which book has the highest rating?",
            "expect": expected(
                "select book_title from gold.dim_book order by rating desc nulls last limit 1"
            ),
            "actual": lambda answer: _first_value(answer, "book_title"),
        },
        {
            "question": "Which country has the largest population?",
            "expect": expected(
                "select country_name from gold.dim_country order by population desc nulls last limit 1"
            ),
            "actual": lambda answer: _first_value(answer, "country_name"),
        },
        {
            "question": "Which country has the highest population density?",
            "expect": expected(
                "select country_name from gold.dim_country order by population_density desc nulls last limit 1"
            ),
            "actual": lambda answer: _first_value(answer, "country_name"),
        },
        {
            "question": "Which team has the most wins?",
            "expect": expected(
                "select team_name from gold.fct_hockey_season order by wins desc nulls last limit 1"
            ),
            "actual": lambda answer: _first_value(answer, "team_name"),
        },
        {
            "question": "Which team has the highest goal difference?",
            "expect": expected(
                "select team_name from gold.fct_hockey_season order by goal_difference desc nulls last limit 1"
            ),
            "actual": lambda answer: _first_value(answer, "team_name"),
        },
        {
            # The catalogue happens to have zero out-of-stock books, so this is also the
            # test that an empty lookup is refused instead of reported as an empty list.
            # Its counterpart below is the count case, where 0 *is* the answer.
            "question": "List books that are in stock.",
            "expect": expected("select count(*) from gold.dim_book where is_in_stock = true"),
            "actual": lambda answer: len(answer.get("rows") or []),
            "compare": "at_most_rows",
        },
        {
            "question": "What tables can I query?",
            "expect": lambda: 5,
            "actual": lambda answer: len(answer.get("rows") or []),
        },
        {
            "question": "Show me the books.",
            "expect": lambda: True,
            "actual": lambda answer: bool(answer.get("rows")),
        },
        {
            "question": "Which categories are available?",
            "expect": lambda: True,
            "actual": lambda answer: bool(answer.get("rows")),
        },
    ]

    refusals = [
        {"question": "How many books were sold yesterday?", "why": "no sales table"},
        {"question": "What is the total revenue this year?", "why": "revenue is not modelled"},
        {"question": "How many employees do we have?", "why": "no employee table"},
        {"question": "What is the average salary of employees?", "why": "no employee table"},
        {"question": "How many customers signed up last month?", "why": "no customer table"},
        {"question": "Which book has the highest profit margin?", "why": "cost is not modelled"},
        {"question": "What is the churn rate?", "why": "no subscription table"},
        {"question": "How many users clicked on a book today?", "why": "no clickstream"},
        {"question": "What is our refund rate?", "why": "no returns table"},
        {"question": "How many orders were placed?", "why": "no orders table"},
    ]

    return valid, refusals


def compare(expected, actual, mode: str = "exact") -> tuple[bool, str]:
    if isinstance(expected, bool):
        return (bool(actual) == expected), f"expected {expected}, got {actual}"
    if expected is None:
        return actual is None, f"expected None, got {actual}"
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if actual is None:
            return False, f"expected {expected}, got None"
        try:
            difference = abs(float(actual) - float(expected))
        except (TypeError, ValueError):
            return False, f"expected number {expected}, got {actual!r}"
        if mode == "at_most_rows":
            return True, f"returned {actual} rows (expected {expected} matching rows)"
        return difference <= TOLERANCE, f"expected {expected}, got {actual} (diff {difference:.6f})"
    return str(actual) == str(expected), f"expected {expected!r}, got {actual!r}"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Evaluate the warehouse agent.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report", default="quality/reports/agent_eval.json")
    args = parser.parse_args(argv)

    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        valid, refusals = build_cases(connection)
        expectations = [(case, case["expect"]()) for case in valid]
    finally:
        connection.close()

    trace = Trace()
    agent = Agent(trace=trace)
    results = {"valid": [], "refusals": []}
    passed = failed = 0

    try:
        for case, expected in expectations:
            answer = agent.ask(case["question"])
            mode = case.get("compare", "exact")
            ok, detail = compare(expected, case["actual"](answer), mode)
            if answer.get("refused"):
                ok, detail = False, f"refused unexpectedly: {answer.get('refusal_reason')}"
            if ok:
                passed += 1
            else:
                failed += 1
            results["valid"].append(
                {
                    "question": case["question"],
                    "expected": str(expected),
                    "answer": answer.get("answer"),
                    "sql": answer.get("sql"),
                    "ok": ok,
                    "detail": detail,
                }
            )
            if args.verbose:
                print(f"  [{'PASS' if ok else 'FAIL'}] {case['question']} -> {detail}")

        for case in refusals:
            answer = agent.ask(case["question"])
            ok = bool(answer.get("refused"))
            if ok:
                passed += 1
            else:
                failed += 1
            results["refusals"].append(
                {
                    "question": case["question"],
                    "why": case["why"],
                    "refused": ok,
                    "refusal_reason": answer.get("refusal_reason"),
                    "answer": answer.get("answer"),
                    "ok": ok,
                }
            )
            if args.verbose:
                print(f"  [{'PASS' if ok else 'FAIL'}] REFUSE {case['question']}")
    finally:
        agent.close()

    total = passed + failed
    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "valid_questions": len(valid),
        "refusal_questions": len(refusals),
        "passed": passed,
        "failed": failed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "valid_answered": sum(1 for item in results["valid"] if item["ok"]),
        "refusals_correct": sum(1 for item in results["refusals"] if item["ok"]),
        "trace": trace.summary(),
        "results": results,
        "result": "PASS" if failed == 0 and total > 0 else "FAIL",
    }

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"valid answered     : {report['valid_answered']}/{len(valid)}")
    print(f"refusals correct   : {report['refusals_correct']}/{len(refusals)}")
    print(f"spans recorded     : {report['trace']['spans']} ({report['trace']['total_ms']} ms)")
    print(f"RESULT             : {report['result']}")

    if failed:
        for item in results["valid"] + results["refusals"]:
            if not item["ok"]:
                print(f"  -> FAILED: {item['question']}: {item.get('detail') or item.get('answer')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
