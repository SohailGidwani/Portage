"""Recover node — classify a Verify failure and choose a bounded recovery strategy.

The plan-§8 taxonomy, made concrete for a file-level migration:

  1. **Replan** — a *not-planned* source file still contains framework code the recipe
     should have migrated (e.g. the planner missed a file): route back to Plan, which
     appends the missing task(s).
  2. **Batch rollback + regenerate** — a crash or behavioral failure rolls back the
     current verified batch, which is now the precise unit of blame. Coupled seam members
     stay coherent; completed earlier batches remain in place. Once attempts pass
     `escalate_after_attempts`, Execute switches to the escalation tier.
  4. **Skip-and-continue / give up** — a task at `max_task_attempts` is rolled back to its
     original source and marked `skipped`; when nothing is left to retry (or the global
     `max_recover_visits` budget is spent), route to Integrate so the report stays honest.

Recover only decides and rolls back; Execute owns regeneration, Plan owns replanning.
"""

from __future__ import annotations

import ast
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from portage_agent.config import settings
from portage_agent.db import task_store
from portage_agent.db.models import TaskStatus

from ..state import GraphState
from .common import (
    _module_names,
    content_hash,
    file_diff,
    iter_py_files,
    load_cut_checkpoint,
    planned_artifact_topology_violations,
    read_file,
    restore_cut_checkpoint,
    run_git,
)

log = logging.getLogger("portage.agent")

# Signatures of a crash (vs. a behavioural assertion failure) in pytest output.
_CRASH_MARKERS = (
    "SyntaxError",
    "IndentationError",
    "ImportError",
    "ModuleNotFoundError",
    "cannot import name",
    "ERROR collecting",
    "errors during collection",
)

_FLASK_IMPORT = re.compile(r"^\s*(from\s+flask\b|import\s+flask\b)", re.MULTILINE)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _find_unplanned_residue(worktree: str, planned_paths: set[str]) -> list[str]:
    """Source files still importing the source framework that the plan doesn't cover."""
    return sorted(
        rel
        for rel, src in iter_py_files(worktree).items()
        if rel not in planned_paths and _FLASK_IMPORT.search(src)
    )


def missing_unplanned_test_compat(
    output: str, compat_path: str, planned_paths: set[str]
) -> bool:
    """Whether pytest failed because the expected deterministic adapter was unplanned.

    This deliberately does not broaden arbitrary ``ModuleNotFoundError`` failures into
    replans.  It matches only the checkpointed Portage-owned compatibility module, and only
    when no task exists for it; a planned adapter failure belongs to normal batch recovery.
    """
    if not compat_path or compat_path in planned_paths:
        return False
    module = Path(compat_path).with_suffix("").as_posix().replace("/", ".")
    pattern = re.compile(
        rf"ModuleNotFoundError:\s*No module named\s+['\"]{re.escape(module)}['\"]"
    )
    return bool(pattern.search(output))


async def _rollback_file(worktree: str, path: str, *, action: str = "rewrite") -> None:
    """Undo one artifact according to its frozen action."""
    if action == "create":
        # `git checkout -- path` reports success for an intent-to-add file but replaces it
        # with an empty path instead of removing it. Clear the worktree-local index entry,
        # then remove the generated artifact explicitly.
        await run_git("reset", "--", path, cwd=worktree)
        candidate = Path(worktree, path)
        if candidate.exists():
            candidate.unlink()
        return
    code, out = await run_git("checkout", "--", path, cwd=worktree)
    if code != 0 and "did not match any file" in out:
        # Deterministic compatibility modules are intentionally new/untracked files.
        candidate = Path(worktree, path)
        if candidate.exists():
            candidate.unlink()
        return
    if code != 0:  # pragma: no cover - checkout of a tracked path shouldn't fail
        raise RuntimeError(f"git checkout -- {path} failed: {out[:300]}")


def repeated_failure_count(actions: list[dict], fingerprint: str) -> int:
    """Occurrence number for this exact failure, including the current Verify."""
    return 1 + sum(1 for action in actions if action.get("fingerprint") == fingerprint)


def targeted_contract_repair_count(attempts_log: list[dict]) -> int:
    """Count durable runtime-targeted repair calls, excluding generation-time checks."""
    return sum(
        attempt.get("action") == "contract_repair"
        and attempt.get("scope") == "runtime_targeted"
        for attempt in attempts_log
    )


def contract_failure_owner(
    output: str, manifest: dict[str, dict], current_batch: set[str],
) -> str | None:
    """Map a runtime failure to one unique frozen contract owner, or refuse attribution."""
    candidates: set[str] = set()

    missing_module = re.search(
        r"ModuleNotFoundError:\s*No module named\s+['\"]([^'\"]+)['\"]", output,
    )
    if missing_module:
        module = missing_module.group(1)
        candidates.update(
            pin["module"] for pin in manifest.values()
            if pin.get("provenance") == "planned_create"
            and module in _module_names(pin["module"])
        )

    missing_export = re.search(
        r"cannot import name\s+['\"]?([A-Za-z_]\w*)['\"]?\s+from\s+['\"]([^'\"]+)",
        output,
    )
    if missing_export:
        symbol, module = missing_export.groups()
        candidates.update(
            pin["module"] for pin in manifest.values()
            if pin.get("symbol") == symbol and module in _module_names(pin["module"])
        )

    missing_attr = re.search(
        r"AttributeError:\s*['\"][^'\"]+['\"] object has no attribute "
        r"['\"]([A-Za-z_]\w*)['\"]",
        output,
    )
    if missing_attr:
        member = missing_attr.group(1)
        candidates.update(
            pin["module"] for pin in manifest.values()
            if member in pin.get("members", [])
        )

    if current_batch:
        candidates &= current_batch
    return next(iter(candidates)) if len(candidates) == 1 else None


def contract_failure_target(
    output: str, manifest: dict[str, dict], current_batch: set[str], worktree: str,
) -> str | None:
    """Choose the uniquely broken provider or consumer side of a frozen contract."""
    owner = contract_failure_owner(output, manifest, current_batch)
    if owner is None:
        return None
    missing_attr = re.search(
        r"AttributeError:\s*['\"][^'\"]+['\"] object has no attribute "
        r"['\"]([A-Za-z_]\w*)['\"]",
        output,
    )
    if not missing_attr:
        return owner
    member = missing_attr.group(1)
    pins = [
        pin for pin in manifest.values()
        if pin.get("module") == owner and member in pin.get("members", [])
    ]
    if len(pins) != 1:
        return owner
    try:
        tree = ast.parse(Path(worktree, owner).read_text())
    except (OSError, SyntaxError):
        return owner
    class_node = next((
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == pins[0].get("symbol")
    ), None)
    implemented = bool(class_node and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == member
        for node in class_node.body
    ))
    if not implemented:
        return owner
    consumers = {
        consumer["module"] for consumer in pins[0].get("consumers", [])
        if consumer.get("module") in current_batch
    }
    return next(iter(consumers)) if len(consumers) == 1 else owner


def circular_import_target(
    output: str, manifest: dict[str, dict], current_batch: set[str], worktree: str,
) -> str | None:
    """Attribute an escaped circular import to one provider importing its consumer."""
    if "circular import" not in output and "partially initialized module" not in output:
        return None
    candidates = {
        path for path in current_batch
        if path in output
        and planned_artifact_topology_violations(
            read_file(worktree, path, limit=20000) or "", manifest, path,
        )
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def unique_traceback_leaf_target(output: str, eligible_paths: set[str]) -> str | None:
    """Return one application artifact when every attributable traceback ends there.

    Pytest may report several failures and repeat test paths in its summary.  We only
    collect the final eligible application frame immediately followed by an exception;
    ambiguous leaves deliberately fall back to normal batch recovery.
    """
    leaves: set[str] = set()
    last_frame: str | None = None
    exception_line = re.compile(
        r"^\s*(?:E\s+)?(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
        r"(?:Error|Exception)|AssertionError)(?::|$)"
    )
    for line in output.splitlines():
        matched = next((
            path for path in sorted(eligible_paths, key=len, reverse=True)
            if re.search(rf"(?:^|[/\\]){re.escape(path)}:\d+:", line)
        ), None)
        if matched:
            last_frame = matched
            continue
        if exception_line.match(line):
            if last_frame:
                leaves.add(last_frame)
            last_frame = None
    return next(iter(leaves)) if len(leaves) == 1 else None


async def recover_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    worktree = state["worktree"]
    visits = int(state.get("recover_visits", 0)) + 1
    budget_used = int(state.get("recover_budget_used", 0))
    integration_failure = state.get("recover_source") == "integrate"
    integration_visits = int(state.get("integration_recovery_visits", 0))
    output = (
        state.get("last_integrate_errors") if integration_failure
        else state.get("last_verify_errors")
    ) or ""
    fingerprint = state.get("last_failure_fingerprint") or ""
    repeat = repeated_failure_count(state.get("recovery_actions") or [], fingerprint)

    tasks = await task_store.load_tasks(uuid.UUID(job_id))
    file_tasks = [t for t in tasks if t.target_path]
    planned_paths = {t.target_path for t in file_tasks}
    hard_limit = (
        settings.max_recover_visits
        + len(file_tasks) * settings.max_targeted_contract_repairs
        + 2
    )

    log.info(
        "RECOVER node | job=%s visit=%s budget=%s/%s hard=%s",
        job_id, visits, budget_used, settings.max_recover_visits, hard_limit,
    )

    def stop_for_budget(classification: str = "budget_exhausted") -> GraphState:
        log.warning(
            "RECOVER give-up | job=%s classification=%s budget=%s/%s",
            job_id, classification, budget_used, settings.max_recover_visits,
        )
        return {
            "recover_visits": visits,
            "recover_budget_used": budget_used,
            "integration_recovery_visits": integration_visits + int(integration_failure),
            "recover_route": "integrate",
            "recovery_actions": [
                {"visit": visits, "classification": classification,
                 "action": "give_up", "budget_used": budget_used, "at": _now()}
            ],
            "step_log": ["recover"],
        }

    if visits > hard_limit:
        return stop_for_budget("hard_safety_cap")

    if state.get("cut_restore_pending_verification"):
        budget_used += 1
        if budget_used > settings.max_recover_visits:
            return stop_for_budget()
        return {
            "recover_visits": visits,
            "recover_budget_used": budget_used,
            "integration_recovery_visits": 1,
            "recover_route": "integrate",
            "cut_restore_pending_verification": False,
            "recovery_actions": [{
                "visit": visits,
                "classification": "restored_cut_reverify_failed",
                "action": "give_up",
                "budget_charged": True,
                "budget_used": budget_used,
                "fingerprint": fingerprint,
                "at": _now(),
            }],
            "step_log": ["recover"],
        }

    # 1. Replan — framework residue in a file the plan doesn't know about, or an exact
    # missing-module failure for deterministic compatibility infrastructure that was never
    # planned. Plan recreates that adapter deterministically and without an LLM call.
    compat_path = state.get("test_compat_path") or ""
    if not integration_failure and missing_unplanned_test_compat(
        output, compat_path, planned_paths
    ):
        budget_used += 1
        if budget_used > settings.max_recover_visits:
            return stop_for_budget()
        log.info(
            "RECOVER classify=missing_test_compat -> REPLAN | job=%s module=%s",
            job_id, compat_path,
        )
        return {
            "recover_visits": visits,
            "recover_budget_used": budget_used,
            "recover_route": "plan",
            "replan_requested": True,
            "recovery_actions": [
                {"visit": visits, "classification": "missing_test_compat",
                 "action": "replan", "targets": [compat_path],
                 "budget_charged": True, "budget_used": budget_used, "at": _now()}
            ],
            "step_log": ["recover"],
        }

    residue = [] if integration_failure else _find_unplanned_residue(worktree, planned_paths)
    if residue:
        budget_used += 1
        if budget_used > settings.max_recover_visits:
            return stop_for_budget()
        log.info("RECOVER classify=unplanned_residue -> REPLAN | job=%s files=%s",
                 job_id, residue)
        return {
            "recover_visits": visits,
            "recover_budget_used": budget_used,
            "recover_route": "plan",
            "replan_requested": True,
            "recovery_actions": [
                {"visit": visits, "classification": "unplanned_residue",
                 "action": "replan", "targets": residue,
                 "budget_charged": True, "budget_used": budget_used, "at": _now()}
            ],
            "step_log": ["recover"],
        }

    # 2/3. A crash identifies the failing batch; behavioral failures use that same precise
    # batch attribution. Earlier verified batches remain intact.
    # Scan only the traceback region: pytest's trailing "short test summary" repeats the
    # TEST file's path after the traceback, which is not useful for primary attribution.
    cut = output.find("short test summary info")
    trace_region = output[:cut] if cut != -1 else output
    crashed = any(m in output for m in _CRASH_MARKERS)
    current_batch = set(state.get("current_batch_paths") or [])
    manifest = state.get("interface_manifest") or {}
    contract_owner = None if integration_failure else (
        circular_import_target(output, manifest, current_batch, worktree)
        or contract_failure_target(output, manifest, current_batch, worktree)
    )
    eligible_leaf_paths = {
        task.target_path for task in file_tasks
        if task.target_path
        and getattr(task, "type", "") not in {"test_harness", "test_compat"}
        and (not current_batch or task.target_path in current_batch)
    }
    leaf_owner = None if integration_failure or contract_owner else (
        unique_traceback_leaf_target(trace_region, eligible_leaf_paths)
    )
    owner = contract_owner or leaf_owner
    if owner:
        classification = "contract_failure" if contract_owner else "traceback_leaf"
        targets = [
            task for task in file_tasks
            if task.target_path == owner and task.status != TaskStatus.skipped.value
        ]
    elif integration_failure:
        classification = "integration_regression"
        mentioned = [
            task for task in file_tasks
            if task.target_path and task.target_path in trace_region
            and task.status != TaskStatus.skipped.value
        ]
        implicated = {task.target_path for task in mentioned}
        verified_batch = next(
            (
                batch for batch in reversed(state.get("verified_batches") or [])
                if implicated & set(batch.get("paths", []))
            ),
            None,
        )
        batch_paths = set((verified_batch or {}).get("paths", []))
        targets = [
            task for task in file_tasks
            if task.status == TaskStatus.done.value
            and (task.target_path in batch_paths if batch_paths else True)
        ]
    else:
        mentioned = [
            t for t in file_tasks
            if t.target_path and t.target_path in trace_region
            and (not current_batch or t.target_path in current_batch)
        ]
        mentioned.sort(key=lambda t: trace_region.rfind(t.target_path or ""))
        if crashed and mentioned:
            classification = "crash_batch"
            targets = [
                t for t in file_tasks
                if t.status != TaskStatus.skipped.value
                and (not current_batch or t.target_path in current_batch)
            ]
        else:
            classification = "behavioral"
            targets = [
                t for t in file_tasks
                if t.status != TaskStatus.skipped.value
                and (not current_batch or t.target_path in current_batch)
            ]

    diagnostic_repair = not integration_failure and repeat == 2
    force_skip = not integration_failure and repeat >= 3
    if diagnostic_repair:
        classification += "_no_progress_diagnostic"
    elif force_skip:
        classification = "no_progress"

    owner_task = next(
        (task for task in targets if task.target_path == owner), None,
    )
    owner_repairs = (
        targeted_contract_repair_count(owner_task.attempts_log) if owner_task else 0
    )
    owner_allowance = bool(
        owner_task
        and owner_repairs < settings.max_targeted_contract_repairs
        and not force_skip
    )
    free_targeted_progress = bool(owner and repeat == 1 and owner_allowance)
    budget_charged = not free_targeted_progress
    budget_used += int(budget_charged)
    if budget_used > settings.max_recover_visits:
        return stop_for_budget()

    checkpoint = state.get("current_batch_checkpoint") or load_cut_checkpoint(worktree)
    if owner and owner_task and not owner_allowance and checkpoint:
        failing_diff = await file_diff(worktree, owner)
        records = checkpoint.get("files", {})
        restored_paths = restore_cut_checkpoint(worktree, checkpoint)
        tasks_by_path = {task.target_path: task for task in file_tasks}
        for path in restored_paths:
            record = records[path]
            task = tasks_by_path.get(path)
            if task is None:
                continue
            if not record.get("existed"):
                await run_git("reset", "--", path, cwd=worktree)
            baseline_done = record.get("status") == TaskStatus.done.value
            restored_status = (
                TaskStatus.done.value if baseline_done else TaskStatus.skipped.value
            )
            restored_content = Path(worktree, path).read_text() if record.get("existed") else ""
            await task_store.update_task(
                task.id,
                status=restored_status,
                content_hash=content_hash(restored_content) if record.get("existed") else None,
                diff=failing_diff if path == owner else None,
                error=(
                    None if baseline_done else
                    "targeted repair failed; restored the pre-cut checkpoint"
                ),
                cascade_subtasks=True,
                append_attempt={
                    "action": "cut_checkpoint_restore",
                    "reason": classification,
                    "visit": visits,
                    "at": _now(),
                },
            )
        snapshots = await task_store.load_tasks(uuid.UUID(job_id))
        has_pending = any(
            task.status == TaskStatus.pending.value for task in snapshots
        )
        return {
            "recover_visits": visits,
            "recover_budget_used": budget_used,
            "recover_route": "verify",
            "contract_repair_owner": "",
            "current_batch_checkpoint": {},
            "cut_restore_pending_verification": True,
            "has_pending_tasks": has_pending,
            "recovery_actions": [{
                "visit": visits,
                "classification": "targeted_repair_restored_cut",
                "action": "restore_cut_reverify",
                "targets": restored_paths,
                "failed_target": owner,
                "budget_charged": budget_charged,
                "budget_used": budget_used,
                "fingerprint": fingerprint,
                "repeat": repeat,
                "at": _now(),
            }],
            "step_log": ["recover"],
        }

    retry_paths: list[str] = []
    skipped_paths: list[str] = []
    for t in targets:
        assert t.target_path is not None
        targeted_repair = owner == t.target_path
        targeted_repairs = targeted_contract_repair_count(t.attempts_log)
        targeted_allowance = (
            targeted_repair
            and targeted_repairs < settings.max_targeted_contract_repairs
        )
        # The single Integrate recovery cycle is a reserved budget. A batch that needed
        # all normal Verify attempts must still get one chance to repair a regression
        # that only the authoritative full suite exposed.
        exhausted = (
            (
                targeted_repair
                and targeted_repairs >= settings.max_targeted_contract_repairs
            )
            or (
                t.attempts >= settings.max_task_attempts
                and not integration_failure
                and not targeted_allowance
            )
        )
        if force_skip or exhausted:
            # Preserve the losing attempt's diff BEFORE rolling back — a skipped task
            # whose failing content is gone can't be post-mortemed (the taxonomy needs it).
            failing_diff = await file_diff(worktree, t.target_path)
            await _rollback_file(
                worktree, t.target_path,
                action=t.verify_spec.get("action", "rewrite"),
            )
            await task_store.update_task(
                t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                error=(f"no progress after {repeat} identical failures; " if force_skip else
                       (
                           "targeted contract repair allowance exhausted after "
                           f"{targeted_repairs} attempt(s); "
                           if targeted_repair else
                           f"exhausted after {t.attempts} attempts; "
                       ))
                      + f"last error: …{output[-400:]}",
                diff=failing_diff,
                append_attempt={"action": "no_progress_stop" if force_skip else
                                          "rollback_skip", "reason": classification,
                                "visit": visits, "at": _now()},
            )
            skipped_paths.append(t.target_path)
        else:
            # Keep the losing attempt's diff in the log entry: Execute shows it to the
            # model on retry, turning blind regeneration into debugging-your-own-code
            # (the factory-convergence lever — 3 blind attempts kept making sibling bugs).
            failing_diff = await file_diff(worktree, t.target_path)
            await _rollback_file(
                worktree, t.target_path,
                action=t.verify_spec.get("action", "rewrite"),
            )
            await task_store.update_task(
                t.id, status=TaskStatus.pending.value, cascade_subtasks=True,
                append_attempt={"action": "rollback_regenerate", "reason": classification,
                                "visit": visits, "at": _now(),
                                "scope": "runtime_targeted" if targeted_repair else "batch",
                                "failing_diff": failing_diff[:4000]},
            )
            retry_paths.append(t.target_path)

    route = "execute" if retry_paths else "integrate"
    log.info(
        "RECOVER classify=%s -> %s | job=%s retry=%s skipped=%s",
        classification, route.upper(), job_id, retry_paths, skipped_paths,
    )
    return {
        "recover_visits": visits,
        "recover_budget_used": budget_used,
        "integration_recovery_visits": integration_visits + int(integration_failure),
        "recover_route": route,
        "diagnostic_repair_requested": diagnostic_repair,
        "contract_repair_owner": owner or "",
        "recovery_actions": [
            {"visit": visits, "classification": classification,
             "action": (
                 "targeted_contract_repair"
                 if owner and retry_paths else
                 "rollback_regenerate" if retry_paths else "give_up"
             ),
             "targets": retry_paths, "skipped": skipped_paths,
             "error_head": output[:240], "fingerprint": fingerprint,
             "repeat": repeat, "budget_charged": budget_charged,
             "budget_used": budget_used, "at": _now()}
        ],
        "step_log": ["recover"],
    }
