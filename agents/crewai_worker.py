"""CrewAI worker: the same three phases, expressed as a CrewAI Flow.

Run by `agents/portability.py` in the `.venv-crewai` interpreter, because crewai and the
platform's pinned langchain/langgraph do not resolve together. The worker reads
`{"questions": [...]}` on stdin and prints one JSON object with `{"answers": [...]}`.

Why a Flow and not a Crew: a Crew needs a language model to decide which tool to call, and
the point of this harness is that the ANSWER is determined by the data, not by a sampling
temperature. A Flow is CrewAI's deterministic orchestration primitive — steps, listeners and
typed state — and it is what a production job that must be reproducible would use. The
warehouse call is still a real `BaseTool`, so CrewAI's own tool contract is exercised.

Nothing here decides anything. `plan`, `act` and `verify` are the shared phases in
`agents/core.py`; this file only says in what order they run and how state moves between
them. If logic appears here, the portability claim stops being about frameworks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Run as a script, so sys.path[0] is agents/ rather than the repository root. The shared
# phases are imported as `agents.core`, which needs the root on the path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402 - see the sys.path note above

from crewai.flow.flow import Flow, listen, start  # noqa: E402
from crewai.tools import BaseTool  # noqa: E402

from agents.core import AgentCore  # noqa: E402
from agents.tracing import Trace  # noqa: E402

class PlanInput(BaseModel):
    plan_json: str = Field(description="The validated plan, as a JSON object string.")

class PlanTool(BaseTool):
    """CrewAI tool over the governed warehouse.

    The tool takes the plan, not a free-form question: the plan has already been checked
    against the registry, so the tool cannot be talked into a table that is not declared.
    """

    name: str = "run_plan"
    description: str = "Execute a validated plan against the governed warehouse and return its rows."
    args_schema: type[BaseModel] = PlanInput

    core: Any = None

    def _run(self, plan_json: str) -> str:
        return json.dumps(self.core.act(json.loads(plan_json)))

class AgentState(BaseModel):
    questions: list[str] = Field(default_factory=list)
    plans: list[dict] = Field(default_factory=list)
    outcomes: list[dict] = Field(default_factory=list)
    answers: list[dict] = Field(default_factory=list)

class PortabilityFlow(Flow[AgentState]):
    def __init__(self, core: AgentCore, tool: PlanTool) -> None:
        super().__init__()
        self._core = core
        self._tool = tool

    @start()
    def plan(self) -> str:
        self.state.plans = [self._core.plan(question) for question in self.state.questions]
        return "planned"

    @listen(plan)
    def act(self, _: str) -> str:
        outcomes = []
        for plan in self.state.plans:
            if plan["tool"] == "refuse":
                outcomes.append({"refused": True, "refusal_reason": plan.get("reason", "")})
                continue
            outcomes.append(json.loads(self._tool.run(plan_json=json.dumps(plan))))
        self.state.outcomes = outcomes
        return "acted"

    @listen(act)
    def verify(self, _: str) -> str:
        answers = []
        for question, plan, outcome in zip(
            self.state.questions, self.state.plans, self.state.outcomes, strict=True
        ):
            if plan["tool"] == "refuse":
                state = {**self._core.refuse(plan)}
            elif outcome.get("refused"):
                state = {**outcome}
            else:
                state = {**outcome, **self._core.verify(plan, outcome.get("rows") or [])}
            answers.append({"question": question, "plan": plan, **state})
        self.state.answers = answers
        return "verified"

def main() -> int:
    # Decoded explicitly as utf-8-sig: piping JSON into a process on Windows prepends a
    # BOM, and `json.loads` rejects it. Reading the bytes and stripping the BOM makes the
    # worker work the same whether the payload came from a subprocess or a shell pipe.
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    questions = payload.get("questions") or []

    trace = Trace(path=Path("quality/reports/portability_crewai.jsonl"))
    core = AgentCore(trace=trace)
    tool = PlanTool(core=core)
    flow = PortabilityFlow(core, tool)
    try:
        flow.kickoff(inputs={"questions": questions})
        answers = flow.state.answers
    finally:
        core.close()

    print(json.dumps({"answers": answers}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
