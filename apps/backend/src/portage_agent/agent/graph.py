"""The Phase 1 graph: Ingest → Verify → Report.

Replaces the Phase 0 trivial graph. Every node transition writes a checkpoint (keyed by
thread_id = job_id), so killing the worker mid-run resumes from the last node boundary —
e.g. a kill during Verify resumes without re-running the expensive Ingest.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from .nodes import ingest_node, report_node, verify_node
from .state import GraphState

log = logging.getLogger("portage.agent")


def build_graph(checkpointer):
    """Compile the Ingest→Verify→Report graph with the given Postgres checkpointer."""
    builder = StateGraph(GraphState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("verify", verify_node)
    builder.add_node("report", report_node)
    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "verify")
    builder.add_edge("verify", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
