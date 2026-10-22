"""Tests for the phase core: scope rules, planning, and the determinism they depend on.

The interesting test here is the cross-process one. A planner that picks a different refusal
reason depending on the interpreter's hash seed is not reproducible, and the symptom only
appears when two processes are compared — which is exactly what the four-framework check does
and what a single-process unit test cannot see.
"""

from __future__ import annotations

import os
import subprocess
import sys

from agents.core import deterministic_plan, scope_refusal

AMBIGUOUS = "Which book has the highest profit margin?"

def test_refusal_reason_is_stable_across_hash_seeds() -> None:
    """Two interpreters with different hash seeds must produce the same reason.

    `profit` and `margin` are both out-of-scope terms of the same length, so the winner is
    decided by the tie-break. Before the fix, the tie was broken by set iteration order, which
    Python randomises per process: this exact question alternated between
    "profit: cost and profit are not modelled" and "margin: ..." between runs.
    """
    code = (
        "from agents.core import scope_refusal;"
        f"print(scope_refusal({AMBIGUOUS!r}))"
    )
    outputs = []
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())

    assert len(set(outputs)) == 1, f"refusal reason depends on the hash seed: {outputs}"
    assert outputs[0].startswith("margin:"), outputs[0]

def test_longest_term_wins_over_a_shorter_one() -> None:
    """Length is the primary rule; the alphabetical tie-break must not override it."""
    # "last month" (10 chars) must win even though "month" alone is not a term at all, and
    # "population density" is a column hint rather than a scope term, so this is about the
    # multi-word entry beating a single-word one that also appears in the question.
    assert scope_refusal("How many customers signed up last month?").startswith("last month:")
    assert scope_refusal("What is the refund rate?").startswith("refund:")

    # A question refused by the table rule rather than the scope rule: "orders" does not match
    # the `\border\b` scope term, and there is no table hint for it either.
    assert scope_refusal("How many orders were placed?") is None
    assert deterministic_plan("How many orders were placed?").tool == "refuse"

def test_plan_is_a_pure_function_of_the_question() -> None:
    first = deterministic_plan("How many books are out of stock?")
    second = deterministic_plan("How many books are out of stock?")
    assert first.as_dict() == second.as_dict()
    assert first.tool == "count"
    assert first.args["filters"] == [{"column": "is_in_stock", "op": "eq", "value": False}]

def test_unknown_table_is_refused_rather_than_defaulted() -> None:
    plan = deterministic_plan("How many widgets are there?")
    assert plan.tool == "refuse"
    assert "no registered table" in plan.reason
