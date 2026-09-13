"""The LangGraph workflow.

    START ─(no approval yet)─> sentinel ─┬─> log_hunter ─> code_detective ─┐
                                         └─> metrics ─────────────────────┤
                                                                  timeline ┘
                                                                     │
                                            correlation ─> commander ─> notify ─> END
    START ─(approval recorded)─────────────────────────────────> operator ─> END

Parallel where the agents are genuinely independent, sequential only where a real
data dependency exists: Code Detective cannot match files against a stack trace
the Log Detective has not produced yet.

The approval gate is a process boundary, not an in-memory pause. Phase one ends
at `notify`; the human decision arrives later over a webhook, state is reloaded
from SQLite, and phase two re-enters the same graph at `operator`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from Agents import (
    CodeDetective,
    Commander,
    CorrelationAgent,
    LogHunter,
    MetricsAgent,
    Operator,
    Sentinel,
    SlackNotifier,
    Timeline,
)
from orchestrator.state import SwarmState


def _node(agent: Any) -> Callable[[SwarmState], Any]:
    async def run(state: SwarmState) -> dict[str, Any]:
        return await agent(state)

    run.__name__ = agent.codename
    return run


def route_entry(state: SwarmState) -> str:
    approval = state.get("approval") or {}
    return "operator" if approval.get("decision") == "approve" else "sentinel"


@lru_cache(maxsize=1)
def build_graph():
    graph = StateGraph(SwarmState)

    graph.add_node("sentinel", _node(Sentinel()))
    graph.add_node("log_hunter", _node(LogHunter()))
    graph.add_node("code_detective", _node(CodeDetective()))
    graph.add_node("metrics", _node(MetricsAgent()))
    # The investigation branches are different lengths (log_hunter -> code_detective
    # is two hops, metrics is one), so Timeline is deferred: it runs only once every
    # other pending branch has finished, instead of firing per arriving edge.
    graph.add_node("timeline", _node(Timeline()), defer=True)
    graph.add_node("correlation", _node(CorrelationAgent()))
    graph.add_node("commander", _node(Commander()))
    graph.add_node("notify", _node(SlackNotifier()))
    graph.add_node("operator", _node(Operator()))

    graph.set_conditional_entry_point(
        route_entry, {"sentinel": "sentinel", "operator": "operator"}
    )

    graph.add_edge("sentinel", "log_hunter")
    graph.add_edge("sentinel", "metrics")
    graph.add_edge("log_hunter", "code_detective")
    graph.add_edge("code_detective", "timeline")
    graph.add_edge("metrics", "timeline")
    graph.add_edge("timeline", "correlation")
    graph.add_edge("correlation", "commander")
    graph.add_edge("commander", "notify")
    graph.add_edge("notify", END)
    graph.add_edge("operator", END)

    return graph.compile()
