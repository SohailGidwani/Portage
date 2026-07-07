"""Plan node — turn the repo into a hierarchical task DAG (Job → Tasks → Subtasks).

Recipe-dispatched: an unknown/non-matching recipe yields an empty plan and `migrate=False`,
so the graph degrades to Phase-1 behaviour (and the pydantic fixture / dod_check still pass).
For a matching recipe it (1) classifies files into file-Tasks with transformation Subtasks,
(2) uses blast-radius to pick the affected tests for each file (the same query that selects
what Verify runs — blast-radius doing its planning + verification double duty), (3) persists
the plan to Postgres, and (4) opens the migration worktree.

Phase 3 — **replan**: when Recover routes back here with `replan_requested`, the recipe's
plan is recomputed against the *original* workspace sources and any file missing from the
persisted plan is appended (idempotently). This is how a planner miss (or the injected
`drop_task` fault) is repaired mid-run.
"""

from __future__ import annotations

import logging
import uuid

from portage_agent.db import task_store
from portage_agent.recipes import get_recipe
from portage_agent.recipes.base import PlannedFile
from portage_agent.retrieval import MCPRetrievalProvider

from ..state import GraphState
from .common import ensure_worktree, iter_py_files, workspace_for, worktree_for

log = logging.getLogger("portage.agent")


def _collect_test_files(obj: object, found: set[str]) -> None:
    """Walk a blast-radius result and collect real pytest *module* paths.

    Only `test_*.py` / `*_test.py` — NOT conftest.py (a fixture file, not a test module; as a
    pytest target it collects nothing). Paths may be absolute (under the workspace); callers
    normalize them to repo-relative via ``_rel``.
    """
    if isinstance(obj, str):
        s = obj.replace("\\", "/")
        base = s.rsplit("/", 1)[-1]
        if (base.startswith("test_") and base.endswith(".py")) or base.endswith("_test.py"):
            found.add(s)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_test_files(v, found)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_test_files(v, found)


def _rel(path: str, workspace: str) -> str:
    """Normalize a (possibly absolute) blast-radius path to repo-relative for the worktree."""
    p = path.replace("\\", "/")
    ws = workspace.rstrip("/")
    return p[len(ws) + 1:] if p.startswith(ws + "/") else p


async def _build_specs(
    planned: list[PlannedFile], workspace: str, *, use_graph: bool = True
) -> tuple[list[dict], set[str]]:
    """Turn the recipe's planned files into persistable task specs (+ affected tests).

    ``use_graph=False`` (Ingest couldn't build a graph) skips blast-radius entirely —
    Verify then runs the sanctioned/full suite, which is the conservative fallback."""
    provider = MCPRetrievalProvider(workspace) if use_graph else None
    affected: set[str] = set()
    specs: list[dict] = []
    for pf in planned:
        vspec = pf.verify_spec()
        try:
            if provider is None:
                raise RuntimeError("no graph available")
            blast = await provider.blast_radius([pf.path])
            raw: set[str] = set()
            _collect_test_files(blast, raw)
            tests = {_rel(t, workspace) for t in raw}
            vspec["affected_tests"] = sorted(tests)
            affected |= tests
        except Exception as exc:  # pragma: no cover - blast-radius is best-effort here
            if provider is not None:
                log.warning("blast-radius failed for %s: %s — falling back to full suite",
                            pf.path, exc)
            vspec["affected_tests"] = []
        specs.append({
            "type": pf.role,
            "title": f"Migrate {pf.path} ({pf.role})",
            "target_path": pf.path,
            "order_index": pf.order,
            "verify_spec": vspec,
            "subtasks": [{"type": s.type, "title": s.title} for s in pf.subtasks],
        })
    return specs, affected


async def plan_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    workspace = state.get("workspace") or workspace_for(job_id)
    recipe_name = state.get("migration_recipe", "")
    recipe = get_recipe(recipe_name)
    replan = bool(state.get("replan_requested"))

    files = iter_py_files(workspace)
    if recipe is None or not recipe.matches(files):
        log.info("PLAN node | job=%s recipe=%s -> no migration (degrade to verify-only)",
                 job_id, recipe_name)
        await task_store.save_plan(uuid.UUID(job_id), [])
        return {"migrate": False, "plan": [], "affected_tests": [], "step_log": ["plan"]}

    planned = recipe.plan_files(files)

    # Injected fault (Phase 3 DoD): the planner "misses" the first file. Only on the
    # initial plan — the whole point is that Recover detects the residue and the replan
    # path repairs it.
    fault = (state.get("config") or {}).get("inject_fault")
    if fault == "drop_task" and not replan and planned:
        dropped = planned.pop(0)
        log.warning("PLAN fault=drop_task | job=%s deliberately omitting %s",
                    job_id, dropped.path)

    log.info("PLAN node | job=%s recipe=%s replan=%s files=%s",
             job_id, recipe_name, replan, [pf.path for pf in planned])

    graph_ok = (state.get("graph_summary") or {}).get("total_nodes", 0) > 0
    specs, affected = await _build_specs(planned, workspace, use_graph=graph_ok)
    if replan:
        snapshots = await task_store.append_tasks(uuid.UUID(job_id), specs)
    else:
        snapshots = await task_store.save_plan(uuid.UUID(job_id), specs)

    worktree = worktree_for(job_id)
    await ensure_worktree(workspace, worktree)

    log.info("PLAN done | job=%s tasks=%s affected_tests=%s worktree=%s",
             job_id, len(snapshots), sorted(affected) or "<all>", worktree)
    return {
        "migrate": True,
        "plan": [s.to_state_dict() for s in snapshots],
        "worktree": worktree,
        "affected_tests": sorted(affected),
        "verify_attempts": 0 if not replan else state.get("verify_attempts", 0),
        "replan_requested": False,
        "step_log": ["plan"],
    }
