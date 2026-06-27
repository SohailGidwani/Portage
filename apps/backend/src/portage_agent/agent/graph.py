"""The trivial Phase 0 graph: start -> work -> end.

A stand-in for the real migration state machine (plan §4). Its only job in Phase 0 is
to prove durable, resumable execution: LangGraph writes a checkpoint at every node
boundary, so killing the worker during `work` and restarting resumes from the post-`start`
checkpoint — `start` does NOT run again.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph

from portage_agent.config import settings

from .state import GraphState

log = logging.getLogger("portage.agent")


async def start_node(state: GraphState) -> GraphState:
    # Stamp a unique marker + timestamp into durable state. On a resumed run this
    # node does not execute, yet the marker survives in the loaded checkpoint.
    run_marker = uuid.uuid4().hex[:8]
    started_at = datetime.now(UTC).isoformat()
    log.info(
        "START node | job=%s run_marker=%s (stamped into checkpoint)",
        state.get("job_id"),
        run_marker,
    )
    return {"run_marker": run_marker, "started_at": started_at, "step_log": ["start"]}


async def work_node(state: GraphState) -> GraphState:
    n = settings.work_sleep_seconds
    # The loaded state here is the discriminator: on a fresh run step_log == ["start"]
    # because `start` just ran; on a RESUMED run it is *also* ["start"] but produced by
    # a prior process — same run_marker, and the START line is absent from this run's logs.
    log.info(
        "WORK node BEGIN | job=%s run_marker=%s loaded_step_log=%s sleeping=%ss",
        state.get("job_id"),
        state.get("run_marker"),
        state.get("step_log"),
        n,
    )
    for i in range(1, n + 1):
        await asyncio.sleep(1)
        if i % 5 == 0 or i == n:
            log.info("WORK node ... %s/%ss | job=%s", i, n, state.get("job_id"))
    log.info("WORK node END | job=%s", state.get("job_id"))
    return {"work_done": True, "step_log": ["work"]}


async def end_node(state: GraphState) -> GraphState:
    finished_at = datetime.now(UTC).isoformat()
    log.info(
        "END node | job=%s run_marker=%s final_step_log=%s",
        state.get("job_id"),
        state.get("run_marker"),
        (state.get("step_log") or []) + ["end"],
    )
    return {"finished_at": finished_at, "step_log": ["end"]}


def build_graph(checkpointer):
    """Compile the graph with the given (Async)PostgresSaver checkpointer."""
    builder = StateGraph(GraphState)
    builder.add_node("start", start_node)
    builder.add_node("work", work_node)
    builder.add_node("end", end_node)
    builder.add_edge(START, "start")
    builder.add_edge("start", "work")
    builder.add_edge("work", "end")
    builder.add_edge("end", END)
    return builder.compile(checkpointer=checkpointer)
