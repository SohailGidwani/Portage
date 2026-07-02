"""add attempts_log to tasks (Phase 3 recovery: per-attempt tier/model/strategy record)

Revision ID: 0004_task_attempts_log
Revises: 0003_create_tasks
Create Date: 2026-07-02
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_task_attempts_log"
down_revision: str | None = "0003_create_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "attempts_log",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "attempts_log")
