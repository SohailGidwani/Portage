"""Postgres-backed durable job queue (implements core.JobQueue).

The claim is a single atomic statement using ``FOR UPDATE SKIP LOCKED`` so N workers can
poll concurrently without grabbing the same row. A job is claimable if it is ``queued``
OR ``running`` with a stale heartbeat (its worker crashed past the lease) — the latter is
what lets a restarted worker pick a job back up and resume it from its checkpoint.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text

from portage_agent.core.interfaces import ClaimedJob
from portage_agent.db.models import Job, JobStatus
from portage_agent.db.session import AsyncSessionLocal

log = logging.getLogger("portage.worker")

_CLAIM_SQL = text(
    """
    UPDATE jobs
       SET status = 'running',
           worker_id = :worker_id,
           heartbeat_at = now(),
           updated_at = now()
     WHERE id = (
            SELECT id FROM jobs
             WHERE status = 'queued'
                OR (status = 'running'
                    AND heartbeat_at < now() - make_interval(secs => :lease))
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
 RETURNING id, repo_url, migration_recipe, config
    """
)


class PostgresJobQueue:
    """Concrete JobQueue. Each method uses its own short-lived session/connection, so the
    heartbeat task never shares a connection with anything running concurrently."""

    async def enqueue(
        self, *, repo_url: str, migration_recipe: str, config: dict,
        user_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        job = Job(
            id=uuid.uuid4(),
            repo_url=repo_url,
            migration_recipe=migration_recipe,
            status=JobStatus.queued.value,
            config=config or {},
            user_id=user_id,
        )
        async with AsyncSessionLocal() as session, session.begin():
            session.add(job)
        return job.id

    async def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        async with AsyncSessionLocal() as session, session.begin():
            row = (
                await session.execute(_CLAIM_SQL, {"worker_id": worker_id, "lease": lease_seconds})
            ).first()
        if row is None:
            return None
        return ClaimedJob(
            id=row.id,
            repo_url=row.repo_url,
            migration_recipe=row.migration_recipe,
            config=row.config or {},
        )

    async def heartbeat(self, job_id: uuid.UUID, *, worker_id: str) -> None:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE jobs SET heartbeat_at = now(), updated_at = now() "
                    "WHERE id = :id AND worker_id = :worker_id AND status = 'running'"
                ),
                {"id": job_id, "worker_id": worker_id},
            )

    async def complete(self, job_id: uuid.UUID) -> None:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE jobs SET status = 'done', heartbeat_at = NULL, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": job_id},
            )

    async def finish(
        self,
        job_id: uuid.UUID,
        *,
        report_path: str | None,
        test_summary: dict | None,
        graph_summary: dict | None,
    ) -> None:
        """Mark done AND persist the Phase 1 result columns in one update."""
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE jobs SET status = 'done', heartbeat_at = NULL, updated_at = now(), "
                    "report_path = :report_path, "
                    "test_summary = CAST(:test_summary AS jsonb), "
                    "graph_summary = CAST(:graph_summary AS jsonb) "
                    "WHERE id = :id"
                ),
                {
                    "id": job_id,
                    "report_path": report_path,
                    "test_summary": json.dumps(test_summary) if test_summary is not None else None,
                    "graph_summary": (
                        json.dumps(graph_summary) if graph_summary is not None else None
                    ),
                },
            )

    async def fail(self, job_id: uuid.UUID, *, error: str) -> None:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE jobs SET status = 'failed', error = :error, "
                    "heartbeat_at = NULL, updated_at = now() WHERE id = :id"
                ),
                {"id": job_id, "error": error[:2000]},
            )
