"""persist eval tree state and make job harvesting idempotent

Revision ID: 0007_eval_run_tree_state
Revises: 0006_auth_tables
Create Date: 2026-07-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_eval_run_tree_state"
down_revision: str | None = "0006_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("tree_state", sa.String(length=32), nullable=False,
                  server_default="unknown"),
    )
    op.execute(
        """
        UPDATE runs
           SET tree_state = jobs.test_summary->>'tree_state'
          FROM jobs
         WHERE runs.job_id = jobs.id
           AND jobs.test_summary->>'tree_state' IS NOT NULL
        """
    )
    op.create_index("uq_runs_job_id", "runs", ["job_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_runs_job_id", table_name="runs")
    op.drop_column("runs", "tree_state")
