"""Domain ORM models.

Phase 0–1: ``jobs``. Phase 2 adds ``tasks`` — the hierarchical migration plan (Job → Tasks
→ Subtasks), modelled as one self-referential table (``parent_id`` NULL = a top-level file
Task; non-NULL = a Subtask). The richer events/artifacts/metrics tables from plan §10 stay
deferred to Phase 4 — adding them now would be premature.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class JobStatus(enum.StrEnum):
    """Stored as VARCHAR (not a native PG enum) — app-side enum keeps Alembic simple
    as we add states in later phases."""

    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_url: Mapped[str] = mapped_column(String, nullable=False)
    migration_recipe: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.queued.value, index=True
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Claim / lease bookkeeping (the SKIP LOCKED queue lives in worker/queue.py).
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error: Mapped[str | None] = mapped_column(String, nullable=True)

    # Phase 1 results (filled by the Report node).
    report_path: Mapped[str | None] = mapped_column(String, nullable=True)
    test_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    graph_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Job {self.id} {self.status} {self.migration_recipe}>"


class TaskStatus(enum.StrEnum):
    """Stored as VARCHAR (app-side enum, not a native PG enum) — same rationale as JobStatus."""

    pending = "pending"
    running = "running"
    done = "done"
    skipped = "skipped"  # idempotent skip (already migrated) or recipe N/A
    failed = "failed"


class Task(Base):
    """A node in the migration plan. ``parent_id is None`` → a top-level Task (one file);
    otherwise → a Subtask (one transformation within its parent file). Self-referential so
    the hierarchy is one table; the DAG order is ``order_index``."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    # For a file Task: the recipe role ("router"/"app_factory"/...). For a Subtask: the
    # transformation type ("route_to_endpoint"/...).
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    target_path: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.pending.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verify_spec: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # sha256 of the migrated file content — the idempotency key (resume skips a matching task).
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # Phase 3: one entry per migration attempt / recovery action —
    # {attempt, tier, model, action, reason, at}. This is the measured-escalation record
    # ("did the stronger model rescue the task?") and the frontend's recovery timeline.
    attempts_log: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        kind = "Subtask" if self.parent_id else "Task"
        return f"<{kind} {self.type} {self.target_path or ''} {self.status}>"
