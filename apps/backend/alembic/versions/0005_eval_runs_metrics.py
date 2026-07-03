"""create runs + metrics tables (Phase 4 eval harness — the leaderboard contract)

Revision ID: 0005_eval_runs_metrics
Revises: 0004_task_attempts_log
Create Date: 2026-07-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_eval_runs_metrics"
down_revision: str | None = "0004_task_attempts_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("suite", sa.String(length=64), nullable=False),
        sa.Column("corpus_name", sa.String(), nullable=False),
        sa.Column("repo_url", sa.String(), nullable=False),
        sa.Column("recipe", sa.String(), nullable=False),
        sa.Column("scenario", sa.String(length=48), nullable=False),
        sa.Column("k_index", sa.Integer(), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("driver_model", sa.String(), nullable=False),
        sa.Column("escalation_model", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tests_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tests_passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recover_visits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escalation_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escalation_rescued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("wall_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_runs_suite", "runs", ["suite"])

    op.create_table(
        "metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("suite", sa.String(length=64), nullable=False),
        sa.Column("corpus_name", sa.String(), nullable=False),
        sa.Column("scenario", sa.String(length=48), nullable=False),
        sa.Column("driver_model", sa.String(), nullable=False),
        sa.Column("metric", sa.String(length=48), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=False),
        sa.Column("variance", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_metrics_suite", "metrics", ["suite"])


def downgrade() -> None:
    op.drop_index("ix_metrics_suite", table_name="metrics")
    op.drop_table("metrics")
    op.drop_index("ix_runs_suite", table_name="runs")
    op.drop_table("runs")
