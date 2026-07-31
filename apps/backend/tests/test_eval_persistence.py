"""Run-row durability and migrated-tree leaderboard integrity."""

from __future__ import annotations

import json
import os
import uuid

from sqlalchemy import delete, select

os.environ.setdefault("POSTGRES_HOST", "localhost")

from portage_agent.api.app import eval_leaderboard  # noqa: E402
from portage_agent.db.models import EvalRun, Job  # noqa: E402
from portage_agent.db.session import AsyncSessionLocal  # noqa: E402
from portage_agent.eval.harness import (  # noqa: E402
    EVAL_CONFIG_KEY,
    persist_completed_eval_job,
    reconcile_completed_eval_runs,
)


async def test_worker_reconciliation_persists_one_idempotent_run_row(tmp_path):
    job_id = uuid.uuid4()
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "tasks_total": 1,
        "tasks_done": 1,
        "tasks": [],
        "recovery": {},
        "llm_usage": {"calls": 1, "cost_usd": 0.01},
        "oracle_integrity": {"integrity_rate": 1.0},
        "migration_outcome": "success",
        "tree_state": "migrated",
    }))
    metadata = {
        "suite": f"reconcile-{uuid.uuid4().hex}",
        "corpus_name": "reconcile-fixture",
        "scenario": "baseline",
        "k_index": 1,
        "driver_model": "test-driver",
        "escalation_model": "test-escalation",
        "plan_only": False,
    }
    job = Job(
        id=job_id,
        repo_url="/fixtures/flask_app",
        migration_recipe="flask_to_fastapi",
        status="done",
        config={EVAL_CONFIG_KEY: metadata},
        report_path=str(report),
        test_summary={"passed": 1, "total": 1, "tree_state": "migrated"},
    )
    async with AsyncSessionLocal() as session, session.begin():
        session.add(job)

    try:
        assert await reconcile_completed_eval_runs() >= 1
        assert await persist_completed_eval_job(job_id)
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(select(EvalRun).where(EvalRun.job_id == job_id))
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "green"
        assert rows[0].tree_state == "migrated"
    finally:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(delete(EvalRun).where(EvalRun.job_id == job_id))
            await session.execute(delete(Job).where(Job.id == job_id))


async def test_leaderboard_counts_only_migrated_trees():
    suite = f"tree-state-{uuid.uuid4().hex}"
    common = {
        "suite": suite,
        "corpus_name": "tree-state-fixture",
        "repo_url": "/fixtures/flask_app",
        "recipe": "flask_to_fastapi",
        "scenario": "baseline",
        "driver_model": "test-driver",
        "escalation_model": "test-escalation",
        "status": "green",
        "tests_total": 2,
        "tests_passed": 2,
        "tasks_total": 1,
        "tasks_done": 1,
    }
    async with AsyncSessionLocal() as session, session.begin():
        session.add_all([
            EvalRun(
                id=uuid.uuid4(), k_index=1, tree_state="migrated", **common,
            ),
            EvalRun(
                id=uuid.uuid4(), k_index=2, tree_state="restored_coherent", **common,
            ),
        ])

    try:
        board = await eval_leaderboard(suites=suite)
        row = board["rows"][0]
        assert row["runs"] == 2
        assert row["migrated_runs"] == 1
        assert row["green"] == 1
        assert row["test_pass_mean"] == 1.0
    finally:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(delete(EvalRun).where(EvalRun.suite == suite))
