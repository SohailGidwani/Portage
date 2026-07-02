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

from portage_agent.retrieval import MCPRetrievalProvider

from ..repo_source import materialize_repo
from ..state import GraphState
from .common import workspace_for

log = logging.getLogger("portage.agent")


async def ingest_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    repo_url = state["repo_url"]
    workspace = workspace_for(job_id)
    log.info("INGEST node | job=%s repo=%s -> %s", job_id, repo_url, workspace)

    await materialize_repo(repo_url, workspace)

    provider = MCPRetrievalProvider(workspace)
    summary = await provider.build(full_rebuild=True)

    ws = Path(workspace)
    rel_files = [str(p.relative_to(ws)) for p in ws.rglob("*.py")][:20]
    if rel_files:
        blast = await provider.blast_radius(rel_files)
    else:
        blast = {"status": "skipped", "reason": "no python files"}

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
