"""Execute node — migrate each pending file task with the LLM, on the git worktree.

For every *pending* file Task (in dependency order): gather context (the behavioural test
contract, the framework-agnostic callees, and any already-migrated siblings), prompt the
model for a full rewrite, write it into the worktree, and record content hash + per-file
diff + an attempts_log entry in Postgres.

Durability & recovery (Phase 3):
  * Idempotent / resume-safe — a `done` task whose worktree file still hashes to the
    recorded value is skipped, so a crash mid-Execute resumes without re-calling the model;
    `skipped` tasks (recovery gave up on them) are never retried.
  * Targeted regeneration — Recover rolls back exactly the files it wants redone and resets
    those tasks to pending; Execute regenerates only those, with the failing test output as
    added context.
  * **Model escalation (measured)** — a task's first `escalate_after_attempts` attempts use
    the driver model; later attempts use the escalation model. Every attempt is recorded in
    attempts_log with its tier/model, so "how often does escalation rescue a task?" is a
    queryable fact, not an anecdote.
  * Fault injection (config `inject_fault`, Phase 3 DoD + Phase 4 harness): `bad_patch`
    corrupts the first attempt of the first task; `bad_patch_until_escalation` corrupts
    every driver-tier attempt of the first task, so only escalation can rescue it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from portage_agent.config import settings
from portage_agent.core.interfaces import LLMMessage
from portage_agent.db import task_store
from portage_agent.db.models import TaskStatus
from portage_agent.llm import get_llm
from portage_agent.recipes import get_recipe
from portage_agent.recipes.base import PlannedFile, Subtask

from ..state import GraphState
from .common import (
    content_hash,
    export_contract,
    extract_code,
    file_diff,
    iter_py_files,
    non_python_listing,
    read_file,
    worktree_diff,
    write_file,
)

log = logging.getLogger("portage.agent")

_FAULT_PAYLOAD = "\n\n<<< portage fault-injection: deliberately invalid python >>>\n"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tier_for(attempt: int) -> tuple[str, str, str]:
    """(tier, model, label) for the given (1-based) attempt number: driver first, then
    escalation. `model` is the raw LiteLLM string used for the call; `label` is what lands
    in attempts_log / logs (a private deployment id never leaves the env)."""
    if attempt <= settings.escalate_after_attempts:
        return ("driver", settings.llm_driver_model,
                settings.llm_driver_model_label or settings.llm_driver_model)
    return ("escalation", settings.llm_escalation_model,
            settings.llm_escalation_model_label or settings.llm_escalation_model)


def _should_corrupt(fault: str | None, *, path: str, first_path: str | None,
                    attempt: int, tier: str) -> bool:
    """Deterministic fault gates. Both faults target only the first planned file."""
    if not fault or path != first_path:
        return False
    if fault == "bad_patch":
        return attempt == 1
    if fault == "bad_patch_until_escalation":
        return tier == "driver"
    return False


def _gather_context(
    worktree: str, *, current: str, target_paths: set[str], done_paths: set[str]
) -> dict[str, str]:
    """Files to show the model: the test contract + callees + already-migrated siblings.

    Skip the file being migrated and any not-yet-migrated sibling target (still old-framework
    code would mislead the model)."""
    ctx: dict[str, str] = {}
    listing = non_python_listing(worktree)
    if listing:
        ctx["<repo file tree — non-Python files (templates, static, config)>"] = listing
    for rel in sorted(iter_py_files(worktree)):
        if rel == current:
            continue
        if rel in target_paths and rel not in done_paths:
            continue
        body = read_file(worktree, rel)
        if body is not None:
            ctx[rel] = body
    return ctx


async def _migrate_file(recipe, worktree: str, *, path: str, role: str, model: str,
                        subtasks: list[Subtask], context: dict[str, str],
                        verify_errors: str, prior_attempt: str = "") -> tuple[str, dict]:
    """Call the model for one file; return (migrated content, usage) — content not yet
    written. Usage feeds the attempts_log entry (cost-per-migration is an eval metric)."""
    source = read_file(worktree, path, limit=20000) or ""
    planned = PlannedFile(path=path, role=role, subtasks=subtasks)
    user = recipe.build_user_prompt(file=planned, source=source, context=context)
    # The export contract, stated explicitly: what sibling files import from this module.
    # Cross-file naming breaks are a measured top failure mode (corpus finding #2) —
    # don't leave the interface for the model to infer from context files.
    contract = export_contract(worktree, path)
    if contract:
        user += (
            "\n\nIMPORT CONTRACT — other files in this repo import these names from "
            f"{path}; the migrated file MUST still define/export every one of them: "
            f"{', '.join(contract)}"
        )
    if prior_attempt:
        user += (
            "\n\nYOUR PREVIOUS ATTEMPT at this file FAILED verification and was rolled "
            "back. Its diff is below — debug it: keep what was right, fix what the test "
            f"failure shows is wrong.\n{prior_attempt}"
        )
    if verify_errors:
        user += (
            "\n\nA previous attempt produced these test failures — fix the migration so they "
            f"pass (do not change the tests):\n{verify_errors[:2500]}"
        )
    messages = [
        LLMMessage(role="system", content=recipe.system_prompt()),
        LLMMessage(role="user", content=user),
    ]
    resp = await get_llm().complete(messages, model=model)
    usage = {
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
        "cost_usd": round(resp.cost_usd, 6),
    }
    return extract_code(resp.text), usage


async def execute_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    if not state.get("migrate"):
        log.info("EXECUTE node | job=%s no migration tasks -> skip", job_id)
        return {"step_log": ["execute"]}

    worktree = state["worktree"]
    recipe = get_recipe(state["migration_recipe"])
    verify_errors = state.get("last_verify_errors") or ""
    cfg = state.get("config") or {}
    fault = cfg.get("inject_fault")
    delay = int(cfg.get("execute_delay_seconds", settings.execute_task_delay_seconds))

    tasks = await task_store.load_tasks(uuid.UUID(job_id))
    file_tasks = [t for t in tasks if t.target_path]
    target_paths = {t.target_path for t in file_tasks}
    first_path = file_tasks[0].target_path if file_tasks else None
    log.info("EXECUTE node | job=%s tasks=%s pending=%s fault=%s", job_id, len(file_tasks),
             sum(t.status == TaskStatus.pending.value for t in file_tasks), fault or "-")

    done_paths: set[str] = set()
    for t in file_tasks:
        path = t.target_path
        assert path is not None
        if t.status == TaskStatus.skipped.value:
            continue
        current = read_file(worktree, path, limit=20000) or ""
        # Idempotent skip (resume): already migrated and unchanged on disk.
        if t.status == TaskStatus.done.value and t.content_hash == content_hash(current):
            log.info("  skip %s — already migrated (content-hash match)", path)
            done_paths.add(path)
            continue

        attempt = t.attempts + 1
        tier, model, model_label = _tier_for(attempt)
        await task_store.update_task(
            t.id, status=TaskStatus.running.value, attempts=attempt, cascade_subtasks=True,
            append_attempt={"attempt": attempt, "tier": tier, "model": model_label,
                            "action": "migrate", "at": _now()},
        )
        if delay:
            log.info("  EXECUTE pre-migrate delay %ss (kill window) | job=%s task=%s",
                     delay, job_id, path)
            await asyncio.sleep(delay)

        try:
            context = _gather_context(worktree, current=path, target_paths=target_paths,
                                      done_paths=done_paths)
            subtasks = [Subtask(s.type, s.title, "") for s in t.subtasks]
            prior = next(
                (a.get("failing_diff", "") for a in reversed(t.attempts_log)
                 if a.get("action") == "rollback_regenerate" and a.get("failing_diff")),
                "",
            )
            content, usage = await _migrate_file(
                recipe, worktree, path=path, role=t.type, model=model,
                subtasks=subtasks, context=context, verify_errors=verify_errors,
                prior_attempt=prior,
            )
            if _should_corrupt(fault, path=path, first_path=first_path,
                               attempt=attempt, tier=tier):
                log.warning("  FAULT %s | corrupting %s (attempt=%s tier=%s)",
                            fault, path, attempt, tier)
                content += _FAULT_PAYLOAD
            h = write_file(worktree, path, content)
            diff = await file_diff(worktree, path)
            await task_store.update_task(t.id, status=TaskStatus.done.value,
                                         content_hash=h, diff=diff, cascade_subtasks=True,
                                         amend_last_attempt=usage)
            done_paths.add(path)
            log.info("  migrated %s (attempt=%s tier=%s model=%s, %s chars)",
                     path, attempt, tier, model_label, len(content))
        except Exception as exc:
            log.exception("  migrate FAILED for %s", path)
            await task_store.update_task(t.id, status=TaskStatus.failed.value, error=repr(exc))
            raise

    diff = await worktree_diff(worktree)
    snapshots = await task_store.load_tasks(uuid.UUID(job_id))
    return {
        "plan": [s.to_state_dict() for s in snapshots],
        "diff": diff,
        "last_verify_errors": "",  # consumed
        "step_log": ["execute"],
    }
