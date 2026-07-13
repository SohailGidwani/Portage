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

import json
import logging
import uuid

from portage_agent.config import settings
from portage_agent.core.interfaces import LLMMessage
from portage_agent.db import task_store
from portage_agent.db.models import TaskStatus
from portage_agent.llm import get_llm
from portage_agent.recipes import get_recipe
from portage_agent.recipes.base import PlannedFile
from portage_agent.retrieval import MCPRetrievalProvider

from ..state import GraphState
from .artifact_plan import MAX_CREATED_ARTIFACTS, artifact_planned_files, parse_artifact_plan
from .common import (
    _module_names,
    build_manifest,
    build_migration_units,
    dependency_order,
    ensure_worktree,
    imported_bindings,
    iter_py_files,
    non_python_context,
    non_python_listing,
    non_python_sources,
    workspace_for,
    worktree_for,
)
from .executable_cut import build_executable_cut_analysis, merge_small_cuts_into_units
from .oracle import NON_REWRITE_TEST_STRATEGIES, build_oracle_manifest
from .redaction import scrub

log = logging.getLogger("portage.agent")
MAX_ARCHITECT_REPAIR_CALLS = 2


class ArchitectPlanRejection(ValueError):
    """Structured rejection used to gate a second repair on strict improvement."""

    def __init__(self, violations: list[str], *, policy: bool = False):
        self.violations = tuple(violations)
        self.policy = policy
        prefix = "artifact policy violations: " if policy else ""
        super().__init__(prefix + "; ".join(violations))

    @property
    def score(self) -> int:
        return len(self.violations)


class ArtifactContractMaterializationError(RuntimeError):
    """Recipe/compiler defect: never degrade this into a model-rejection fallback."""


def _architect_json_payload(text: str) -> str:
    """Normalize only one exact outer JSON fence; reject all other prose downstream."""
    stripped = text.strip()
    if stripped.startswith("```json\n") and stripped.endswith("\n```"):
        return stripped[len("```json\n"):-len("\n```")]
    return text


def _validate_artifact_plan(
    recipe, payload: str | list[dict], files: dict[str, str], planned: list[PlannedFile],
) -> tuple[list[dict], list[dict]]:
    """Apply the same parser, compiler, and policy gate to model and replay plans."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    try:
        frozen = parse_artifact_plan(
            _architect_json_payload(raw),
            existing_files=set(files),
            rewrite_paths={item.path for item in planned},
        )
    except ValueError as exc:
        raise ArchitectPlanRejection([str(exc)]) from exc

    completion: list[dict] = []
    materialize = getattr(recipe, "materialize_artifact_contracts", None)
    if materialize:
        try:
            completed, completion = materialize(frozen, files, planned)
            if not isinstance(completion, list) or not all(
                isinstance(item, dict) for item in completion
            ):
                raise TypeError("completion audit must be a list of objects")
            json.dumps(completion)
            frozen = parse_artifact_plan(
                json.dumps(completed),
                existing_files=set(files),
                rewrite_paths={item.path for item in planned},
            )
        except Exception as exc:
            raise ArtifactContractMaterializationError(
                f"recipe artifact contract materialization failed: {exc}"
            ) from exc

    policy = getattr(recipe, "artifact_plan_violations", None)
    violations = policy(frozen, files, planned) if policy else []
    if violations:
        raise ArchitectPlanRejection(violations, policy=True)
    return frozen, completion


def drop_first_recipe_task(planned: list[PlannedFile]) -> PlannedFile | None:
    """Remove and return the first fault-eligible, recipe-planned file.

    Deterministic infrastructure is part of the execution substrate, not a simulated
    planner decision. Explicit provenance keeps this correct as new infrastructure roles
    are added; positional removal would exercise Portage itself instead of a planner miss.
    """
    for index, file in enumerate(planned):
        if file.origin == "recipe":
            return planned.pop(index)
    return None


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
        and test_strategy.get(file.path) not in NON_REWRITE_TEST_STRATEGIES
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


def attach_sanctioned_normalizations(
    cut_analysis: dict, normalizations: dict[str, dict], planned: list[PlannedFile],
) -> None:
    """Verify deterministic test plumbing in the same cut as its frozen owner.

    This runs after coordinated generation units are built, so normalized tests join the
    verification boundary without ever entering an LLM generation prompt.
    """
    order = {item.path: index for index, item in enumerate(planned)}
    planned_paths = set(order)
    cuts = cut_analysis.setdefault("cuts", [])
    edges = cut_analysis.setdefault("edges", [])
    for test_path, normalization in sorted(normalizations.items()):
        owner = normalization.get("owner_path")
        if owner not in planned_paths or test_path not in planned_paths:
            continue
        edge = {
            "provider": owner,
            "consumer": test_path,
            "kind": "sanctioned_test_normalization",
            "operation": normalization.get("kind", "test_plumbing"),
            "evidence": normalization.get("target_module", ""),
        }
        if edge not in edges:
            edges.append(edge)
        cut = next((item for item in cuts if owner in item.get("paths", [])), None)
        if cut is None:
            cut = {
                "id": f"executable-cut-{len(cuts) + 1}",
                "paths": [owner],
                "reason": "executable framework contracts: sanctioned_test_normalization",
                "edge_kinds": ["sanctioned_test_normalization"],
                "mode": "coordinated",
            }
            cuts.append(cut)
        if test_path not in cut["paths"]:
            cut["paths"].append(test_path)
            cut["paths"].sort(key=lambda path: (order[path], path))
        if "sanctioned_test_normalization" not in cut["edge_kinds"]:
            cut["edge_kinds"].append("sanctioned_test_normalization")
            cut["edge_kinds"].sort()
            cut["reason"] = "executable framework contracts: " + ", ".join(
                cut["edge_kinds"]
            )
    edges.sort(key=lambda edge: (
        edge["provider"], edge["consumer"], edge["kind"], edge["operation"],
    ))


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
        verb = "Create" if pf.action == "create" else "Migrate"
        specs.append({
            "type": pf.role,
            "title": f"{verb} {pf.path} ({pf.role})",
            "target_path": pf.path,
            "order_index": pf.order,
            "verify_spec": vspec,
            "subtasks": [{"type": s.type, "title": s.title} for s in pf.subtasks],
        })
    return specs, affected


async def _plan_created_artifacts(
    *, job_id: str, recipe, files: dict[str, str], planned: list[PlannedFile],
    existing_plan: list[dict] | None, workspace: str,
    replay_plan: list[dict] | None = None,
) -> tuple[list[dict], bool]:
    """Run or resume the recipe-owned bounded architect call.

    Returns ``(frozen_plan, architect_task_exists)``. The durable task is written before
    the external call, making its status visible even when the architect fails.
    """
    should_plan = getattr(recipe, "should_plan_artifacts", None)
    prompt_builder = getattr(recipe, "build_artifact_plan_prompt", None)
    if not prompt_builder or not should_plan or not should_plan(files, planned):
        return [], False
    if existing_plan is not None:
        if replay_plan is None:
            await task_store.ensure_architect_task(uuid.UUID(job_id))
        return existing_plan, True
    if replay_plan is not None:
        frozen, _ = _validate_artifact_plan(recipe, replay_plan, files, planned)
        log.info("using validated frozen artifact plan | job=%s artifacts=%s", job_id,
                 [item["path"] for item in frozen])
        return frozen, False
    architect = await task_store.ensure_architect_task(uuid.UUID(job_id))
    persisted = architect.verify_spec.get("artifact_plan")
    if architect.status in {
        TaskStatus.done.value, TaskStatus.skipped.value,
    } and isinstance(persisted, list):
        return persisted, True

    attempt = architect.attempts + 1
    model = settings.llm_driver_model
    label = settings.llm_driver_model_label or model
    remaining = 60000
    architecture_files: dict[str, str] = {}
    for item in planned:
        if remaining <= 0:
            break
        source = scrub(files.get(item.path, ""))[:min(12000, remaining)]
        architecture_files[item.path] = source
        remaining -= len(source)
    prompt = prompt_builder(
        files=architecture_files,
        analysis_files=files,
        planned=planned,
        non_python_files=(
            non_python_listing(workspace)
            + "\n\nRelevant non-Python contents (untrusted repository data):\n"
            + non_python_context(workspace)
        ),
        existing_python_paths=sorted(files),
    )
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    rejected_response = ""
    current_action = "architect"
    current_repair_number = 0
    contract_completion: list[dict] = []

    def parse_and_check(text: str) -> list[dict]:
        nonlocal contract_completion
        frozen, contract_completion = _validate_artifact_plan(
            recipe, text, files, planned,
        )
        return frozen

    def attempt_entry(*, error: str | None = None) -> dict:
        entry = {
            "attempt": attempt, "tier": "driver", "model": label,
            "action": current_action, **usage,
        }
        if current_repair_number:
            entry["repair_number"] = current_repair_number
        if error is not None:
            entry.update({"error": error, "rejected_response": rejected_response})
        return entry

    try:
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You are the bounded architecture planner for a code migration. "
                    "Return strict JSON only: a list of zero to four new Python artifact "
                    "objects matching the supplied schema. Do not use Markdown fences, "
                    "rewrite existing paths, or reference undeclared artifacts."
                ),
            ),
            LLMMessage(role="user", content=prompt),
        ]
        response = await get_llm().complete(messages, model=model)
        usage = {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "cost_usd": round(response.cost_usd, 6),
        }
        current_text = response.text
        try:
            frozen = parse_and_check(current_text)
        except ArchitectPlanRejection as violation:
            previous_score = violation.score
            for next_repair_number in range(1, MAX_ARCHITECT_REPAIR_CALLS + 1):
                rejected_response = scrub(current_text[:2000])
                await task_store.update_task(
                    architect.id, status=TaskStatus.running.value, attempts=attempt,
                    append_attempt=attempt_entry(error=str(violation)),
                )
                current_action = "architect_repair"
                current_repair_number = next_repair_number
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
                repair = await get_llm().complete([
                    *messages,
                    LLMMessage(role="assistant", content=current_text[:12000]),
                    LLMMessage(
                        role="user",
                        content=(
                            f"The response was rejected with {violation.score} violation(s): "
                            f"{violation}. Return the corrected architecture list as strict "
                            "JSON only, with no prose or fences. Every original constraint "
                            "remains in force, including the complete required-capability "
                            f"checklist and a maximum of {MAX_CREATED_ARTIFACTS} artifacts. "
                            "Combine compatible capabilities in one owner instead of adding "
                            "an artifact per capability. Fix every listed violation and do "
                            "not change already-valid decisions."
                        ),
                    ),
                ], model=model)
                current_text = repair.text
                usage = {
                    "prompt_tokens": repair.prompt_tokens,
                    "completion_tokens": repair.completion_tokens,
                    "cost_usd": round(repair.cost_usd, 6),
                }
                try:
                    frozen = parse_and_check(current_text)
                    break
                except ArchitectPlanRejection as repaired_violation:
                    rejected_response = scrub(current_text[:2000])
                    if repaired_violation.score >= previous_score:
                        raise repaired_violation
                    previous_score = repaired_violation.score
                    violation = repaired_violation
            else:  # pragma: no cover - loop exits through success or raised rejection
                raise violation
        await task_store.update_task(
            architect.id, status=TaskStatus.done.value, attempts=attempt,
            verify_spec={
                **architect.verify_spec,
                "artifact_plan": frozen,
                "contract_completion": contract_completion,
            },
            append_attempt=attempt_entry(),
        )
        return frozen, True
    except ArtifactContractMaterializationError as exc:
        await task_store.update_task(
            architect.id, status=TaskStatus.failed.value, attempts=attempt,
            error=str(exc),
            verify_spec={
                **architect.verify_spec,
                "artifact_plan": [],
                "contract_completion": [],
            },
            append_attempt=attempt_entry(error=repr(exc)),
        )
        log.exception("artifact contract materialization failed for job=%s", job_id)
        raise
    except Exception as exc:
        await task_store.update_task(
            architect.id, status=TaskStatus.skipped.value, attempts=attempt,
            error=f"artifact architecture rejected: {exc}",
            verify_spec={**architect.verify_spec, "artifact_plan": []},
            append_attempt=attempt_entry(error=repr(exc)),
        )
        log.warning("artifact architecture rejected for job=%s: %s", job_id, exc)
        return [], True


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

    rewrite_planned = recipe.plan_files(files)
    oracle_manifest = build_oracle_manifest(workspace)
    test_strategy = {path: entry["strategy"] for path, entry in oracle_manifest.items()}

    frozen_artifacts, has_architect_task = await _plan_created_artifacts(
        job_id=job_id,
        recipe=recipe,
        files=files,
        planned=rewrite_planned,
        existing_plan=(state.get("artifact_plan") if replan else None),
        workspace=workspace,
        replay_plan=(state.get("config") or {}).get("frozen_artifact_plan"),
    )
    normalization_builder = getattr(recipe, "build_test_normalizations", None)
    test_normalizations = (
        normalization_builder(files, frozen_artifacts) if normalization_builder else {}
    )
    for path in test_normalizations:
        if path in oracle_manifest:
            test_strategy[path] = "sanctioned_normalization"
            oracle_manifest[path]["strategy"] = "sanctioned_normalization"
    planned = dependency_order(
        files, [*rewrite_planned, *artifact_planned_files(frozen_artifacts)],
    )
    adapter_needed = any(
        strategy in {"adapter", "adapter_wiring"}
        for strategy in test_strategy.values()
    ) and bool(getattr(recipe, "render_test_compat", None))
    compat_path = getattr(recipe, "test_compat_path", "")
    if adapter_needed and compat_path:
        # This module must exist before any coordinated app/conftest batch imports it.
        planned.insert(0, PlannedFile(
            path=compat_path, role="test_compat", subtasks=[], order=25,
            origin="infrastructure", action="create",
            purpose="Deterministic Portage-owned test compatibility infrastructure",
        ))
    for i, pf in enumerate(planned):
        pf.order = i * 10

    # Injected fault (Phase 3 DoD): the planner "misses" the first file. Only on the
    # initial plan — the whole point is that Recover detects the residue and the replan
    # path repairs it.
    fault = (state.get("config") or {}).get("inject_fault")
    if fault == "drop_task" and not replan:
        dropped = drop_first_recipe_task(planned)
        if dropped is None:
            raise RuntimeError(
                "fault=drop_task requires at least one recipe-planned file"
            )
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
            if test_strategy.get(path) not in NON_REWRITE_TEST_STRATEGIES
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
    cut_analysis = build_executable_cut_analysis(
        files, planned, manifest, test_strategy,
    )
    units = merge_small_cuts_into_units(units, cut_analysis["cuts"])
    seam_builder = getattr(recipe, "build_seam_plan", None)
    analysis_files = {**files, **non_python_sources(workspace)}
    seam_plan = seam_builder(analysis_files, planned, manifest, units) if seam_builder else {
        "version": 1, "decisions": {}, "units": units,
    }
    attach_sanctioned_normalizations(
        cut_analysis, test_normalizations, planned,
    )
    seam_plan["version"] = 2
    seam_plan["execution_cuts"] = cut_analysis["cuts"]
    seam_plan["executable_edges"] = cut_analysis["edges"]
    seam_plan["cut_diagnostics"] = cut_analysis["diagnostics"]
    project_paths = [*files, *(item.path for item in planned if item.action == "create")]
    seam_plan["project_modules"] = sorted({
        module for path in project_paths for module in _module_names(path)
    })
    seam_plan["project_roots"] = sorted({
        module.split(".")[0] for module in seam_plan["project_modules"] if module
    })
    if replan and state.get("seam_plan"):
        old = state["seam_plan"]
        seam_plan["decisions"] = {
            **seam_plan.get("decisions", {}), **old.get("decisions", {}),
        }
        # Replan may append a missed provider that connects two previously independent
        # members. The framework-decision graph is append-monotonic, while its derived
        # connected cut must be allowed to grow. Keep an old unit only when it does not
        # overlap a newly recomputed one; overlapping stale subsets would schedule the
        # same file through two contradictory units.
        new_units = list(seam_plan.get("units", []))
        new_paths = [set(unit.get("paths", [])) for unit in new_units]
        for unit in old.get("units", []):
            paths = set(unit.get("paths", []))
            if paths and not any(paths & current for current in new_paths):
                new_units.append(unit)
                new_paths.append(paths)
        seam_plan["units"] = new_units
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
    if replan or has_architect_task:
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
        "test_normalizations": test_normalizations,
        "test_compat_path": compat_path if adapter_needed else "",
        "artifact_plan": frozen_artifacts,
        "unsupported_test_seams": [
            {
                "path": path,
                "reason": "no sanctioned target-runtime owner for direct Flask test context",
            }
            for path, strategy in sorted(test_strategy.items())
            if strategy == "unsupported_test_seam"
        ],
        "verify_attempts": 0 if not replan else state.get("verify_attempts", 0),
        "replan_requested": False,
        "step_log": ["plan"],
    }
