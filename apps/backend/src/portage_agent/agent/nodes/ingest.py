"""Ingest node — clone the repo + build the structural graph (unchanged from Phase 1).

Kept byte-stable on its observable contract: the "INGEST node" / "INGEST done" log lines and
the `graph_summary` / `blast_radius_sample` outputs are what the crash-recovery and Phase-1
DoD checks assert on. The graph is built AFTER the initial git snapshot, so the worktree
created later (from HEAD) is clean of the `.code-review-graph/` artifacts.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from portage_agent.core.interfaces import GraphSummary
from portage_agent.retrieval import MCPRetrievalProvider

from ..repo_source import materialize_repo
from ..state import GraphState
from .common import workspace_for

log = logging.getLogger("portage.agent")


async def ingest_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    repo_url = state["repo_url"]
    cfg = state.get("config") or {}
    ref = str(cfg.get("repo_ref") or "")
    subdir = str(cfg.get("repo_subdir") or "")
    workspace = workspace_for(job_id)
    log.info("INGEST node | job=%s repo=%s%s%s -> %s", job_id, repo_url,
             f" @{ref[:12]}" if ref else "", f" subdir={subdir}" if subdir else "",
             workspace)

    await materialize_repo(repo_url, workspace, ref=ref, subdir=subdir)

    # The structural graph is an *enhancement* (better planning context, targeted test
    # selection) — not a prerequisite. If CRG hangs (timeout) or crashes on a repo, the
    # migration proceeds without it: Plan classifies from source, Verify falls back to the
    # sanctioned/full suite. Degrading beats livelocking the queue.
    provider = MCPRetrievalProvider(workspace)
    try:
        summary = await provider.build(full_rebuild=True)
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 - degrade on any CRG failure
        log.warning("INGEST graph build failed (%s: %s) — continuing without a graph",
                    type(exc).__name__, str(exc)[:200])
        summary = GraphSummary(errors=[f"graph build failed: {type(exc).__name__}"])

    ws = Path(workspace)
    if summary.ok:
        rel_files = [str(p.relative_to(ws)) for p in ws.rglob("*.py")][:20]
        try:
            blast = await provider.blast_radius(rel_files) if rel_files else {
                "status": "skipped", "reason": "no python files"}
        except (TimeoutError, Exception) as exc:  # noqa: BLE001
            blast = {"status": "error", "reason": f"{type(exc).__name__}"}
    else:
        blast = {"status": "skipped", "reason": "no graph"}

    log.info(
        "INGEST done | job=%s nodes=%s edges=%s files=%s blast=%s",
        job_id, summary.total_nodes, summary.total_edges, summary.files_parsed,
        blast.get("status"),
    )
    return {
        "workspace": workspace,
        "graph_summary": asdict(summary),
        "blast_radius_sample": blast,
        "step_log": ["ingest"],
    }
