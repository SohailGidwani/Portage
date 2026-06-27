"""add Phase 1 result columns to jobs (report_path, test_summary, graph_summary)

Revision ID: 0002_job_results
Revises: 0001_create_jobs
Create Date: 2026-06-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_job_results"
down_revision: str | None = "0001_create_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("report_path", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("test_summary", postgresql.JSONB(), nullable=True))
    op.add_column("jobs", sa.Column("graph_summary", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "graph_summary")
    op.drop_column("jobs", "test_summary")
    op.drop_column("jobs", "report_path")
