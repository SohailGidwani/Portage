"""Async persistence for the migration plan (Job → Tasks → Subtasks).

Recipe-agnostic on purpose: nodes hand it plain dict "specs", so adding a second recipe
never touches this layer. ``save_plan`` is idempotent — on a resumed job the plan already
exists, so it returns the persisted tasks instead of duplicating them. Task rows (+ git
commits in the worktree) are the durable record *within* the Execute node, complementing
LangGraph's node-boundary checkpoint: a crash mid-Execute resumes and skips done tasks.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from .models import Task, TaskStatus
from .session import AsyncSessionLocal


@dataclass(slots=True)
class SubtaskSnapshot:
    id: str
    type: str
    title: str
    status: str


@dataclass(slots=True)
class TaskSnapshot:
    """JSON-serializable view of a file Task (+ its subtasks) for graph state / reports."""

    id: str
    type: str
    title: str
    target_path: str | None
    status: str
    attempts: int
    order_index: int
    verify_spec: dict
    content_hash: str | None
    error: str | None = None
    diff: str | None = None
    attempts_log: list = field(default_factory=list)
    subtasks: list[SubtaskSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_state_dict(self) -> dict:
        """Compact form for LangGraph state / report.json — omits the (potentially large)
        per-task diff, which stays queryable via GET /jobs/{id}/tasks."""
        d = asdict(self)
        d.pop("diff", None)
        return d


async def _load(session, job_id: uuid.UUID) -> list[TaskSnapshot]:
    rows = (
        await session.execute(
            select(Task).where(Task.job_id == job_id).order_by(Task.order_index, Task.created_at)
        )
    ).scalars().all()
    parents = [t for t in rows if t.parent_id is None]
    children: dict[uuid.UUID, list[Task]] = {}
    for t in rows:
        if t.parent_id is not None:
            children.setdefault(t.parent_id, []).append(t)
    out: list[TaskSnapshot] = []
    for p in parents:
        out.append(
            TaskSnapshot(
                id=str(p.id),
                type=p.type,
                title=p.title,
                target_path=p.target_path,
                status=p.status,
                attempts=p.attempts,
                order_index=p.order_index,
                verify_spec=p.verify_spec or {},
                content_hash=p.content_hash,
                error=p.error,
                diff=p.diff,
                attempts_log=list(p.attempts_log or []),
                subtasks=[
                    SubtaskSnapshot(id=str(c.id), type=c.type, title=c.title, status=c.status)
                    for c in children.get(p.id, [])
                ],
            )
        )
    return out


async def save_plan(job_id: uuid.UUID, specs: list[dict]) -> list[TaskSnapshot]:
    """Persist file Tasks (+ Subtask children) for a job, idempotently.

    Each spec: ``{type, title, target_path, order_index, verify_spec, subtasks:[{type,title}]}``.
    If the job already has tasks (resume), the existing plan is returned unchanged.
    """
    async with AsyncSessionLocal() as session, session.begin():
        existing = (
            await session.execute(
                select(Task.id).where(Task.job_id == job_id, Task.parent_id.is_(None)).limit(1)
            )
        ).first()
        if existing is None:
            for spec in specs:
                parent = Task(
                    id=uuid.uuid4(),
                    job_id=job_id,
                    parent_id=None,
                    type=spec["type"],
                    title=spec["title"],
                    target_path=spec.get("target_path"),
                    status=TaskStatus.pending.value,
                    order_index=spec.get("order_index", 0),
                    verify_spec=spec.get("verify_spec", {}),
                )
                session.add(parent)
                await session.flush()
                for st in spec.get("subtasks", []):
                    session.add(
                        Task(
                            id=uuid.uuid4(),
                            job_id=job_id,
                            parent_id=parent.id,
                            type=st["type"],
                            title=st["title"],
                            status=TaskStatus.pending.value,
                            order_index=0,
                            verify_spec={},
                        )
                    )
        snapshots = await _load(session, job_id)
    return snapshots


async def load_tasks(job_id: uuid.UUID) -> list[TaskSnapshot]:
    async with AsyncSessionLocal() as session:
        return await _load(session, job_id)


async def update_task(
    task_id: str | uuid.UUID,
    *,
    status: str | None = None,
    attempts: int | None = None,
    content_hash: str | None = None,
    diff: str | None = None,
    error: str | None = None,
    append_attempt: dict | None = None,
    amend_last_attempt: dict | None = None,
    cascade_subtasks: bool = False,
) -> None:
    """Update one Task row; optionally mirror ``status`` onto its subtasks.

    ``append_attempt`` appends one entry to ``attempts_log``; ``amend_last_attempt``
    merges keys into the most recent entry (e.g. token/cost usage known only after the
    LLM call the entry recorded). Both reassign the list, not mutate it, so SQLAlchemy
    detects the JSONB change."""
    tid = uuid.UUID(str(task_id))
    async with AsyncSessionLocal() as session, session.begin():
        task = (await session.execute(select(Task).where(Task.id == tid))).scalar_one()
        if status is not None:
            task.status = status
        if attempts is not None:
            task.attempts = attempts
        if content_hash is not None:
            task.content_hash = content_hash
        if diff is not None:
            task.diff = diff
        if error is not None:
            task.error = error
        if append_attempt is not None:
            task.attempts_log = [*(task.attempts_log or []), append_attempt]
        if amend_last_attempt is not None and task.attempts_log:
            task.attempts_log = [
                *task.attempts_log[:-1],
                {**task.attempts_log[-1], **amend_last_attempt},
            ]
        if cascade_subtasks and status is not None:
            children = (
                await session.execute(select(Task).where(Task.parent_id == tid))
            ).scalars().all()
            for c in children:
                c.status = status


async def append_tasks(job_id: uuid.UUID, specs: list[dict]) -> list[TaskSnapshot]:
    """Append file Tasks to an existing plan (the replan path — e.g. a file the original
    plan missed). Specs use the same shape as ``save_plan``; already-planned target paths
    are skipped so a repeated replan stays idempotent."""
    async with AsyncSessionLocal() as session, session.begin():
        existing_paths = {
            p for (p,) in (
                await session.execute(
                    select(Task.target_path).where(
                        Task.job_id == job_id, Task.parent_id.is_(None)
                    )
                )
            ).all()
        }
        for spec in specs:
            if spec.get("target_path") in existing_paths:
                continue
            parent = Task(
                id=uuid.uuid4(),
                job_id=job_id,
                parent_id=None,
                type=spec["type"],
                title=spec["title"],
                target_path=spec.get("target_path"),
                status=TaskStatus.pending.value,
                order_index=spec.get("order_index", 0),
                verify_spec=spec.get("verify_spec", {}),
            )
            session.add(parent)
            await session.flush()
            for st in spec.get("subtasks", []):
                session.add(
                    Task(
                        id=uuid.uuid4(),
                        job_id=job_id,
                        parent_id=parent.id,
                        type=st["type"],
                        title=st["title"],
                        status=TaskStatus.pending.value,
                        order_index=0,
                        verify_spec={},
                    )
                )
        snapshots = await _load(session, job_id)
    return snapshots
