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

import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from portage_agent.config import settings
from portage_agent.db import task_store
from portage_agent.db.models import TaskStatus

from ..state import GraphState
from .common import file_diff, iter_py_files, run_git

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


async def _rollback_file(worktree: str, path: str) -> None:
    """Restore one file to its original (pre-migration) source from the worktree HEAD."""
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


async def recover_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    worktree = state["worktree"]
    visits = int(state.get("recover_visits", 0)) + 1
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

    log.info("RECOVER node | job=%s visit=%s/%s", job_id, visits, settings.max_recover_visits)

    if visits > settings.max_recover_visits:
        log.warning("RECOVER give-up | job=%s recover budget exhausted", job_id)
        return {
            "recover_visits": visits,
            "integration_recovery_visits": integration_visits + int(integration_failure),
            "recover_route": "integrate",
            "recovery_actions": [
                {"visit": visits, "classification": "budget_exhausted",
                 "action": "give_up", "at": _now()}
            ],
            "step_log": ["recover"],
        }

    # 1. Replan — framework residue in a file the plan doesn't know about.
    residue = [] if integration_failure else _find_unplanned_residue(worktree, planned_paths)
    if residue:
        log.info("RECOVER classify=unplanned_residue -> REPLAN | job=%s files=%s",
                 job_id, residue)
        return {
            "recover_visits": visits,
            "recover_route": "plan",
            "replan_requested": True,
            "recovery_actions": [
                {"visit": visits, "classification": "unplanned_residue",
                 "action": "replan", "targets": residue, "at": _now()}
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
    if integration_failure:
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

    retry_paths: list[str] = []
    skipped_paths: list[str] = []
    for t in targets:
        assert t.target_path is not None
        # The single Integrate recovery cycle is a reserved budget. A batch that needed
        # all normal Verify attempts must still get one chance to repair a regression
        # that only the authoritative full suite exposed.
        exhausted = t.attempts >= settings.max_task_attempts and not integration_failure
        if force_skip or exhausted:
            # Preserve the losing attempt's diff BEFORE rolling back — a skipped task
            # whose failing content is gone can't be post-mortemed (the taxonomy needs it).
            failing_diff = await file_diff(worktree, t.target_path)
            await _rollback_file(worktree, t.target_path)
            await task_store.update_task(
                t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                error=(f"no progress after {repeat} identical failures; " if force_skip else
                       f"exhausted after {t.attempts} attempts; ")
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
            await _rollback_file(worktree, t.target_path)
            await task_store.update_task(
                t.id, status=TaskStatus.pending.value, cascade_subtasks=True,
                append_attempt={"action": "rollback_regenerate", "reason": classification,
                                "visit": visits, "at": _now(),
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
        "integration_recovery_visits": integration_visits + int(integration_failure),
        "recover_route": route,
        "diagnostic_repair_requested": diagnostic_repair,
        "recovery_actions": [
            {"visit": visits, "classification": classification,
             "action": "rollback_regenerate" if retry_paths else "give_up",
             "targets": retry_paths, "skipped": skipped_paths,
             "error_head": output[:240], "fingerprint": fingerprint,
             "repeat": repeat, "at": _now()}
        ],
        "step_log": ["recover"],
    }
