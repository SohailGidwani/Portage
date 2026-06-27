"""The Phase 1 graph nodes: Ingest → Verify → Report.

Each node returns a partial state update; LangGraph checkpoints at every node boundary, so
killing the worker mid-Verify resumes from the post-Ingest checkpoint — Ingest (the
expensive clone + graph build) does NOT re-run.

Scope: Verify runs the repo's *existing* tests. No patching / Plan / Execute (Phase 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from portage_agent.config import settings
from portage_agent.retrieval import MCPRetrievalProvider
from portage_agent.sandbox import DockerSandbox, parse_junit_xml
from portage_agent.storage import LocalStorage

from .repo_source import materialize_repo
from .state import GraphState

log = logging.getLogger("portage.agent")

_REPORT_FILE = ".portage-report.xml"


def _workspace_for(job_id: str) -> str:
    return f"{settings.workspaces_mount}/{job_id}"


async def ingest_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    repo_url = state["repo_url"]
    workspace = _workspace_for(job_id)
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


async def verify_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    workspace = state["workspace"]
    # On a RESUMED run this logs the graph_summary already loaded from the checkpoint —
    # proof that Ingest did not re-run.
    gs = state.get("graph_summary") or {}
    log.info(
        "VERIFY node | job=%s loaded_graph(nodes=%s,edges=%s) workspace=%s",
        job_id, gs.get("total_nodes"), gs.get("total_edges"), workspace,
    )

    # Optional pre-test delay → a deterministic window to kill the worker mid-Verify
    # (the crash-recovery demo). Driven by job config or settings; default 0.
    cfg = state.get("config") or {}
    delay = int(cfg.get("verify_delay_seconds", settings.verify_pre_delay_seconds))
    for i in range(1, delay + 1):
        await asyncio.sleep(1)
        if i % 5 == 0 or i == delay:
            log.info("VERIFY pre-test delay %s/%ss | job=%s", i, delay, job_id)

    result = await DockerSandbox().run(["run-tests"], workdir=workspace)
    report_path = Path(workspace) / _REPORT_FILE
    if report_path.exists():
        report = parse_junit_xml(report_path.read_text())
        summary = report.to_dict()
    else:
        summary = {
            "total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "duration_seconds": 0.0, "cases": [],
            "error": f"no report produced (sandbox exit {result.exit_code}): "
                     f"{result.stderr[:300] or result.stdout[:300]}",
        }
    log.info(
        "VERIFY done | job=%s total=%s passed=%s failed=%s errors=%s",
        job_id, summary.get("total"), summary.get("passed"),
        summary.get("failed"), summary.get("errors"),
    )
    return {"test_summary": summary, "step_log": ["verify"]}


async def report_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    report = {
        "job_id": job_id,
        "repo_url": state.get("repo_url"),
        "migration_recipe": state.get("migration_recipe"),
        "graph_summary": state.get("graph_summary"),
        "blast_radius_sample": state.get("blast_radius_sample"),
        "test_summary": state.get("test_summary"),
    }
    storage = LocalStorage()
    path = await storage.put(
        f"{job_id}/report.json",
        json.dumps(report, indent=2).encode(),
        content_type="application/json",
    )
    log.info("REPORT node | job=%s -> %s", job_id, path)
    return {"report_path": path, "step_log": ["report"]}
