"""Domain ORM models.

Phase 0–1: ``jobs``. Phase 2: ``tasks`` — the hierarchical migration plan (Job → Tasks →
Subtasks), one self-referential table (``parent_id`` NULL = a top-level file Task; non-NULL
= a Subtask). Phase 4: ``runs`` + ``metrics`` — the eval harness's output and the contract
the Phase 6 leaderboard reads (plan §10/§11). The ``events`` table stays deferred until
something needs it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
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

    # Phase 7: owner (NULL = pre-auth legacy jobs and disabled-mode local runs).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )

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


class User(Base):
    """A GitHub-authenticated user (Phase 7; rev-C: GitHub OAuth is the sole provider —
    no passwords, no email flows). `role` gates admin surfaces server-side."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    github_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    login: Mapped[str] = mapped_column(String, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    disabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User {self.login} {self.role}>"


class ApiKey(Base):
    """Machine credential (CLI/MCP/CI). Only the SHA-256 hash is stored; the plaintext
    (prefix `pk_`) is shown once at creation. High-entropy random tokens don't need a
    slow hash — revocation is the security property, per key."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked: Mapped[bool] = mapped_column(nullable=False, default=False)


class RefreshToken(Base):
    """One rotating browser session. `family_id` groups a rotation chain: presenting an
    already-rotated (revoked) token is reuse — the whole family is revoked (stolen-token
    containment). The active-session list per user IS this table."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvalRun(Base):
    """One harness-driven migration run: (suite, corpus repo, scenario, k) → one job,
    harvested into a flat row the leaderboard and the aggregator read. `suite` identifies
    one harness invocation (a batch of runs compared together)."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    suite: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    corpus_name: Mapped[str] = mapped_column(String, nullable=False)
    repo_url: Mapped[str] = mapped_column(String, nullable=False)
    recipe: Mapped[str] = mapped_column(String, nullable=False)
    scenario: Mapped[str] = mapped_column(String(48), nullable=False)  # baseline | fault name
    k_index: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    driver_model: Mapped[str] = mapped_column(String, nullable=False)
    escalation_model: Mapped[str] = mapped_column(String, nullable=False)

    # Outcome. status: green (job done + full suite passed) | red (finished, suite not
    # green) | error (job failed) | timeout (harness gave up waiting).
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tests_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recover_visits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalation_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalation_rescued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    wall_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<EvalRun {self.corpus_name}/{self.scenario} k={self.k_index} {self.status}>"


class EvalMetric(Base):
    """One aggregated statistic: (suite, corpus repo, scenario, driver model, metric) →
    mean ± variance over the K runs. Written by the harness aggregator; read by the
    leaderboard. K-runs with variance is the plan-§11 rigor signal."""

    __tablename__ = "metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    suite: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    corpus_name: Mapped[str] = mapped_column(String, nullable=False)
    scenario: Mapped[str] = mapped_column(String(48), nullable=False)
    driver_model: Mapped[str] = mapped_column(String, nullable=False)
    metric: Mapped[str] = mapped_column(String(48), nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    mean: Mapped[float] = mapped_column(Float, nullable=False)
    variance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<EvalMetric {self.metric} {self.mean:.3f}±{self.variance:.3f} k={self.k}>"
