"""Recover node — classify a Verify failure and choose a bounded recovery strategy.

The plan-§8 taxonomy, made concrete for a file-level migration:

  1. **Replan** — a *not-planned* source file still contains framework code the recipe
     should have migrated (e.g. the planner missed a file): route back to Plan, which
     appends the missing task(s).
  2. **Targeted rollback + regenerate** — the failure is a crash (syntax/import error)
     whose traceback implicates specific *planned* files: roll ONLY those files back to
     their original source (`git checkout -- <path>`) and reset their tasks to pending.
     Execute regenerates them; once a task's attempts pass `escalate_after_attempts`,
     Execute switches it to the escalation-tier model (measured via attempts_log).
  3. **Behavioral retry** — test assertions fail but nothing crashes; attribution is
     fuzzy, so roll back and regenerate every non-skipped file task, with the failing
     output as context.
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
    if code != 0:  # pragma: no cover - checkout of a tracked path shouldn't fail
        raise RuntimeError(f"git checkout -- {path} failed: {out[:300]}")


async def recover_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    worktree = state["worktree"]
    visits = int(state.get("recover_visits", 0)) + 1
    output = state.get("last_verify_errors") or ""

    tasks = await task_store.load_tasks(uuid.UUID(job_id))
    file_tasks = [t for t in tasks if t.target_path]
    planned_paths = {t.target_path for t in file_tasks}

    log.info("RECOVER node | job=%s visit=%s/%s", job_id, visits, settings.max_recover_visits)

    if visits > settings.max_recover_visits:
        log.warning("RECOVER give-up | job=%s recover budget exhausted", job_id)
        return {
            "recover_visits": visits,
            "recover_route": "integrate",
            "recovery_actions": [
                {"visit": visits, "classification": "budget_exhausted",
                 "action": "give_up", "at": _now()}
            ],
            "step_log": ["recover"],
        }

    # 1. Replan — framework residue in a file the plan doesn't know about.
    residue = _find_unplanned_residue(worktree, planned_paths)
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

    # 2/3. Crash implicating planned files -> targeted; otherwise behavioural -> all active.
    # A crash traceback walks the import chain (conftest -> app -> api), naming every file
    # along the way; the file that is actually broken is the DEEPEST frame — the last
    # planned path to appear — so only that one is rolled back and regenerated.
    # Scan only the traceback region: pytest's trailing "short test summary" repeats the
    # TEST file's path after the traceback and would defeat the deepest-frame heuristic
    # (observed: retrying the test module while the truly broken errors.py kept its bug).
    cut = output.find("short test summary info")
    trace_region = output[:cut] if cut != -1 else output
    crashed = any(m in output for m in _CRASH_MARKERS)
    mentioned = [
        t for t in file_tasks
        if t.target_path and t.target_path in trace_region
    ]
    mentioned.sort(key=lambda t: trace_region.rfind(t.target_path or ""))
    if crashed and mentioned:
        classification, targets = "crash", [mentioned[-1]]
        # Widen on repeat: if the previous visit already crash-targeted this same lone
        # file, single-file blame isn't converging — the deepest frame can be the crash
        # SITE while the offending change lives in a caller (seen: a migrated test module
        # calling create_app() at import; the factory takes the blame every round and
        # burns its budget). Reset every active task so the true offender regenerates too.
        prev = (state.get("recovery_actions") or [])
        if prev:
            last = prev[-1]
            if (last.get("classification") == "crash"
                    and last.get("targets") == [targets[0].target_path]):
                classification = "crash_widened"
                targets = [t for t in file_tasks if t.status != TaskStatus.skipped.value]
    else:
        classification = "behavioral"
        targets = [t for t in file_tasks if t.status != TaskStatus.skipped.value]

    retry_paths: list[str] = []
    skipped_paths: list[str] = []
    for t in targets:
        assert t.target_path is not None
        if t.attempts >= settings.max_task_attempts:
            # Preserve the losing attempt's diff BEFORE rolling back — a skipped task
            # whose failing content is gone can't be post-mortemed (the taxonomy needs it).
            failing_diff = await file_diff(worktree, t.target_path)
            await _rollback_file(worktree, t.target_path)
            await task_store.update_task(
                t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                error=f"exhausted after {t.attempts} attempts; last error: …{output[-400:]}",
                diff=failing_diff,
                append_attempt={"action": "rollback_skip", "reason": classification,
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
        "recover_route": route,
        "recovery_actions": [
            {"visit": visits, "classification": classification,
             "action": "rollback_regenerate" if retry_paths else "give_up",
             "targets": retry_paths, "skipped": skipped_paths,
             "error_head": output[:240], "at": _now()}
        ],
        "step_log": ["recover"],
    }
