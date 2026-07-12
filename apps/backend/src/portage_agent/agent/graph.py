"""The graph: Ingest → Plan → Execute → Verify → (Recover) → Integrate → Report.

Every node transition writes a checkpoint (thread_id = job_id), so killing the worker mid-run
resumes from the last node boundary — a kill mid-Execute resumes without re-cloning or
re-planning, and the content-hash skip avoids re-migrating already-applied files.

Phase 3 wiring: a failed Verify routes to **Recover**, which classifies the failure and picks
a bounded strategy — regenerate the implicated files (Execute), repair a planner miss (Plan),
or give up honestly (Integrate). Non-migration runs never enter Recover: a red suite on an
unmigrated repo is a *finding*, not a failure to recover from — they go straight to Integrate,
preserving the Phase-1 behaviour.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from .nodes import (
    execute_node,
    ingest_node,
    integrate_node,
    plan_node,
    recover_node,
    report_node,
    verify_node,
)
from .state import GraphState

log = logging.getLogger("portage.agent")


def _after_verify(state: GraphState) -> str:
    if state.get("verify_passed"):
        if state.get("migrate") and state.get("has_pending_tasks"):
            return "execute"
        return "integrate"
    if not state.get("migrate"):
        return "integrate"  # nothing was changed; report the repo's own red suite as-is
    return "recover"


def _after_recover(state: GraphState) -> str:
    return state.get("recover_route") or "integrate"


def _after_integrate(state: GraphState) -> str:
    if (state.get("migrate") and not state.get("integration_passed")
            and int(state.get("integration_recovery_visits", 0)) < 1):
        return "recover"
    return "report"


def build_graph(checkpointer):
    """Compile the Ingest→Plan→Execute→Verify→(Recover)→Integrate→Report graph."""
    builder = StateGraph(GraphState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_node)
    builder.add_node("verify", verify_node)
    builder.add_node("recover", recover_node)
    builder.add_node("integrate", integrate_node)
    builder.add_node("report", report_node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", "verify")
    builder.add_conditional_edges(
        "verify", _after_verify,
        {"execute": "execute", "recover": "recover", "integrate": "integrate"},
    )
    builder.add_conditional_edges(
        "recover", _after_recover,
        {"execute": "execute", "plan": "plan", "integrate": "integrate"},
    )
    builder.add_conditional_edges(
        "integrate", _after_integrate, {"recover": "recover", "report": "report"},
    )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
