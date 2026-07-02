"""Execute node — migrate each planned file with the LLM, on the git worktree.

For every file Task (in dependency order): gather context (the behavioural test contract, the
framework-agnostic callees, and any already-migrated siblings), prompt the model for a full
rewrite, write it into the worktree, and record a content hash + diff in Postgres.

Durability (Phase 2 scope):
  * Idempotent / resume-safe — a task whose worktree file already hashes to the recorded
    value is skipped, so a crash mid-Execute resumes without re-calling the model.
  * Bounded retry — when Verify fails, the graph loops back here (≤ max_execute_attempts).
    A retry restores the worktree to the original sources and re-migrates with the failing
    test output in context. The richer Recover taxonomy + model escalation is Phase 3.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

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
    extract_code,
    iter_py_files,
    read_file,
    run_git,
    worktree_diff,
    write_file,
)

log = logging.getLogger("portage.agent")


def _gather_context(
    worktree: str, *, current: str, target_paths: set[str], done_paths: set[str]
) -> dict[str, str]:
    """Files to show the model: the test contract + callees + already-migrated siblings.

    Skip the file being migrated and any not-yet-migrated sibling target (still old-framework
    code would mislead the model)."""
    ctx: dict[str, str] = {}
    for rel in sorted(iter_py_files(worktree)):
        if rel == current:
            continue
        if rel in target_paths and rel not in done_paths:
            continue
        body = read_file(worktree, rel)
        if body is not None:
            ctx[rel] = body
    return ctx


async def _migrate_file(recipe, worktree: str, *, path: str, role: str,
                        subtasks: list[Subtask], context: dict[str, str],
                        verify_errors: str) -> tuple[str, str]:
    """Call the model for one file; return (migrated_content, hash)."""
    source = read_file(worktree, path, limit=20000) or ""
    planned = PlannedFile(path=path, role=role, subtasks=subtasks)
    user = recipe.build_user_prompt(file=planned, source=source, context=context)
    if verify_errors:
        user += (
            "\n\nA previous attempt produced these test failures — fix the migration so they "
            f"pass (do not change the tests):\n{verify_errors[:2500]}"
        )
    messages = [
        LLMMessage(role="system", content=recipe.system_prompt()),
        LLMMessage(role="user", content=user),
    ]
    resp = await get_llm().complete(messages)
    content = extract_code(resp.text)
    h = write_file(worktree, path, content)
    log.info("  migrated %s (role=%s, %s chars, model=%s)", path, role, len(content), resp.model)
    return content, h


async def execute_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    if not state.get("migrate"):
        log.info("EXECUTE node | job=%s no migration tasks -> skip", job_id)
        return {"step_log": ["execute"]}

    worktree = state["worktree"]
    recipe = get_recipe(state["migration_recipe"])
    verify_errors = state.get("last_verify_errors") or ""
    retry = bool(verify_errors)
    delay = int((state.get("config") or {}).get(
        "execute_delay_seconds", settings.execute_task_delay_seconds))

    tasks = await task_store.load_tasks(uuid.UUID(job_id))
    target_paths = {t.target_path for t in tasks if t.target_path}
    log.info("EXECUTE node | job=%s tasks=%s retry=%s", job_id, len(tasks), retry)

    if retry:
        # Discard the failed attempt's edits and re-migrate from the original sources.
        await run_git("checkout", "--", ".", cwd=worktree)
        for t in tasks:
            await task_store.update_task(t.id, status=TaskStatus.pending.value,
                                         content_hash=None, cascade_subtasks=True)
        tasks = await task_store.load_tasks(uuid.UUID(job_id))

    done_paths: set[str] = set()
    for t in tasks:
        path = t.target_path
        if not path:
            continue
        current = read_file(worktree, path, limit=20000) or ""
        # Idempotent skip (resume): already migrated and unchanged on disk.
        already_done = t.status == TaskStatus.done.value and t.content_hash == content_hash(current)
        if not retry and already_done:
            log.info("  skip %s — already migrated (content-hash match)", path)
            done_paths.add(path)
            continue

        await task_store.update_task(t.id, status=TaskStatus.running.value,
                                     attempts=t.attempts + 1, cascade_subtasks=True)
        if delay:
            log.info("  EXECUTE pre-migrate delay %ss (kill window) | job=%s task=%s",
                     delay, job_id, path)
            await asyncio.sleep(delay)

        try:
            context = _gather_context(worktree, current=path, target_paths=target_paths,
                                      done_paths=done_paths)
            subtasks = [Subtask(s.type, s.title, "") for s in t.subtasks]
            _, h = await _migrate_file(recipe, worktree, path=path, role=t.type,
                                       subtasks=subtasks, context=context,
                                       verify_errors=verify_errors)
            await task_store.update_task(t.id, status=TaskStatus.done.value,
                                         content_hash=h, cascade_subtasks=True)
            done_paths.add(path)
        except Exception as exc:
            log.exception("  migrate FAILED for %s", path)
            await task_store.update_task(t.id, status=TaskStatus.failed.value, error=repr(exc))
            raise

    diff = await worktree_diff(worktree)
    snapshots = await task_store.load_tasks(uuid.UUID(job_id))
    return {
        "plan": [s.to_dict() for s in snapshots],
        "diff": diff,
        "last_verify_errors": "",  # consumed
        "step_log": ["execute"],
    }
