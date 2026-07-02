"""The Phase 2 graph: Ingest → Plan → Execute → Verify → Integrate → Report.

Every node transition writes a checkpoint (thread_id = job_id), so killing the worker mid-run
resumes from the last node boundary — a kill mid-Execute resumes without re-cloning or
re-planning, and the content-hash skip avoids re-migrating already-applied files.

The one conditional edge is the bounded Execute↔Verify retry: Verify passes → Integrate;
Verify fails but attempts remain → back to Execute (re-migrate with the failure in context);
otherwise → Integrate (which runs the full suite so the report reflects reality). This is the
*minimal* reconciliation of "the full suite must pass" with LLM nondeterminism — the full
Recover taxonomy (replan, model escalation, rollback) is Phase 3.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from portage_agent.config import settings

from .nodes import (
    execute_node,
    ingest_node,
    integrate_node,
    plan_node,
    report_node,
    verify_node,
)
from .state import GraphState

log = logging.getLogger("portage.agent")


def _after_verify(state: GraphState) -> str:
    if state.get("verify_passed"):
        return "integrate"
    if int(state.get("verify_attempts", 0)) < settings.max_execute_attempts:
        log.info("verify failed (attempt %s/%s) -> retry execute",
                 state.get("verify_attempts"), settings.max_execute_attempts)
        return "execute"
    log.info("verify failed and retries exhausted -> integrate (report the failure)")
    return "integrate"


def build_graph(checkpointer):
    """Compile the Ingest→Plan→Execute→Verify→Integrate→Report graph."""
    builder = StateGraph(GraphState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_node)
    builder.add_node("verify", verify_node)
    builder.add_node("integrate", integrate_node)
    builder.add_node("report", report_node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", "verify")
    builder.add_conditional_edges(
        "verify", _after_verify, {"execute": "execute", "integrate": "integrate"}
    )
    builder.add_edge("integrate", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
