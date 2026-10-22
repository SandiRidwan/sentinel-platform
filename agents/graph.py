"""The LangGraph wrapper: plan -> act -> verify as an explicit state machine.

The value of the graph is not the library, it is that the control flow is inspectable. A
question either produces a plan, or a refusal, or a tool call, and every transition is a
named edge rather than an if-tree inside a prompt handler. That makes `verify` impossible
to skip — which is the step that stops an empty result from being reported as zero.

The phases themselves live in `agents/core.py` and know nothing about LangGraph, because
`agents/portability.py` runs the same three phases through three other frameworks. This
file is the wiring: nodes, edges, and the router. If logic ever appears here instead of in
the core, the portability check stops testing frameworks and starts testing copies.

The planner has two implementations, both in the core:

  deterministic  keyword and scope rules. Default. Runs offline, identical every time,
                 which is what makes the eval meaningful.
  llm            asks a chat model for a JSON plan, then validates it against the same
                 registry. Opt-in via SENTINEL_AGENT_PLANNER=llm and an [OI]-compatible
                 endpoint. The model chooses a table and a metric; it never writes SQL.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents.core import (  # noqa: F401 - re-exported for callers that imported them here
    MAX_ROWS,
    AgentCore,
    Plan,
    deterministic_plan,
    llm_plan,
    scope_refusal,
    summarise,
)
from agents.tools import Warehouse
from agents.tracing import Trace

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

def build_graph(warehouse: Warehouse, trace: Trace, planner=None):
    core = AgentCore(warehouse=warehouse, trace=trace, planner=planner)

    def plan_node(state: AgentState) -> AgentState:
        return {"plan": core.plan(state["question"])}

    def route(state: AgentState) -> str:
        return "refuse" if state["plan"]["tool"] == "refuse" else "act"

    def refuse_node(state: AgentState) -> AgentState:
        return core.refuse(state["plan"])

    def act_node(state: AgentState) -> AgentState:
        return core.act(state["plan"])

    def verify_node(state: AgentState) -> AgentState:
        return core.verify(state.get("plan") or {}, state.get("rows") or [])

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

class Agent:
    """Convenience wrapper: one question in, one answer out, with a trace."""

    def __init__(self, warehouse: Warehouse | None = None, planner=None, trace: Trace | None = None) -> None:
        self.warehouse = warehouse or Warehouse()
        self.trace = trace or Trace()
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
