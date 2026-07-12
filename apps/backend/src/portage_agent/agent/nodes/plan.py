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
from .common import (
    build_manifest,
    build_migration_units,
    dependency_order,
    ensure_worktree,
    imported_bindings,
    iter_py_files,
    workspace_for,
    worktree_for,
)
from .oracle import build_oracle_manifest

log = logging.getLogger("portage.agent")


def complete_unit_dependencies(
    root: str, planned: list[PlannedFile], units: list[dict], test_strategy: dict,
    *, max_files: int = 4,
) -> list[dict]:
    """Fill spare unit capacity with direct planned dependencies of its members.

    A coordinated app factory cannot be sandbox-verified while a router it registers is
    still from the source framework. This is import-driven and bounded; large components
    continue through the verifiable-batch scheduler instead of becoming unbounded prompts.
    """
    order = {file.path: index for index, file in enumerate(planned)}
    eligible = [
        file.path for file in planned
        if file.role != "test_compat"
        and test_strategy.get(file.path) not in {"adapter", "unchanged"}
    ]
    for unit in units:
        while len(unit["paths"]) < max_files:
            dependency = next((
                candidate for candidate in eligible
                if candidate not in unit["paths"]
                and any(
                    binding.importer in unit["paths"]
                    for binding in imported_bindings(root, candidate)
                )
            ), None)
            if dependency is None:
                break
            unit["paths"].append(dependency)
        unit["paths"].sort(key=lambda path: (order[path], path))
    return units


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
    oracle_manifest = build_oracle_manifest(workspace)
    test_strategy = {path: entry["strategy"] for path, entry in oracle_manifest.items()}

    planned = dependency_order(files, planned)
    adapter_needed = any(
        strategy in {"adapter", "adapter_wiring"}
        for strategy in test_strategy.values()
    ) and bool(getattr(recipe, "render_test_compat", None))
    compat_path = getattr(recipe, "test_compat_path", "")
    if adapter_needed and compat_path:
        # This module must exist before any coordinated app/conftest batch imports it.
        planned.insert(0, PlannedFile(
            path=compat_path, role="test_compat", subtasks=[], order=25,
        ))
    for i, pf in enumerate(planned):
        pf.order = i * 10

    # Injected fault (Phase 3 DoD): the planner "misses" the first file. Only on the
    # initial plan — the whole point is that Recover detects the residue and the replan
    # path repairs it.
    fault = (state.get("config") or {}).get("inject_fault")
    if fault == "drop_task" and not replan and planned:
        dropped = planned.pop(0)
        log.warning("PLAN fault=drop_task | job=%s deliberately omitting %s",
                    job_id, dropped.path)

    # R1: freeze the target-interface manifest. On replan only ADD new symbols — pins
    # already made keep binding every retry/escalation/reset to the same decision.
    # A pin conflict (ValueError) is a recipe bug: let it fail the job loudly.
    rules = getattr(recipe, "pin_rules", [])
    manifest = build_manifest(workspace, planned, rules)
    if replan:
        manifest = {**manifest, **(state.get("interface_manifest") or {})}

    # R1.1: freeze framework-owned capability decisions next to symbol decisions, then
    # identify only the small resource/factory/harness seams worth coordinating in one
    # initial generation call. Recipes opt in; recipe #2 remains unaffected.
    units = build_migration_units(files, planned, manifest)
    for unit in units:
        unit["paths"] = [
            path for path in unit["paths"]
            if test_strategy.get(path) not in {"adapter", "unchanged"}
        ]
    units = [unit for unit in units if len(unit["paths"]) >= 2]
    # Compatibility wiring and the application factory it wraps must become visible in
    # the same verification batch. Otherwise a dependency-first app rewrite is tested
    # through the still-Flask conftest API before its adapter wiring exists.
    order = {pf.path: i for i, pf in enumerate(planned)}
    wiring_files = [
        path for path, strategy in test_strategy.items()
        if strategy == "adapter_wiring" and path in order
    ]
    factory_paths = [pf.path for pf in planned if pf.role == "app_factory"]
    for wiring in wiring_files:
        related = [
            factory for factory in factory_paths
            if any(binding.importer == wiring
                   for binding in imported_bindings(workspace, factory))
        ]
        if not related and len(factory_paths) == 1:
            related = factory_paths
        if not related:
            continue
        factory = min(related, key=lambda path: (order[path], path))
        existing = next(
            (unit for unit in units if factory in unit["paths"] or wiring in unit["paths"]),
            None,
        )
        if existing is not None:
            for path in (factory, wiring):
                if path not in existing["paths"] and len(existing["paths"]) < 4:
                    existing["paths"].append(path)
            existing["paths"].sort(key=lambda path: (order[path], path))
            existing["reason"] = "application-factory/test-adapter seam"
        else:
            units.append({
                "id": f"test-adapter-seam-{len(units) + 1}",
                "paths": sorted([factory, wiring], key=lambda path: (order[path], path)),
                "reason": "application-factory/test-adapter seam",
            })
    units = complete_unit_dependencies(workspace, planned, units, test_strategy)
    seam_builder = getattr(recipe, "build_seam_plan", None)
    seam_plan = seam_builder(files, planned, manifest, units) if seam_builder else {
        "version": 1, "decisions": {}, "units": units,
    }
    if replan and state.get("seam_plan"):
        old = state["seam_plan"]
        seam_plan["decisions"] = {
            **seam_plan.get("decisions", {}), **old.get("decisions", {}),
        }
        merged_units = {
            tuple(unit.get("paths", [])): unit
            for unit in [*old.get("units", []), *seam_plan.get("units", [])]
        }
        seam_plan["units"] = list(merged_units.values())
    if adapter_needed:
        for wiring in wiring_files:
            seam_plan.get("decisions", {}).pop(f"test_harness:{wiring}", None)
        seam_plan.setdefault("decisions", {})["test_compatibility"] = {
            "kind": "test_compatibility",
            "files": wiring_files,
            "module": compat_path[:-3].replace("/", "."),
            "instruction": (
                f"Keep behavioural tests unchanged. Import `adapt_app` from "
                f"`{compat_path[:-3]}` and wrap the real FastAPI app returned by "
                "create_app. Pass real exported Click commands and the original instance "
                "path when those seams exist. The adapter supplies test_client, "
                "app_context, test_cli_runner, config, testing, instance_path, and "
                "session_transaction; do not reimplement those APIs in conftest."
            ),
        }

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
        "interface_manifest": manifest,
        "seam_plan": seam_plan,
        "oracle_manifest": oracle_manifest,
        "test_strategy": test_strategy,
        "test_compat_path": compat_path if adapter_needed else "",
        "verify_attempts": 0 if not replan else state.get("verify_attempts", 0),
        "replan_requested": False,
        "step_log": ["plan"],
    }
