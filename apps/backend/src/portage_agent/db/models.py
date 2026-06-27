"""Domain ORM models.

Phase 0 has exactly one table: ``jobs``. The richer task tree (plans/tasks/subtasks/
events/artifacts/metrics from the plan §10) lands in later phases — adding it now would
be premature.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
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
