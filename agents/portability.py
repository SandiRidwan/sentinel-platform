"""The same agent, four frameworks, one answer per case.

The claim being tested is narrow: the agent's three phases (plan, act, verify) run under
LangGraph, LangChain, LlamaIndex and CrewAI, against the same 30 eval cases, and produce
the same answers as the SQL expectations the harness computes for itself.

Three things make that a real test rather than a demonstration:

  1. The phases live in `agents/core.py`, which imports none of the four frameworks. Each
     wrapper below is wiring only. If a wrapper contained logic, this would compare four
     copies of the logic, not four frameworks.
  2. The expectations come from `agents.eval`, computed by hand-written SQL. A framework
     that returns a confident wrong answer fails here the same way the agent does.
  3. The refusal cases are included. An integration that only proves the happy path is
     exactly the integration that quietly answers questions the data cannot support.

CrewAI runs in its own virtualenv. That is not a shortcut, it is the finding: crewai 1.15
and the platform's pinned langchain/langgraph do not resolve together in one environment,
so the worker is a subprocess. `SENTINEL_CREWAI_PYTHON` points at the interpreter; without
it the framework is reported as not run, never as passed.

    python -m agents.portability
    python -m agents.portability --frameworks langgraph,langchain
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from agents.core import AgentCore
from agents.eval import build_cases, compare
from agents.tracing import Trace
from tools.common.config import get_settings

REPORT = Path("quality/reports/framework_portability.json")
WORKER = Path(__file__).resolve().parent / "crewai_worker.py"

FRAMEWORKS = ("langgraph", "langchain", "llamaindex", "crewai")

def cases() -> list[dict]:
    """The 30 cases, with expectations resolved by SQL outside every framework.

    `actual` is the extractor that turns a framework's answer into the value being
    compared; `expect` is already a value, not a callable, by the time it leaves here.
    """
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        valid, refusals = build_cases(connection)
        resolved = [dict(case, expected=case["expect"](), kind="answer") for case in valid]
    finally:
        connection.close()

    resolved += [dict(case, expected=None, kind="refusal") for case in refusals]
    return resolved

def score(case: dict, answer: dict) -> tuple[bool, str]:
    """One verdict per case, using the same comparator as `agents.eval`."""
    if case["kind"] == "refusal":
        ok = bool(answer.get("refused"))
        return ok, "refused" if ok else f"answered instead: {answer.get('answer')!r}"

    if answer.get("refused"):
        return False, f"refused unexpectedly: {answer.get('refusal_reason')}"
    return compare(case["expected"], case["actual"](answer), case.get("compare", "exact"))

# --------------------------------------------------------------------------------------
# LangGraph: the default implementation, imported as-is.
# --------------------------------------------------------------------------------------

def run_langgraph(questions: list[str]) -> list[dict]:
    from agents.graph import Agent

    agent = Agent(trace=Trace(path=Path("quality/reports/portability_langgraph.jsonl")))
    try:
        return [agent.ask(question) for question in questions]
    finally:
        agent.close()

# --------------------------------------------------------------------------------------
# LangChain: an LCEL chain. RunnableLambda for the phases, RunnableBranch for the refusal
# route — the same control flow as the graph, expressed in LangChain's own primitives.
# --------------------------------------------------------------------------------------

def run_langchain(questions: list[str]) -> list[dict]:
    from langchain_core.runnables import RunnableBranch, RunnableLambda

    trace = Trace(path=Path("quality/reports/portability_langchain.jsonl"))
    core = AgentCore(trace=trace)

    def planned(state: dict) -> dict:
        return {**state, "plan": core.plan(state["question"])}

    def refused(state: dict) -> dict:
        return {**state, **core.refuse(state["plan"])}

    def acted(state: dict) -> dict:
        outcome = core.act(state["plan"])
        state = {**state, **outcome}
        if not outcome.get("refused"):
            state = {**state, **core.verify(state["plan"], outcome.get("rows") or [])}
        return state

    chain = RunnableLambda(lambda question: {"question": question}) | RunnableLambda(planned) | RunnableBranch(
        (lambda state: state["plan"]["tool"] == "refuse", RunnableLambda(refused)),
        RunnableLambda(acted),
    )

    try:
        raw = [chain.invoke(question) for question in questions]
    finally:
        core.close()
    return [_shape(item) for item in raw]

# --------------------------------------------------------------------------------------
# LlamaIndex: a Workflow with one step per phase, and a FunctionTool for the tool call.
# --------------------------------------------------------------------------------------

def run_llamaindex(questions: list[str]) -> list[dict]:
    from llama_index.core.tools import FunctionTool
    from llama_index.core.workflow import Event, StartEvent, StopEvent, Workflow, step

    trace = Trace(path=Path("quality/reports/portability_llamaindex.jsonl"))
    core = AgentCore(trace=trace)

    def act_tool(plan_json: str) -> str:
        """Run the planned warehouse query and return its rows as JSON."""
        return json.dumps(core.act(json.loads(plan_json)))

    tool = FunctionTool.from_defaults(
        fn=act_tool,
        name="run_plan",
        description="Execute a validated plan against the governed warehouse.",
    )

    class Planned(Event):
        question: str
        plan: dict

    class Refused(Event):
        question: str
        plan: dict

    class Acted(Event):
        question: str
        plan: dict
        outcome: dict

    class AgentWorkflow(Workflow):
        @step
        async def plan(self, ev: StartEvent) -> Planned | Refused:
            plan = core.plan(ev.question)
            # The refusal route is an explicit branch, not an exception: LangGraph does the
            # same with a conditional edge, and a wrapper that sent "refuse" into the tool
            # would answer with a tool error instead of the reason.
            if plan["tool"] == "refuse":
                return Refused(question=ev.question, plan=plan)
            return Planned(question=ev.question, plan=plan)

        @step
        async def refuse(self, ev: Refused) -> StopEvent:
            state = {"question": ev.question, "plan": ev.plan, **core.refuse(ev.plan)}
            return StopEvent(result=_shape(state))

        @step
        async def act(self, ev: Planned) -> Acted:
            outcome = json.loads(tool.call(plan_json=json.dumps(ev.plan)).raw_output)
            return Acted(question=ev.question, plan=ev.plan, outcome=outcome)

        @step
        async def verify(self, ev: Acted) -> StopEvent:
            state = {"question": ev.question, "plan": ev.plan, **ev.outcome}
            if not ev.outcome.get("refused"):
                state = {**state, **core.verify(ev.plan, ev.outcome.get("rows") or [])}
            return StopEvent(result=_shape(state))

    async def run_all() -> list[dict]:
        workflow = AgentWorkflow(timeout=120)
        return [await workflow.run(question=question) for question in questions]

    try:
        return asyncio.run(run_all())
    finally:
        core.close()

# --------------------------------------------------------------------------------------
# CrewAI: a Flow in its own virtualenv, driven as a subprocess.
# --------------------------------------------------------------------------------------

def crewai_python() -> str | None:
    explicit = os.environ.get("SENTINEL_CREWAI_PYTHON")
    if explicit:
        return explicit if Path(explicit).exists() else None

    root = get_settings().root
    candidates = [
        root / ".venv-crewai" / "Scripts" / "python.exe",
        root / ".venv-crewai" / "bin" / "python",
    ]
    return next((str(path) for path in candidates if path.exists()), None)

def run_crewai(questions: list[str]) -> list[dict]:
    interpreter = crewai_python()
    if interpreter is None:
        raise RuntimeError(
            "crewai is not available: no SENTINEL_CREWAI_PYTHON and no .venv-crewai "
            "(python -m venv .venv-crewai && .venv-crewai/Scripts/pip install crewai)"
        )

    payload = json.dumps({"questions": questions})
    result = subprocess.run(
        [interpreter, str(WORKER)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(get_settings().root),
        timeout=900,
        env={**os.environ, "CREWAI_TRACING_ENABLED": "false", "CREWAI_DISABLE_TELEMETRY": "true"},
    )
    if result.returncode != 0:
        tail = "\n".join(((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-6:])
        raise RuntimeError(f"crewai worker failed ({result.returncode}):\n{tail}")

    # The worker prints one JSON object; anything before it is crewai's banner.
    for line in reversed((result.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            # Normalised through `_shape` like the in-process runners: the worker's state
            # omits `refused` entirely when it is false, and a missing key is not the same
            # answer as `false` when two frameworks are being compared.
            return [_shape(answer) for answer in json.loads(line)["answers"]]
    raise RuntimeError("crewai worker produced no JSON result")

# --------------------------------------------------------------------------------------

def _shape(state: dict) -> dict:
    """Normalise a framework's end state to the agent's answer dict."""
    return {
        "question": state.get("question", ""),
        "answer": state.get("answer", ""),
        "refused": bool(state.get("refused")),
        "refusal_reason": state.get("refusal_reason", ""),
        "tool": state.get("tool", ""),
        "sql": state.get("sql", ""),
        "rows": state.get("rows", []),
        "plan": state.get("plan", {}),
    }

RUNNERS = {
    "langgraph": run_langgraph,
    "langchain": run_langchain,
    "llamaindex": run_llamaindex,
    "crewai": run_crewai,
}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the same agent through four frameworks.")
    parser.add_argument("--frameworks", default=",".join(FRAMEWORKS))
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    selected = [name.strip() for name in args.frameworks.split(",") if name.strip()]
    unknown = [name for name in selected if name not in RUNNERS]
    if unknown:
        raise SystemExit(f"unknown frameworks: {unknown}. Known: {list(RUNNERS)}")

    harness = cases()
    questions = [case["question"] for case in harness]
    print(f"cases: {len(harness)} ({sum(1 for c in harness if c['kind'] == 'answer')} answer, "
          f"{sum(1 for c in harness if c['kind'] == 'refusal')} refusal)")

    started = datetime.now(timezone.utc)
    per_framework: dict[str, dict] = {}
    answers: dict[str, list[dict]] = {}
    not_run: dict[str, str] = {}

    for name in selected:
        try:
            produced = RUNNERS[name](questions)
        except Exception as exc:  # noqa: BLE001 - the message is the result
            not_run[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            print(f"  {name:12s} NOT RUN  {not_run[name]}")
            continue

        if len(produced) != len(harness):
            not_run[name] = f"returned {len(produced)} answers for {len(harness)} cases"
            print(f"  {name:12s} NOT RUN  {not_run[name]}")
            continue

        answers[name] = produced
        failures = []
        for case, answer in zip(harness, produced, strict=True):
            ok, detail = score(case, answer)
            if not ok:
                failures.append({"question": case["question"], "detail": detail, "answer": answer.get("answer")})
        passed = len(harness) - len(failures)
        per_framework[name] = {"passed": passed, "total": len(harness), "failures": failures}
        print(f"  {name:12s} {passed}/{len(harness)}")
        if args.verbose:
            for failure in failures:
                print(f"      FAIL {failure['question']}: {failure['detail']}")

    # Cross-framework agreement: same question, same answer text and same refusal flag.
    disagreements = []
    if len(answers) >= 2:
        reference = next(iter(answers))
        for name, produced in answers.items():
            if name == reference:
                continue
            for index, (left, right) in enumerate(zip(answers[reference], produced, strict=True)):
                if (left.get("answer"), left.get("refused")) != (right.get("answer"), right.get("refused")):
                    disagreements.append(
                        {
                            "question": harness[index]["question"],
                            "frameworks": [reference, name],
                            "answers": [left.get("answer"), right.get("answer")],
                        }
                    )

    ran_all = len(per_framework) == len(selected) and not not_run
    everyone_passed = all(item["passed"] == item["total"] for item in per_framework.values())
    identical = bool(answers) and not disagreements

    checks = {
        "every_selected_framework_ran": ran_all,
        "every_case_passed_in_every_framework": everyone_passed,
        "answers_identical_across_frameworks": identical,
    }

    report = {
        "ran_at": started.isoformat(),
        "cases": {
            "total": len(harness),
            "answer": sum(1 for case in harness if case["kind"] == "answer"),
            "refusal": sum(1 for case in harness if case["kind"] == "refusal"),
        },
        "frameworks_selected": selected,
        "frameworks_run": sorted(per_framework),
        "not_run": not_run,
        "per_framework": per_framework,
        "disagreements": disagreements,
        "checks": checks,
        "result": "PASS" if all(checks.values()) else "FAIL",
        "notes": [
            "Expectations are computed by hand-written SQL in agents.eval, not by the agent.",
            "The phases live in agents.core, which imports none of the four frameworks.",
            "CrewAI runs in .venv-crewai because its pins conflict with the platform's "
            "langchain/langgraph; the subprocess boundary is recorded, not hidden.",
        ],
    }

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    for name, ok in checks.items():
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"RESULT: {report['result']}")
    return 0 if report["result"] == "PASS" else 1

if __name__ == "__main__":
    raise SystemExit(main())
