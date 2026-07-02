"""Verify + Integrate nodes — run tests in the network-off sandbox.

Verify runs the **blast-radius-affected** tests (the subset Plan selected) against the
migrated worktree and reports pass/fail; on failure the graph loops back to Execute (bounded).
Integrate runs the **full** suite as the authoritative gate — that result is what the DoD
("the full test suite passes") is scored against. For a non-migration run both degrade to the
Phase-1 behaviour: run the existing suite against the workspace.

Rich failure classification / recovery is Phase 3; Verify here is basic pass/fail branching.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from portage_agent.config import settings
from portage_agent.sandbox import DockerSandbox, parse_junit_xml

from ..state import GraphState
from .common import worktree_diff

log = logging.getLogger("portage.agent")

_REPORT_FILE = ".portage-report.xml"


def _workdir(state: GraphState) -> str:
    """Migrated worktree for a migration run; the original workspace otherwise."""
    return state["worktree"] if state.get("migrate") else state["workspace"]


def _summarize(workdir: str, result) -> dict:
    report_path = Path(workdir) / _REPORT_FILE
    if report_path.exists():
        return parse_junit_xml(report_path.read_text()).to_dict()
    return {
        "total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
        "duration_seconds": 0.0, "cases": [],
        "error": f"no report produced (sandbox exit {result.exit_code}): "
                 f"{(result.stderr or result.stdout)[:300]}",
    }


async def _run_tests(workdir: str, targets: list[str]) -> tuple[dict, object]:
    # Only pass targets that actually exist in the workdir — a stale blast-radius path
    # shouldn't make pytest collect nothing and look like a failure.
    existing = [t for t in targets if (Path(workdir) / t).exists()]
    cmd = ["run-tests", *existing]
    result = await DockerSandbox().run(cmd, workdir=workdir)
    return _summarize(workdir, result), result


async def verify_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    workdir = _workdir(state)
    migrate = bool(state.get("migrate"))
    attempts = int(state.get("verify_attempts", 0)) + 1
    gs = state.get("graph_summary") or {}
    # On a RESUMED run this logs the graph_summary already loaded from the checkpoint —
    # proof that Ingest did not re-run.
    log.info("VERIFY node | job=%s migrate=%s attempt=%s loaded_graph(nodes=%s) workdir=%s",
             job_id, migrate, attempts, gs.get("total_nodes"), workdir)

    # Deterministic kill window (crash-recovery demo) — unchanged from Phase 1.
    cfg = state.get("config") or {}
    delay = int(cfg.get("verify_delay_seconds", settings.verify_pre_delay_seconds))
    for i in range(1, delay + 1):
        await asyncio.sleep(1)
        if i % 5 == 0 or i == delay:
            log.info("VERIFY pre-test delay %s/%ss | job=%s", i, delay, job_id)

    targets = state.get("affected_tests", []) if migrate else []
    summary, result = await _run_tests(workdir, targets)
    passed = summary.get("total", 0) > 0 and summary.get("failed", 0) == 0 \
        and summary.get("errors", 0) == 0

    log.info("VERIFY done | job=%s total=%s passed=%s failed=%s errors=%s -> %s",
             job_id, summary.get("total"), summary.get("passed"), summary.get("failed"),
             summary.get("errors"), "PASS" if passed else "FAIL")

    out: GraphState = {
        "test_summary": summary,
        "verify_passed": passed,
        "verify_attempts": attempts,
        "step_log": ["verify"],
    }
    if not passed and migrate:
        out["last_verify_errors"] = (result.stdout or "")[-3000:]
    return out


async def integrate_node(state: GraphState) -> GraphState:
    """Run the full suite (the DoD gate). For a non-migration run, reuse Verify's result."""
    job_id = state["job_id"]
    if not state.get("migrate"):
        log.info("INTEGRATE node | job=%s (no migration) reuse verify result", job_id)
        return {"integrate_summary": state.get("test_summary", {}), "step_log": ["integrate"]}

    workdir = state["worktree"]
    summary, _ = await _run_tests(workdir, [])  # [] => full suite
    diff = state.get("diff") or await worktree_diff(workdir)
    log.info("INTEGRATE node | job=%s full-suite total=%s passed=%s failed=%s errors=%s",
             job_id, summary.get("total"), summary.get("passed"), summary.get("failed"),
             summary.get("errors"))
    return {"integrate_summary": summary, "diff": diff, "step_log": ["integrate"]}
